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

import logging
import re


from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import date, datetime, timezone
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import settings
from conditions.cache import write_conditions_cache
from conditions.fetch_errors import log_fetch_failure
from conditions.noaa_nwrfc import fetch_noaa_nwrfc, resolve_gauge_id
from conditions.inciweb import fetch_inciweb
from conditions.nps_alerts import fetch_nps_alerts
from conditions.snotel import fetch_snotel
from conditions.wdfw_emergency import fetch_wdfw_emergency, location_head, reopens_fishing
from conditions.wdfw_regulations import fetch_and_update_regulations
from conditions.wdfw_stocking import fetch_wdfw_stocking
from conditions.wta_scraper import fetch_wta_reports
from db.connection import AsyncSessionLocal
from db.models import ConditionsCache, EmergencyClosure, StockingEvent, WaterBody
from exceptions import ScraperStructureError

log = logging.getLogger(__name__)

_INTERVAL_SCHEDULED = 120  # minutes — used for conditions_hash boundary rounding

scheduler: AsyncIOScheduler | None = None


def _log_job_done(job: str, failed: int = 0, **counts) -> None:
    """One summary line per run. APScheduler reports "executed successfully"
    even when a job swallowed failures, so this line is the source of truth:
    `job_degraded` (WARNING) if anything failed, else `job_done`."""
    if failed:
        log.warning("job_degraded", extra={"job": job, "failed": failed, **counts})
    else:
        log.info("job_done", extra={"job": job, **counts})


# ---------------------------------------------------------------------------
# Scheduler lifecycle — called from main.py startup/shutdown events
# ---------------------------------------------------------------------------

def create_scheduler() -> AsyncIOScheduler:
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
    scheduler.add_job(
        job_nps_alerts,
        trigger=IntervalTrigger(hours=2),
        id="nps_alerts",
        replace_existing=True,
    )

    # --- Daily 3 AM Pacific ---
    _daily = CronTrigger(hour=3, minute=0, timezone="America/Los_Angeles")
    scheduler.add_job(job_wdfw_stocking, trigger=_daily, id="wdfw_stocking", replace_existing=True)
    scheduler.add_job(job_wta, trigger=_daily, id="wta", replace_existing=True)
    scheduler.add_job(job_snotel, trigger=_daily, id="snotel", replace_existing=True)
    # Nightly scorer runs at 3:30 AM — after stocking and WTA jobs have written new data
    scheduler.add_job(
        job_score_all_water_bodies,
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
        return
    except Exception as exc:
        # Existing rows are kept (served stale) — never silently go dark.
        log_fetch_failure("wdfw_emergency", exc, job="wdfw_emergency")
        _log_job_done("wdfw_emergency", failed=1, serving="stale_rows")
        return

    if not rules:
        log.warning("wdfw_emergency_empty_rules", extra={"job": "wdfw_emergency"})
        return

    # Build name→water_body_id lookup (names + aliases) for FK resolution
    name_to_wb_id: dict[str, object] = {}
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(WaterBody.id, WaterBody.name, WaterBody.aliases))
        for row in result.all():
            for name in [row.name, *(row.aliases or [])]:
                if name and "(" not in name:  # "MINERAL LK (LEWI)"-style stocking names never match prose
                    name_to_wb_id.setdefault(" ".join(name.upper().split()), row.id)
    # Longest first, whole words: "Lake Washington" before "Washington", and
    # "Fish Lake" must not match inside "Spearfish Lake".
    name_patterns = [
        (re.compile(rf"\b{re.escape(n)}\b"), wb_id)
        for n, wb_id in sorted(name_to_wb_id.items(), key=lambda kv: -len(kv[0]))
    ]

    def _resolve_water_body_id(rule: dict) -> object:
        """Match the rule's title, then the water named at the head of its
        Location field. Never the full text: boundary landmarks ("...to the
        confluence of Sol Duc and Bogachiel rivers") linked rules to the wrong
        water (TODO-15)."""
        for candidate in (rule.get("title"), location_head(rule.get("location"))):
            text_upper = " ".join(re.sub(r"\([^)]*\)", " ", candidate or "").upper().split())
            if not text_upper:
                continue
            for pattern, wb_id in name_patterns:
                if pattern.search(text_upper):
                    return wb_id
        return None

    for rule in rules:
        rule["water_body_id"] = _resolve_water_body_id(rule)
    superseded = _apply_supersession(rules)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("DELETE FROM emergency_closures")
            )
            for rule in rules:
                session.add(EmergencyClosure(
                    water_body_id=rule["water_body_id"],
                    rule_text=rule["rule_text"],
                    title=rule.get("title"),
                    action=rule.get("action"),
                    location=rule.get("location"),
                    closes_fishing=bool(rule.get("closes_fishing")),
                    effective=_parse_date(rule.get("effective")),
                    expires=_parse_date(rule.get("expires")),
                    source_url=rule["source_url"],
                    fetched_at=datetime.now(tz=timezone.utc),
                ))

    _log_job_done(
        "wdfw_emergency",
        rules_stored=len(rules),
        water_body_matched=sum(1 for r in rules if r["water_body_id"]),
        closures=sum(1 for r in rules if r.get("closes_fishing")),
        superseded_closures=superseded,
        # No parsed expiry ⇒ active for as long as WDFW lists the rule.
        rules_without_expiry=sum(1 for r in rules if not r.get("expires")),
    )


def _apply_supersession(rules: list[dict]) -> list[str]:
    """
    Clear closes_fishing on closures that a later rule for the same water
    reopened (e.g. Quillayute: closed Sept. 9 "until further notice", reopened
    Oct. 6). "Same water" = same water_body_id, or, for rules matching no water
    body, the same Location head. Returns the superseded rules' titles.
    """
    def water_key(rule: dict):
        return rule.get("water_body_id") or location_head(rule.get("location")).lower() or None

    superseded = []
    for closure in rules:
        key = water_key(closure)
        if not closure.get("closes_fishing") or key is None or not closure.get("effective"):
            continue
        for other in rules:
            if (other is not closure and water_key(other) == key
                    and reopens_fishing(other.get("action"))
                    and (other.get("effective") or "") > closure["effective"]):
                closure["closes_fishing"] = False
                superseded.append(closure.get("title"))
                break
    if superseded:
        log.info("wdfw_emergency_closures_superseded", extra={"titles": superseded})
    return superseded


# ---------------------------------------------------------------------------
# InciWeb wildfires — 2-hour
# ---------------------------------------------------------------------------

async def job_inciweb() -> None:
    log.info("job_start", extra={"job": "inciweb"})
    data = await fetch_inciweb()
    if data is None:
        _log_job_done("inciweb", failed=1, serving="stale_cache")
        return

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await write_conditions_cache(
                session, water_body_id=None, source="inciweb", data=data
            )

    _log_job_done("inciweb", wa_fires=len(data.get("active_wa_fires", [])))


# ---------------------------------------------------------------------------
# NPS park alerts — 2-hour (NOCA + OLYM)
# ---------------------------------------------------------------------------

async def job_nps_alerts() -> None:
    log.info("job_start", extra={"job": "nps_alerts"})
    data = await fetch_nps_alerts()
    if data is None:
        _log_job_done("nps_alerts", failed=1, serving="stale_cache")
        return

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await write_conditions_cache(
                session, water_body_id=None, source="nps_alerts", data=data
            )

    total = sum(p["alert_count"] for p in data.get("parks", {}).values())
    _log_job_done("nps_alerts", total_alerts=total)


# ---------------------------------------------------------------------------
# NOAA NWRFC river forecasts — 2-hour, per water body
# ---------------------------------------------------------------------------

async def job_noaa_nwrfc() -> None:
    log.info("job_start", extra={"job": "noaa_nwrfc"})
    water_bodies = await _water_bodies_with_usgs_ids()
    wrote = 0
    stale: list[str] = []

    for wb in water_bodies:
        for usgs_id in (wb.usgs_site_ids or []):
            gauge_id = resolve_gauge_id(usgs_id)
            if not gauge_id:
                continue
            data = await fetch_noaa_nwrfc(gauge_id)
            if data is None:
                log.warning("job_stale_fallback", extra={
                    "job": "noaa_nwrfc", "water_body": wb.name, "water_body_id": str(wb.id), "gauge_id": gauge_id,
                })
                stale.append(gauge_id)
                continue
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await write_conditions_cache(
                        session, water_body_id=wb.id, source="noaa_nwrfc", data=data
                    )
            wrote += 1

    _log_job_done("noaa_nwrfc", failed=len(stale), records_written=wrote, stale_gauges=stale)


# ---------------------------------------------------------------------------
# WDFW stocking — daily 3 AM
# ---------------------------------------------------------------------------

async def job_wdfw_stocking() -> None:
    log.info("job_start", extra={"job": "wdfw_stocking"})
    year = datetime.now(tz=timezone.utc).year
    records = await fetch_wdfw_stocking(year)
    if records is None:
        _log_job_done("wdfw_stocking", failed=1, serving="stale_cache")
        return

    # Build name→water_body_id map for linking stocking events
    name_to_wb_id: dict[str, object] = {}
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(WaterBody.id, WaterBody.name))
        for row in result.all():
            name_to_wb_id[row.name.upper()] = row.id

    async with AsyncSessionLocal() as session:
        async with session.begin():
            for r in records:
                water_name = (r.get("water_name") or "").strip().upper()
                wb_id = name_to_wb_id.get(water_name)
                stmt = pg_insert(StockingEvent).values(
                    water_body_id=wb_id,
                    stocked_date=_parse_date(r.get("stocked_date")),
                    species=r.get("species"),
                    count=r.get("count"),
                    size_description=r.get("size_description"),
                    source_record_id=r.get("source_record_id"),
                    fetched_at=datetime.now(tz=timezone.utc),
                ).on_conflict_do_nothing(index_elements=["source_record_id"])
                await session.execute(stmt)

            # The fetch is the full year's dataset, so drop that year's rows it no
            # longer contains: records WDFW corrected/removed, and rows keyed by the
            # old unstable Socrata :id (~271k duplicates as of 2026-10).
            removed = 0
            if records:
                current_ids = [r["source_record_id"] for r in records]
                result = await session.execute(
                    delete(StockingEvent)
                    .where(StockingEvent.stocked_date >= date(year, 1, 1))
                    .where(StockingEvent.stocked_date < date(year + 1, 1, 1))
                    .where(StockingEvent.source_record_id.not_in(current_ids))
                )
                removed = result.rowcount

    # Propagate most-recent stocked_date per water body
    latest_by_name: dict[str, object] = {}
    for r in records:
        water_name = (r.get("water_name") or "").strip()
        stocked_date = _parse_date(r.get("stocked_date"))
        if not water_name or stocked_date is None:
            continue
        key = water_name.upper()
        if key not in latest_by_name or stocked_date > latest_by_name[key]:
            latest_by_name[key] = stocked_date

    water_bodies_updated = 0
    if latest_by_name:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                for name_upper, stocked_date in latest_by_name.items():
                    result = await session.execute(
                        text("""
                            UPDATE water_bodies
                               SET last_stocked_date = :stocked_date
                             WHERE UPPER(name) = :name
                               AND (last_stocked_date IS NULL OR last_stocked_date < :stocked_date)
                        """),
                        {"name": name_upper, "stocked_date": stocked_date},
                    )
                    water_bodies_updated += result.rowcount

    _log_job_done(
        "wdfw_stocking",
        records_fetched=len(records),
        stale_rows_removed=removed,
        water_bodies_updated=water_bodies_updated,
    )


# ---------------------------------------------------------------------------
# WTA trail reports — daily 3 AM, per water body
# ---------------------------------------------------------------------------

async def job_wta() -> None:
    log.info("job_start", extra={"job": "wta"})
    water_bodies = await _water_bodies_with_wta_url()
    wrote = 0
    failed = 0

    for wb in water_bodies:
        try:
            result = await fetch_wta_reports(wb.wta_trail_url)
        except ScraperStructureError as exc:
            log.critical(
                "scraper_structure_failure",
                extra={"source": exc.source, "url": exc.url, "detail": exc.detail},
            )
            failed += 1
            continue
        if result is None:
            log.warning("job_stale_fallback", extra={
                "job": "wta", "water_body": wb.name, "water_body_id": str(wb.id),
            })
            failed += 1
            continue

        fishing_reports = result["fishing_reports"]
        trail_conditions = result["trail_conditions"]

        # Write to cache when there are reports (even all-clear — scorer needs
        # the "no issues" signal to award the positive access bonus).
        if not fishing_reports and trail_conditions.get("total_reports", 0) == 0:
            continue

        async with AsyncSessionLocal() as session:
            async with session.begin():
                await write_conditions_cache(
                    session,
                    water_body_id=wb.id,
                    source="wta",
                    data={
                        "reports": fishing_reports,
                        "trail_conditions": trail_conditions,
                        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                    },
                )
        wrote += len(fishing_reports)

    _log_job_done("wta", failed=failed, trails=len(water_bodies), fishing_reports_stored=wrote)


# ---------------------------------------------------------------------------
# SNOTEL snowpack — daily 3 AM, per water body
# ---------------------------------------------------------------------------

async def job_snotel() -> None:
    log.info("job_start", extra={"job": "snotel"})
    water_bodies = await _water_bodies_with_snotel()
    wrote = 0
    failed = 0

    # Cache per station triplet — multiple water bodies can share one station.
    station_cache: dict[str, dict | None] = {}
    for wb in water_bodies:
        triplet = wb.snotel_station_id
        if triplet not in station_cache:
            station_cache[triplet] = await fetch_snotel(triplet)
        data = station_cache[triplet]
        if data is None:
            log.warning("job_stale_fallback", extra={
                "job": "snotel", "water_body": wb.name, "water_body_id": str(wb.id), "station": triplet,
            })
            await _mark_stale(wb.id, source="snotel")
            failed += 1
            continue
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await write_conditions_cache(
                    session, water_body_id=wb.id, source="snotel", data=data
                )
        wrote += 1

    _log_job_done("snotel", failed=failed, records_written=wrote)


# ---------------------------------------------------------------------------
# Nightly Tier 1 scorer — 3:30 AM Pacific, all water bodies
# ---------------------------------------------------------------------------

async def job_score_all_water_bodies() -> None:
    log.info("job_start", extra={"job": "score_all_water_bodies"})
    from spots.scorer import compute_and_store_score

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(WaterBody))
        water_body_ids = [str(wb.id) for wb in result.scalars().all()]

    scored = 0
    failed = 0
    for wb_id in water_body_ids:
        try:
            async with AsyncSessionLocal() as session:
                await compute_and_store_score(wb_id, session)
            scored += 1
        except Exception as exc:
            log.warning(
                "score_water_body_failed",
                extra={"water_body_id": wb_id, "error": str(exc)},
            )
            failed += 1

    _log_job_done("score_all_water_bodies", failed=failed, scored=scored)


# ---------------------------------------------------------------------------
# Annual WDFW regulations re-scrape — December 1, 4 AM Pacific
# ---------------------------------------------------------------------------

async def job_wdfw_regulations() -> None:
    log.info("job_start", extra={"job": "wdfw_regulations"})
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await fetch_and_update_regulations(session)
        _log_job_done("wdfw_regulations")
    except Exception:
        log.exception("wdfw_regulations_job_failed", extra={"job": "wdfw_regulations"})


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _mark_stale(water_body_id, source: str) -> None:
    """Set stale=True on the most recent conditions_cache entry for a water_body+source."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(ConditionsCache)
                .where(ConditionsCache.water_body_id == water_body_id)
                .where(ConditionsCache.source == source)
                .order_by(ConditionsCache.fetched_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row and isinstance(row.data, dict):
                row.data = {**row.data, "stale": True}


async def _water_bodies_with_usgs_ids() -> list[WaterBody]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(WaterBody).where(WaterBody.usgs_site_ids.is_not(None))
        )
        return list(result.scalars().all())


async def _water_bodies_with_wta_url() -> list[WaterBody]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(WaterBody).where(WaterBody.wta_trail_url.is_not(None))
        )
        return list(result.scalars().all())


async def _water_bodies_with_snotel() -> list[WaterBody]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(WaterBody).where(WaterBody.snotel_station_id.is_not(None))
        )
        return list(result.scalars().all())


# Backward-compat alias used by tests
job_score_all_spots = job_score_all_water_bodies


def _parse_date(value: str | None):
    """Parse an ISO date string to a Python date, or return None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None
