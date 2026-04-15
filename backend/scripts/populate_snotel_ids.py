"""
Map alpine spots to their nearest WA SNOTEL station.

For each spot with is_alpine=True and valid coordinates, finds the closest
WA SNOTEL station by haversine distance, preferring stations at similar or
higher elevation (within +1500 ft of spot elevation). Updates
spots.snotel_station_id with the station triplet (e.g. '679:WA:SNTL').

Usage (from backend/ directory):
  python -m scripts.populate_snotel_ids
  python -m scripts.populate_snotel_ids --dry-run

Prerequisites:
  - DATABASE_URL in environment
"""

import argparse
import asyncio
import logging
import math
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_STATIONS_URL = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/stations"
# Max distance in km to consider a SNOTEL station for a spot
_MAX_DISTANCE_KM = 80
# Max elevation BELOW spot to consider (avoid mapping high alpine to valley station)
_MAX_ELEV_BELOW_FT = 1000


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def _fetch_wa_snotel_stations() -> list[dict]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.get(_STATIONS_URL, params={
            "stateCode": "WA",
            "networkCds": "SNTL",
            "activeOnly": "true",
        })
        resp.raise_for_status()
        data = resp.json()
    return [s for s in data if s.get("stateCode") == "WA" and s.get("networkCode") == "SNTL"]


def _best_station(spot_lat: float, spot_lon: float, spot_elev_ft: float,
                  stations: list[dict]) -> dict | None:
    """
    Return the closest WA SNOTEL station within _MAX_DISTANCE_KM that is not
    more than _MAX_ELEV_BELOW_FT below the spot's elevation.
    """
    best = None
    best_dist = float("inf")

    for s in stations:
        slat = s.get("latitude")
        slon = s.get("longitude")
        selev_ft = s.get("elevation", 0)  # NRCS returns elevation in feet
        if slat is None or slon is None:
            continue
        # Skip stations significantly lower than the spot
        if spot_elev_ft - selev_ft > _MAX_ELEV_BELOW_FT:
            continue
        dist = _haversine_km(spot_lat, spot_lon, slat, slon)
        if dist > _MAX_DISTANCE_KM:
            continue
        if dist < best_dist:
            best_dist = dist
            best = s

    return best


async def _run(dry_run: bool) -> None:
    from sqlalchemy import select, text
    from db.connection import AsyncSessionLocal
    from db.models import Spot

    log.info("fetching_wa_snotel_stations")
    stations = await _fetch_wa_snotel_stations()
    log.info(f"loaded_stations count={len(stations)}")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Spot).where(
                Spot.is_alpine == True,
                Spot.latitude.is_not(None),
                Spot.longitude.is_not(None),
            )
        )
        alpine_spots = list(result.scalars().all())

    log.info(f"alpine_spots_to_process count={len(alpine_spots)}")

    updated = 0
    skipped_no_station = 0
    already_set = 0

    for spot in alpine_spots:
        elev_ft = spot.elevation_ft or 0
        station = _best_station(
            float(spot.latitude), float(spot.longitude), elev_ft, stations
        )

        if station is None:
            log.warning(
                f"no_station_found spot={spot.name!r} "
                f"lat={spot.latitude} lon={spot.longitude} elev={elev_ft}ft"
            )
            skipped_no_station += 1
            continue

        triplet = station["stationTriplet"]
        dist_km = _haversine_km(
            float(spot.latitude), float(spot.longitude),
            station["latitude"], station["longitude"],
        )

        if spot.snotel_station_id == triplet:
            log.info(
                f"already_set spot={spot.name!r} station={triplet}"
            )
            already_set += 1
            continue

        log.info(
            f"assigning spot={spot.name!r} elev={elev_ft}ft "
            f"-> {triplet} ({station['name']}) "
            f"elev={station.get('elevation', 0):.0f}ft dist={dist_km:.1f}km"
        )

        if not dry_run:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await session.execute(
                        text("UPDATE spots SET snotel_station_id = :triplet WHERE id = :id"),
                        {"triplet": triplet, "id": str(spot.id)},
                    )
        updated += 1

    log.info(
        f"populate_snotel_complete "
        f"updated={updated} already_set={already_set} "
        f"no_station={skipped_no_station} dry_run={dry_run}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Map alpine spots to SNOTEL stations")
    parser.add_argument("--dry-run", action="store_true", help="Log actions, no DB writes")
    args = parser.parse_args()
    asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
