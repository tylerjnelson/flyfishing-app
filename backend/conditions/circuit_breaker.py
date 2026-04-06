"""
Circuit breaker instances — one per external data source.

Parameters (all sources except emergency):
  fail_max=3       trip after 3 consecutive failures
  reset_timeout=300  wait 5 minutes before allowing a test request

WDFW emergency closures are intentionally excluded: that fetcher bypasses the
circuit entirely. On failure it logs WARNING and serves the last cached rows
unmodified — it must never silently go dark.

Usage in a fetcher:
    from conditions.circuit_breaker import usgs_breaker
    import pybreaker

    @usgs_breaker
    async def _fetch(site_id: str): ...

    async def fetch_usgs_gauge(site_id: str):
        try:
            return await _fetch(site_id)
        except pybreaker.CircuitBreakerError:
            log.warning("circuit_open", source="usgs", site_id=site_id)
            return None  # caller falls back to last conditions_cache entry
"""

import pybreaker

_CB = dict(fail_max=3, reset_timeout=300)

usgs_breaker = pybreaker.CircuitBreaker(**_CB, name="usgs")
noaa_nws_breaker = pybreaker.CircuitBreaker(**_CB, name="noaa_nws")
noaa_nwrfc_breaker = pybreaker.CircuitBreaker(**_CB, name="noaa_nwrfc")
wdfw_stocking_breaker = pybreaker.CircuitBreaker(**_CB, name="wdfw_stocking")
wta_breaker = pybreaker.CircuitBreaker(**_CB, name="wta")
airnow_breaker = pybreaker.CircuitBreaker(**_CB, name="airnow")
snotel_breaker = pybreaker.CircuitBreaker(**_CB, name="snotel")
inciweb_breaker = pybreaker.CircuitBreaker(**_CB, name="inciweb")
routing_breaker = pybreaker.CircuitBreaker(fail_max=2, reset_timeout=60, name="routing")
# HERE routing: tighter thresholds (fail_max=2, reset_timeout=60) per §6.3 —
# faster fallback to Haversine estimate when HERE is unavailable.
nps_breaker = pybreaker.CircuitBreaker(**_CB, name="nps")
