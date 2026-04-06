"""
One-time spot seeding script — Phase 3.

Seeds the spots table from three sources:
  1. WDFW Fish Plants (Socrata 6fex-3r7d) → seed_confidence='confirmed'
     All stocked waters — publicly accessible, legally open, holds fish.
  2. Curated spot list → seed_confidence='probable'
     Well-known WA fly fishing destinations with wild fish populations:
     Methow/Wenatchee/Icicle drainages, Olympic Peninsula steelhead rivers,
     Puget Sound lowland rivers, fly-fishing-only Columbia Basin lakes
     (Chopaka, Lenice, Nunnally, Dry Falls, Lake Lenore, Pass Lake, etc.).
     Ensures the recommender can surface new spots the group hasn't fished.
  3. §7.7 baseline fishability thresholds applied to all spots by name match.

WA Public Fishing Access Sites are not available as a Socrata dataset.
The curated list covers the important non-stocked fly fishing waters.
Add new spots to _CURATED_SPOTS as the group discovers them.

Usage (run from backend/ directory):
  python -m scripts.seed_spots
  python -m scripts.seed_spots --dry-run   # print counts, no DB writes

Run AFTER: alembic upgrade head
Run BEFORE: scripts/embed_spots.py (generates name_embedding for seeded spots)
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import httpx

# Allow running as a module from the backend directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------

# Dataset: WDFW Fish Plants — https://data.wa.gov/Natural-Resources-Environment/WDFW-Fish-Plants/6fex-3r7d
_STOCKING_SOCRATA_URL = "https://data.wa.gov/resource/6fex-3r7d.json"
_PAGE_SIZE = 1000
_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=5.0, pool=5.0)


# ---------------------------------------------------------------------------
# §7.7 Baseline fishability thresholds — hardcoded from spec
# River CFS ranges: Emerald Water Anglers (Seattle)
# Temperature limits: Keep Fish Wet 2024 meta-analysis
# NOTE: Spots for rivers/lakes not listed here will have null min_cfs/max_cfs.
#       Research and add thresholds manually for any new spots outside this set.
# ---------------------------------------------------------------------------

# {spot_name_fragment_lower: {min_cfs, max_cfs}}
_RIVER_CFS_THRESHOLDS: dict[str, dict] = {
    "yakima":      {"min_cfs": 700,  "max_cfs": 1500},
    "snoqualmie":  {"min_cfs": 300,  "max_cfs": 1500},
    "skykomish":   {"min_cfs": 700,  "max_cfs": 7000},
    "skagit":      {"min_cfs": 2000, "max_cfs": 9000},
    "sauk":        {"min_cfs": 600,  "max_cfs": 3000},
    "hoh":         {"min_cfs": 1200, "max_cfs": 3500},
}

# {species_lower: max_temp_f}
_SPECIES_MAX_TEMP_F: dict[str, float] = {
    "steelhead":       61.0,
    "rainbow trout":   61.0,
    "cutthroat trout": 61.0,
    "brown trout":     66.0,
    "bull trout":      54.0,
}

_COLDWATER_MIN_TEMP_F = 40.0


# ---------------------------------------------------------------------------
# Curated spot list — probable-tier waters not covered by stocking data
#
# These are well-known WA fly fishing destinations with wild fish populations,
# fly-fishing-only lakes, and under-the-radar options the group may not have
# fished yet. seed_confidence='probable', source='curated'.
#
# min_cfs / max_cfs are left null for rivers not in §7.7 — research and add
# manually per spot as the group fishes them (see memory: fishability thresholds).
# is_alpine=True flags lakes above ~3000ft that use the SNOTEL access model.
# ---------------------------------------------------------------------------

_CURATED_SPOTS: list[dict] = [

    # --- Methow Valley (Okanogan) — remote, lightly pressured ---
    {"name": "Methow River",       "type": "river",  "county": "Okanogan",  "species_primary": ["Steelhead", "Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Twisp River",        "type": "river",  "county": "Okanogan",  "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Chewuch River",      "type": "river",  "county": "Okanogan",  "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Early Winters Creek","type": "creek",  "county": "Okanogan",  "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Lost River",         "type": "river",  "county": "Okanogan",  "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},

    # --- Wenatchee / Icicle drainage (Chelan) ---
    {"name": "Wenatchee River",    "type": "river",  "county": "Chelan",    "species_primary": ["Steelhead", "Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Icicle Creek",       "type": "creek",  "county": "Chelan",    "species_primary": ["Rainbow Trout", "Cutthroat Trout", "Bull Trout"]},
    {"name": "Peshastin Creek",    "type": "creek",  "county": "Chelan",    "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Entiat River",       "type": "river",  "county": "Chelan",    "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Little Wenatchee River", "type": "river", "county": "Chelan", "species_primary": ["Cutthroat Trout"]},

    # --- Yakima tributaries (Kittitas / Yakima) ---
    {"name": "Teanaway River",     "type": "river",  "county": "Kittitas",  "species_primary": ["Cutthroat Trout", "Rainbow Trout"]},
    {"name": "Cle Elum River",     "type": "river",  "county": "Kittitas",  "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Naches River",       "type": "river",  "county": "Yakima",    "species_primary": ["Rainbow Trout"]},
    {"name": "American River",     "type": "river",  "county": "Yakima",    "species_primary": ["Cutthroat Trout"]},
    {"name": "Bumping River",      "type": "river",  "county": "Yakima",    "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Tieton River",       "type": "river",  "county": "Yakima",    "species_primary": ["Rainbow Trout"]},

    # --- South Cascades / Columbia (Skamania / Klickitat / Cowlitz) ---
    {"name": "Wind River",         "type": "river",  "county": "Skamania",  "species_primary": ["Rainbow Trout", "Cutthroat Trout"]},
    {"name": "Klickitat River",    "type": "river",  "county": "Klickitat", "species_primary": ["Steelhead"]},
    {"name": "Kalama River",       "type": "river",  "county": "Cowlitz",   "species_primary": ["Steelhead"]},
    {"name": "Lewis River",        "type": "river",  "county": "Clark",     "species_primary": ["Steelhead", "Rainbow Trout"]},

    # --- Puget Sound lowlands ---
    {"name": "Stillaguamish River",       "type": "river",  "county": "Snohomish", "species_primary": ["Steelhead", "Cutthroat Trout"]},
    {"name": "North Fork Stillaguamish",  "type": "river",  "county": "Snohomish", "species_primary": ["Steelhead", "Cutthroat Trout"]},
    {"name": "Pilchuck River",            "type": "river",  "county": "Snohomish", "species_primary": ["Steelhead", "Cutthroat Trout"]},
    {"name": "Tolt River",                "type": "river",  "county": "King",      "species_primary": ["Steelhead", "Cutthroat Trout"]},
    {"name": "Green River",               "type": "river",  "county": "King",      "species_primary": ["Steelhead"]},

    # --- Olympic Peninsula (Clallam / Jefferson / Grays Harbor) ---
    {"name": "Sol Duc River",      "type": "river",  "county": "Clallam",   "species_primary": ["Steelhead"]},
    {"name": "Bogachiel River",    "type": "river",  "county": "Clallam",   "species_primary": ["Steelhead"]},
    {"name": "Queets River",       "type": "river",  "county": "Jefferson", "species_primary": ["Steelhead"]},
    {"name": "Quinault River",     "type": "river",  "county": "Grays Harbor", "species_primary": ["Steelhead"]},
    {"name": "Humptulips River",   "type": "river",  "county": "Grays Harbor", "species_primary": ["Steelhead", "Cutthroat Trout"]},

    # --- Fly-fishing-only lakes — Okanogan / Columbia Basin ---
    # These are some of the best stillwater dry fly and chironomid fisheries in WA.
    # All selective gear / fly only per WDFW regs (regulations scraper will confirm).
    {"name": "Chopaka Lake",  "type": "lake", "county": "Okanogan", "species_primary": ["Rainbow Trout"],          "is_alpine": False},
    {"name": "Lenice Lake",   "type": "lake", "county": "Grant",    "species_primary": ["Rainbow Trout"],          "is_alpine": False},
    {"name": "Nunnally Lake", "type": "lake", "county": "Grant",    "species_primary": ["Rainbow Trout"],          "is_alpine": False},
    {"name": "Dry Falls Lake","type": "lake", "county": "Grant",    "species_primary": ["Rainbow Trout"],          "is_alpine": False},
    {"name": "Lake Lenore",   "type": "lake", "county": "Grant",    "species_primary": ["Lahontan Cutthroat Trout"], "is_alpine": False},
    {"name": "Omak Lake",     "type": "lake", "county": "Okanogan", "species_primary": ["Lahontan Cutthroat Trout"], "is_alpine": False},

    # --- Fly-fishing-only lakes — Western WA ---
    {"name": "Pass Lake",     "type": "lake", "county": "Skagit",   "species_primary": ["Rainbow Trout", "Cutthroat Trout"], "is_alpine": False},
    {"name": "Lone Lake",     "type": "lake", "county": "Island",   "species_primary": ["Rainbow Trout"],          "is_alpine": False},
]


# ---------------------------------------------------------------------------
# Stocking data → spots
# ---------------------------------------------------------------------------

async def _fetch_all_stocking() -> list[dict]:
    """Fetch all current-year WDFW Fish Plants records from Socrata."""
    year = datetime.now(tz=timezone.utc).year
    records = []
    offset = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while True:
            resp = await client.get(
                _STOCKING_SOCRATA_URL,
                params={
                    "$limit": _PAGE_SIZE,
                    "$offset": offset,
                    "$where": f"release_year = '{year}'",
                    "$order": ":id",
                },
            )
            resp.raise_for_status()
            page = resp.json()
            records.extend(page)
            log.info(f"  fetch: offset={offset} got={len(page)}")
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    return records


def _group_stocking_by_water(records: list[dict]) -> dict[str, dict]:
    """
    Group Fish Plants records by water body name.

    Field mapping for dataset 6fex-3r7d:
      release_location  → name
      county            → county
      release_start_date → last_stocked_date
      species           → species_primary
      geo_code          → wdfw_water_id

    Returns dict keyed by normalised name.
    """
    waters: dict[str, dict] = {}

    for r in records:
        name = (r.get("release_location") or "").strip()
        if not name:
            continue

        key = name.lower()
        date_str = r.get("release_start_date") or r.get("release_end_date") or ""
        stocked_date = None
        if date_str:
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    stocked_date = datetime.strptime(date_str[:19], fmt[:len(date_str[:19])]).date()
                    break
                except (ValueError, TypeError):
                    continue

        species = (r.get("species") or "").strip()
        county = (r.get("county") or "").strip().title()
        wdfw_id = r.get("geo_code") or None

        if key not in waters:
            waters[key] = {
                "name": name,
                "county": county or None,
                "wdfw_water_id": wdfw_id,
                "type": _infer_type(name),
                "species_set": set(),
                "last_stocked_date": stocked_date,
                "last_stocked_species": [species] if species else [],
            }
        else:
            entry = waters[key]
            if stocked_date and (
                entry["last_stocked_date"] is None or stocked_date > entry["last_stocked_date"]
            ):
                entry["last_stocked_date"] = stocked_date
                entry["last_stocked_species"] = [species] if species else []

        if species:
            waters[key]["species_set"].add(species)

    for entry in waters.values():
        entry["species_primary"] = sorted(entry.pop("species_set"))

    return waters


async def seed_from_stocking(db, dry_run: bool) -> int:
    """Upsert spots from WDFW Fish Plants data. Returns count of new spots created."""
    from sqlalchemy import select, text
    from db.models import Spot

    log.info("Fetching WDFW Fish Plants data...")
    records = await _fetch_all_stocking()
    log.info(f"  {len(records)} stocking records fetched")

    waters = _group_stocking_by_water(records)
    log.info(f"  {len(waters)} unique water bodies found")

    if dry_run:
        log.info("[DRY RUN] Would upsert %d spots", len(waters))
        return len(waters)

    created = 0
    for water_data in waters.values():
        result = await db.execute(
            select(Spot).where(
                text("lower(name) = lower(:name)")
            ).params(name=water_data["name"])
        )
        existing = result.scalar_one_or_none()

        if existing:
            if water_data["last_stocked_date"]:
                existing.last_stocked_date = water_data["last_stocked_date"]
                existing.last_stocked_species = water_data["last_stocked_species"]
            if water_data["species_primary"] and not existing.species_primary:
                existing.species_primary = water_data["species_primary"]
            continue

        spot = Spot(
            name=water_data["name"],
            type=water_data["type"],
            county=water_data["county"],
            species_primary=water_data["species_primary"] or None,
            last_stocked_date=water_data["last_stocked_date"],
            last_stocked_species=water_data["last_stocked_species"] or None,
            wdfw_water_id=water_data["wdfw_water_id"],
            seed_confidence="confirmed",
            source="wdfw_stocking",
            is_public=True,
            fly_fishing_legal=True,
            min_temp_f=_COLDWATER_MIN_TEMP_F,
        )
        db.add(spot)
        created += 1

    await db.commit()
    log.info(f"  {created} new spots created")
    return created


# ---------------------------------------------------------------------------
# Curated spots → DB
# ---------------------------------------------------------------------------

async def seed_from_curated(db, dry_run: bool) -> int:
    """
    Upsert curated probable-tier spots. Skips any spot whose name already
    exists in the DB (case-insensitive) to avoid overwriting confirmed spots
    that came from stocking data.

    Returns count of new spots created.
    """
    from sqlalchemy import select, text
    from db.models import Spot

    if dry_run:
        log.info(f"[DRY RUN] Would upsert {len(_CURATED_SPOTS)} curated spots")
        return len(_CURATED_SPOTS)

    created = 0
    for entry in _CURATED_SPOTS:
        result = await db.execute(
            select(Spot).where(
                text("lower(name) = lower(:name)")
            ).params(name=entry["name"])
        )
        if result.scalar_one_or_none():
            continue

        spot = Spot(
            name=entry["name"],
            type=entry["type"],
            county=entry.get("county"),
            species_primary=entry.get("species_primary"),
            is_alpine=entry.get("is_alpine", False),
            seed_confidence="probable",
            source="curated",
            is_public=True,
            fly_fishing_legal=True,
            min_temp_f=_COLDWATER_MIN_TEMP_F,
        )
        db.add(spot)
        created += 1

    await db.commit()
    log.info(f"  {created} new curated spots created")
    return created


# ---------------------------------------------------------------------------
# §7.7 Fishability threshold application
# ---------------------------------------------------------------------------

async def apply_fishability_thresholds(db, dry_run: bool) -> int:
    """
    Apply §7.7 baseline CFS thresholds to spots whose names match the known
    rivers. Sets max_temp_f from species data where possible.
    """
    from sqlalchemy import select
    from db.models import Spot

    result = await db.execute(select(Spot))
    spots = result.scalars().all()

    updated = 0
    for spot in spots:
        name_lower = spot.name.lower()

        for river_fragment, thresholds in _RIVER_CFS_THRESHOLDS.items():
            if river_fragment in name_lower:
                if dry_run:
                    log.info(f"[DRY RUN] CFS {thresholds} → '{spot.name}'")
                else:
                    spot.min_cfs = thresholds["min_cfs"]
                    spot.max_cfs = thresholds["max_cfs"]
                updated += 1
                break

        if spot.species_primary:
            max_temp = None
            for sp in spot.species_primary:
                sp_lower = sp.lower()
                for species_key, temp_limit in _SPECIES_MAX_TEMP_F.items():
                    if species_key in sp_lower:
                        if max_temp is None or temp_limit < max_temp:
                            max_temp = temp_limit
            if max_temp is not None and not dry_run:
                spot.max_temp_f = max_temp

    if not dry_run and updated:
        await db.commit()

    return updated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_type(name: str) -> str:
    name_lower = name.lower()
    if any(w in name_lower for w in ("lake", "pond", "reservoir", "slough", "bog", "tarn")):
        return "lake"
    if "creek" in name_lower:
        return "creek"
    if any(w in name_lower for w in ("coast", "bay", "sound", "strait", "ocean", "puget")):
        return "coastal"
    return "river"


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    from db.connection import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        n = await seed_from_stocking(db, dry_run=args.dry_run)
        log.info(f"Stocking seed: {n} spots processed")

        n = await seed_from_curated(db, dry_run=args.dry_run)
        log.info(f"Curated seed: {n} spots created")

        n = await apply_fishability_thresholds(db, dry_run=args.dry_run)
        log.info(f"Fishability thresholds: {n} spots updated")

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed spots from WDFW Fish Plants data")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without writing to DB")
    args = parser.parse_args()
    asyncio.run(main(args))
