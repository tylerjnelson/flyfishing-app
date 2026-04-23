"""
Manually trigger all scheduled fetcher jobs in sequence.

Runs every job that APScheduler would run nightly/bi-hourly.
Skips job_wdfw_regulations (annual; run only in December).

Usage (from backend/):
  sudo env $(grep -v '^#' /etc/flyfish/app.env | grep -v '^$' | xargs) \\
       /opt/flyfish/venv/bin/python scripts/run_all_fetchers.py
"""

import asyncio
import logging
import sys
import time

# ---------------------------------------------------------------------------
# Logging — print INFO+ to stdout so results are visible
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("run_all_fetchers")


async def main():
    from conditions.scheduler import (
        job_wdfw_emergency,
        job_inciweb,
        job_nps_alerts,
        job_noaa_nwrfc,
        job_wdfw_stocking,
        job_wta,
        job_snotel,
        job_score_all_water_bodies,
    )

    jobs = [
        ("wdfw_emergency",        job_wdfw_emergency),
        ("inciweb",               job_inciweb),
        ("nps_alerts",            job_nps_alerts),
        ("noaa_nwrfc",            job_noaa_nwrfc),
        ("wdfw_stocking",         job_wdfw_stocking),
        ("wta",                   job_wta),
        ("snotel",                job_snotel),
        ("score_all_water_bodies", job_score_all_water_bodies),
    ]

    results = []
    for name, fn in jobs:
        log.info("=" * 60)
        log.info(f"Running job: {name}")
        t0 = time.monotonic()
        try:
            await fn()
            elapsed = time.monotonic() - t0
            log.info(f"DONE  {name}  ({elapsed:.1f}s)")
            results.append((name, "OK", elapsed))
        except Exception as exc:
            elapsed = time.monotonic() - t0
            # Use type+repr rather than str(exc) — SQLAlchemy connection errors
            # can embed the full DB URL (with password) in the message string.
            safe_err = f"{type(exc).__name__}: {repr(exc)[:200]}"
            log.error(f"FAILED  {name}  ({elapsed:.1f}s)  error={safe_err}")
            results.append((name, f"FAILED: {type(exc).__name__}", elapsed))

    log.info("=" * 60)
    log.info("SUMMARY")
    for name, status, elapsed in results:
        log.info(f"  {name:<30}  {elapsed:5.1f}s  {status}")

    failed = [r for r in results if r[1] != "OK"]
    if failed:
        log.error(f"{len(failed)} job(s) failed")
        sys.exit(1)
    else:
        log.info("All jobs completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
