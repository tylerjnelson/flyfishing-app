"""
APScheduler job definitions for all background data fetchers.

Scheduler: AsyncIOScheduler (runs jobs in the asyncio event loop — matches
async fetchers without needing asyncio.run() wrappers).

Job store: SQLAlchemyJobStore backed by PostgreSQL (via psycopg2, installed
as a dependency of apscheduler[postgresql]).  Persistent across restarts;
pg-level locking prevents duplicate execution across Gunicorn workers.

Pin reminder: apscheduler must stay <4. APScheduler 4.x removed
BackgroundScheduler and PostgreSQLJobStore entirely.

Schedule summary:
  Every 2 hours   : WDFW emergency closures, InciWeb wildfires, NOAA NWRFC
  Daily 3 AM PT   : WDFW stocking, WTA reports, SNOTEL snowpack

Real-time fetchers (USGS, NOAA NWS, AirNow) are triggered at session open
by the chat context builder — they are NOT scheduled here.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, text

from config import settings
from conditions.noaa_nwrfc import fetch_noaa_nwrfc, resolve_gauge_id
from conditions.inciweb import fetch_inciweb
from conditions.snotel import fetch_snotel
from conditions.wdfw_emergency import fetch_wdfw_emergency
from conditions.wdfw_regulations import fetch_and_update_regulations
from conditions.wdfw_stocking import fetch_wdfw_stocking
from conditions.wta_scraper import fetch_wta_reports
from db.connection import AsyncSessionLocal
from db.models import ConditionsCache, EmergencyClosure, Spot, StockingEvent
from exceptions import ScraperStructureError

log = logging.getLogger(__name__)

_INTERVAL_SCHEDULED = 120  # minutes — used for conditions_hash boundary rounding

scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Scheduler lifecycle — called from main.py startup/shutdown events
# ---------------------------------------------------------------------------

def create_scheduler() -> AsyncIOScheduler:
    # APScheduler job store needs a sync SQLAlchemy URL (psycopg2 driver,
    # installed as a dependency of apscheduler[postgresql]).
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    jobstores = {"default": SQLAlchemyJobStore(url=sync_url, tablename="apscheduler_jobs")}
    return AsyncIOScheduler(jobstores=jobstores, timezone="America/Los_Angeles")


def start_scheduler() -> None:
    global scheduler
    scheduler = create_scheduler()

    # --- 2-hour jobs ---
    scheduler.add_job(
        job_wdfw_emergency,
        trigger=IntervalTrigger(hours=2),
        id="wdfw_emergency",
        replace_existing=True,
        next_run_time=datetime.now(tz=timezone.utc),  # run immediately on startup
    )
    scheduler.add_job(
        job_inciweb,
        trigger=IntervalTrigger(hours=2),
        id="inciweb",
        replace_existing=True,
        next_run_time=datetime.now(tz=timezone.utc),
    )
    scheduler.add_job(
        job_noaa_nwrfc,
        trigger=IntervalTrigger(hours=2),
        id="noaa_nwrfc",
        replace_existing=True,
    )

    # --- Daily 3 AM Pacific ---
    _daily = CronTrigger(hour=3, minute=0, timezone="America/Los_Angeles")
    scheduler.add_job(job_wdfw_stocking, trigger=_daily, id="wdfw_stocking", replace_existing=True)
    scheduler.add_job(job_wta, trigger=_daily, id="wta", replace_existing=True)
    scheduler.add_job(job_snotel, trigger=_daily, id="snotel", replace_existing=True)
    # Nightly scorer runs at 3:30 AM — after stocking and WTA jobs have written new data
    scheduler.add_job(
        job_score_all_spots,
        trigger=CronTrigger(hour=3, minute=30, timezone="America/Los_Angeles"),
        id="score_all_spots",
        replace_existing=True,
    )

    # --- Annual December 1 — WDFW regulations re-scrape ---
    scheduler.add_job(
        job_wdfw_regulations,
        trigger=CronTrigger(month=12, day=1, hour=4, minute=0, timezone="America/Los_Angeles"),
        id="wdfw_regulations",
        replace_existing=True,
    )

    scheduler.start()
    log.info("scheduler_started", extra={"jobs": [j.id for j in scheduler.get_jobs()]})


def stop_scheduler() -> None:
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")


# ---------------------------------------------------------------------------
# WDFW emergency closures — 2-hour, exempt from circuit breaker
# ---------------------------------------------------------------------------

async def job_wdfw_emergency() -> None:
    log.info("job_start", extra={"job": "wdfw_emergency"})
    try:
        rules = await fetch_wdfw_emergency()
    except ScraperStructureError as exc:
        log.critical(
            "scraper_structure_failure",
            extra={"source": exc.source, "url": exc.url, "detail": exc.detail},
        )
        return  # serve last cached rows unmodified
    except Exception as exc:
        log.warning("wdfw_emergency_fetch_failed", extra={"error": str(exc)})
        return  # serve last cached rows unmodified

    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Replace all non-expired closures with fresh data
            await session.execute(
                text("DELETE FROM emergency_closures WHERE expires IS NULL OR expires >= CURRENT_DATE")
            )
            for rule in rules:
                session.add(EmergencyClosure(
                    rule_text=rule["rule_text"],
                    effective=_parse_date(rule.get("effective")),
                    expires=_parse_date(rule.get("expires")),
                    source_url=rule["source_url"],
                    fetched_at=datetime.now(tz=timezone.utc),
                ))

    log.info("job_done", extra={"job": "wdfw_emergency", "rules_stored": len(rules)})


# ---------------------------------------------------------------------------
# InciWeb wildfires — 2-hour
# ---------------------------------------------------------------------------

async def job_inciweb() -> None:
    log.info("job_start", extra={"job": "inciweb"})
    data = await fetch_inciweb()
    if data is None:
        log.warning("job_stale_fallback", extra={"job": "inciweb"})
        return

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await _write_conditions_cache(
                session, spot_id=None, source="inciweb", data=data
            )

    log.info("job_done", extra={
        "job": "inciweb",
        "wa_fires": len(data.get("active_wa_fires", [])),
    })


# ---------------------------------------------------------------------------
# NOAA NWRFC river forecasts — 2-hour, per spot
# ---------------------------------------------------------------------------

async def job_noaa_nwrfc() -> None:
    log.info("job_start", extra={"job": "noaa_nwrfc"})
    spots = await _spots_with_usgs_ids()
    wrote = 0

    for spot in spots:
        for usgs_id in (spot.usgs_site_ids or []):
            gauge_id = await resolve_gauge_id(usgs_id)
            if not gauge_id:
                continue
            data = await fetch_noaa_nwrfc(gauge_id)
            if data is None:
                log.warning("job_stale_fallback", extra={"job": "noaa_nwrfc", "spot_id": str(spot.id)})
                continue
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await _write_conditions_cache(
                        session, spot_id=spot.id, source="noaa_nwrfc", data=data
                    )
            wrote += 1

    log.info("job_done", extra={"job": "noaa_nwrfc", "records_written": wrote})


# ---------------------------------------------------------------------------
# WDFW stocking — daily 3 AM
# ---------------------------------------------------------------------------

async def job_wdfw_stocking() -> None:
    log.info("job_start", extra={"job": "wdfw_stocking"})
    records = await fetch_wdfw_stocking()
    if records is None:
        log.warning("job_stale_fallback", extra={"job": "wdfw_stocking"})
        return

    async with AsyncSessionLocal() as session:
        async with session.begin():
            for r in records:
                session.add(StockingEvent(
                    stocked_date=_parse_date(r.get("stocked_date")),
                    species=r.get("species"),
                    count=r.get("count"),
                    size_description=r.get("size_description"),
                    source_record_id=r.get("source_record_id"),
                    fetched_at=datetime.now(tz=timezone.utc),
                    # spot_id linked in Phase 3 when spots are seeded and matched
                ))

    log.info("job_done", extra={"job": "wdfw_stocking", "records_stored": len(records)})


# ---------------------------------------------------------------------------
# WTA trail reports — daily 3 AM, per spot
# ---------------------------------------------------------------------------

async def job_wta() -> None:
    log.info("job_start", extra={"job": "wta"})
    spots = await _spots_with_wta_url()
    wrote = 0

    for spot in spots:
        try:
            reports = await fetch_wta_reports(spot.wta_trail_url)
        except ScraperStructureError as exc:
            log.critical(
                "scraper_structure_failure",
                extra={"source": exc.source, "url": exc.url, "detail": exc.detail},
            )
            continue
        if reports is None:
            log.warning("job_stale_fallback", extra={"job": "wta", "spot_id": str(spot.id)})
            continue

        if not reports:
            continue

        async with AsyncSessionLocal() as session:
            async with session.begin():
                await _write_conditions_cache(
                    session,
                    spot_id=spot.id,
                    source="wta",
                    data={"reports": reports, "fetched_at": datetime.now(tz=timezone.utc).isoformat()},
                )
        wrote += len(reports)

    log.info("job_done", extra={"job": "wta", "fishing_reports_stored": wrote})


# ---------------------------------------------------------------------------
# SNOTEL snowpack — daily 3 AM, per spot
# ---------------------------------------------------------------------------

async def job_snotel() -> None:
    log.info("job_start", extra={"job": "snotel"})
    spots = await _spots_with_snotel()
    wrote = 0

    for spot in spots:
        data = await fetch_snotel(spot.snotel_station_id)
        if data is None:
            log.warning("job_stale_fallback", extra={"job": "snotel", "spot_id": str(spot.id)})
            await _mark_stale(spot.id, source="snotel")
            continue
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await _write_conditions_cache(
                    session, spot_id=spot.id, source="snotel", data=data
                )
        wrote += 1

    log.info("job_done", extra={"job": "snotel", "records_written": wrote})


# ---------------------------------------------------------------------------
# Nightly Tier 1 scorer — 3:30 AM Pacific, all spots
# ---------------------------------------------------------------------------

async def job_score_all_spots() -> None:
    log.info("job_start", extra={"job": "score_all_spots"})
    from spots.scorer import compute_and_store_score

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Spot))
        spot_ids = [str(s.id) for s in result.scalars().all()]

    scored = 0
    failed = 0
    for spot_id in spot_ids:
        try:
            async with AsyncSessionLocal() as session:
                await compute_and_store_score(spot_id, session)
            scored += 1
        except Exception as exc:
            log.warning(
                "score_spot_failed",
                extra={"spot_id": spot_id, "error": str(exc)},
            )
            failed += 1

    log.info("job_done", extra={"job": "score_all_spots", "scored": scored, "failed": failed})


# ---------------------------------------------------------------------------
# Annual WDFW regulations re-scrape — December 1, 4 AM Pacific
# ---------------------------------------------------------------------------

async def job_wdfw_regulations() -> None:
    log.info("job_start", extra={"job": "wdfw_regulations"})
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await fetch_and_update_regulations(session)
        log.info("job_done", extra={"job": "wdfw_regulations"})
    except Exception as exc:
        log.warning("wdfw_regulations_job_failed", extra={"error": str(exc)})


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _write_conditions_cache(session, spot_id, source: str, data: dict) -> None:
    """Insert a new conditions_cache row. spot_id may be None for global sources."""
    data_hash = hashlib.md5(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    session.add(ConditionsCache(
        spot_id=spot_id,
        source=source,
        data=data,
        data_hash=data_hash,
        fetched_at=datetime.now(tz=timezone.utc),
    ))


async def _mark_stale(spot_id, source: str) -> None:
    """Set stale=True on the most recent conditions_cache entry for a spot+source."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(ConditionsCache)
                .where(ConditionsCache.spot_id == spot_id)
                .where(ConditionsCache.source == source)
                .order_by(ConditionsCache.fetched_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row and isinstance(row.data, dict):
                row.data = {**row.data, "stale": True}


async def _spots_with_usgs_ids() -> list[Spot]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Spot).where(Spot.usgs_site_ids.is_not(None))
        )
        return list(result.scalars().all())


async def _spots_with_wta_url() -> list[Spot]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Spot).where(Spot.wta_trail_url.is_not(None))
        )
        return list(result.scalars().all())


async def _spots_with_snotel() -> list[Spot]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Spot).where(Spot.snotel_station_id.is_not(None))
        )
        return list(result.scalars().all())


def _parse_date(value: str | None):
    """Parse an ISO date string to a Python date, or return None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None
