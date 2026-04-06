"""
Tier 1 nightly base scorer — §7.2, §7.3, §7.4.

compute_score() is the public entry point. Called by the nightly APScheduler
job for all spots and by spot-specific re-score after a debrief is filed.

Score is written to spots.score and spots.score_updated.
session_score (Tier 2 volatile overlay, §7.5) is computed at session-open
time in context_builder.py — it is never written back to the spots table.

Input data is passed as plain dicts/lists so the scorer remains a pure
function that is easy to unit test without a database connection.

seed_confidence multiplier: confirmed=1.0, probable=0.6, unvalidated=0.2
"""

import logging
from datetime import date, datetime, timezone
from typing import Literal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIDENCE_MULTIPLIER: dict[str, float] = {
    "confirmed": 1.0,
    "probable": 0.6,
    "unvalidated": 0.2,
}

# River/creek Tier 1 signal weights (normal mode, with debriefs)
_RIVER_WEIGHTS = {
    "debrief_rating": 3.0,
    "note_sentiment": 2.0,
    "flow_trend": 2.0,
    "conditions_reliability": 2.0,
    "species_match": 2.0,
    "data_coverage": 0.5,
}

# Zero-debrief fallback redistributes debrief_rating weight (§7.2)
_RIVER_WEIGHTS_ZERO_DEBRIEF = {
    "debrief_rating": 0.0,
    "note_sentiment": 3.0,
    "flow_trend": 2.0,
    "conditions_reliability": 3.0,
    "species_match": 2.0,
    "data_coverage": 1.0,
}

# Lake Tier 1 signal weights (normal mode)
_LAKE_WEIGHTS = {
    "stocking_recency": 3.0,
    "debrief_rating": 3.0,
    "note_sentiment": 2.0,
    "seasonal_access": 2.0,
    "species_match": 2.0,
    "data_coverage": 0.5,
}

# Lake zero-debrief fallback
_LAKE_WEIGHTS_ZERO_DEBRIEF = {
    "stocking_recency": 3.0,
    "debrief_rating": 0.0,
    "note_sentiment": 3.0,
    "seasonal_access": 2.0,
    "species_match": 2.0,
    "data_coverage": 1.0,
}

# Recency penalty: -0.5 per 14-day period, capped at -3.0
_RECENCY_PENALTY_PER_PERIOD = -0.5
_RECENCY_PERIOD_DAYS = 14
_RECENCY_PENALTY_CAP = -3.0


# ---------------------------------------------------------------------------
# Signal normalisers
# ---------------------------------------------------------------------------

def _norm_debrief_rating(avg_rating: float | None) -> float:
    """(rating − 1) / 4 → [0.0, 1.0]. Returns 0.0 when no debriefs."""
    if avg_rating is None:
        return 0.0
    return max(0.0, min(1.0, (avg_rating - 1) / 4))


def _norm_note_sentiment(positive: int, neutral: int, negative: int) -> float:
    """Weighted average: positive=1.0, neutral=0.5, negative=0.0."""
    total = positive + neutral + negative
    if total == 0:
        return 0.5  # neutral default when no notes
    return (positive * 1.0 + neutral * 0.5 + negative * 0.0) / total


def _norm_flow_trend(trend: Literal["dropping", "stable", "rising"] | None) -> float:
    """dropping=1.0, stable=0.5, rising=0.0. None → 0.5."""
    return {"dropping": 1.0, "stable": 0.5, "rising": 0.0}.get(trend or "stable", 0.5)


def _norm_conditions_reliability(fraction: float | None) -> float:
    """Fraction of time CFS in fishable range → [0.0, 1.0]."""
    if fraction is None:
        return 0.0
    return max(0.0, min(1.0, fraction))


def _norm_species_match(match: Literal["matched", "partial", "none"] | None) -> float:
    return {"matched": 1.0, "partial": 0.5, "none": 0.0}.get(match or "none", 0.0)


def _norm_stocking_recency(days_since_stocked: int | None) -> float:
    """Step function per §7.4: ≤30=1.0, 31-60=0.75, 61-90=0.5, 91-180=0.25, >180=0.0."""
    if days_since_stocked is None:
        return 0.0
    if days_since_stocked <= 30:
        return 1.0
    if days_since_stocked <= 60:
        return 0.75
    if days_since_stocked <= 90:
        return 0.5
    if days_since_stocked <= 180:
        return 0.25
    return 0.0


def _norm_seasonal_access(snowpack_pct_of_median: float | None, is_alpine: bool) -> float:
    """
    For non-alpine lakes returns 0.0 (CFS/flow not applicable; weight=0 in non-alpine).
    For alpine: snowpack % of median → access estimate.
    1.0 = fully open (low snowpack), 0.5 = uncertain, 0.0 = ice-on / closed.
    """
    if not is_alpine:
        return 0.0
    if snowpack_pct_of_median is None:
        return 0.5  # uncertain when no SNOTEL data
    if snowpack_pct_of_median <= 50:
        return 1.0
    if snowpack_pct_of_median <= 100:
        return 0.5
    return 0.0


def _norm_data_coverage(populated_sources: int, total_sources: int) -> float:
    """Fraction of data source columns that are non-null."""
    if total_sources == 0:
        return 0.0
    return max(0.0, min(1.0, populated_sources / total_sources))


def _recency_penalty(last_visited: date | None) -> float:
    """−0.5 per 14 days since last visit; cap at −3.0. 0.0 if never visited."""
    if last_visited is None:
        return 0.0
    today = datetime.now(tz=timezone.utc).date()
    days = (today - last_visited).days
    if days <= 0:
        return 0.0
    periods = days / _RECENCY_PERIOD_DAYS
    return max(_RECENCY_PENALTY_CAP, _RECENCY_PENALTY_PER_PERIOD * periods)


def _weighted_sum(signals: dict[str, float], weights: dict[str, float]) -> float:
    """Dot product of signal values and weights."""
    return sum(signals.get(k, 0.0) * w for k, w in weights.items())


# ---------------------------------------------------------------------------
# CFS similarity — conditions-conditioned debrief weighting (§7.2)
# ---------------------------------------------------------------------------

def cfs_similarity(snap_cfs: float | None, current_cfs: float | None) -> float:
    """
    1 / (1 + |snap_cfs − current_cfs| / current_cfs)

    Returns 0.0 when either value is None or current_cfs is zero.
    Used to weight individual debrief ratings by how similar flow conditions
    were at the time of the debrief to current conditions.
    """
    if snap_cfs is None or current_cfs is None or current_cfs == 0:
        return 0.0
    return 1.0 / (1.0 + abs(snap_cfs - current_cfs) / current_cfs)


def weighted_debrief_rating(
    debriefs: list[dict],  # each: {rating: float, snap_cfs: float | None}
    current_cfs: float | None,
) -> float | None:
    """
    Conditions-conditioned average debrief rating.

    If current_cfs is None, falls back to simple average.
    Returns None when debriefs list is empty.
    """
    if not debriefs:
        return None

    if current_cfs is None:
        ratings = [d["rating"] for d in debriefs if d.get("rating") is not None]
        return sum(ratings) / len(ratings) if ratings else None

    weights = []
    weighted = []
    for d in debriefs:
        r = d.get("rating")
        if r is None:
            continue
        w = cfs_similarity(d.get("snap_cfs"), current_cfs)
        weights.append(w)
        weighted.append(r * w)

    total_weight = sum(weights)
    if total_weight == 0:
        # All similarities zero — fall back to simple average
        ratings = [d["rating"] for d in debriefs if d.get("rating") is not None]
        return sum(ratings) / len(ratings) if ratings else None

    return sum(weighted) / total_weight


# ---------------------------------------------------------------------------
# Tier 1 scoring — rivers and creeks (§7.3)
# ---------------------------------------------------------------------------

def score_river(
    *,
    seed_confidence: str,
    debriefs: list[dict],        # [{rating, snap_cfs}]
    current_cfs: float | None,
    flow_trend: Literal["dropping", "stable", "rising"] | None,
    note_positive: int = 0,
    note_neutral: int = 0,
    note_negative: int = 0,
    conditions_reliability: float | None = None,
    species_match: Literal["matched", "partial", "none"] | None = None,
    populated_sources: int = 0,
    total_sources: int = 6,
    last_visited: date | None = None,
) -> float:
    """
    Compute Tier 1 base score for a river or creek spot.

    Returns a float score. The seed_confidence multiplier is applied to the
    data_coverage signal only — it does not scale the entire score.
    """
    confidence_mult = _CONFIDENCE_MULTIPLIER.get(seed_confidence, 0.2)
    debrief_count = len(debriefs)
    weights = _RIVER_WEIGHTS if debrief_count > 0 else _RIVER_WEIGHTS_ZERO_DEBRIEF

    avg_rating = weighted_debrief_rating(debriefs, current_cfs)

    signals = {
        "debrief_rating": _norm_debrief_rating(avg_rating),
        "note_sentiment": _norm_note_sentiment(note_positive, note_neutral, note_negative),
        "flow_trend": _norm_flow_trend(flow_trend),
        "conditions_reliability": _norm_conditions_reliability(conditions_reliability),
        "species_match": _norm_species_match(species_match),
        "data_coverage": _norm_data_coverage(populated_sources, total_sources) * confidence_mult,
    }

    base = _weighted_sum(signals, weights)
    penalty = _recency_penalty(last_visited)
    return round(base + penalty, 4)


# ---------------------------------------------------------------------------
# Tier 1 scoring — lakes (§7.4)
# ---------------------------------------------------------------------------

def score_lake(
    *,
    seed_confidence: str,
    debriefs: list[dict],        # [{rating, snap_cfs}]
    current_cfs: float | None = None,
    days_since_stocked: int | None = None,
    note_positive: int = 0,
    note_neutral: int = 0,
    note_negative: int = 0,
    is_alpine: bool = False,
    snowpack_pct_of_median: float | None = None,
    species_match: Literal["matched", "partial", "none"] | None = None,
    populated_sources: int = 0,
    total_sources: int = 6,
    last_visited: date | None = None,
) -> float:
    """
    Compute Tier 1 base score for a lake spot.

    Lakes do not use flow_trend or conditions_reliability. Stocking recency
    replaces those signals. Alpine lakes additionally use seasonal_access.
    """
    confidence_mult = _CONFIDENCE_MULTIPLIER.get(seed_confidence, 0.2)
    debrief_count = len(debriefs)
    weights = _LAKE_WEIGHTS if debrief_count > 0 else _LAKE_WEIGHTS_ZERO_DEBRIEF

    avg_rating = weighted_debrief_rating(debriefs, current_cfs)

    signals = {
        "stocking_recency": _norm_stocking_recency(days_since_stocked),
        "debrief_rating": _norm_debrief_rating(avg_rating),
        "note_sentiment": _norm_note_sentiment(note_positive, note_neutral, note_negative),
        "seasonal_access": _norm_seasonal_access(snowpack_pct_of_median, is_alpine),
        "species_match": _norm_species_match(species_match),
        "data_coverage": _norm_data_coverage(populated_sources, total_sources) * confidence_mult,
    }

    base = _weighted_sum(signals, weights)
    penalty = _recency_penalty(last_visited)
    return round(base + penalty, 4)


# ---------------------------------------------------------------------------
# DB-level entry point — called by scheduler and post-debrief re-score
# ---------------------------------------------------------------------------

async def compute_and_store_score(spot_id: str, db) -> float:
    """
    Fetch spot + related data from DB, compute Tier 1 score, write back.

    Returns the computed score.
    """
    from datetime import datetime, timezone
    from sqlalchemy import func, select, text
    from db.models import Note, Session, Spot, Trip

    result = await db.execute(select(Spot).where(Spot.id == spot_id))
    spot = result.scalar_one_or_none()
    if not spot:
        log.warning("score_spot_not_found", extra={"spot_id": spot_id})
        return 0.0

    # Note sentiment counts
    note_result = await db.execute(
        select(
            func.count().filter(Note.outcome == "positive").label("pos"),
            func.count().filter(Note.outcome == "neutral").label("neu"),
            func.count().filter(Note.outcome == "negative").label("neg"),
        ).where(Note.spot_id == spot.id)
    )
    sentiment_row = note_result.one()

    # Debriefs (trips with a debrief_note_id → they have a filed debrief)
    debrief_result = await db.execute(
        select(Trip.session_intake, Trip.conditions_snapshot)
        .where(Trip.spot_id == spot.id)
        .where(Trip.debrief_note_id.is_not(None))
    )
    debrief_rows = debrief_result.all()

    debriefs = []
    for row in debrief_rows:
        intake = row.session_intake or {}
        snapshot = row.conditions_snapshot or {}
        rating = intake.get("overall_rating")
        if rating is not None:
            debriefs.append({
                "rating": float(rating),
                "snap_cfs": snapshot.get("cfs"),
            })

    # Data source coverage — count non-null source columns
    source_fields = [
        spot.usgs_site_ids,
        spot.noaa_station_id,
        spot.snotel_station_id,
        spot.wdfw_water_id,
        spot.wta_trail_url,
        spot.fishing_regs,
    ]
    populated = sum(1 for f in source_fields if f is not None)
    total = len(source_fields)

    # Stocking recency for lakes
    days_since_stocked = None
    if spot.last_stocked_date:
        days_since_stocked = (datetime.now(tz=timezone.utc).date() - spot.last_stocked_date).days

    if spot.type in ("river", "creek", "coastal"):
        computed = score_river(
            seed_confidence=spot.seed_confidence or "unvalidated",
            debriefs=debriefs,
            current_cfs=None,     # nightly job does not apply real-time CFS
            flow_trend=None,
            note_positive=sentiment_row.pos,
            note_neutral=sentiment_row.neu,
            note_negative=sentiment_row.neg,
            conditions_reliability=None,
            species_match=None,
            populated_sources=populated,
            total_sources=total,
            last_visited=spot.last_visited,
        )
    else:
        computed = score_lake(
            seed_confidence=spot.seed_confidence or "unvalidated",
            debriefs=debriefs,
            days_since_stocked=days_since_stocked,
            note_positive=sentiment_row.pos,
            note_neutral=sentiment_row.neu,
            note_negative=sentiment_row.neg,
            is_alpine=spot.is_alpine or False,
            snowpack_pct_of_median=None,  # fetched from SNOTEL at session-open for Tier 2
            species_match=None,
            populated_sources=populated,
            total_sources=total,
            last_visited=spot.last_visited,
        )

    spot.score = computed
    spot.score_updated = datetime.now(tz=timezone.utc)
    await db.commit()

    log.info("score_updated", extra={"spot_id": spot_id, "score": computed})
    return computed
