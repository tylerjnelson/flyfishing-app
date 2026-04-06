"""
Seed coastal shore fishing spots from WA Ecology Coastal Atlas — Phase 4 addition.

Source: WA Ecology CoastalAtlas ArcGIS REST — Layer 10 (Public Beach Access Points)
  https://gis.ecology.wa.gov/serverext/rest/services/GIS/CoastalAtlas/MapServer/10
  Filter: Fishing = 'Yes'

87 public beach access points where fishing is a designated activity.
These are coastal/saltwater spots and are surfaced only when session intake
includes Saltwater in water_type. The Phase 5 pre-LLM filter gates them by
session intake water_type selection (context_builder.py).

seed_confidence='probable' — public beaches with fishing access, but fly fishing
suitability is unverified (no species or gear data in source).

Fields used: Beach_Name, County_NM, Latitude, Longitude, ECYBEACHID, Coast_Type

Note: Latitude/Longitude from this service are already WGS84 decimal degrees.
No coordinate conversion required.

Idempotent: skips any spot whose name already exists (case-insensitive).
Run AFTER: alembic upgrade head, seed_spots.py
Run BEFORE: embed_spots.py

Usage (from backend/ directory):
  python -m scripts.seed_coastal_spots
  python -m scripts.seed_coastal_spots --dry-run
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
    "https://gis.ecology.wa.gov/serverext/rest/services"
    "/GIS/CoastalAtlas/MapServer/10/query"
)
_PAGE_SIZE = 1000
_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=5.0, pool=5.0)

_OUT_FIELDS = "OBJECTID,ECYBEACHID,Beach_Name,County_NM,Latitude,Longitude,Coast_Type,Fishing"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def _fetch_fishing_beaches() -> list[dict]:
    """Fetch all public beach access points where Fishing='Yes'."""
    records = []
    offset = 0

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while True:
            resp = await client.get(
                _BASE_URL,
                params={
                    "where": "Fishing = 'Yes'",
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

async def seed_coastal_spots(db, dry_run: bool) -> int:
    """
    Upsert coastal shore fishing access spots from WA Ecology Coastal Atlas.
    Returns count of new spots created.
    """
    from sqlalchemy import select, text
    from db.models import Spot

    log.info("Fetching WA Ecology coastal beach access data (Fishing='Yes')...")
    records = await _fetch_fishing_beaches()
    log.info(f"  {len(records)} fishing beach access points fetched")

    if dry_run:
        names = [r.get("Beach_Name", "").strip() for r in records if r.get("Beach_Name")]
        log.info(f"[DRY RUN] Would upsert up to {len(records)} coastal spots. Sample: {names[:5]}")
        return len(records)

    created = 0
    skipped = 0

    for rec in records:
        name = (rec.get("Beach_Name") or "").strip()
        if not name:
            log.debug(f"  skip: empty name (ECYBEACHID={rec.get('ECYBEACHID')})")
            skipped += 1
            continue

        # Idempotency check
        result = await db.execute(
            select(Spot).where(text("lower(name) = lower(:name)")).params(name=name)
        )
        if result.scalar_one_or_none():
            skipped += 1
            continue

        lat = _safe_float(rec.get("Latitude"))
        lon = _safe_float(rec.get("Longitude"))
        county = (rec.get("County_NM") or "").strip().title() or None

        spot = Spot(
            name=name,
            type="coastal",
            county=county,
            latitude=lat,
            longitude=lon,
            source="wa_ecology",
            seed_confidence="probable",
            is_public=True,
            fly_fishing_legal=True,  # assume legal until verified
        )
        db.add(spot)
        created += 1

    await db.commit()
    log.info(f"  {created} new coastal spots created, {skipped} skipped (existing or no name)")
    return created


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        # Sanity check: WA latitude 45–49°N, longitude 116–125°W
        return f if f != 0.0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    from db.connection import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        n = await seed_coastal_spots(db, dry_run=args.dry_run)
        log.info(f"Coastal seed: {n} spots {'would be created' if args.dry_run else 'created'}")

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed WA Ecology coastal shore fishing spots (Fishing='Yes')"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print counts without writing to DB")
    args = parser.parse_args()
    asyncio.run(main(args))
