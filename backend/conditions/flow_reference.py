"""
Judging a flow reading against references for the gauge that took it (TODO-18).

Three references, each tied to one gauge:
  - normals: USGS day-of-year flow percentiles (p5–p95) from the gauge's own
    record — "what's normal here on this date". Every live gauge has them.
  - bands: published fishable ranges (wade / float / general "fish"), stored
    with the gauge their source wrote them for (flow_bands table). A range
    only means something on that gauge: Emerald's Yakima range is for
    Umtanum, not Union Gap, which runs ~2x higher.
  - flood stages: NWS action / minor-flood stage at the gauge (same datum as
    USGS gauge height; checked 2026-10-13).

Pure functions only; the DB reads and HTTP loads live in gauge_reference.py
and the callers.
"""

from __future__ import annotations

from datetime import date, timedelta

PERCENTILES = (5, 10, 25, 50, 75, 90, 95)

# Daily percentiles are noisy (one storm in a 60-year record moves p90 by
# thousands of cfs from one day to the next), so average a ±7-day window.
NORMALS_WINDOW_DAYS = 7

# Scoring when a published band covers the reading (best across wade/float).
BAND_IN_RANGE_BONUS = 1.0
BAND_LOW_PENALTY = -2.0
BAND_HIGH_PENALTY = -2.5
# Scoring from normals alone (no band): only the tails move the score.
VERY_LOW_PCT = 10
VERY_HIGH_PCT = 90
VERY_LOW_PENALTY = -1.0
VERY_HIGH_PENALTY = -1.5
FLOOD_ACTION_PENALTY = -3.0

MODE_LABELS = {"wade": "Wading", "float": "Floating", "fish": "Fishing"}


def time_of_year(d: date) -> str:
    return d.strftime("%m-%d")


def window_keys(target: date, days: int = NORMALS_WINDOW_DAYS) -> list[str]:
    """MM-DD keys for target ± days (wraps the year; Feb 29 included in leap years)."""
    return [time_of_year(target + timedelta(days=i)) for i in range(-days, days + 1)]


def smooth_normals(rows: dict[str, list[float]], target: date) -> dict[int, float] | None:
    """
    Average each percentile over the ±NORMALS_WINDOW_DAYS window around target.
    rows: {"MM-DD": [p5, p10, p25, p50, p75, p90, p95]}. None if no row falls
    in the window.
    """
    picked = [rows[k] for k in window_keys(target) if k in rows]
    if not picked:
        return None
    return {
        p: sum(r[i] for r in picked) / len(picked)
        for i, p in enumerate(PERCENTILES)
    }


def percentile_of(cfs: float, normals: dict[int, float]) -> float:
    """
    Where cfs falls in the date's distribution, 0–100, interpolating linearly
    between published percentiles. Outside p5–p95 it is extrapolated toward
    0 / 100 (below p5: proportional to cfs/p5; above p95: 95 + 5·(1 − p95/cfs)).
    """
    pts = [(p, normals[p]) for p in PERCENTILES]
    lo_p, lo_v = pts[0]
    hi_p, hi_v = pts[-1]
    if cfs <= lo_v:
        return lo_p * (cfs / lo_v) if lo_v > 0 else 0.0
    if cfs >= hi_v:
        return hi_p + (100 - hi_p) * (1 - hi_v / cfs) if cfs > 0 else float(hi_p)
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if v1 <= cfs <= v2:
            if v2 == v1:
                return float(p1)
            return p1 + (p2 - p1) * (cfs - v1) / (v2 - v1)
    return 50.0  # unreachable for monotonic percentiles


def prob_in_range(lo: float | None, hi: float | None, normals: dict[int, float]) -> float:
    """Estimated chance flow is within [lo, hi] on this date (0–1), from percentiles."""
    upper = percentile_of(hi, normals) if hi is not None else 100.0
    lower = percentile_of(lo, normals) if lo is not None else 0.0
    return max(0.0, min(1.0, (upper - lower) / 100))


def ordinal(n: float) -> str:
    n = int(round(n))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def normal_label(pct: float) -> str:
    if pct < VERY_LOW_PCT:
        return "very low"
    if pct < 25:
        return "below normal"
    if pct <= 75:
        return "normal"
    if pct <= VERY_HIGH_PCT:
        return "above normal"
    return "very high"


def band_status(cfs: float, band: dict) -> str:
    """'in' / 'low' / 'high' for one band {min_cfs, max_cfs}."""
    if band.get("min_cfs") is not None and cfs < band["min_cfs"]:
        return "low"
    if band.get("max_cfs") is not None and cfs > band["max_cfs"]:
        return "high"
    return "in"


def _band_distance(cfs: float, band: dict) -> float:
    """Relative distance outside the band (0 when inside)."""
    lo, hi = band.get("min_cfs"), band.get("max_cfs")
    if lo is not None and cfs < lo:
        return (lo - cfs) / lo if lo else 0.0
    if hi is not None and cfs > hi:
        return (cfs - hi) / hi if hi else 0.0
    return 0.0


def _range_text(band: dict) -> str:
    lo, hi = band.get("min_cfs"), band.get("max_cfs")
    if lo is not None and hi is not None:
        return f"{lo:,}–{hi:,} cfs"
    return f"{lo:,}+ cfs" if lo is not None else f"up to {hi:,} cfs"


def flood_status(stage_ft: float | None, stages: dict | None) -> str | None:
    """'flood' at/above NWS minor flood stage, 'action' at/above action stage, else None."""
    if stage_ft is None or not stages:
        return None
    minor, action = stages.get("minor_ft"), stages.get("action_ft")
    if minor is not None and stage_ft >= minor:
        return "flood"
    if action is not None and stage_ft >= action:
        return "action"
    return None


def assess_flow(
    *,
    cfs: float | None,
    normals: dict[int, float] | None,
    target: date,
    bands: list[dict],
    band_cfs: dict[str, float | None],
) -> tuple[float, list[str], list[str]]:
    """
    Score a river's flow against its references. Returns (delta, warnings, context).

    cfs / normals are for the water body's own gauge. bands are its published
    ranges, each with gauge_site_id / gauge_name; band_cfs maps a band gauge
    to its reading (current or forecast), so a band is checked against the
    gauge it was written for. A band whose gauge has no reading is skipped.

    Scoring uses the BEST band (wade vs float is the angler's call after the
    recommendation, so it never costs a spot points); every band is listed in
    context so the model can say "good for wading, too low to float". Without
    a usable band, normals alone score the tails (very low / very high for
    the date). context lines are for the model; warnings are also advisories.
    """
    delta = 0.0
    warnings: list[str] = []
    context: list[str] = []
    when = f"{target:%b} {target.day}"

    if cfs is not None and normals:
        pct = percentile_of(cfs, normals)
        label = normal_label(pct)
        context.append(
            f"Flow vs. this gauge's history: {label} for {when} "
            f"(~{ordinal(pct)} percentile; typical range {normals[25]:,.0f}–{normals[75]:,.0f} cfs)"
        )
    else:
        pct = label = None

    checked = []
    for band in bands:
        reading = band_cfs.get(band["gauge_site_id"])
        if reading is None:
            continue
        checked.append((band, reading, band_status(reading, band)))

    if checked:
        source = checked[0][0].get("source") or "published range"
        by_gauge: dict[str, list] = {}
        for band, reading, status in checked:
            by_gauge.setdefault(band["gauge_site_id"], []).append((band, reading, status))
        for items in by_gauge.values():
            gauge_name = items[0][0].get("gauge_name") or items[0][0]["gauge_site_id"]
            parts = []
            for band, _, status in items:
                state = {"in": "in range", "low": "below range", "high": "above range"}[status]
                parts.append(f"{MODE_LABELS.get(band['mode'], band['mode'])} {_range_text(band)}: {state}")
            context.append(f"{source} at {gauge_name} ({items[0][1]:,.0f} cfs): " + "; ".join(parts))

        best = min(checked, key=lambda t: _band_distance(t[1], t[0]))
        band, reading, status = best
        if status == "in":
            delta += BAND_IN_RANGE_BONUS
        elif status == "low":
            delta += BAND_LOW_PENALTY
            warnings.append(
                f"Low flow: {reading:,.0f} cfs at {band.get('gauge_name') or band['gauge_site_id']}, "
                f"below the published fishable range ({_range_text(band)})"
            )
        else:
            delta += BAND_HIGH_PENALTY
            warnings.append(
                f"High flow: {reading:,.0f} cfs at {band.get('gauge_name') or band['gauge_site_id']}, "
                f"above the published fishable range ({_range_text(band)}) — likely blown out"
            )
    elif pct is not None:
        if pct < VERY_LOW_PCT:
            delta += VERY_LOW_PENALTY
            warnings.append(f"Very low flow for {when}: {cfs:,.0f} cfs (~{ordinal(pct)} percentile at this gauge)")
        elif pct > VERY_HIGH_PCT:
            delta += VERY_HIGH_PENALTY
            warnings.append(
                f"Very high flow for {when}: {cfs:,.0f} cfs (~{ordinal(pct)} percentile at this gauge) — "
                "likely high and off-color"
            )

    return delta, warnings, context


def best_band_reliability(bands: list[dict], normals_by_site: dict[str, dict[int, float]]) -> float | None:
    """
    Nightly conditions_reliability: the best band's estimated chance of being
    in range on this date, from its own gauge's normals. None when no band
    has normals.
    """
    probs = [
        prob_in_range(b.get("min_cfs"), b.get("max_cfs"), normals_by_site[b["gauge_site_id"]])
        for b in bands
        if normals_by_site.get(b["gauge_site_id"])
    ]
    return max(probs) if probs else None
