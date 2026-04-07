"""
Note ingestion pipeline — runs as a FastAPI BackgroundTask after initial note creation.

Source type dispatch:
  handwritten — Steps A (map detection), B (map extraction), C (OCR + date),
                D (spot resolution), field extraction, embedding
  typed       — field extraction, embedding
  map         — OpenCV correction, vision spatial description, embedding

All LLM calls use the resident Llama 3.1 8B for text tasks and the evict-after-use
Llama 3.2 11B Vision for image tasks (per §6.1 keep_alive rules).

Processing state is tracked in notes.processing_notes as a pipe-separated string:
  'awaiting_date_confirmation'  — date extracted but not yet confirmed by user
  'awaiting_spot_confirmation'  — spot candidates stored, user must select
  'low_quality_scan'            — map quality gate failed, original crop stored
  'spot_auto_linked'            — spot resolved at >= 0.85 confidence (non-blocking)
"""

import base64
import json
import logging
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import AsyncSessionLocal
from db.models import Note
from llm.client import CHAT_MODEL, VISION_MODEL, call_json_llm, ollama_generate
from notes.map_extractor import correct_standalone_map, extract_and_correct
from notes.spot_resolver import resolve_spot
from notes.upload_handler import read_upload, store_upload
from prompts.registry import (
    FIELD_EXTRACTION_PROMPT,
    MAP_DESCRIPTION_PROMPT,
    MAP_DETECTION_PROMPT,
)
from rag.embedder import embed_text

log = logging.getLogger(__name__)

_FIELD_EXTRACTION_DEFAULT = {
    "species": [],
    "flies": [],
    "outcome": "neutral",
    "negative_reason": None,
    "approx_cfs": None,
    "approx_temp": None,
    "time_of_day": None,
}

_MAP_DETECTION_DEFAULT = {
    "contains_map": False,
    "confidence": "low",
    "bounding_box": None,
}

_VALID_NEGATIVE_REASONS = {"conditions", "access", "fish_absence", "gear", "unknown"}
_VALID_OUTCOMES = {"positive", "neutral", "negative"}
_VALID_TIME_OF_DAY = {"morning", "afternoon", "evening", "all-day"}


def _sanitise_fields(fields: dict) -> dict:
    """
    Enforce the negative_reason enum contract and coerce invalid values.
    A value outside the enum breaks scorer weighting silently (§11.1 note).
    """
    outcome = fields.get("outcome", "neutral")
    if outcome not in _VALID_OUTCOMES:
        outcome = "neutral"
    fields["outcome"] = outcome

    nr = fields.get("negative_reason")
    if outcome != "negative":
        fields["negative_reason"] = None
    elif nr not in _VALID_NEGATIVE_REASONS:
        # Unknown value from LLM — safer to use 'unknown' than a bad enum value
        fields["negative_reason"] = "unknown"

    tod = fields.get("time_of_day")
    if tod not in _VALID_TIME_OF_DAY:
        fields["time_of_day"] = None

    return fields


def _encode_image(image_bytes: bytes) -> str:
    """Base64-encode image bytes for Ollama vision payload."""
    return base64.b64encode(image_bytes).decode("utf-8")


def _flags_to_str(flags: list[str]) -> str:
    return "|".join(flags) if flags else ""


async def _update_note(note_id: UUID, updates: dict, db: AsyncSession) -> None:
    """Apply a dict of column updates to a Note row and flush."""
    result = await db.execute(select(Note).where(Note.id == note_id))
    note = result.scalar_one_or_none()
    if not note:
        log.warning("ingest_note_not_found", extra={"note_id": str(note_id)})
        return
    for key, value in updates.items():
        setattr(note, key, value)
    db.add(note)
    await db.flush()


# ---------------------------------------------------------------------------
# Source-type handlers
# ---------------------------------------------------------------------------


async def _ingest_typed(note_id: UUID, db: AsyncSession) -> None:
    """Typed note: field extraction + embedding (no image involved)."""
    result = await db.execute(select(Note).where(Note.id == note_id))
    note = result.scalar_one_or_none()
    if not note or not note.content:
        return

    # Field extraction
    prompt = FIELD_EXTRACTION_PROMPT.format(note_text=note.content)
    fields = await call_json_llm(prompt, CHAT_MODEL, _FIELD_EXTRACTION_DEFAULT)
    fields = _sanitise_fields(fields)

    # Spot resolution
    resolution = await resolve_spot(note.content, db)
    flags: list[str] = []
    spot_resolution_json = None

    if resolution["band"] == "auto":
        spot_id = resolution["auto_spot_id"]
        flags.append("spot_auto_linked")
    elif resolution["band"] == "medium":
        spot_id = None
        spot_resolution_json = json.dumps(
            {
                "band": resolution["band"],
                "location_string": resolution["location_string"],
                "candidates": resolution["candidates"],
            }
        )
        flags.append("awaiting_spot_confirmation")
    else:
        spot_id = None
        flags.append("awaiting_spot_confirmation")
        spot_resolution_json = json.dumps(
            {
                "band": "low",
                "location_string": resolution["location_string"],
                "candidates": [],
            }
        )

    # Embedding
    embedding = await embed_text(note.content)

    updates: dict[str, Any] = {
        "species": fields.get("species") or [],
        "flies": fields.get("flies") or [],
        "outcome": fields.get("outcome"),
        "negative_reason": fields.get("negative_reason"),
        "approx_cfs": fields.get("approx_cfs"),
        "approx_temp": fields.get("approx_temp"),
        "time_of_day": fields.get("time_of_day"),
        "embedding": embedding,
        "processing_notes": _build_processing_notes(flags, spot_resolution_json),
    }
    if spot_id:
        updates["spot_id"] = spot_id

    await _update_note(note_id, updates, db)
    log.info("typed_note_ingested", extra={"note_id": str(note_id)})


async def _ingest_handwritten(note_id: UUID, author_id: UUID, db: AsyncSession) -> None:
    """
    Handwritten note: Steps A (map detection), B (map extraction), C (OCR + date),
    D (spot resolution), field extraction, embedding.
    """
    result = await db.execute(select(Note).where(Note.id == note_id))
    note = result.scalar_one_or_none()
    if not note:
        return

    image_bytes = read_upload(str(note_id))
    b64 = _encode_image(image_bytes)

    # Step A — Map detection
    detection = await call_json_llm(
        MAP_DETECTION_PROMPT,
        VISION_MODEL,
        _MAP_DETECTION_DEFAULT,
    )
    # Note: VISION_MODEL uses keep_alive=0 per call_json_llm (not in RESIDENT_MODELS)

    map_note_id: UUID | None = None
    if detection.get("contains_map") and detection.get("bounding_box"):
        # Step B — Map extraction (runs before OCR completes; both use same source image)
        map_note_id = uuid.uuid4()
        try:
            map_path, is_low_quality = extract_and_correct(
                image_bytes,
                detection["bounding_box"],
                detection.get("confidence", "low"),
                str(map_note_id),
            )
            map_flags = ["low_quality_scan"] if is_low_quality else []

            # Llama 3.2 11B Vision generates spatial description (prose, not JSON)
            map_img_bytes = read_upload(str(map_note_id))
            map_b64 = _encode_image(map_img_bytes)
            spatial_desc = await ollama_generate(
                VISION_MODEL,
                MAP_DESCRIPTION_PROMPT,
                temperature=0.3,
                keep_alive=0,
                images=[map_b64],
            )

            map_embedding = await embed_text(spatial_desc)

            # Create child map note record
            map_note = Note(
                id=map_note_id,
                author_id=author_id,
                source_type="map",
                parent_note_id=note_id,
                image_path=map_path,
                content=spatial_desc,
                embedding=map_embedding,
                processing_notes=_build_processing_notes(
                    map_flags + ["awaiting_date_confirmation"], None
                ),
            )
            db.add(map_note)
            await db.flush()
            log.info(
                "map_note_created",
                extra={"map_note_id": str(map_note_id), "parent_note_id": str(note_id)},
            )
        except Exception:
            log.exception(
                "map_extraction_failed", extra={"note_id": str(note_id)}
            )
            map_note_id = None

    # Step C — OCR + date extraction
    ocr_prompt = (
        "Transcribe all text visible on this notebook page exactly as written. "
        "Then on a new line starting with 'TRIP DATE:', extract the trip date if present "
        "(format: YYYY-MM-DD or 'unknown' if not found)."
    )
    ocr_response = await ollama_generate(
        VISION_MODEL,
        ocr_prompt,
        temperature=0.1,
        keep_alive=0,
        images=[b64],
    )

    # Parse OCR response: split on TRIP DATE: marker
    ocr_text = ocr_response
    extracted_date_str: str | None = None
    if "TRIP DATE:" in ocr_response:
        parts = ocr_response.split("TRIP DATE:", 1)
        ocr_text = parts[0].strip()
        date_part = parts[1].strip().split()[0] if parts[1].strip() else ""
        if date_part and date_part != "unknown":
            extracted_date_str = date_part

    # Step D — Spot resolution
    resolution = await resolve_spot(ocr_text, db)
    flags: list[str] = ["awaiting_date_confirmation"]
    spot_resolution_json = None

    if resolution["band"] == "auto":
        spot_id = resolution["auto_spot_id"]
        flags.append("spot_auto_linked")
    elif resolution["band"] == "medium":
        spot_id = None
        spot_resolution_json = json.dumps(
            {
                "band": resolution["band"],
                "location_string": resolution["location_string"],
                "candidates": resolution["candidates"],
            }
        )
        flags.append("awaiting_spot_confirmation")
    else:
        spot_id = None
        spot_resolution_json = json.dumps(
            {
                "band": "low",
                "location_string": resolution["location_string"],
                "candidates": [],
            }
        )
        flags.append("awaiting_spot_confirmation")

    # Structured field extraction from OCR'd text
    prompt = FIELD_EXTRACTION_PROMPT.format(note_text=ocr_text)
    fields = await call_json_llm(prompt, CHAT_MODEL, _FIELD_EXTRACTION_DEFAULT)
    fields = _sanitise_fields(fields)

    # Embedding
    embedding = await embed_text(ocr_text)

    updates: dict[str, Any] = {
        "content": ocr_text,
        "species": fields.get("species") or [],
        "flies": fields.get("flies") or [],
        "outcome": fields.get("outcome"),
        "negative_reason": fields.get("negative_reason"),
        "approx_cfs": fields.get("approx_cfs"),
        "approx_temp": fields.get("approx_temp"),
        "time_of_day": fields.get("time_of_day"),
        "embedding": embedding,
        "processing_notes": _build_processing_notes(flags, spot_resolution_json),
    }
    if extracted_date_str:
        updates["note_date"] = extracted_date_str
    if spot_id:
        updates["spot_id"] = spot_id

    await _update_note(note_id, updates, db)

    # Propagate spot_id to map child note if auto-linked
    if map_note_id and spot_id:
        await _update_note(map_note_id, {"spot_id": spot_id}, db)

    log.info("handwritten_note_ingested", extra={"note_id": str(note_id)})


async def _ingest_standalone_map(note_id: UUID, db: AsyncSession) -> None:
    """
    Standalone map upload: OpenCV correction → vision spatial description → embedding.
    User confirms spot and date manually (no parent to inherit from).
    """
    image_bytes = read_upload(str(note_id))

    path, is_low_quality = correct_standalone_map(image_bytes, str(note_id))
    flags = ["low_quality_scan"] if is_low_quality else []
    flags += ["awaiting_date_confirmation", "awaiting_spot_confirmation"]

    map_img_bytes = read_upload(str(note_id))
    b64 = _encode_image(map_img_bytes)
    spatial_desc = await ollama_generate(
        VISION_MODEL,
        MAP_DESCRIPTION_PROMPT,
        temperature=0.3,
        keep_alive=0,
        images=[b64],
    )

    embedding = await embed_text(spatial_desc)

    await _update_note(
        note_id,
        {
            "content": spatial_desc,
            "image_path": path,
            "embedding": embedding,
            "processing_notes": _build_processing_notes(flags, None),
        },
        db,
    )
    log.info("standalone_map_ingested", extra={"note_id": str(note_id)})


def _build_processing_notes(flags: list[str], spot_resolution_json: str | None) -> str:
    """
    Encode processing state as a pipe-separated flag string,
    optionally followed by a JSON blob separated by a newline.

    Format: "flag1|flag2\n{...json...}"
    """
    flag_str = "|".join(flags) if flags else ""
    if spot_resolution_json:
        return f"{flag_str}\n{spot_resolution_json}"
    return flag_str


# ---------------------------------------------------------------------------
# Public entry point (called as BackgroundTask)
# ---------------------------------------------------------------------------


async def ingest_note_task(note_id: UUID, source_type: str, author_id: UUID) -> None:
    """
    BackgroundTask entry point.  Creates its own DB session so that the
    original request session (already closed by the time this runs) is not used.
    """
    async with AsyncSessionLocal() as db:
        try:
            if source_type == "typed":
                await _ingest_typed(note_id, db)
            elif source_type == "handwritten":
                await _ingest_handwritten(note_id, author_id, db)
            elif source_type == "map":
                await _ingest_standalone_map(note_id, db)
            else:
                log.warning(
                    "unknown_source_type",
                    extra={"note_id": str(note_id), "source_type": source_type},
                )
                return
            await db.commit()
        except Exception:
            await db.rollback()
            log.exception(
                "ingest_note_task_failed",
                extra={"note_id": str(note_id), "source_type": source_type},
            )
