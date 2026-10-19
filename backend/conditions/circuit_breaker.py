"""
Circuit breaker instances — one per external data source.

Parameters (all sources except emergency):
  fail_max=3       trip after 3 consecutive failures
  reset_timeout=300  wait 5 minutes before allowing a test request

WDFW emergency closures are intentionally excluded: that fetcher bypasses the
circuit entirely. On failure it logs WARNING and serves the last cached rows
unmodified — it must never silently go dark.

pybreaker only understands sync functions (and Tornado coroutines): decorating
an `async def` with a plain pybreaker.CircuitBreaker times only the creation of
the coroutine, which never fails, so the breakers never tripped (TODO-14).
AsyncCircuitBreaker awaits the coroutine inside the breaker.

What counts as a failure: anything the call raises, except
  - a 4xx response other than 408/429: the request was wrong (bad site id,
    retired endpoint), not the service down. It counts as a success, as
    pybreaker counts every excluded exception.
  - ScraperStructureError: the page layout changed; the job logs it CRITICAL.
  - cancellation (client disconnect, an outer timeout): not the service's
    doing, and must propagate as CancelledError, not CircuitBreakerError.
    Put a deadline that should count inside the guarded function.

The call that trips the breaker raises its own exception (so fetchers log the
cause as fetch_failed); later calls raise pybreaker.CircuitBreakerError until
reset_timeout passes, then one round of calls is let through (half-open).

Usage in a fetcher:
    from conditions.circuit_breaker import usgs_breaker
    import pybreaker

    @usgs_breaker
    async def _fetch(site_id: str): ...

    async def fetch_usgs_gauge(site_id: str):
        try:
            return await _fetch(site_id)
        except pybreaker.CircuitBreakerError:
            log.warning("circuit_open", extra={"source": "usgs", "site_id": site_id})
            return None  # caller falls back to last conditions_cache entry
"""

import asyncio
import functools
import inspect
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pybreaker

from exceptions import ScraperStructureError

log = logging.getLogger(__name__)


def _client_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return 400 <= code < 500 and code not in (408, 429)
    return False


class _StateLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        old = old_state.name if old_state else None
        if old == new_state.name:
            return
        level = logging.WARNING if new_state.name == pybreaker.STATE_OPEN else logging.INFO
        extra = {"source": cb.name, "from_state": old, "to_state": new_state.name}
        if new_state.name == pybreaker.STATE_OPEN:
            extra["reset_timeout_s"] = cb.reset_timeout
        log.log(level, "circuit_state_change", extra=extra)


class AsyncCircuitBreaker(pybreaker.CircuitBreaker):
    """A pybreaker.CircuitBreaker that also guards `async def` functions."""

    def __init__(self, **kwargs):
        kwargs.setdefault("exclude", [_client_error, ScraperStructureError])
        kwargs.setdefault("listeners", [_StateLogger()])
        kwargs.setdefault("throw_new_error_on_trip", False)
        super().__init__(**kwargs)

    def is_blocking(self) -> bool:
        """Open and still inside reset_timeout: a call would raise CircuitBreakerError."""
        if self.current_state != pybreaker.STATE_OPEN:
            return False
        opened_at = self._state_storage.opened_at
        return bool(opened_at) and datetime.now(timezone.utc) < opened_at + timedelta(seconds=self.reset_timeout)

    async def call_coroutine(self, func, *args, **kwargs):
        with self._lock:
            if self.is_blocking():
                raise pybreaker.CircuitBreakerError("Timeout not elapsed yet, circuit breaker still open")
            if self.current_state == pybreaker.STATE_OPEN:
                self.half_open()
        try:
            ret = await func(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # The state now, not at the start: a concurrent call may have
            # tripped or closed the breaker while this one was awaiting.
            with self._lock:
                self.state._handle_error(exc)  # re-raises exc
            raise
        with self._lock:
            self.state._handle_success()
        return ret

    def __call__(self, func):
        if not inspect.iscoroutinefunction(func):
            return super().__call__(func)

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await self.call_coroutine(func, *args, **kwargs)

        return wrapper


_CB = dict(fail_max=3, reset_timeout=300)

usgs_breaker = AsyncCircuitBreaker(**_CB, name="usgs")
noaa_nws_breaker = AsyncCircuitBreaker(**_CB, name="noaa_nws")
noaa_nwrfc_breaker = AsyncCircuitBreaker(**_CB, name="noaa_nwrfc")
wdfw_stocking_breaker = AsyncCircuitBreaker(**_CB, name="wdfw_stocking")
wta_breaker = AsyncCircuitBreaker(**_CB, name="wta")
airnow_breaker = AsyncCircuitBreaker(**_CB, name="airnow")
snotel_breaker = AsyncCircuitBreaker(**_CB, name="snotel")
inciweb_breaker = AsyncCircuitBreaker(**_CB, name="inciweb")
routing_breaker = AsyncCircuitBreaker(fail_max=2, reset_timeout=60, name="routing")
# HERE routing: tighter thresholds (fail_max=2, reset_timeout=60) per §6.3 —
# faster fallback to Haversine estimate when HERE is unavailable.
nps_breaker = AsyncCircuitBreaker(**_CB, name="nps")

ALL_BREAKERS = (
    usgs_breaker, noaa_nws_breaker, noaa_nwrfc_breaker, wdfw_stocking_breaker, wta_breaker,
    airnow_breaker, snotel_breaker, inciweb_breaker, routing_breaker, nps_breaker,
)
