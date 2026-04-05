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
