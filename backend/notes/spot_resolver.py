"""
Spot entity resolution for ingested notes (§6.8 Step D).

Searches water_bodies by name. Auto-link and candidates return fishing_spot_id
(the default fishing_spot for the matched water_body).

Pipeline:
  D1. Extract location string from OCR text (Llama 3.1 8B — §18.6)
  D2. Embed the location string via nomic-embed-text
  D3. Run semantic (pgvector) + fuzzy (pg_trgm) lookups against water_bodies
  D4. Merge: combined_score = 0.6 * sem_score + 0.4 * trgm_score, take top 3
  D5. Branch on top combined_score:
        >= 0.85  → auto-link (set fishing_spot_id, show pre-filled card)
        0.50–0.84 → return top 3 for user selection (blocking)
        < 0.50   → return "create new spot" signal
"""

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from llm.client import CHAT_MODEL, call_json_llm
from prompts.registry import LOCATION_EXTRACTION_PROMPT
from rag.embedder import embed_text

log = logging.getLogger(__name__)

_AUTO_LINK_THRESHOLD = 0.85
_CANDIDATE_THRESHOLD = 0.50

_LOCATION_DEFAULT = {"location_string": "", "confidence": "none"}

# JSON Schema enforced via response_format under the llama.cpp utility engine
# (Phase 1 / Phase 0g). Ignored on ollama, so behaviour there is unchanged.
_LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "location_string": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low", "none"]},
    },
    "required": ["location_string", "confidence"],
}


async def extract_location(note_text: str) -> dict:
    """Call Llama 3.1 8B to extract the fishing location from note text."""
    prompt = LOCATION_EXTRACTION_PROMPT.format(note_text=note_text)
    result = await call_json_llm(
        prompt, CHAT_MODEL, _LOCATION_DEFAULT, schema=_LOCATION_SCHEMA
    )
    return result


async def _semantic_lookup(embedding: list[float], db: AsyncSession) -> list[dict]:
    """Top-10 semantic matches by name_embedding cosine similarity against water_bodies."""
    rows = await db.execute(
        text(
            """
            SELECT wb.id::text AS water_body_id,
                   wb.name,
                   wb.county,
                   wb.seed_confidence,
                   fs.id::text AS fishing_spot_id,
                   1 - (wb.name_embedding <=> CAST(:emb AS vector)) AS sem_score
            FROM water_bodies wb
            LEFT JOIN fishing_spots fs ON fs.water_body_id = wb.id
            ORDER BY wb.name_embedding <=> CAST(:emb AS vector)
            LIMIT 10
            """
        ),
        {"emb": str(embedding)},
    )
    return [
        {
            "spot_id": r.fishing_spot_id or r.water_body_id,
            "water_body_id": r.water_body_id,
            "name": r.name,
            "county": r.county,
            "seed_confidence": r.seed_confidence,
            "sem_score": float(r.sem_score) if r.sem_score is not None else 0.0,
        }
        for r in rows
    ]


async def _fuzzy_lookup(location_string: str, db: AsyncSession) -> list[dict]:
    """Top-10 fuzzy matches using pg_trgm similarity against water_bodies name and aliases."""
    rows = await db.execute(
        text(
            """
            SELECT wb.id::text AS water_body_id,
                   wb.name,
                   wb.county,
                   wb.seed_confidence,
                   fs.id::text AS fishing_spot_id,
                   GREATEST(
                     similarity(wb.name, :loc),
                     COALESCE(MAX(similarity(alias_val, :loc)), 0)
                   ) AS trgm_score
            FROM water_bodies wb
            LEFT JOIN fishing_spots fs ON fs.water_body_id = wb.id
            LEFT JOIN LATERAL unnest(wb.aliases) AS alias_val ON true
            WHERE wb.name % :loc
               OR EXISTS (SELECT 1 FROM unnest(wb.aliases) a WHERE a % :loc)
            GROUP BY wb.id, wb.name, wb.county, wb.seed_confidence, fs.id
            ORDER BY trgm_score DESC
            LIMIT 10
            """
        ),
        {"loc": location_string},
    )
    return [
        {
            "spot_id": r.fishing_spot_id or r.water_body_id,
            "water_body_id": r.water_body_id,
            "name": r.name,
            "county": r.county,
            "seed_confidence": r.seed_confidence,
            "trgm_score": float(r.trgm_score),
        }
        for r in rows
    ]


def _merge_results(semantic: list[dict], fuzzy: list[dict]) -> list[dict]:
    """
    Merge semantic and fuzzy results by water_body_id.
    combined_score = 0.6 * sem_score + 0.4 * trgm_score
    Returns top 3 sorted by combined_score descending.
    """
    by_id: dict[str, dict] = {}
    for row in semantic:
        by_id[row["water_body_id"]] = {**row, "trgm_score": 0.0}
    for row in fuzzy:
        wid = row["water_body_id"]
        if wid in by_id:
            by_id[wid]["trgm_score"] = row["trgm_score"]
        else:
            by_id[wid] = {**row, "sem_score": 0.0}

    for entry in by_id.values():
        entry["combined_score"] = (
            0.6 * entry.get("sem_score", 0.0) + 0.4 * entry.get("trgm_score", 0.0)
        )

    ranked = sorted(by_id.values(), key=lambda e: e["combined_score"], reverse=True)
    return ranked[:3]


async def resolve_spot(note_text: str, db: AsyncSession) -> dict:
    """
    Full entity resolution pipeline. Returns a dict with:
      {
        "band": "auto" | "medium" | "low",
        "location_string": str,
        "location_confidence": str,
        "candidates": [...],         # top 3 merged results (spot_id = fishing_spot_id)
        "auto_spot_id": str | None,  # fishing_spot_id when band="auto"
      }
    """
    loc = await extract_location(note_text)
    location_string = loc.get("location_string", "")
    loc_confidence = loc.get("confidence", "none")

    if loc_confidence == "none" or not location_string:
        log.info("spot_resolver_no_location", extra={"loc_confidence": loc_confidence})
        return {
            "band": "low",
            "location_string": "",
            "location_confidence": "none",
            "candidates": [],
            "auto_spot_id": None,
        }

    embedding = await embed_text(location_string)
    semantic = await _semantic_lookup(embedding, db)
    fuzzy = await _fuzzy_lookup(location_string, db)
    candidates = _merge_results(semantic, fuzzy)

    if not candidates:
        band = "low"
        auto_spot_id = None
    elif candidates[0]["combined_score"] >= _AUTO_LINK_THRESHOLD:
        band = "auto"
        auto_spot_id = candidates[0]["spot_id"]
    elif candidates[0]["combined_score"] >= _CANDIDATE_THRESHOLD:
        band = "medium"
        auto_spot_id = None
    else:
        band = "low"
        auto_spot_id = None

    log.info(
        "spot_resolver_result",
        extra={
            "band": band,
            "location_string": location_string,
            "top_score": candidates[0]["combined_score"] if candidates else 0.0,
        },
    )
    return {
        "band": band,
        "location_string": location_string,
        "location_confidence": loc_confidence,
        "candidates": candidates,
        "auto_spot_id": auto_spot_id,
    }


async def apply_correction(
    correct_spot_id: str,
    location_string: str,
    note_id: UUID,
    db: AsyncSession,
) -> None:
    """
    D6: On user correction, append the location_string to the correct water_body's
    aliases[] and re-generate name_embedding.
    correct_spot_id may be a fishing_spot_id; we look up the water_body via the join.
    """
    if not location_string:
        return

    # Resolve to water_body_id — correct_spot_id may be fishing_spot_id or water_body_id
    wb_id_row = await db.execute(
        text("""
            SELECT id::text FROM water_bodies WHERE id = :id
            UNION
            SELECT water_body_id::text FROM fishing_spots WHERE id = :id
            LIMIT 1
        """),
        {"id": correct_spot_id},
    )
    wb_id_result = wb_id_row.one_or_none()
    if not wb_id_result:
        return
    water_body_id = wb_id_result[0]

    await db.execute(
        text(
            """
            UPDATE water_bodies
            SET aliases = array_append(
                    COALESCE(aliases, ARRAY[]::text[]),
                    :loc
                )
            WHERE id = :wb_id
              AND NOT (:loc = ANY(COALESCE(aliases, ARRAY[]::text[])))
            """
        ),
        {"loc": location_string, "wb_id": water_body_id},
    )

    from sqlalchemy import select
    from db.models import WaterBody

    result = await db.execute(select(WaterBody).where(WaterBody.id == water_body_id))
    wb = result.scalar_one_or_none()
    if wb:
        new_embedding = await embed_text(wb.name)
        wb.name_embedding = new_embedding
        db.add(wb)

    log.info(
        "alias_appended",
        extra={"water_body_id": water_body_id, "location_string": location_string},
    )
