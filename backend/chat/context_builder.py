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
from conditions.airnow import fetch_airnow
from conditions.normalizer import INTERVAL_REALTIME, compute_conditions_hash
from conditions.noaa_nws import fetch_noaa_nws
from conditions.routing import get_drive_time, haversine_km, haversine_miles
from conditions.usgs import fetch_usgs_gauge
from db.connection import AsyncSessionLocal
from db.models import (
    ConditionsCache,
    Conversation,
    EmergencyClosure,
    FishingSpot,
    Message,
    Note,
    Trip,
    User,
    WaterBody,
)
from prompts.registry import DEBRIEF_CONVERSATION_PROMPT, RECOMMENDATION_SYSTEM_PROMPT
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

def _matches_water_type(water_body: WaterBody, water_types: list[str]) -> bool:
    if not water_types or "any" in water_types:
        return True
    return water_body.type in water_types


_CLOSURE_KEYWORDS: frozenset[str] = frozenset(
    {"close", "closed", "closure", "closes", "prohibited", "emergency"}
)

_GEO_TYPE_WORDS: frozenset[str] = frozenset({
    "river", "creek", "lake", "pond", "reservoir", "stream",
    "tributary", "bay", "channel", "fork", "run",
})


def _has_active_closure(water_body_name: str, active_closures: list) -> bool:
    """
    Return True if any active closure's rule_text references this water body by name
    and contains at least one closure keyword.
    """
    if not active_closures or not water_body_name:
        return False
    all_name_words = frozenset(water_body_name.lower().split())
    content_words = all_name_words - _GEO_TYPE_WORDS
    if not content_words:
        content_words = all_name_words
    for cl in active_closures:
        text_lower = (cl.rule_text or "").lower()
        text_words = frozenset(text_lower.split())
        if content_words <= text_words and _CLOSURE_KEYWORDS & text_words:
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


def _cfs_out_of_range(
    water_body: WaterBody,
    usgs_data: dict | None,
) -> bool:
    """
    True when live CFS is outside min/max and realtime conditions are available.
    This is the only hard conditions filter for rivers/creeks; temperature,
    turbidity, and other signals are soft penalties in _compute_volatile_delta.
    """
    if water_body.type not in ("river", "creek"):
        return False
    if not water_body.has_realtime_conditions or usgs_data is None:
        return False
    cfs = usgs_data.get("cfs")
    if cfs is None:
        return False
    if water_body.min_cfs and cfs < float(water_body.min_cfs):
        return True
    if water_body.max_cfs and cfs > float(water_body.max_cfs):
        return True
    return False


# ---------------------------------------------------------------------------
# [1.5] Real-time conditions fetch — session open (§4.1)
# ---------------------------------------------------------------------------

async def _fetch_and_cache_realtime(fishing_spots: list, water_bodies: list[WaterBody]) -> None:
    """
    Fetch USGS, NOAA NWS, and AirNow data for candidate spots in parallel.
    Writes fresh rows to conditions_cache keyed by water_body_id.
    """
    import hashlib
    import json

    wb_by_id = {str(wb.id): wb for wb in water_bodies}

    usgs_pairs = [
        (fs, wb_by_id[str(fs.water_body_id)])
        for fs in fishing_spots
        if fs.water_body_id and str(fs.water_body_id) in wb_by_id
        and wb_by_id[str(fs.water_body_id)].usgs_site_ids
    ]
    geo_spots = [fs for fs in fishing_spots if fs.latitude is not None and fs.longitude is not None]

    if not usgs_pairs and not geo_spots:
        return

    usgs_results = await asyncio.gather(
        *[fetch_usgs_gauge(wb.usgs_site_ids[0]) for _, wb in usgs_pairs]
    )
    nws_results = await asyncio.gather(
        *[fetch_noaa_nws(float(fs.latitude), float(fs.longitude)) for fs in geo_spots]
    )
    airnow_results = await asyncio.gather(
        *[fetch_airnow(float(fs.latitude), float(fs.longitude)) for fs in geo_spots]
    )

    to_write: list[tuple] = []  # (water_body_id, source, data_dict)
    for (fs, wb), data in zip(usgs_pairs, usgs_results):
        if data is not None:
            to_write.append((wb.id, "usgs", data))
    for fs, data in zip(geo_spots, nws_results):
        if data is not None:
            to_write.append((fs.water_body_id, "noaa_nws", data))
    for fs, data in zip(geo_spots, airnow_results):
        if data is not None:
            to_write.append((fs.water_body_id, "airnow", data))

    if not to_write:
        return

    async with AsyncSessionLocal() as session:
        async with session.begin():
            for water_body_id, source, data in to_write:
                data_hash = hashlib.md5(
                    json.dumps(
                        data, sort_keys=True, separators=(",", ":"), default=str
                    ).encode()
                ).hexdigest()
                session.add(ConditionsCache(
                    water_body_id=water_body_id,
                    source=source,
                    data=data,
                    data_hash=data_hash,
                    fetched_at=datetime.now(tz=timezone.utc),
                ))

    log.info(
        "realtime_conditions_fetched",
        extra={"usgs_count": len(usgs_pairs), "nws_count": len(geo_spots),
               "airnow_count": len(geo_spots), "written": len(to_write)},
    )


# ---------------------------------------------------------------------------
# [2] Tier 2 volatile delta helpers (§7.5)
# ---------------------------------------------------------------------------

_FUTURE_TRIP_HOURS = 24


def _sum_7day_precip_estimate(daily_periods: list[dict]) -> float:
    total = 0.0
    for period in daily_periods[:14]:
        pop = (period.get("probabilityOfPrecipitation") or {}).get("value") or 0
        if pop >= 70:
            total += 0.20
        elif pop >= 40:
            total += 0.05
    return total


def _nwrfc_cfs_at(nwrfc_data: dict, departure_time: datetime) -> float | None:
    forecasts = (nwrfc_data or {}).get("forecast") or []
    best_cfs: float | None = None
    best_diff: float | None = None
    for entry in forecasts:
        valid_str = entry.get("validTime")
        secondary = entry.get("secondary")
        if valid_str is None or secondary is None:
            continue
        try:
            valid_dt = datetime.fromisoformat(valid_str.replace("Z", "+00:00"))
            if valid_dt.tzinfo is None:
                valid_dt = valid_dt.replace(tzinfo=timezone.utc)
            diff = abs((valid_dt - departure_time).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_cfs = float(secondary) * 1000.0
        except (ValueError, TypeError):
            continue
    return best_cfs


def _species_match_delta(target_species: list[str], water_body: WaterBody) -> float:
    """
    Compare session target_species against water_body.species_primary.
    Full match → +1.5, partial → +0.5, no match → -0.5.
    Returns 0.0 if either side is empty.
    """
    if not target_species or not water_body.species_primary:
        return 0.0
    target_lower = {s.lower() for s in target_species}
    spot_lower = {s.lower() for s in water_body.species_primary}
    if target_lower & spot_lower == target_lower:
        return 1.5
    if target_lower & spot_lower:
        return 0.5
    return -0.5


def _compute_volatile_delta(
    water_body: WaterBody,
    usgs_data: dict | None,
    nws_data: dict | None,
    nwrfc_data: dict | None,
    target_species: list[str],
    departure_time: datetime | None = None,
    airnow_data: dict | None = None,
    snotel_data: dict | None = None,
    active_fires: list[dict] | None = None,
    spot_lat: float | None = None,
    spot_lon: float | None = None,
) -> tuple[float, list[str]]:
    """
    Compute signed volatile delta per §7.5. Returns (delta, warnings).
    delta is added to water_body.score to produce session_score (never written back).
    warnings are surfaced in the conditions block for the LLM.

    Hard filters (closures, CFS out-of-range) are handled separately.
    Everything else — wildfire, alpine access, AQI, turbidity, temp — is a
    scored penalty here so the LLM can communicate the issue to the angler.
    """
    delta = 0.0
    warnings: list[str] = []
    now = datetime.now(tz=timezone.utc)
    today = now.date()

    # Wildfire proximity penalty
    if active_fires and spot_lat is not None and spot_lon is not None:
        if _wildfire_near_spot(spot_lat, spot_lon, active_fires):
            delta -= 2.5
            warnings.append("Active wildfire within 25km — smoke and access risk")

    # Alpine access penalties
    if water_body.is_alpine:
        if snotel_data:
            swe = snotel_data.get("snow_water_equivalent_in")
            if swe is not None and swe > 30:
                delta -= 2.5
                warnings.append(f"Heavy snowpack ({swe:.1f}\" SWE) — access uncertain")

        elev = water_body.elevation_ft or 0
        month = today.month
        blocked = (
            (elev >= 6000 and month < 7)
            or (elev >= 5000 and month < 6)
            or (elev >= 4000 and month < 5)
        )
        if blocked:
            delta -= 2.0
            warnings.append(f"Early season at elevation ({elev:,} ft) — access uncertain")

    if water_body.type in ("river", "creek"):
        cfs: float | None = None
        if (
            nwrfc_data
            and departure_time is not None
            and (departure_time - now).total_seconds() > _FUTURE_TRIP_HOURS * 3600
        ):
            cfs = _nwrfc_cfs_at(nwrfc_data, departure_time)
        elif usgs_data:
            cfs = usgs_data.get("cfs")

        if cfs is not None and water_body.min_cfs and water_body.max_cfs:
            ideal = (float(water_body.min_cfs) + float(water_body.max_cfs)) / 2
            if ideal > 0:
                pct_off = abs(cfs - ideal) / ideal
                if pct_off <= 0.10:
                    delta += 1.0
                elif pct_off <= 0.25:
                    delta += 0.5

        if usgs_data:
            trend = usgs_data.get("trend")
            if trend == "dropping":
                delta += 0.5
            elif trend == "rising":
                delta -= 0.5

            temp_f = usgs_data.get("temp_f")
            if temp_f is not None:
                # Penalty for above-ceiling and near-ceiling temps
                ceiling = _species_temp_ceiling(target_species)
                if ceiling:
                    gap = ceiling - temp_f
                    if gap <= 2:
                        delta -= 2.5
                        if gap <= 0:
                            warnings.append(f"Water temp above safe fishing threshold ({temp_f:.1f}°F)")
                    elif gap <= 5:
                        delta -= 1.0

                # Penalty for temp below minimum active feeding threshold
                if water_body.min_temp_f and temp_f < float(water_body.min_temp_f):
                    delta -= 1.0
                    warnings.append(f"Water temp below active feeding threshold ({temp_f:.1f}°F)")

            turbidity = usgs_data.get("turbidity_fnu")
            if turbidity is not None and turbidity > 100:
                delta -= 1.5
                warnings.append(f"High turbidity: {turbidity:.0f} FNU")

    if nws_data:
        daily = nws_data.get("daily_forecast") or []
        precip = _sum_7day_precip_estimate(daily)
        if precip <= 0.25:
            delta += 1.0
        elif precip <= 1.0:
            delta -= 0.5
        else:
            delta -= 1.5

        wind_mph = (nws_data.get("current") or {}).get("wind_speed_mph")
        if wind_mph is not None:
            if wind_mph > 25:
                delta -= 1.0
            elif wind_mph > 15:
                delta -= 0.5

    if airnow_data:
        aqi = airnow_data.get("aqi")
        if aqi is not None:
            if aqi > 200:
                delta -= 3.0
                category = airnow_data.get("category") or "Hazardous"
                warnings.append(f"Air quality: {category} (AQI {aqi})")
            elif aqi >= 151:
                delta -= 1.0

    delta += _species_match_delta(target_species, water_body)

    return delta, warnings


# ---------------------------------------------------------------------------
# [3] Variety rotation (§7.6)
# ---------------------------------------------------------------------------

def _apply_variety_rotation(candidates: list[dict]) -> list[dict]:
    """
    Ensure at least one spot with last_visited null or > 60 days ago appears
    in the top 5.
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
    """RRF hybrid search: pgvector cosine + tsvector FTS → top-10, re-ranked."""
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
               n.species, n.outcome, n.approx_cfs, n.fishing_spot_id,
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

    today = date.today()
    scored = []
    for r in rows:
        boost = float(r["rrf_score"])

        note_date = r["note_date"]
        if note_date:
            season_delta = abs((note_date.month - today.month + 6) % 12 - 6)
            if season_delta <= 1:
                boost += 0.30

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

async def _fetch_maps(db, fishing_spot_ids: list[str]) -> list[dict]:
    """Retrieve all map notes for the given fishing_spot IDs."""
    if not fishing_spot_ids:
        return []
    result = await db.execute(
        select(Note.id, Note.fishing_spot_id, Note.image_path, Note.note_date)
        .where(Note.source_type == "map")
        .where(Note.fishing_spot_id.in_(fishing_spot_ids))
        .where(Note.image_path.is_not(None))
    )
    return [dict(r._mapping) for r in result.all()]


# ---------------------------------------------------------------------------
# [7] Context formatting helpers
# ---------------------------------------------------------------------------

def _format_conditions_block(candidates: list[dict]) -> str:
    lines = []
    for c in candidates[:_SURFACE_TOP_N]:
        conds = c.get("conditions") or {}
        usgs = conds.get("usgs") or {}
        nws = conds.get("noaa_nws") or {}
        wta = conds.get("wta") or {}
        airnow = conds.get("airnow") or {}

        # Display water_body_name; append spot name when spot has its own name
        heading = c["water_body_name"]
        if c.get("spot_name"):
            heading = f"{c['water_body_name']} — {c['spot_name']}"
        lines.append(f"\n=== {heading} ===")

        if c.get("is_haversine"):
            lines.append(f"Distance: ~{c.get('straight_line_miles', '?')} miles straight-line")
        elif c.get("drive_minutes"):
            lines.append(f"Drive time: {c['drive_minutes']} min")

        cfs = usgs.get("cfs")
        temp = usgs.get("temp_f")
        turb = usgs.get("turbidity_fnu")
        trend = usgs.get("trend")
        if cfs is not None:
            trend_str = f" ({trend})" if trend and trend != "stable" else ""
            lines.append(f"Flow: {cfs:.0f} CFS{trend_str}")
        if temp is not None:
            lines.append(f"Water temp: {temp:.1f}°F")
        if turb is not None:
            lines.append(f"Turbidity: {turb:.0f} FNU")

        current = (nws.get("current") or {})
        if current.get("short_forecast"):
            temp_str = f", {current['temp_f']}°F" if current.get("temp_f") else ""
            lines.append(f"Weather: {current['short_forecast']}{temp_str}")
        wind_mph = current.get("wind_speed_mph")
        if wind_mph is not None and wind_mph > 10:
            lines.append(f"Wind: {wind_mph:.0f} mph")

        aqi = airnow.get("aqi")
        if aqi is not None and aqi > 50:
            category = airnow.get("category") or "Moderate"
            pollutant = airnow.get("pollutant") or ""
            pollutant_str = f", {pollutant}" if pollutant else ""
            lines.append(f"Air quality: {category} (AQI {aqi}{pollutant_str})")

        wta_reports = wta.get("reports") or []
        if wta_reports:
            lines.append("Angler reports (WTA):")
            for r in wta_reports[:3]:
                date_str = r.get("note_date") or "unknown date"
                confidence = r.get("confidence", "low")
                text = (r.get("report_text") or "")[:200]
                lines.append(f"  [{date_str}] ({confidence} confidence) {text}")

        # Surface penalty warnings so the LLM can advise the angler
        for w in (c.get("warnings") or []):
            lines.append(f"Advisory: {w}")

    return "\n".join(lines)


def _format_notes_block(notes: list[dict], maps: list[dict]) -> str:
    if not notes and not maps:
        return ""
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
# POST_TRIP debrief conversation context (§13.6)
# ---------------------------------------------------------------------------

async def _build_debrief_context(
    user: User,
    trip: Trip,
    conversation: Conversation,
    query: str,
    db,
) -> BuildResult:
    """Minimal context for debrief conversations."""
    trip_context_lines: list[str] = []
    if trip.fishing_spot_id:
        fs_result = await db.execute(
            select(FishingSpot, WaterBody)
            .join(WaterBody, FishingSpot.water_body_id == WaterBody.id)
            .where(FishingSpot.id == trip.fishing_spot_id)
        )
        row = fs_result.one_or_none()
        if row:
            fs, wb = row
            display = wb.name if not fs.name else f"{wb.name} — {fs.name}"
            trip_context_lines.append(f"PLANNED SPOT: {display}")
    elif conversation.session_candidates:
        candidates = (conversation.session_candidates or {}).get("candidates", [])
        if candidates:
            trip_context_lines.append(f"PLANNED SPOT: {candidates[0].get('water_body_name', 'unknown')}")

    if trip.trip_date:
        trip_context_lines.append(f"TRIP DATE: {trip.trip_date}")
    if trip.departure_time:
        trip_context_lines.append(f"DEPARTURE: {trip.departure_time.strftime('%Y-%m-%d %H:%M UTC')}")

    trip_context = "\n".join(trip_context_lines)

    msg_result = await db.execute(
        select(Message.role, Message.content)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at)
    )
    prior_messages = [{"role": r.role, "content": r.content} for r in msg_result.all()]

    system_content = "\n\n".join(filter(None, [
        DEBRIEF_CONVERSATION_PROMPT.strip(),
        trip_context,
    ]))

    messages = [{"role": "system", "content": system_content}]
    for m in prior_messages:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": query})

    return BuildResult(
        messages=messages,
        session_candidates=conversation.session_candidates or {},
        conditions_hash=None,
        drive_time_unavailable=False,
        cached_response=None,
    )


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
    if trip.state == "POST_TRIP":
        return await _build_debrief_context(user, trip, conversation, query, db)

    intake = trip.session_intake or {}
    prefs = user.preferences or {}

    water_types: list[str] = intake.get("water_type") or []
    target_species: list[str] = intake.get("target_species") or []
    trip_goal: str = intake.get("trip_goal") or "maximize_catch"
    max_drive_minutes = int(intake.get("max_drive_minutes") or _DEFAULT_MAX_DRIVE_MINUTES)
    departure_time = trip.departure_time or datetime.now(tz=timezone.utc)

    departure_location = intake.get("departure_location") or prefs.get("home_location") or {}
    if isinstance(departure_location, str):
        departure_location = {"label": departure_location, "lat": None, "lon": None}
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
        # JOIN fishing_spots → water_bodies; only spots with coords
        fs_wb_result = await db.execute(
            select(FishingSpot, WaterBody)
            .join(WaterBody, FishingSpot.water_body_id == WaterBody.id)
            .where(WaterBody.fly_fishing_legal.is_(True))
        )
        all_pairs: list[tuple] = fs_wb_result.all()  # [(FishingSpot, WaterBody)]

        # Water type filter
        pairs = [
            (fs, wb) for fs, wb in all_pairs
            if _matches_water_type(wb, water_types)
        ]

        # Rough geo pre-filter before calling HERE
        if origin_lat and origin_lon:
            pairs = [
                (fs, wb) for fs, wb in pairs
                if haversine_km(
                    float(fs.latitude), float(fs.longitude),
                    origin_lat, origin_lon,
                ) <= _PREFILTER_KM
            ]

        fishing_spots = [fs for fs, _ in pairs]
        water_bodies = [wb for _, wb in pairs]
        water_body_ids = [wb.id for wb in water_bodies]

        # Real-time conditions fetch
        await _fetch_and_cache_realtime(fishing_spots, water_bodies)

        # Active emergency closures
        today_date = date.today()
        closure_result = await db.execute(
            select(EmergencyClosure).where(
                (EmergencyClosure.effective <= today_date) | EmergencyClosure.effective.is_(None),
                (EmergencyClosure.expires >= today_date) | EmergencyClosure.expires.is_(None),
            )
        )
        active_closures: list = closure_result.scalars().all()

        # InciWeb active WA fires
        inciweb_result = await db.execute(
            select(ConditionsCache.data)
            .where(ConditionsCache.source == "inciweb")
            .where(ConditionsCache.water_body_id.is_(None))
            .order_by(ConditionsCache.fetched_at.desc())
            .limit(1)
        )
        inciweb_row = inciweb_result.scalar_one_or_none()
        active_fires: list[dict] = (inciweb_row or {}).get("active_wa_fires", [])

        # Conditions cache keyed by (water_body_id, source)
        cond_result = await db.execute(
            select(ConditionsCache).where(ConditionsCache.water_body_id.in_(water_body_ids))
        )
        cond_by: dict[tuple, dict] = {}
        for c in cond_result.scalars().all():
            cond_by[(str(c.water_body_id), c.source)] = c.data

        # Apply hard filters — only permit, closure, and out-of-range CFS
        # (wildfire, alpine, AQI, turbidity, temp are now soft penalties)
        filtered_pairs: list[tuple] = []
        for fs, wb in pairs:
            wb_id = str(wb.id)
            if wb.permit_required:
                continue
            if _has_active_closure(wb.name, active_closures):
                continue
            if _cfs_out_of_range(wb, cond_by.get((wb_id, "usgs"))):
                continue
            filtered_pairs.append((fs, wb))

        # Drive-time filter — parallel HERE calls
        drive_time_unavailable = False
        candidates_raw: list[dict] = []

        if origin_lat and origin_lon and filtered_pairs:
            tasks = [
                get_drive_time(
                    origin_lat, origin_lon,
                    float(fs.latitude), float(fs.longitude),
                    departure_time,
                )
                for fs, _ in filtered_pairs
            ]
            drive_results = await asyncio.gather(*tasks)

            for (fs, wb), (drive_min, is_fallback) in zip(filtered_pairs, drive_results):
                if is_fallback:
                    drive_time_unavailable = True
                if drive_min > max_drive_minutes:
                    continue
                wb_id = str(wb.id)
                candidates_raw.append({
                    "fishing_spot": fs,
                    "water_body": wb,
                    "drive_minutes": drive_min,
                    "is_haversine": is_fallback,
                    "straight_line_miles": (
                        haversine_miles(origin_lat, origin_lon, float(fs.latitude), float(fs.longitude))
                        if is_fallback else None
                    ),
                    "usgs": cond_by.get((wb_id, "usgs")),
                    "nws": cond_by.get((wb_id, "noaa_nws")),
                    "nwrfc": cond_by.get((wb_id, "noaa_nwrfc")),
                    "wta": cond_by.get((wb_id, "wta")),
                    "airnow": cond_by.get((wb_id, "airnow")),
                    "snotel": cond_by.get((wb_id, "snotel")),
                })
        else:
            for fs, wb in filtered_pairs:
                wb_id = str(wb.id)
                candidates_raw.append({
                    "fishing_spot": fs,
                    "water_body": wb,
                    "drive_minutes": None,
                    "is_haversine": False,
                    "straight_line_miles": None,
                    "usgs": cond_by.get((wb_id, "usgs")),
                    "nws": cond_by.get((wb_id, "noaa_nws")),
                    "nwrfc": cond_by.get((wb_id, "noaa_nwrfc")),
                    "wta": cond_by.get((wb_id, "wta")),
                    "airnow": cond_by.get((wb_id, "airnow")),
                    "snotel": cond_by.get((wb_id, "snotel")),
                })

        # --------------------------------------------------------------
        # [2] Tier 2 volatile delta → session_score
        # --------------------------------------------------------------
        for c in candidates_raw:
            wb = c["water_body"]
            fs = c["fishing_spot"]
            delta, warnings = _compute_volatile_delta(
                wb, c["usgs"], c["nws"], c.get("nwrfc"),
                target_species, departure_time, c.get("airnow"),
                snotel_data=c.get("snotel"),
                active_fires=active_fires,
                spot_lat=float(fs.latitude) if fs.latitude is not None else None,
                spot_lon=float(fs.longitude) if fs.longitude is not None else None,
            )
            c["warnings"] = warnings
            base = float(wb.score or 0) + delta
            if trip_goal == "explore" and fs.last_visited is None:
                base += 2.0
            c["session_score"] = base

        candidates_raw.sort(key=lambda c: c["session_score"], reverse=True)
        candidates_raw = candidates_raw[:_MAX_CANDIDATES]

        # Serialise to JSONB-safe dicts
        candidates = [
            {
                "spot_id": str(c["fishing_spot"].id),      # backward compat for frontend
                "fishing_spot_id": str(c["fishing_spot"].id),
                "water_body_id": str(c["water_body"].id),
                "spot_name": c["fishing_spot"].name,        # None for single-spot waters
                "water_body_name": c["water_body"].name,
                "spot_type": c["water_body"].type,
                "session_score": round(c["session_score"], 4),
                "drive_minutes": c["drive_minutes"],
                "is_haversine": c["is_haversine"],
                "straight_line_miles": c["straight_line_miles"],
                "last_visited": (
                    c["fishing_spot"].last_visited.isoformat()
                    if c["fishing_spot"].last_visited else None
                ),
                "warnings": c.get("warnings") or [],
                "conditions": {
                    "usgs": c["usgs"],
                    "noaa_nws": c["nws"],
                    "noaa_nwrfc": c.get("nwrfc"),
                    "wta": c.get("wta"),
                    "airnow": c.get("airnow"),
                    "snotel": c.get("snotel"),
                },
            }
            for c in candidates_raw
        ]

        # Backfill spot_name with water_body_name for display when spot has no own name
        for c in candidates:
            if not c["spot_name"]:
                c["spot_name"] = c["water_body_name"]

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
            cached_response = await get_cached_response(db, top["fishing_spot_id"], conditions_hash)

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
    top_fishing_spot_ids = [c["fishing_spot_id"] for c in candidates[:_SURFACE_TOP_N]]
    maps = await _fetch_maps(db, top_fishing_spot_ids)

    # ------------------------------------------------------------------
    # [7] Context assembly
    # ------------------------------------------------------------------
    history_result = await db.execute(
        select(Trip.trip_date, WaterBody.name)
        .join(FishingSpot, Trip.fishing_spot_id == FishingSpot.id)
        .join(WaterBody, FishingSpot.water_body_id == WaterBody.id)
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
            f"MAP_ID:{m['id']}:SPOT:{m['fishing_spot_id']}" for m in maps
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
