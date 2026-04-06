"""
Populate elevation_ft and is_alpine for lake spots — Phase 3 gap fix.

Fills the §13.3 spec item: "Populate elevation_ft and is_alpine for all lake spots."

For each lake spot missing elevation_ft:
  1. If lat/lon is missing: geocode via HERE Geocoding API and save coordinates.
  2. Fetch elevation via USGS Elevation Point Query Service (free, no key required).
     https://epqs.nationalmap.gov/v1/json?x={lon}&y={lat}&units=Feet
  3. Set elevation_ft = result (rounded to nearest foot).
  4. Set is_alpine = True if elevation_ft >= 2500 (§7.8 lowest ice-off band).

permit_required is NOT automated — it requires manual research per spot
(e.g. Colville Tribal permit for Omak Lake, Discover Pass for state-managed sites).
Set permit_required = True directly on the spot record via DB or admin UI as needed.

Idempotent: skips spots where elevation_ft is already populated.
Rate limiting: 0.25s delay between HERE calls, 0.1s between USGS calls.

Run AFTER: seed_spots.py (and all other seeding scripts)
Run BEFORE: embed_spots.py

Usage (from backend/ directory):
  python -m scripts.populate_lake_elevation
  python -m scripts.populate_lake_elevation --dry-run
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_ALPINE_THRESHOLD_FT = 2500  # §7.8 lower elevation band

_HERE_GEOCODE_URL = "https://geocode.search.hereapi.com/v1/geocode"
_USGS_ELEV_URL = "https://epqs.nationalmap.gov/v1/json"

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=5.0, pool=5.0)


# ---------------------------------------------------------------------------
# HERE geocoding
# ---------------------------------------------------------------------------

async def _geocode_lake(name: str, county: str | None, client: httpx.AsyncClient, here_api_key: str) -> tuple[float | None, float | None]:
    """
    Geocode a lake name via HERE. Appends county and state for precision.
    Returns (lat, lon) or (None, None) on failure.
    """
    query_parts = [name]
    if county:
        query_parts.append(f"{county} County")
    query_parts.append("Washington State")

    try:
        resp = await client.get(
            _HERE_GEOCODE_URL,
            params={
                "q": ", ".join(query_parts),
                "in": "countryCode:USA",
                "apiKey": here_api_key,
            },
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            log.debug(f"  geocode_no_results: {name!r}")
            return None, None

        pos = items[0]["position"]
        lat, lon = float(pos["lat"]), float(pos["lng"])

        # Sanity check: must be in Washington State bounding box
        if 45.5 <= lat <= 49.1 and -125.0 <= lon <= -116.9:
            return lat, lon

        log.debug(f"  geocode_out_of_bounds: {name!r} → ({lat}, {lon})")
        return None, None

    except Exception as exc:
        log.debug(f"  geocode_failed: {name!r} — {exc}")
        return None, None


# ---------------------------------------------------------------------------
# USGS elevation
# ---------------------------------------------------------------------------

async def _fetch_elevation(lat: float, lon: float, client: httpx.AsyncClient) -> int | None:
    """
    Fetch elevation in feet from USGS Elevation Point Query Service.
    Returns rounded integer elevation, or None on failure / nodata.
    """
    try:
        resp = await client.get(
            _USGS_ELEV_URL,
            params={
                "x": lon,
                "y": lat,
                "units": "Feet",
                "includeDate": "false",
            },
        )
        resp.raise_for_status()
        value = resp.json().get("value")
        if value is None:
            return None
        elev = float(value)
        # USGS returns -1000000 for nodata; reject implausible values
        if elev < 0 or elev > 20000:
            return None
        return round(elev)

    except Exception as exc:
        log.debug(f"  usgs_elev_failed: ({lat}, {lon}) — {exc}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(dry_run: bool) -> None:
    from sqlalchemy import select
    from db.connection import AsyncSessionLocal
    from db.models import Spot
    from config import settings

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Spot).where(
                Spot.type == "lake",
                Spot.elevation_ft.is_(None),
            )
        )
        spots = list(result.scalars().all())

    log.info(f"{len(spots)} lake spots missing elevation_ft")
    if not spots:
        log.info("Nothing to do.")
        return

    geocoded = 0
    elev_set = 0
    alpine_set = 0
    skipped = 0

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for spot in spots:
            lat = float(spot.latitude) if spot.latitude is not None else None
            lon = float(spot.longitude) if spot.longitude is not None else None

            # Step 1 — geocode if coordinates missing
            if lat is None or lon is None:
                await asyncio.sleep(0.25)  # rate limit HERE
                lat, lon = await _geocode_lake(spot.name, spot.county, client, settings.here_api_key)
                if lat is None:
                    log.info(f"  geocode_miss: {spot.name!r} — skipping elevation")
                    skipped += 1
                    continue
                log.info(f"  geocoded: {spot.name!r} → ({lat:.4f}, {lon:.4f})")
                geocoded += 1

            # Step 2 — fetch elevation
            await asyncio.sleep(0.1)  # rate limit USGS
            elev_ft = await _fetch_elevation(lat, lon, client)

            if elev_ft is None:
                log.info(f"  elev_miss: {spot.name!r} @ ({lat:.4f}, {lon:.4f})")
                skipped += 1
                # Still save coordinates even if elevation fails
                if not dry_run and (spot.latitude is None or spot.longitude is None):
                    async with AsyncSessionLocal() as session:
                        async with session.begin():
                            s = await session.get(Spot, spot.id)
                            s.latitude = lat
                            s.longitude = lon
                continue

            is_alpine = elev_ft >= _ALPINE_THRESHOLD_FT
            log.info(
                f"  {'[DRY RUN] ' if dry_run else ''}"
                f"{spot.name!r}: {elev_ft} ft {'(ALPINE)' if is_alpine else ''}"
            )

            if not dry_run:
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        s = await session.get(Spot, spot.id)
                        s.latitude = lat
                        s.longitude = lon
                        s.elevation_ft = elev_ft
                        s.is_alpine = is_alpine

            elev_set += 1
            if is_alpine:
                alpine_set += 1

    log.info(
        f"Done — geocoded={geocoded} elevation_set={elev_set} "
        f"alpine_flagged={alpine_set} skipped={skipped} dry_run={dry_run}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate elevation_ft and is_alpine for lake spots"
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions, no DB writes")
    args = parser.parse_args()
    asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
