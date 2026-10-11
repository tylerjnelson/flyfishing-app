"""
Fetcher failures must be logged with context, never dropped.

Covers:
  - log_format.JsonFormatter: emits `extra` fields, valid JSON, tracebacks,
    and scrubs API keys (the old printf template dropped all extras).
  - conditions.fetch_errors.log_fetch_failure: status code, path-only URL.
  - every fetcher's public entry point: an upstream failure returns None and
    logs one `fetch_failed` with its source (previously it escaped raw).
  - scheduler jobs: a failed fetch yields `job_degraded`, not `job_done`.
"""

import json
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from conditions.fetch_errors import log_fetch_failure
from exceptions import ScraperStructureError
from log_format import JsonFormatter


def _format(msg="evt", level=logging.WARNING, exc_info=None, **extra):
    record = logging.getLogger("t").makeRecord(
        "t", level, __file__, 1, msg, (), exc_info, extra=extra or None
    )
    return json.loads(JsonFormatter().format(record))


def _status_error(status=403, url="https://example.gov/data?site=1&API_KEY=sekrit"):
    req = httpx.Request("GET", url)
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"Client error '{status}' for url '{url}'", request=req, response=resp)


def _fetch_failed(caplog):
    return [r for r in caplog.records if r.getMessage() == "fetch_failed"]


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

class TestJsonFormatter:
    def test_extra_fields_are_emitted(self):
        out = _format(job="wdfw_emergency", error="boom", count=3)
        assert out["job"] == "wdfw_emergency"
        assert out["error"] == "boom"
        assert out["count"] == 3
        assert out["message"] == "evt"
        assert out["level"] == "WARNING"
        assert out["request_id"] == "-"

    def test_message_with_quotes_is_valid_json(self):
        out = _format(msg='Job "job_inciweb" executed successfully')
        assert out["message"] == 'Job "job_inciweb" executed successfully'

    def test_exception_traceback_in_exc_field(self):
        try:
            raise ValueError("bad payload")
        except ValueError:
            import sys
            out = _format(exc_info=sys.exc_info())
        assert "ValueError: bad payload" in out["exc"]

    @pytest.mark.parametrize("param", ["API_KEY", "api_key", "apiKey", "token"])
    def test_secrets_redacted_in_message_and_extras(self, param):
        url = f"https://api.example.gov/x?lat=1&{param}=sekrit123&b=2"
        out = _format(msg=f"failed for url '{url}'", err=url, nested={"u": [url]})
        blob = json.dumps(out)
        assert "sekrit123" not in blob
        assert f"{param}=[REDACTED]" in out["message"]
        assert "lat=1" in out["err"]  # non-secret params survive

    def test_non_json_values_are_stringified(self):
        import uuid
        u = uuid.uuid4()
        assert _format(water_body_id=u)["water_body_id"] == str(u)


# ---------------------------------------------------------------------------
# log_fetch_failure
# ---------------------------------------------------------------------------

class TestLogFetchFailure:
    def test_http_status_error(self, caplog):
        with caplog.at_level(logging.WARNING):
            log_fetch_failure("airnow", _status_error(504), lat=1.0)
        (rec,) = _fetch_failed(caplog)
        assert rec.source == "airnow"
        assert rec.status_code == 504
        assert rec.error == "HTTP 504 Gateway Timeout"
        assert rec.url == "https://example.gov/data"  # query (and its key) dropped
        assert rec.lat == 1.0

    def test_response_body_snippet_included(self, caplog):
        req = httpx.Request("GET", "https://www.airnowapi.org/aq/x?API_KEY=k")
        body = '{"WebServiceError":[{"Message":"This web service has been retired as of October 1, 2026."}]}'
        resp = httpx.Response(410, request=req, text=body)
        with caplog.at_level(logging.WARNING):
            log_fetch_failure("airnow", httpx.HTTPStatusError("x", request=req, response=resp))
        (rec,) = _fetch_failed(caplog)
        assert rec.error == "HTTP 410 Gone"
        assert "has been retired" in rec.response_snippet

    def test_empty_message_error_falls_back_to_repr(self, caplog):
        exc = httpx.ReadTimeout("", request=httpx.Request("GET", "https://x.gov/a?API_KEY=k"))
        with caplog.at_level(logging.WARNING):
            log_fetch_failure("airnow", exc)
        (rec,) = _fetch_failed(caplog)
        assert rec.error_type == "ReadTimeout"
        assert rec.error  # never blank
        assert rec.url == "https://x.gov/a"

    def test_non_http_error(self, caplog):
        with caplog.at_level(logging.WARNING):
            log_fetch_failure("usgs", KeyError("value"), site_id="12505000")
        (rec,) = _fetch_failed(caplog)
        assert rec.error_type == "KeyError"
        assert not hasattr(rec, "url")


# ---------------------------------------------------------------------------
# Every fetcher: upstream failure → None + one fetch_failed log
# ---------------------------------------------------------------------------

_FETCHERS = [
    # (module, inner fn to break, public fn, args, source)
    # site with no NWPS mapping, so the NWPS fallback (and its network call) never triggers
    ("conditions.usgs", "_fetch", "fetch_usgs_gauge", ("00000000",), "usgs"),
    ("conditions.airnow", "_fetch", "fetch_airnow", (46.6, -120.5), "airnow"),
    ("conditions.noaa_nws", "_fetch", "fetch_noaa_nws", (46.6, -120.5), "noaa_nws"),
    ("conditions.noaa_nwrfc", "_fetch_stageflow", "fetch_noaa_nwrfc", ("PARW1",), "noaa_nwrfc"),
    ("conditions.inciweb", "_fetch", "fetch_inciweb", (), "inciweb"),
    ("conditions.nps_alerts", "_fetch", "fetch_nps_alerts", (), "nps_alerts"),
    ("conditions.snotel", "_fetch", "fetch_snotel", ("1043:WA:SNTL",), "snotel"),
    ("conditions.wdfw_stocking", "_fetch_all", "fetch_wdfw_stocking", (2026,), "wdfw_stocking"),
    ("conditions.wta_scraper", "_scrape_reports", "fetch_wta_reports", ("https://www.wta.org/x",), "wta"),
]


@pytest.mark.parametrize("module,inner,public,args,source", _FETCHERS, ids=[f[4] for f in _FETCHERS])
@pytest.mark.parametrize("exc", [
    _status_error(403),
    httpx.ConnectError("[Errno -5] No address associated with hostname"),
    ValueError("malformed payload"),
], ids=["http403", "dns", "parse"])
async def test_fetcher_failure_returns_none_and_logs(caplog, module, inner, public, args, source, exc):
    import importlib
    mod = importlib.import_module(module)
    with patch.object(mod, inner, AsyncMock(side_effect=exc)), caplog.at_level(logging.WARNING):
        result = await getattr(mod, public)(*args)
    assert result is None
    (rec,) = _fetch_failed(caplog)
    assert rec.source == source
    assert rec.error_type == type(exc).__name__


async def test_wta_structure_error_still_propagates():
    import conditions.wta_scraper as wta
    err = ScraperStructureError(source="wta", url="u", detail="d")
    with patch.object(wta, "_scrape_reports", AsyncMock(side_effect=err)):
        with pytest.raises(ScraperStructureError):
            await wta.fetch_wta_reports("https://www.wta.org/x")


# ---------------------------------------------------------------------------
# Scheduler jobs report degraded runs
# ---------------------------------------------------------------------------

async def test_inciweb_job_failure_logs_job_degraded(caplog):
    import conditions.scheduler as sched
    with patch.object(sched, "fetch_inciweb", AsyncMock(return_value=None)), caplog.at_level(logging.INFO):
        await sched.job_inciweb()
    msgs = [r.getMessage() for r in caplog.records]
    assert "job_degraded" in msgs and "job_done" not in msgs
    (rec,) = [r for r in caplog.records if r.getMessage() == "job_degraded"]
    assert rec.job == "inciweb" and rec.failed == 1


async def test_wdfw_emergency_job_logs_cause_and_keeps_rows(caplog):
    import conditions.scheduler as sched
    with patch.object(sched, "fetch_wdfw_emergency", AsyncMock(side_effect=_status_error(403))), \
         patch.object(sched, "AsyncSessionLocal") as session_factory, \
         caplog.at_level(logging.INFO):
        await sched.job_wdfw_emergency()
    session_factory.assert_not_called()  # stale rows untouched
    (rec,) = _fetch_failed(caplog)
    assert rec.source == "wdfw_emergency" and rec.status_code == 403
    assert any(r.getMessage() == "job_degraded" for r in caplog.records)
