"""
Populate usgs_site_ids on river/creek spots and backfill any CFS thresholds
that were missed because the spot was seeded after seed_spots.py ran.

Sets has_realtime_conditions=True on every spot that receives a USGS gauge ID.

USGS site IDs sourced from two tiers:
  Tier A — validated against NOAA NWPS API 2026-04-10 (see noaa_nwrfc.py).
  Tier B — well-known USGS gauge IDs from USGS Water Resources; spot-checked
            against https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous/items?monitoring_location_id=USGS-{id}
            (legacy waterservices.usgs.gov until 2026-10-11).

Name matching is case-insensitive substring — e.g. "skagit" matches both
"SKAGIT R  03.0176" (WDFW stocking name) and any future curated "Skagit River" entry.

Run from backend/ directory:
  sudo /opt/flyfish/venv/bin/python -m scripts.populate_usgs_ids
  sudo /opt/flyfish/venv/bin/python -m scripts.populate_usgs_ids --dry-run
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# River → primary USGS gauge site ID(s)
#
# Key: lowercase name fragment that must appear in spot.name.lower()
# Value: list of USGS site ID strings (primary gauge first)
#
# Using a single primary gauge per river for simplicity. The NWRFC every-2h
# job also maps these IDs to NWPS forecast gauges via _USGS_TO_NWRFC_LID.
# ---------------------------------------------------------------------------

_RIVER_USGS_IDS: dict[str, list[str]] = {
    # Tier A = validated via NWRFC PARW1 etc. (see noaa_nwrfc.py _USGS_TO_NWRFC_LID)
    # Tier A* = confirmed via NWPS API 2026-04-13 (has NWRFC LID)
    # Tier B = USGS real-time only; no NWRFC forecast coverage

    # --- Yakima drainage (Kittitas / Yakima) ---
    # 12505000 (Parker) stopped reporting to USGS, and it's below the Sunnyside
    # diversion (392 vs 1,490 cfs at Union Gap on 2026-10-11). Union Gap is at the spot.
    "yakima":        ["12500450"],   # Yakima above Ahtanum Cr at Union Gap (live)

    # --- Snoqualmie (King) ---
    "snoqualmie":    ["12149000"],   # Tier A  — CRNW1, near Carnation

    # --- Skykomish (Snohomish / King) ---
    "skykomish":     ["12134500"],   # Tier A  — GLBW1, near Gold Bar

    # --- Skagit (Skagit) ---
    "skagit":        ["12194000"],   # Tier A  — CONW1, near Concrete

    # --- Sauk (Skagit tributary) ---
    # No NWPS LID — NOAA does not forecast the Sauk. USGS real-time only.
    "sauk":          ["12189500"],   # Tier B  — near Sauk, WA

    # --- Hoh (Clallam / Jefferson) ---
    # No NWPS LID — NOAA does not forecast the Hoh. USGS real-time only.
    "hoh":           ["12041200"],   # Tier B  — near Forks, WA

    # --- Wenatchee / Icicle drainage (Chelan) ---
    "wenatchee":     ["12462500"],   # Tier A  — MONW1, at Monitor
    # The Wenatchee-at-Monitor gauge is 50 km downstream on the mainstem; USGS
    # has no Little Wenatchee gauge. Explicitly none (2026-10-11 audit).
    "little wenatchee": [],

    # --- Methow drainage (Okanogan) ---
    "methow":        ["12449950"],   # Tier A  — PATW1, near Pateros

    # --- Stillaguamish (Snohomish) ---
    # NWPS LID ARLW1 tracks USGS 12167400 (at Arlington).
    # Using 12167400 so NWRFC forecast coverage works for both main stem
    # and North Fork Stillaguamish spots (NF joins above Arlington).
    # 12167400 no longer reports to USGS (2026-10); kept for the NWRFC forecast and
    # the NWPS observed fallback (ARLW1). The NF has its own live gauge.
    "stillaguamish": ["12167400"],   # Tier A* — ARLW1, at Arlington
    "north fork stillaguamish": ["12167000"],   # NF Stillaguamish nr Arlington (live)

    # --- Sol Duc (Clallam) ---
    # 12044900 was the ELWHA above Lake Mills (discontinued 2011). USGS has no
    # active Sol Duc ("Soleduck") gauge: explicitly none (2026-10-11 audit).
    "sol duc":       [],
    "solduc":        [],

    # --- Elwha (Clallam) ---
    "elwha":         ["12045500"],   # Elwha River at McDonald Bridge (live)

    # --- Green River (King) ---
    # Fragment "green river" (two words) avoids matching unrelated "green" spots.
    "green river":   ["12113000"],   # Tier A* — AUBW1, near Auburn

    # --- Tolt River (King) ---
    # NWPS LID TOLW1 tracks USGS 12148500 (near Carnation).
    "tolt":          ["12148500"],   # Tier A* — TOLW1, near Carnation

    # --- Pilchuck River (Snohomish) ---
    # NWPS LID PILW1 tracks USGS 12155300 (near Snohomish, lower river).
    "pilchuck":      ["12155300"],   # Tier A* — PILW1, near Snohomish

    # --- Nisqually (Pierce / Thurston) ---
    # NWPS LID NISW1 tracks USGS 12082500 (near National, upper river).
    "nisqually":     ["12082500"],   # Tier A* — NISW1, near National

    # --- Kalama (Cowlitz) ---
    # 14221500 is CEDAR CREEK near Ariel, not the Kalama. USGS has no active
    # Kalama gauge: explicitly none (2026-10-11 audit).
    "kalama":        [],

    # --- Naches (Yakima) ---
    # NWPS LID NACW1 tracks USGS 12494000 (near Naches). The USGS gauge stopped
    # reporting; flow now comes from the NWPS observed fallback (NACW1).
    "naches":        ["12494000"],   # Tier A* — NACW1, near Naches

    # --- Klickitat (Klickitat) ---
    # NWPS LID PITW1 tracks USGS 14113000 (near Pitt).
    "klickitat":     ["14113000"],   # Tier A* — PITW1, near Pitt

    # --- Queets (Jefferson) ---
    # NWPS LID QUEW1 tracks USGS 12040500 (near Clearwater).
    "queets":        ["12040500"],   # Tier A* — QUEW1, near Clearwater
}

# CFS thresholds for rivers that may have been seeded after seed_spots.py ran.
# These duplicate _RIVER_CFS_THRESHOLDS in seed_spots.py — kept here to backfill
# spots that missed the original threshold application step.
_RIVER_CFS_BACKFILL: dict[str, dict] = {
    "yakima":     {"min_cfs": 700,  "max_cfs": 1500},
    "snoqualmie": {"min_cfs": 300,  "max_cfs": 1500},
    "skykomish":  {"min_cfs": 700,  "max_cfs": 7000},
    "skagit":     {"min_cfs": 2000, "max_cfs": 9000},
    "sauk":       {"min_cfs": 600,  "max_cfs": 3000},
    "hoh":        {"min_cfs": 1200, "max_cfs": 3500},
}


async def main(dry_run: bool) -> None:
    from sqlalchemy import select
    from db.connection import AsyncSessionLocal
    from db.models import Spot

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Spot).where(Spot.type.in_(["river", "creek"]))
        )
        spots: list[Spot] = list(result.scalars().all())

    log.info(f"Found {len(spots)} river/creek spots")

    usgs_updated = 0
    cfs_backfilled = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Spot).where(Spot.type.in_(["river", "creek"]))
        )
        spots = list(result.scalars().all())

        for spot in spots:
            name_lower = spot.name.lower()
            matched_usgs: list[str] | None = None
            matched_key: str | None = None

            # Check each fragment — longer fragments first to prefer more specific matches
            for fragment in sorted(_RIVER_USGS_IDS, key=len, reverse=True):
                if fragment in name_lower:
                    matched_usgs = _RIVER_USGS_IDS[fragment]
                    matched_key = fragment
                    break

            if matched_key is not None and not matched_usgs:
                # Explicit "no gauge": clear a previously wrong mapping.
                if spot.usgs_site_ids:
                    log.info(f"  {'[DRY] ' if dry_run else ''}usgs_site_ids cleared for '{spot.name}' "
                             f"(fragment: '{matched_key}' has no valid gauge)")
                    if not dry_run:
                        spot.usgs_site_ids = []
                        spot.has_realtime_conditions = False
                    usgs_updated += 1
                continue

            if matched_usgs:
                if spot.usgs_site_ids != matched_usgs:
                    log.info(
                        f"  {'[DRY] ' if dry_run else ''}usgs_site_ids → {matched_usgs} "
                        f"for '{spot.name}' (fragment: '{matched_key}')"
                    )
                    if not dry_run:
                        spot.usgs_site_ids = matched_usgs
                        spot.has_realtime_conditions = True
                    usgs_updated += 1
                elif not spot.has_realtime_conditions:
                    if not dry_run:
                        spot.has_realtime_conditions = True

                # Backfill missing CFS thresholds for spots seeded after seed_spots.py ran
                if matched_key in _RIVER_CFS_BACKFILL and spot.min_cfs is None:
                    thresholds = _RIVER_CFS_BACKFILL[matched_key]
                    log.info(
                        f"  {'[DRY] ' if dry_run else ''}cfs_backfill "
                        f"{thresholds['min_cfs']}–{thresholds['max_cfs']} "
                        f"→ '{spot.name}'"
                    )
                    if not dry_run:
                        spot.min_cfs = thresholds["min_cfs"]
                        spot.max_cfs = thresholds["max_cfs"]
                    cfs_backfilled += 1

        if not dry_run:
            await db.commit()

    log.info(
        f"Done. usgs_site_ids set/updated: {usgs_updated}, "
        f"CFS thresholds backfilled: {cfs_backfilled}"
        + (" [DRY RUN — no DB writes]" if dry_run else "")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for line in open("/etc/flyfish/app.env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    asyncio.run(main(dry_run=args.dry_run))
