"""
Seed public lake shore fishing access spots from WDFW ShoreFishingSites — Phase 4 addition.

Source: WDFW ArcGIS REST — FP_FishMaps/ShoreFishingSites/MapServer/0
  https://geodataservices.wdfw.wa.gov/arcgis/rest/services/FP_FishMaps/ShoreFishingSites/MapServer/0

Fills the §7.1 spec gap: "WA Public Fishing Access Sites" — confirmed seeding source.
Phase 3 seed_spots.py noted "WA Public Fishing Access Sites are not available as a Socrata
dataset" — sourced from WDFW ArcGIS Feature Service instead.

731 designated public shore fishing access sites at lakes statewide.
All are WDFW-managed public land; Discover Pass required at most sites.

Idempotent: skips any spot whose name already exists (case-insensitive).
Run AFTER: alembic upgrade head, seed_spots.py
Run BEFORE: embed_spots.py

Usage (from backend/ directory):
  python -m scripts.seed_wdfw_access_spots
  python -m scripts.seed_wdfw_access_spots --dry-run
"""

import argparse
import asyncio
import logging
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------

_BASE_URL = (
    "https://geodataservices.wdfw.wa.gov/arcgis/rest/services"
    "/FP_FishMaps/ShoreFishingSites/MapServer/0/query"
)
_PAGE_SIZE = 1000
_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=5.0, pool=5.0)

# Required fields from the feature service
_OUT_FIELDS = "AccessSiteID,LakeName,County,Latitude,Longitude,Description"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def _fetch_all_sites() -> list[dict]:
    """Fetch all ShoreFishingSites records via ArcGIS REST pagination."""
    records = []
    offset = 0

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while True:
            resp = await client.get(
                _BASE_URL,
                params={
                    "where": "1=1",
                    "outFields": _OUT_FIELDS,
                    "resultRecordCount": _PAGE_SIZE,
                    "resultOffset": offset,
                    "f": "json",
                },
            )
            resp.raise_for_status()
            payload = resp.json()

            features = payload.get("features", [])
            records.extend(f["attributes"] for f in features)
            log.info(f"  fetch: offset={offset} got={len(features)}")

            if not payload.get("exceededTransferLimit", False):
                break
            offset += _PAGE_SIZE

    return records


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

async def seed_wdfw_access(db, dry_run: bool) -> int:
    """
    Upsert WDFW lake shore fishing access spots.
    Skips any spot whose name already exists (case-insensitive) to avoid
    overwriting confirmed spots from stocking data.
    Returns count of new spots created.
    """
    from sqlalchemy import select, text
    from db.models import Spot

    log.info("Fetching WDFW ShoreFishingSites data...")
    records = await _fetch_all_sites()
    log.info(f"  {len(records)} shore fishing sites fetched")

    if dry_run:
        # Show a sample of what would be seeded
        names = [r.get("LakeName", "").strip() for r in records if r.get("LakeName")]
        log.info(f"[DRY RUN] Would upsert up to {len(records)} spots. Sample: {names[:5]}")
        return len(records)

    created = 0
    skipped = 0

    for rec in records:
        name = (rec.get("LakeName") or "").strip()
        if not name:
            log.debug(f"  skip: empty name (AccessSiteID={rec.get('AccessSiteID')})")
            skipped += 1
            continue

        # Idempotency check — skip if name already in DB
        result = await db.execute(
            select(Spot).where(text("lower(name) = lower(:name)")).params(name=name)
        )
        if result.scalar_one_or_none():
            skipped += 1
            continue

        lat = _safe_float(rec.get("Latitude"))
        lon = _safe_float(rec.get("Longitude"))
        county = (rec.get("County") or "").strip().title() or None

        spot = Spot(
            name=name,
            type="lake",
            county=county,
            latitude=lat,
            longitude=lon,
            source="wdfw_access",
            seed_confidence="confirmed",
            is_public=True,
            fly_fishing_legal=True,  # corrected annually by wdfw_regulations job
            min_temp_f=40.0,
        )
        db.add(spot)
        created += 1

    await db.commit()
    log.info(f"  {created} new spots created, {skipped} skipped (existing or no name)")
    return created


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        n = await seed_wdfw_access(db, dry_run=args.dry_run)
        log.info(f"WDFW access seed: {n} spots {'would be created' if args.dry_run else 'created'}")

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed WDFW lake shore fishing access spots from ArcGIS Feature Service"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print counts without writing to DB")
    args = parser.parse_args()
    asyncio.run(main(args))
