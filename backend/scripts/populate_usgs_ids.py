"""
Populate usgs_site_ids on river/creek spots and backfill any CFS thresholds
that were missed because the spot was seeded after seed_spots.py ran.

Sets has_realtime_conditions=True on every spot that receives a USGS gauge ID.

USGS site IDs sourced from two tiers:
  Tier A — validated against NOAA NWPS API 2026-04-10 (see noaa_nwrfc.py).
  Tier B — well-known USGS gauge IDs from USGS Water Resources; spot-checked
            against https://waterservices.usgs.gov/nwis/iv/?sites={id}&format=json.

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
    "yakima":        ["12505000"],   # Tier A  — PARW1, near Parker

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

    # --- Methow drainage (Okanogan) ---
    "methow":        ["12449950"],   # Tier A  — PATW1, near Pateros

    # --- Stillaguamish (Snohomish) ---
    # NWPS LID ARLW1 tracks USGS 12167400 (at Arlington).
    # Using 12167400 so NWRFC forecast coverage works for both main stem
    # and North Fork Stillaguamish spots (NF joins above Arlington).
    "stillaguamish": ["12167400"],   # Tier A* — ARLW1, at Arlington

    # --- Sol Duc (Clallam) ---
    # No NWPS LID — NOAA does not forecast the Sol Duc. USGS real-time only.
    "sol duc":       ["12044900"],   # Tier B  — near Forks, WA
    "solduc":        ["12044900"],   # Tier B  — same gauge, alt name match

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
    # No NWPS LID for Kalama River (KLMW1 is the Columbia at Kalama — different).
    # USGS real-time only.
    "kalama":        ["14221500"],   # Tier B  — near Kalama, WA

    # --- Naches (Yakima) ---
    # NWPS LID NACW1 tracks USGS 12494000 (near Naches).
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
