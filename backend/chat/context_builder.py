"""
Context assembly pipeline — §6.3.

build_context() is the single entry point, called by chat/router.py on every
chat message. Returns a BuildResult containing the assembled Ollama messages
and session metadata.

Pipeline (§6.3):
  [1] Hard pre-LLM filters — drive time, closures, conditions, permits
  [2] Tier 2 volatile delta overlay → session_score per candidate (§7.5)
  [3] Variety rotation — 60-day rule (§7.6)
  [4] Response cache check
  [5] Hybrid RAG retrieval — pgvector HNSW + tsvector FTS → RRF → re-rank (§5.3, §6.6)
  [6] Map surfacing (§6.7)
  [7] Context assembly within token budget

Steps 1–3 are skipped when conversation.session_candidates is already populated
and force_rerun=False. The pipeline re-runs only on confirmed FILTER_UPDATE.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select, text

from chat.response_cache import get_cached_response
from conditions.normalizer import INTERVAL_REALTIME, compute_conditions_hash
from conditions.routing import get_drive_time, haversine_km, haversine_miles
from db.models import (
    ConditionsCache,
    Conversation,
    EmergencyClosure,
    Message,
    Note,
    Spot,
    Trip,
    User,
)
from prompts.registry import RECOMMENDATION_SYSTEM_PROMPT
from rag.embedder import embed_text
from spots.scorer import cfs_similarity

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token budget (1 token ≈ 4 chars)
# ---------------------------------------------------------------------------
_BUDGET_CONDITIONS = 8_000    # ~2 000 tokens
_BUDGET_NOTES = 16_000        # ~4 000 tokens
_BUDGET_HISTORY = 8_000       # ~2 000 tokens

# Drive-time defaults
_DEFAULT_MAX_DRIVE_MINUTES = 180
_PREFILTER_KM = 250           # rough Haversine pre-filter before HERE calls

# Wildfire proximity threshold
_WILDFIRE_PROXIMITY_KM = 25.0

# Candidate pool size
_MAX_CANDIDATES = 25
_SURFACE_TOP_N = 5            # spots passed to LLM in initial context

# Variety rotation
_VARIETY_DAYS = 60


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    messages: list[dict]           # [{role, content}] for Ollama chat endpoint
    session_candidates: dict       # serialised for conversations.session_candidates JSONB
    conditions_hash: str | None    # cache key for top spot
    drive_time_unavailable: bool   # True when HERE fell back to Haversine
    cached_response: str | None    # non-None on cache hit — skip LLM call


# ---------------------------------------------------------------------------
# [1] Hard filter helpers
# ---------------------------------------------------------------------------

def _matches_water_type(spot: Spot, water_types: list[str]) -> bool:
    if not water_types or "any" in water_types:
        return True
    return spot.type in water_types


def _has_active_closure(spot_id, closures_by_spot: dict[str, list]) -> bool:
    today = date.today()
    for cl in closures_by_spot.get(str(spot_id), []):
        effective = cl.effective or date.min
        expires = cl.expires or date.max
        if effective <= today <= expires:
            return True
    return False


def _wildfire_near_spot(
    spot_lat: float,
    spot_lon: float,
    active_fires: list[dict],
) -> bool:
    """True if any active InciWeb WA fire is within _WILDFIRE_PROXIMITY_KM of the spot."""
    for fire in active_fires:
        try:
            flat = float(fire["latitude"])
            flon = float(fire["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if haversine_km(spot_lat, spot_lon, flat, flon) <= _WILDFIRE_PROXIMITY_KM:
            return True
    return False


_SPECIES_CEILINGS: dict[str, float] = {
    "steelhead": 61.0,
    "trout": 61.0,
    "cutthroat": 61.0,
    "salmon": 61.0,
    "bass": 70.0,
}


def _species_temp_ceiling(target_species: list[str]) -> float | None:
    ceilings = [_SPECIES_CEILINGS[s] for s in target_species if s in _SPECIES_CEILINGS]
    return min(ceilings) if ceilings else None


def _passes_conditions_filter(
    spot: Spot,
    usgs_data: dict | None,
    target_species: list[str],
) -> bool:
    """
    Hard conditions filter for rivers and creeks.
    Lakes are not CFS-filtered; alpine lakes handled separately via _alpine_access_ok.
    Returns True if spot should remain in candidates.
    """
    if spot.type not in ("river", "creek"):
        return True
    if not spot.has_realtime_conditions or usgs_data is None:
        return True  # no data → give benefit of the doubt

    cfs = usgs_data.get("cfs")
    temp_f = usgs_data.get("temp_f")
    turbidity = usgs_data.get("turbidity_fnu")

    if cfs is not None:
        if spot.min_cfs and cfs < float(spot.min_cfs):
            return False
        if spot.max_cfs and cfs > float(spot.max_cfs):
            return False

    if temp_f is not None:
        ceiling = _species_temp_ceiling(target_species)
        if ceiling and temp_f > ceiling:
            return False
        if spot.min_temp_f and temp_f < float(spot.min_temp_f):
            return False

    if turbidity is not None and turbidity > 100:
        return False

    return True


def _alpine_access_ok(
    spot: Spot,
    snotel_data: dict | None,
    today: date,
) -> bool:
    """
    Estimate alpine lake seasonal access. Returns False only when ice-on is
    highly likely. Uncertain cases are included (LLM context will flag them).
    """
    if not spot.is_alpine:
        return True

    if snotel_data:
        swe = snotel_data.get("snow_water_equivalent_in")
        if swe is not None and swe > 30:
            return False  # heavy snowpack — ice-on very likely

    # Elevation + date heuristic when SNOTEL unavailable
    elev = spot.elevation_ft or 0
    month = today.month
    if elev >= 6000 and month < 7:
        return False
    if elev >= 5000 and month < 6:
        return False
    if elev >= 4000 and month < 5:
        return False

    return True


# ---------------------------------------------------------------------------
# [2] Tier 2 volatile delta helpers (§7.5)
# ---------------------------------------------------------------------------

def _sum_7day_precip_estimate(daily_periods: list[dict]) -> float:
    """
    Sum precipitation across up to 14 half-day NWS periods (~7 days).
    Uses probabilityOfPrecipitation as a proxy for intensity.
    """
    total = 0.0
    for period in daily_periods[:14]:
        pop = (period.get("probabilityOfPrecipitation") or {}).get("value") or 0
        if pop >= 70:
            total += 0.20
        elif pop >= 40:
            total += 0.05
    return total


def _compute_volatile_delta(
    spot: Spot,
    usgs_data: dict | None,
    nws_data: dict | None,
    target_species: list[str],
) -> float:
    """
    Compute signed volatile delta per §7.5. Added to spots.score to produce
    session_score. Never written back to the spots table.
    """
    delta = 0.0

    if spot.type in ("river", "creek") and usgs_data:
        cfs = usgs_data.get("cfs")
        if cfs is not None and spot.min_cfs and spot.max_cfs:
            ideal = (float(spot.min_cfs) + float(spot.max_cfs)) / 2
            if ideal > 0:
                pct_off = abs(cfs - ideal) / ideal
                if pct_off <= 0.10:
                    delta += 1.0
                elif pct_off <= 0.25:
                    delta += 0.5
                # 25–50% off → 0 delta

        trend = usgs_data.get("trend")
        if trend == "dropping":
            delta += 0.5
        elif trend == "rising":
            delta -= 0.5

        temp_f = usgs_data.get("temp_f")
        if temp_f is not None:
            ceiling = _species_temp_ceiling(target_species)
            if ceiling:
                gap = ceiling - temp_f
                if gap <= 2:
                    delta -= 2.5
                elif gap <= 5:
                    delta -= 1.0

    if nws_data:
        daily = nws_data.get("daily_forecast") or []
        precip = _sum_7day_precip_estimate(daily)
        if precip <= 0.25:
            delta += 1.0
        elif precip <= 1.0:
            delta -= 0.5
        else:
            delta -= 1.5

    return delta


# ---------------------------------------------------------------------------
# [3] Variety rotation (§7.6)
# ---------------------------------------------------------------------------

def _apply_variety_rotation(candidates: list[dict]) -> list[dict]:
    """
    Ensure at least one spot with last_visited null or > 60 days ago appears
    in the top 5. If none of the current top 5 qualifies, inject the
    highest-scoring qualifying spot from the pool at position 5, displacing
    the lowest-ranked top-5 candidate.
    """
    today = date.today()

    def qualifies(c: dict) -> bool:
        lv = c.get("last_visited")
        if lv is None:
            return True
        if isinstance(lv, str):
            lv = date.fromisoformat(lv)
        return (today - lv).days >= _VARIETY_DAYS

    if any(qualifies(c) for c in candidates[:5]):
        return candidates

    qualifying_rest = [c for c in candidates[5:] if qualifies(c)]
    if not qualifying_rest:
        return candidates

    injected = qualifying_rest[0]
    rotated = (
        candidates[:4]
        + [injected]
        + [c for c in candidates[5:] if c["spot_id"] != injected["spot_id"]]
    )
    log.debug("variety_rotation_applied", extra={"injected": injected["spot_name"]})
    return rotated


# ---------------------------------------------------------------------------
# [5] Hybrid RAG retrieval (§5.3, §6.6)
# ---------------------------------------------------------------------------

async def _hybrid_rag(
    db,
    query: str,
    target_species: list[str],
    current_cfs: float | None,
) -> list[dict]:
    """
    RRF hybrid search: pgvector cosine + tsvector FTS → top-10, re-ranked.
    Returns a list of note dicts for the context notes block.
    """
    try:
        embedding = await embed_text(query)
    except Exception as exc:
        log.warning("rag_embed_failed", extra={"reason": str(exc)})
        return []

    embedding_str = f"[{','.join(str(v) for v in embedding)}]"

    rrf_sql = text("""
        WITH vector_results AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       ORDER BY embedding <=> CAST(:embedding AS vector)
                   ) AS rank
            FROM notes
            WHERE source_type != 'map'
              AND embedding IS NOT NULL
            LIMIT 20
        ),
        keyword_results AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank(fts, plainto_tsquery('english', :query_text)) DESC
                   ) AS rank
            FROM notes
            WHERE fts @@ plainto_tsquery('english', :query_text)
              AND source_type != 'map'
            LIMIT 20
        )
        SELECT n.id, n.content, n.source_type, n.note_date,
               n.species, n.outcome, n.approx_cfs, n.spot_id,
               (1.0 / (60 + COALESCE(v.rank, 21)) +
                1.0 / (60 + COALESCE(k.rank, 21))) AS rrf_score
        FROM notes n
        FULL OUTER JOIN vector_results v ON n.id = v.id
        FULL OUTER JOIN keyword_results k ON n.id = k.id
        WHERE v.id IS NOT NULL OR k.id IS NOT NULL
        ORDER BY rrf_score DESC
        LIMIT 10
    """)

    try:
        result = await db.execute(
            rrf_sql,
            {"embedding": embedding_str, "query_text": query[:500]},
        )
        rows = result.mappings().all()
    except Exception as exc:
        log.warning("rrf_query_failed", extra={"reason": str(exc)})
        return []

    # Re-rank by recency (same season), species match, CFS similarity, outcome (§6.6)
    today = date.today()
    scored = []
    for r in rows:
        boost = float(r["rrf_score"])

        note_date = r["note_date"]
        if note_date:
            season_delta = abs((note_date.month - today.month + 6) % 12 - 6)
            if season_delta <= 1:
                boost += 0.30   # same season in prior years

        note_species = r["species"] or []
        if any(sp in " ".join(note_species).lower() for sp in target_species):
            boost += 0.40

        if current_cfs and r["approx_cfs"]:
            boost += cfs_similarity(float(r["approx_cfs"]), current_cfs) * 0.30

        if r["outcome"] == "positive":
            boost += 0.20

        scored.append((boost, dict(r)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]


# ---------------------------------------------------------------------------
# [6] Map surfacing (§6.7)
# ---------------------------------------------------------------------------

async def _fetch_maps(db, spot_ids: list[str]) -> list[dict]:
    """Retrieve all map notes for the given spot IDs (no cap per §6.7)."""
    if not spot_ids:
        return []
    result = await db.execute(
        select(Note.id, Note.spot_id, Note.image_path, Note.note_date)
        .where(Note.source_type == "map")
        .where(Note.spot_id.in_(spot_ids))
        .where(Note.image_path.is_not(None))
    )
    return [dict(r._mapping) for r in result.all()]


# ---------------------------------------------------------------------------
# [7] Context formatting helpers
# ---------------------------------------------------------------------------

def _format_conditions_block(candidates: list[dict]) -> str:
    lines = []
    for c in candidates[:_SURFACE_TOP_N]:
        usgs = (c.get("conditions") or {}).get("usgs") or {}
        nws = (c.get("conditions") or {}).get("noaa_nws") or {}
        lines.append(f"\n=== {c['spot_name']} (score: {c['session_score']:.2f}) ===")

        if c.get("is_haversine"):
            lines.append(f"Distance: ~{c.get('straight_line_miles', '?')} miles straight-line")
        elif c.get("drive_minutes"):
            lines.append(f"Drive time: {c['drive_minutes']} min")

        cfs = usgs.get("cfs")
        temp = usgs.get("temp_f")
        turb = usgs.get("turbidity_fnu")
        if cfs is not None:
            lines.append(f"Flow: {cfs:.0f} CFS")
        if temp is not None:
            lines.append(f"Water temp: {temp:.1f}°F")
        if turb is not None:
            lines.append(f"Turbidity: {turb:.0f} FNU")

        current = (nws.get("current") or {})
        if current.get("short_forecast"):
            temp_str = f", {current['temp_f']}°F" if current.get("temp_f") else ""
            lines.append(f"Weather: {current['short_forecast']}{temp_str}")

    return "\n".join(lines)


def _format_notes_block(notes: list[dict], maps: list[dict]) -> str:
    lines = ["=== GROUP NOTES ==="]
    for n in notes:
        nd = n.get("note_date") or "unknown date"
        outcome = (n.get("outcome") or "neutral").upper()
        content = (n.get("content") or "")[:400]
        lines.append(f"[{nd}] {outcome} — {content}")
    if maps:
        lines.append(f"\n[{len(maps)} hand-drawn map(s) available — rendered inline by UI]")
    return "\n".join(lines)


def _format_history_block(past_trips: list[dict]) -> str:
    if not past_trips:
        return ""
    lines = ["=== TRIP HISTORY ==="]
    for t in past_trips:
        lines.append(f"[{t.get('trip_date', '?')}] {t.get('spot_name', '?')}")
    return "\n".join(lines)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[... truncated for token budget]"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def build_context(
    *,
    user: User,
    trip: Trip,
    conversation: Conversation,
    query: str,
    db,
    force_rerun: bool = False,
) -> BuildResult:
    """
    Assemble the full LLM context for a chat message.

    force_rerun=True is passed by POST /chat/confirm-filter when the user
    confirms a FILTER_UPDATE — triggers a full pipeline re-run and replaces
    session_candidates.
    """
    intake = trip.session_intake or {}
    prefs = user.preferences or {}

    water_types: list[str] = intake.get("water_type") or []
    target_species: list[str] = intake.get("target_species") or []
    max_drive_minutes = int(intake.get("max_drive_minutes") or _DEFAULT_MAX_DRIVE_MINUTES)
    departure_time = trip.departure_time or datetime.now(tz=timezone.utc)

    # Departure location: session override (pivot) takes precedence over profile home
    departure_location = intake.get("departure_location") or prefs.get("home_location") or {}
    origin_lat = departure_location.get("lat")
    origin_lon = departure_location.get("lon")

    # ------------------------------------------------------------------
    # Re-use existing session_candidates when pipeline re-run not needed
    # ------------------------------------------------------------------
    existing = conversation.session_candidates
    if existing and not force_rerun:
        if isinstance(existing, dict):
            candidates = existing.get("candidates", [])
            drive_time_unavailable = existing.get("drive_time_unavailable", False)
        else:
            candidates = existing
            drive_time_unavailable = False
    else:
        # --------------------------------------------------------------
        # [1] Hard pre-LLM filters
        # --------------------------------------------------------------
        spot_result = await db.execute(
            select(Spot).where(
                Spot.latitude.is_not(None),
                Spot.longitude.is_not(None),
                Spot.fly_fishing_legal.is_(True),
            )
        )
        all_spots: list[Spot] = spot_result.scalars().all()

        # Water type filter
        spots = [s for s in all_spots if _matches_water_type(s, water_types)]

        # Rough geo pre-filter before calling HERE
        if origin_lat and origin_lon:
            spots = [
                s for s in spots
                if haversine_km(
                    float(s.latitude), float(s.longitude),
                    origin_lat, origin_lon,
                ) <= _PREFILTER_KM
            ]

        spot_ids = [s.id for s in spots]

        # Active emergency closures
        closure_result = await db.execute(
            select(EmergencyClosure).where(EmergencyClosure.spot_id.in_(spot_ids))
        )
        closures_by_spot: dict[str, list] = {}
        for cl in closure_result.scalars().all():
            closures_by_spot.setdefault(str(cl.spot_id), []).append(cl)

        # InciWeb active WA fires (global — spot_id IS NULL in conditions_cache)
        inciweb_result = await db.execute(
            select(ConditionsCache.data)
            .where(ConditionsCache.source == "inciweb")
            .where(ConditionsCache.spot_id.is_(None))
            .order_by(ConditionsCache.fetched_at.desc())
            .limit(1)
        )
        inciweb_row = inciweb_result.scalar_one_or_none()
        active_fires: list[dict] = (inciweb_row or {}).get("active_wa_fires", [])

        # Conditions cache for all candidate spots
        cond_result = await db.execute(
            select(ConditionsCache).where(ConditionsCache.spot_id.in_(spot_ids))
        )
        cond_by: dict[tuple, dict] = {}
        for c in cond_result.scalars().all():
            cond_by[(str(c.spot_id), c.source)] = c.data

        # Apply hard filters
        today = date.today()
        filtered: list[Spot] = []
        for spot in spots:
            sid = str(spot.id)
            if spot.permit_required:
                continue
            if _has_active_closure(spot.id, closures_by_spot):
                continue
            if _wildfire_near_spot(float(spot.latitude), float(spot.longitude), active_fires):
                continue
            if not _passes_conditions_filter(spot, cond_by.get((sid, "usgs")), target_species):
                continue
            if spot.is_alpine and not _alpine_access_ok(spot, cond_by.get((sid, "snotel")), today):
                continue
            filtered.append(spot)

        # Drive-time filter — parallel HERE calls
        drive_time_unavailable = False
        candidates_raw: list[dict] = []

        if origin_lat and origin_lon and filtered:
            tasks = [
                get_drive_time(
                    origin_lat, origin_lon,
                    float(s.latitude), float(s.longitude),
                    departure_time,
                )
                for s in filtered
            ]
            drive_results = await asyncio.gather(*tasks)

            for spot, (drive_min, is_fallback) in zip(filtered, drive_results):
                if is_fallback:
                    drive_time_unavailable = True
                if drive_min > max_drive_minutes:
                    continue
                sid = str(spot.id)
                candidates_raw.append({
                    "spot": spot,
                    "drive_minutes": drive_min,
                    "is_haversine": is_fallback,
                    "straight_line_miles": (
                        haversine_miles(origin_lat, origin_lon, float(spot.latitude), float(spot.longitude))
                        if is_fallback else None
                    ),
                    "usgs": cond_by.get((sid, "usgs")),
                    "nws": cond_by.get((sid, "noaa_nws")),
                })
        else:
            # No home location — include all filtered spots without drive-time gate
            for spot in filtered:
                sid = str(spot.id)
                candidates_raw.append({
                    "spot": spot,
                    "drive_minutes": None,
                    "is_haversine": False,
                    "straight_line_miles": None,
                    "usgs": cond_by.get((sid, "usgs")),
                    "nws": cond_by.get((sid, "noaa_nws")),
                })

        # --------------------------------------------------------------
        # [2] Tier 2 volatile delta → session_score
        # --------------------------------------------------------------
        for c in candidates_raw:
            delta = _compute_volatile_delta(c["spot"], c["usgs"], c["nws"], target_species)
            c["session_score"] = float(c["spot"].score or 0) + delta

        candidates_raw.sort(key=lambda c: c["session_score"], reverse=True)
        candidates_raw = candidates_raw[:_MAX_CANDIDATES]

        # Serialise to JSONB-safe dicts
        candidates = [
            {
                "spot_id": str(c["spot"].id),
                "spot_name": c["spot"].name,
                "spot_type": c["spot"].type,
                "session_score": round(c["session_score"], 4),
                "drive_minutes": c["drive_minutes"],
                "is_haversine": c["is_haversine"],
                "straight_line_miles": c["straight_line_miles"],
                "last_visited": (
                    c["spot"].last_visited.isoformat() if c["spot"].last_visited else None
                ),
                "conditions": {"usgs": c["usgs"], "noaa_nws": c["nws"]},
            }
            for c in candidates_raw
        ]

        # --------------------------------------------------------------
        # [3] Variety rotation — 60-day rule (§7.6)
        # --------------------------------------------------------------
        candidates = _apply_variety_rotation(candidates)

    # ------------------------------------------------------------------
    # [4] Response cache check
    # ------------------------------------------------------------------
    cached_response = None
    conditions_hash = None
    top = candidates[0] if candidates else None

    if top:
        usgs = (top.get("conditions") or {}).get("usgs") or {}
        if usgs.get("cfs") is not None or usgs.get("temp_f") is not None:
            conditions_hash = compute_conditions_hash(
                cfs=usgs.get("cfs"),
                temp_f=usgs.get("temp_f"),
                turbidity_fnu=usgs.get("turbidity_fnu"),
                fetched_at=datetime.now(tz=timezone.utc),
                interval_minutes=INTERVAL_REALTIME,
            )
            cached_response = await get_cached_response(db, top["spot_id"], conditions_hash)

    serialised_candidates = {
        "candidates": candidates,
        "drive_time_unavailable": drive_time_unavailable,
    }

    if cached_response:
        return BuildResult(
            messages=[],
            session_candidates=serialised_candidates,
            conditions_hash=conditions_hash,
            drive_time_unavailable=drive_time_unavailable,
            cached_response=cached_response,
        )

    # ------------------------------------------------------------------
    # [5] Hybrid RAG retrieval
    # ------------------------------------------------------------------
    top_usgs = (top.get("conditions") or {}).get("usgs") if top else None
    current_cfs = (top_usgs or {}).get("cfs")
    notes = await _hybrid_rag(db, query, target_species, current_cfs)

    # ------------------------------------------------------------------
    # [6] Map surfacing
    # ------------------------------------------------------------------
    top_spot_ids = [c["spot_id"] for c in candidates[:_SURFACE_TOP_N]]
    maps = await _fetch_maps(db, top_spot_ids)

    # ------------------------------------------------------------------
    # [7] Context assembly
    # ------------------------------------------------------------------
    history_result = await db.execute(
        select(Trip.trip_date, Spot.name)
        .join(Spot, Trip.spot_id == Spot.id)
        .where(Trip.user_id == user.id)
        .where(Trip.state == "DEBRIEFED")
        .order_by(Trip.trip_date.desc())
        .limit(5)
    )
    past_trips = [
        {"trip_date": str(r.trip_date or ""), "spot_name": r.name}
        for r in history_result.all()
    ]

    msg_result = await db.execute(
        select(Message.role, Message.content)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at)
    )
    prior_messages = [{"role": r.role, "content": r.content} for r in msg_result.all()]

    conditions_block = _truncate(_format_conditions_block(candidates), _BUDGET_CONDITIONS)
    notes_block = _truncate(_format_notes_block(notes, maps), _BUDGET_NOTES)
    history_block = _truncate(_format_history_block(past_trips), _BUDGET_HISTORY)

    map_refs = ""
    if maps:
        map_refs = "\n=== MAPS ===\n" + "\n".join(
            f"MAP_ID:{m['id']}:SPOT:{m['spot_id']}" for m in maps
        )

    system_content = "\n\n".join(filter(None, [
        RECOMMENDATION_SYSTEM_PROMPT.strip(),
        conditions_block,
        notes_block,
        history_block,
        map_refs,
    ]))

    messages = [{"role": "system", "content": system_content}]
    for m in prior_messages:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": query})

    return BuildResult(
        messages=messages,
        session_candidates=serialised_candidates,
        conditions_hash=conditions_hash,
        drive_time_unavailable=drive_time_unavailable,
        cached_response=None,
    )
