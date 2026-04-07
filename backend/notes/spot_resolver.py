"""
Spot entity resolution for ingested notes (§6.8 Step D).

Pipeline:
  D1. Extract location string from OCR text (Llama 3.1 8B — §18.6)
  D2. Embed the location string via nomic-embed-text
  D3. Run semantic (pgvector) + fuzzy (pg_trgm) lookups against spots table
  D4. Merge: combined_score = 0.6 * sem_score + 0.4 * trgm_score, take top 3
  D5. Branch on top combined_score:
        >= 0.85  → auto-link (set spot_id, show pre-filled card)
        0.50–0.84 → return top 3 for user selection (blocking)
        < 0.50   → return "create new spot" signal

Confidence bands are also returned so the router / frontend can decide
what UI to show the user.
"""

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from llm.client import CHAT_MODEL, call_json_llm
from prompts.registry import LOCATION_EXTRACTION_PROMPT
from rag.embedder import embed_text

log = logging.getLogger(__name__)

# Band thresholds (§6.8 D5)
_AUTO_LINK_THRESHOLD = 0.85
_CANDIDATE_THRESHOLD = 0.50

_LOCATION_DEFAULT = {"location_string": "", "confidence": "none"}


async def extract_location(note_text: str) -> dict:
    """
    Call Llama 3.1 8B to extract the fishing location from note text.
    Returns {"location_string": str, "confidence": str}.
    """
    prompt = LOCATION_EXTRACTION_PROMPT.format(note_text=note_text)
    result = await call_json_llm(prompt, CHAT_MODEL, _LOCATION_DEFAULT)
    return result


async def _semantic_lookup(embedding: list[float], db: AsyncSession) -> list[dict]:
    """Top-10 semantic matches by name_embedding cosine similarity."""
    rows = await db.execute(
        text(
            """
            SELECT id::text, name, county, seed_confidence,
                   1 - (name_embedding <=> CAST(:emb AS vector)) AS sem_score
            FROM spots
            ORDER BY name_embedding <=> CAST(:emb AS vector)
            LIMIT 10
            """
        ),
        {"emb": str(embedding)},
    )
    return [
        {
            "spot_id": r.id,
            "name": r.name,
            "county": r.county,
            "seed_confidence": r.seed_confidence,
            "sem_score": float(r.sem_score) if r.sem_score is not None else 0.0,
        }
        for r in rows
    ]


async def _fuzzy_lookup(location_string: str, db: AsyncSession) -> list[dict]:
    """Top-10 fuzzy matches using pg_trgm similarity against name and aliases."""
    rows = await db.execute(
        text(
            """
            SELECT id::text, name, county, seed_confidence,
                   GREATEST(
                     similarity(name, :loc),
                     COALESCE(MAX(similarity(alias_val, :loc)), 0)
                   ) AS trgm_score
            FROM spots
            LEFT JOIN LATERAL unnest(aliases) AS alias_val ON true
            WHERE name % :loc
               OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE a % :loc)
            GROUP BY id, name, county, seed_confidence
            ORDER BY trgm_score DESC
            LIMIT 10
            """
        ),
        {"loc": location_string},
    )
    return [
        {
            "spot_id": r.id,
            "name": r.name,
            "county": r.county,
            "seed_confidence": r.seed_confidence,
            "trgm_score": float(r.trgm_score),
        }
        for r in rows
    ]


def _merge_results(semantic: list[dict], fuzzy: list[dict]) -> list[dict]:
    """
    Merge semantic and fuzzy results by spot_id.
    combined_score = 0.6 * sem_score + 0.4 * trgm_score
    Returns top 3 sorted by combined_score descending.
    """
    by_id: dict[str, dict] = {}
    for row in semantic:
        by_id[row["spot_id"]] = {**row, "trgm_score": 0.0}
    for row in fuzzy:
        sid = row["spot_id"]
        if sid in by_id:
            by_id[sid]["trgm_score"] = row["trgm_score"]
        else:
            by_id[sid] = {**row, "sem_score": 0.0}

    for entry in by_id.values():
        entry["combined_score"] = (
            0.6 * entry.get("sem_score", 0.0) + 0.4 * entry.get("trgm_score", 0.0)
        )

    ranked = sorted(by_id.values(), key=lambda e: e["combined_score"], reverse=True)
    return ranked[:3]


async def resolve_spot(note_text: str, db: AsyncSession) -> dict:
    """
    Full entity resolution pipeline.  Returns a dict with:
      {
        "band": "auto" | "medium" | "low",
        "location_string": str,
        "location_confidence": str,  # from LLM
        "candidates": [...],         # top 3 merged results (empty for band="low")
        "auto_spot_id": str | None,  # set only when band="auto"
      }

    Callers should:
      - band="auto"   → set notes.spot_id = auto_spot_id immediately (non-blocking UI)
      - band="medium" → show candidates to user for selection (blocking UI)
      - band="low"    → show "Create new spot" flow (blocking UI)
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
    D6: On user correction, append the original location_string to the
    correct spot's aliases[] and re-generate name_embedding for that spot.

    This improves future resolution accuracy organically.
    """
    if not location_string:
        return

    await db.execute(
        text(
            """
            UPDATE spots
            SET aliases = array_append(
                    COALESCE(aliases, ARRAY[]::text[]),
                    :loc
                )
            WHERE id = :spot_id
              AND NOT (:loc = ANY(COALESCE(aliases, ARRAY[]::text[])))
            """
        ),
        {"loc": location_string, "spot_id": correct_spot_id},
    )

    # Re-generate name_embedding for the corrected spot (updated name + new alias).
    # Import inline to avoid circular imports.
    from sqlalchemy import select

    from db.models import Spot

    result = await db.execute(select(Spot).where(Spot.id == correct_spot_id))
    spot = result.scalar_one_or_none()
    if spot:
        new_embedding = await embed_text(spot.name)
        spot.name_embedding = new_embedding
        db.add(spot)

    log.info(
        "alias_appended",
        extra={"spot_id": correct_spot_id, "location_string": location_string},
    )
