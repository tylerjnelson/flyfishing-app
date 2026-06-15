"""
Turn builder — Phase 2 of the architectural migration plan.

Decouples narrative reasoning from data display. After the LLM stream
completes, build_turn() validates the [RECOMMEND: ...] block captured by
StreamHandler, assembles structured spot cards from session candidate data
and DB lookups, and returns a clean {narrative, cards} payload.

The router emits these as separate SSE events:
  {type: 'narrative', text: '...'}  — during stream (unchanged)
  {type: 'spot_cards', cards: [...]} — after stream end
"""

import logging
import re
import uuid as _uuid_mod

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import FishingSpot, Note, WaterBody

log = logging.getLogger(__name__)

_RE_RECOMMEND = re.compile(r'\[RECOMMEND:\s*([^\]]+)\]', re.IGNORECASE)


def _is_valid_uuid(value: str) -> bool:
    try:
        _uuid_mod.UUID(str(value))
        return True
    except ValueError:
        return False


def parse_recommend_block(text: str) -> tuple[str, list[str]]:
    """
    Extract [RECOMMEND: uuid1, uuid2, uuid3] from text.

    Returns (narrative, uuids) where narrative has the block stripped.
    uuids is [] when no block is found or it cannot be parsed.
    """
    m = re.search(_RE_RECOMMEND, text)
    if not m:
        return text.strip(), []
    raw = m.group(1)
    uuids = [s.strip() for s in raw.split(",") if s.strip()]
    narrative = (text[: m.start()] + text[m.end() :]).strip()
    return narrative, uuids


async def build_turn(
    narrative: str,
    recommend_block: str | None,
    candidates: list[dict],
    db: AsyncSession,
) -> dict:
    """
    Validate [RECOMMEND: ...] and assemble spot cards.

    narrative        — LLM output with the [RECOMMEND] block already stripped
                       (captured by StreamHandler before forwarding to SSE)
    recommend_block  — raw "[RECOMMEND: uuid1, uuid2, uuid3]" string, or None
    candidates       — session_candidates["candidates"] list from build_context
    db               — async DB session for WaterBody and Note lookups

    Returns:
      {"narrative": str, "cards": [card, card, card]}         — success
      {"narrative": str, "cards": [], "error": "reason"}      — failure
    """
    if not recommend_block:
        log.warning("recommend_block_missing")
        return {"narrative": narrative, "cards": [], "error": "missing_recommend_block"}

    _, uuids = parse_recommend_block(recommend_block)

    if len(uuids) != 3 or not all(_is_valid_uuid(u) for u in uuids):
        log.warning("recommend_block_invalid", extra={"raw": recommend_block[:200]})
        return {"narrative": narrative, "cards": [], "error": "invalid_recommend_block"}

    # Index session candidates by spot_id for O(1) lookup
    by_id = {c["spot_id"]: c for c in candidates}

    uuid_objs = [_uuid_mod.UUID(u) for u in uuids]

    # Fetch WaterBody fields (regs, fly-only, stocking, species) for all 3 spots
    fs_result = await db.execute(
        select(FishingSpot, WaterBody)
        .join(WaterBody, FishingSpot.water_body_id == WaterBody.id)
        .where(FishingSpot.id.in_(uuid_objs))
    )
    fs_map: dict[str, tuple] = {
        str(row.FishingSpot.id): (row.FishingSpot, row.WaterBody)
        for row in fs_result
    }

    # Note counts per fishing spot
    note_result = await db.execute(
        select(Note.fishing_spot_id, func.count(Note.id).label("cnt"))
        .where(Note.fishing_spot_id.in_(uuid_objs))
        .group_by(Note.fishing_spot_id)
    )
    note_counts: dict[str, int] = {
        str(r.fishing_spot_id): r.cnt for r in note_result
    }

    cards = []
    for spot_id in uuids:
        cand = by_id.get(spot_id, {})
        cond = cand.get("conditions") or {}
        usgs = cond.get("usgs") or {}
        nws = cond.get("noaa_nws") or {}
        nws_current = nws.get("current") or {}
        airnow = cond.get("airnow") or {}

        db_row = fs_map.get(spot_id)
        if db_row:
            fs, wb = db_row
            fly_fishing_legal = wb.fly_fishing_legal
            fishing_regs = wb.fishing_regs
            last_stocked_date = (
                wb.last_stocked_date.isoformat() if wb.last_stocked_date else None
            )
            last_stocked_species = list(wb.last_stocked_species or [])
            species_primary = list(wb.species_primary or [])
        else:
            fly_fishing_legal = None
            fishing_regs = None
            last_stocked_date = None
            last_stocked_species = []
            species_primary = []

        cards.append(
            {
                "spot_id": spot_id,
                "name": cand.get("spot_name") or cand.get("water_body_name", ""),
                "water_body_name": cand.get("water_body_name", ""),
                "spot_type": cand.get("spot_type", ""),
                "drive_minutes": cand.get("drive_minutes"),
                "is_haversine": cand.get("is_haversine", False),
                "straight_line_miles": cand.get("straight_line_miles"),
                "session_score": cand.get("session_score"),
                "last_visited": cand.get("last_visited"),
                "warnings": list(cand.get("warnings") or []),
                "conditions": {
                    "cfs": usgs.get("cfs"),
                    "cfs_trend": usgs.get("trend"),
                    "water_temp_f": usgs.get("temp_f"),
                    "weather_summary": nws_current.get("short_forecast"),
                    "air_temp_f": nws_current.get("temp_f"),
                    "aqi": airnow.get("aqi"),
                },
                "fly_fishing_legal": fly_fishing_legal,
                "fishing_regs": fishing_regs,
                "last_stocked_date": last_stocked_date,
                "last_stocked_species": last_stocked_species,
                "species_primary": species_primary,
                "note_count": note_counts.get(spot_id, 0),
            }
        )

    log.info("turn_built", extra={"spot_ids": uuids})
    return {"narrative": narrative, "cards": cards}
