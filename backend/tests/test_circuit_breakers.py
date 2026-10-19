"""
Unit tests for conditions/circuit_breaker.py

Verifies that all expected breakers exist with the correct thresholds.
"""

import pybreaker

from conditions.circuit_breaker import (
    airnow_breaker,
    inciweb_breaker,
    noaa_nwrfc_breaker,
    noaa_nws_breaker,
    nps_breaker,
    routing_breaker,
    snotel_breaker,
    usgs_breaker,
    wdfw_stocking_breaker,
    wta_breaker,
)

_STANDARD = {"fail_max": 3, "reset_timeout": 300}
_ROUTING = {"fail_max": 2, "reset_timeout": 60}

_BREAKERS = [
    ("usgs", usgs_breaker, _STANDARD),
    ("noaa_nws", noaa_nws_breaker, _STANDARD),
    ("noaa_nwrfc", noaa_nwrfc_breaker, _STANDARD),
    ("wdfw_stocking", wdfw_stocking_breaker, _STANDARD),
    ("wta", wta_breaker, _STANDARD),
    ("airnow", airnow_breaker, _STANDARD),
    ("snotel", snotel_breaker, _STANDARD),
    ("inciweb", inciweb_breaker, _STANDARD),
    ("nps", nps_breaker, _STANDARD),
    ("routing", routing_breaker, _ROUTING),
]


class TestCircuitBreakerThresholds:
    def test_all_breakers_are_circuit_breaker_instances(self):
        for name, breaker, _ in _BREAKERS:
            assert isinstance(breaker, pybreaker.CircuitBreaker), \
                f"{name}_breaker is not a CircuitBreaker"

    def test_fail_max_thresholds(self):
        for name, breaker, expected in _BREAKERS:
            assert breaker.fail_max == expected["fail_max"], \
                f"{name}_breaker fail_max: expected {expected['fail_max']}, got {breaker.fail_max}"

    def test_reset_timeout(self):
        for name, breaker, expected in _BREAKERS:
            assert breaker.reset_timeout == expected["reset_timeout"], \
                f"{name}_breaker reset_timeout: expected {expected['reset_timeout']}, got {breaker.reset_timeout}"

    def test_no_emergency_breaker(self):
        """wdfw_emergency is intentionally exempt — verify it is not exported."""
        import conditions.circuit_breaker as cb
        assert not hasattr(cb, "wdfw_emergency_breaker")

    def test_all_breakers_listed(self):
        from conditions.circuit_breaker import ALL_BREAKERS
        assert {id(b) for _, b, _ in _BREAKERS} == {id(b) for b in ALL_BREAKERS}


# ---------------------------------------------------------------------------
# TODO-14: the breakers must trip on async functions (plain pybreaker only
# timed the creation of the coroutine, so fail_counter stayed 0 forever).
# ---------------------------------------------------------------------------

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from conditions.circuit_breaker import AsyncCircuitBreaker
from exceptions import ScraperStructureError


def _status_error(status):
    req = httpx.Request("GET", "https://example.gov/x")
    return httpx.HTTPStatusError(f"{status}", request=req, response=httpx.Response(status, request=req))


def _guarded(breaker, exc=None):
    calls = []

    @breaker
    async def fetch():
        calls.append(1)
        await asyncio.sleep(0)
        if exc is not None:
            raise exc
        return "ok"

    return fetch, calls


def _expire(breaker):
    breaker._state_storage.opened_at -= timedelta(seconds=breaker.reset_timeout + 1)


class TestAsyncBreaker:
    async def test_three_failures_open_it(self):
        b = AsyncCircuitBreaker(fail_max=3, reset_timeout=300, name="t")
        fetch, calls = _guarded(b, httpx.ConnectError("down"))
        for n in (1, 2, 3):
            with pytest.raises(httpx.ConnectError):  # the tripping call keeps its own cause
                await fetch()
            assert b.fail_counter == n
        assert b.current_state == "open"
        assert b.is_blocking()
        with pytest.raises(pybreaker.CircuitBreakerError):
            await fetch()
        assert len(calls) == 3  # the open breaker never called upstream

    async def test_success_resets_the_count(self):
        b = AsyncCircuitBreaker(fail_max=3, reset_timeout=300, name="t")
        bad, _ = _guarded(b, httpx.ReadTimeout("slow"))
        good, _ = _guarded(b)
        for _ in range(2):
            with pytest.raises(httpx.ReadTimeout):
                await bad()
        assert await good() == "ok"
        assert b.fail_counter == 0
        with pytest.raises(httpx.ReadTimeout):
            await bad()
        assert b.current_state == "closed"

    async def test_half_open_after_timeout(self):
        b = AsyncCircuitBreaker(fail_max=3, reset_timeout=300, name="t")
        bad, bad_calls = _guarded(b, httpx.ConnectError("down"))
        good, _ = _guarded(b)
        for _ in range(3):
            with pytest.raises(httpx.ConnectError):
                await bad()
        _expire(b)
        assert not b.is_blocking()
        with pytest.raises(httpx.ConnectError):  # trial call fails: open again at once
            await bad()
        assert len(bad_calls) == 4
        assert b.is_blocking()
        _expire(b)
        assert await good() == "ok"  # trial call succeeds: closed
        assert b.current_state == "closed" and b.fail_counter == 0

    @pytest.mark.parametrize("status,counts", [(404, False), (403, False), (410, False),
                                               (408, True), (429, True), (500, True), (503, True)])
    async def test_client_errors_do_not_count(self, status, counts):
        b = AsyncCircuitBreaker(fail_max=3, reset_timeout=300, name="t")
        fetch, _ = _guarded(b, _status_error(status))
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):
                await fetch()
        assert (b.current_state == "open") is counts

    async def test_scraper_structure_error_does_not_count(self):
        b = AsyncCircuitBreaker(fail_max=3, reset_timeout=300, name="t")
        fetch, _ = _guarded(b, ScraperStructureError("wta", "https://wta.org", "layout"))
        for _ in range(3):
            with pytest.raises(ScraperStructureError):
                await fetch()
        assert b.current_state == "closed"

    async def test_cancellation_propagates_and_does_not_count(self):
        b = AsyncCircuitBreaker(fail_max=1, reset_timeout=300, name="t")

        @b
        async def hang():
            await asyncio.sleep(10)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(hang(), timeout=0.01)
        assert b.current_state == "closed" and b.fail_counter == 0

    def test_sync_functions_still_guarded(self):
        b = AsyncCircuitBreaker(fail_max=2, reset_timeout=300, name="t")

        @b
        def boom():
            raise ConnectionError("down")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                boom()
        with pytest.raises(pybreaker.CircuitBreakerError):
            boom()

    async def test_state_change_logged(self, caplog):
        b = AsyncCircuitBreaker(fail_max=1, reset_timeout=300, name="t")
        fetch, _ = _guarded(b, httpx.ConnectError("down"))
        with caplog.at_level("INFO", logger="conditions.circuit_breaker"):
            with pytest.raises(httpx.ConnectError):
                await fetch()
        recs = [r for r in caplog.records if r.getMessage() == "circuit_state_change"]
        assert [(r.source, r.from_state, r.to_state, r.levelname) for r in recs] == \
            [("t", "closed", "open", "WARNING")]


class TestFetchersUseIt:
    """The real decorated fetchers trip (all upstream calls mocked; nothing live)."""

    async def test_usgs_opens_after_three_failures(self, caplog):
        from conditions import usgs
        get = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch.object(httpx.AsyncClient, "get", get), caplog.at_level("INFO"):
            for _ in range(4):
                assert await usgs._fetch_usgs_reading("12345678") is None
        assert get.await_count == 3
        msgs = [r.getMessage() for r in caplog.records]
        assert msgs.count("fetch_failed") == 3
        assert msgs.count("circuit_open") == 1
        assert usgs_breaker.current_state == "open"

    @pytest.mark.parametrize("decorated", [
        "conditions.usgs._fetch", "conditions.noaa_nws._fetch", "conditions.noaa_nwrfc._fetch_stageflow",
        "conditions.wdfw_stocking._fetch_all", "conditions.wta_scraper._scrape_reports",
        "conditions.airnow._fetch", "conditions.snotel._fetch", "conditions.inciweb._fetch",
        "conditions.nps_alerts._fetch", "conditions.routing._here_request",
    ])
    def test_every_fetcher_is_wrapped_as_a_coroutine(self, decorated):
        import importlib
        import inspect
        mod, fn = decorated.rsplit(".", 1)
        f = getattr(importlib.import_module(mod), fn)
        assert inspect.iscoroutinefunction(f), f"{decorated} is not awaited inside its breaker"
        assert inspect.iscoroutinefunction(f.__wrapped__)


class TestRoutingBreaker:
    """HERE is mocked at httpx; no live calls (and FLYFISH_DISABLE_HERE stays irrelevant)."""

    @pytest.fixture(autouse=True)
    def _here_enabled(self):
        from config import settings
        with patch.object(settings, "here_disabled", False):
            yield

    async def test_two_failures_then_haversine_without_calling_here(self):
        from conditions import routing
        get = AsyncMock(side_effect=_status_error(503))
        with patch.object(httpx.AsyncClient, "get", get):
            results = [await routing.get_drive_time(47.6, -122.3, 48.0, -121.5, None) for _ in range(4)]
        assert get.await_count == 2
        assert all(fallback for _, fallback in results)
        assert routing_breaker.is_blocking()

    async def test_slow_here_counts_as_a_failure(self):
        from conditions import routing

        async def slow(*a, **k):
            await asyncio.sleep(1)

        with patch.object(httpx.AsyncClient, "get", slow), patch.object(routing, "_DEADLINE_S", 0.01):
            for _ in range(2):
                minutes, fallback = await routing.get_drive_time(47.6, -122.3, 48.0, -121.5, None)
                assert fallback and minutes > 0
        assert routing_breaker.current_state == "open"
