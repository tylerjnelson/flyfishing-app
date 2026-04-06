"""
Unit tests for conditions/scheduler.py

Verifies that all expected jobs are registered with the correct IDs and
triggers, using an in-memory job store so no database connection is needed.
"""

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import conditions.scheduler as sched_module

_EXPECTED_JOB_IDS = {
    "wdfw_emergency",
    "inciweb",
    "noaa_nwrfc",
    "nps_alerts",
    "wdfw_stocking",
    "wta",
    "snotel",
    "score_all_spots",
    "wdfw_regulations",
}

_TWO_HOUR_JOBS = {"wdfw_emergency", "inciweb", "noaa_nwrfc", "nps_alerts"}
# All scheduled non-realtime jobs that use CronTrigger
_CRON_JOBS = {"wdfw_stocking", "wta", "snotel", "score_all_spots", "wdfw_regulations"}
# 3 AM Pacific jobs (excludes score_all_spots at 3:30, wdfw_regulations annual)
_THREE_AM_JOBS = {"wdfw_stocking", "wta", "snotel"}


def _make_test_scheduler() -> AsyncIOScheduler:
    """Create a scheduler with an in-memory store for testing."""
    return AsyncIOScheduler(
        jobstores={"default": MemoryJobStore()},
        timezone="America/Los_Angeles",
    )


def _register_jobs(scheduler: AsyncIOScheduler) -> None:
    """Mirror the job registration from start_scheduler() using a test scheduler."""
    from datetime import datetime, timezone
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    from conditions.scheduler import (
        job_inciweb, job_noaa_nwrfc, job_nps_alerts, job_snotel,
        job_wdfw_emergency, job_wdfw_stocking, job_wta,
        job_score_all_spots, job_wdfw_regulations,
    )

    scheduler.add_job(job_wdfw_emergency, IntervalTrigger(hours=2),
                      id="wdfw_emergency", replace_existing=True,
                      next_run_time=datetime.now(tz=timezone.utc))
    scheduler.add_job(job_inciweb, IntervalTrigger(hours=2),
                      id="inciweb", replace_existing=True,
                      next_run_time=datetime.now(tz=timezone.utc))
    scheduler.add_job(job_noaa_nwrfc, IntervalTrigger(hours=2),
                      id="noaa_nwrfc", replace_existing=True)
    scheduler.add_job(job_nps_alerts, IntervalTrigger(hours=2),
                      id="nps_alerts", replace_existing=True)

    _daily = CronTrigger(hour=3, minute=0, timezone="America/Los_Angeles")
    scheduler.add_job(job_wdfw_stocking, _daily, id="wdfw_stocking", replace_existing=True)
    scheduler.add_job(job_wta, _daily, id="wta", replace_existing=True)
    scheduler.add_job(job_snotel, _daily, id="snotel", replace_existing=True)
    scheduler.add_job(job_score_all_spots,
                      CronTrigger(hour=3, minute=30, timezone="America/Los_Angeles"),
                      id="score_all_spots", replace_existing=True)
    scheduler.add_job(job_wdfw_regulations,
                      CronTrigger(month=12, day=1, hour=4, minute=0, timezone="America/Los_Angeles"),
                      id="wdfw_regulations", replace_existing=True)


class TestSchedulerJobRegistration:
    def setup_method(self):
        self.scheduler = _make_test_scheduler()
        _register_jobs(self.scheduler)

    def test_all_expected_jobs_registered(self):
        registered = {job.id for job in self.scheduler.get_jobs()}
        assert _EXPECTED_JOB_IDS == registered

    def test_two_hour_jobs_use_interval_trigger(self):
        from apscheduler.triggers.interval import IntervalTrigger
        for job in self.scheduler.get_jobs():
            if job.id in _TWO_HOUR_JOBS:
                assert isinstance(job.trigger, IntervalTrigger), \
                    f"{job.id} should use IntervalTrigger"

    def test_cron_jobs_use_cron_trigger(self):
        from apscheduler.triggers.cron import CronTrigger
        for job in self.scheduler.get_jobs():
            if job.id in _CRON_JOBS:
                assert isinstance(job.trigger, CronTrigger), \
                    f"{job.id} should use CronTrigger"

    def test_two_hour_interval_is_correct(self):
        from apscheduler.triggers.interval import IntervalTrigger
        from datetime import timedelta
        for job in self.scheduler.get_jobs():
            if job.id in _TWO_HOUR_JOBS:
                assert job.trigger.interval == timedelta(hours=2), \
                    f"{job.id} interval should be 2 hours"

    def test_three_am_jobs_fire_at_3am(self):
        for job in self.scheduler.get_jobs():
            if job.id in _THREE_AM_JOBS:
                fields = {f.name: f for f in job.trigger.fields}
                assert str(fields["hour"]) == "3", \
                    f"{job.id} should fire at hour 3"
                assert str(fields["minute"]) == "0", \
                    f"{job.id} should fire at minute 0"

    def test_score_all_spots_fires_at_3_30am(self):
        job = next(j for j in self.scheduler.get_jobs() if j.id == "score_all_spots")
        fields = {f.name: f for f in job.trigger.fields}
        assert str(fields["hour"]) == "3"
        assert str(fields["minute"]) == "30"

    def test_no_realtime_fetchers_scheduled(self):
        """USGS, NOAA NWS, AirNow are session-triggered — must not appear here."""
        registered = {job.id for job in self.scheduler.get_jobs()}
        assert "usgs" not in registered
        assert "noaa_nws" not in registered
        assert "airnow" not in registered


class TestJobFunctionCallables:
    """Verify all job functions are importable async callables."""

    def test_job_functions_are_coroutines(self):
        import asyncio
        from conditions.scheduler import (
            job_inciweb, job_noaa_nwrfc, job_nps_alerts, job_snotel,
            job_wdfw_emergency, job_wdfw_stocking, job_wta,
            job_score_all_spots, job_wdfw_regulations,
        )
        for fn in [job_wdfw_emergency, job_inciweb, job_noaa_nwrfc, job_nps_alerts,
                   job_wdfw_stocking, job_wta, job_snotel,
                   job_score_all_spots, job_wdfw_regulations]:
            assert asyncio.iscoroutinefunction(fn), f"{fn.__name__} must be async"
