"""
Logs must be accurate (TODO-17, 2026-10-12 audit):
  - unhandled request errors reach app.log as `request_failed` with a request_id
    (they used to go only to gunicorn's stderr), and every request logs one
    `request_done` with status and timing, path only (no query string);
  - every scheduler run ends in exactly one outcome line with duration_s:
    job_done / job_degraded, or job_failed (with traceback) if the job raised;
  - gauges with no NWPS forecast product don't degrade the forecast job;
  - malformed tool-call arguments from the model are logged;
  - chatty library loggers (httpx, apscheduler) are held at WARNING.
"""

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import main


def _msgs(caplog, name):
    return [r for r in caplog.records if r.getMessage() == name]


@pytest.fixture(scope="module")
def client_app():
    async def boom():
        raise RuntimeError("kaboom")

    async def fine():
        return {"ok": True}

    main.app.add_api_route("/__test/boom", boom)
    main.app.add_api_route("/__test/fine", fine)
    return main.app


async def _get(app, path, **kw):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path, **kw)


async def test_unhandled_error_logged_with_request_id(client_app, caplog):
    with caplog.at_level(logging.INFO):
        resp = await _get(client_app, "/__test/boom", headers={"X-Request-ID": "rid-123"})
    assert resp.status_code == 500
    assert resp.headers["X-Request-ID"] == "rid-123"
    (failed,) = _msgs(caplog, "request_failed")
    assert failed.path == "/__test/boom" and failed.method == "GET"
    assert failed.exc_info and "kaboom" in str(failed.exc_info[1])
    (done,) = _msgs(caplog, "request_done")
    assert done.status == 500 and done.levelno == logging.WARNING


async def test_request_done_logs_path_without_query(client_app, caplog):
    with caplog.at_level(logging.INFO):
        resp = await _get(client_app, "/__test/fine?token=sekrit")
    assert resp.status_code == 200
    (done,) = _msgs(caplog, "request_done")
    assert done.path == "/__test/fine"
    assert done.status == 200 and done.levelno == logging.INFO
    assert isinstance(done.response_ms, int)
    assert "sekrit" not in str(done.__dict__)


def test_library_loggers_quieted():
    for name in ("httpx", "httpcore", "apscheduler"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


# ---------------------------------------------------------------------------
# Scheduler outcome lines
# ---------------------------------------------------------------------------

def _fake_session_factory():
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    @asynccontextmanager
    async def _cm(*_a, **_k):
        yield session

    session = MagicMock()
    session.begin = _cm
    return _cm


async def test_job_done_carries_duration(caplog):
    import conditions.scheduler as sched
    with patch.object(sched, "fetch_inciweb", AsyncMock(return_value={"active_wa_fires": []})), \
         patch.object(sched, "AsyncSessionLocal", _fake_session_factory()), \
         patch.object(sched, "write_conditions_cache", AsyncMock()), \
         caplog.at_level(logging.INFO):
        await sched.job_inciweb()
    assert len(_msgs(caplog, "job_start")) == 1
    (done,) = _msgs(caplog, "job_done")
    assert done.job == "inciweb" and isinstance(done.duration_s, float)


async def test_job_that_raises_logs_job_failed(caplog):
    import conditions.scheduler as sched
    with patch.object(sched, "fetch_inciweb", AsyncMock(return_value={"active_wa_fires": []})), \
         patch.object(sched, "AsyncSessionLocal", side_effect=RuntimeError("db down")), \
         caplog.at_level(logging.INFO):
        await sched.job_inciweb()  # must not raise into APScheduler
    (failed,) = _msgs(caplog, "job_failed")
    assert failed.job == "inciweb" and failed.error_type == "RuntimeError"
    assert failed.levelno == logging.ERROR and failed.exc_info
    assert not _msgs(caplog, "job_done") and not _msgs(caplog, "job_degraded")


async def test_wdfw_structure_failure_ends_with_job_degraded(caplog):
    import conditions.scheduler as sched
    from exceptions import ScraperStructureError
    err = ScraperStructureError(source="wdfw_emergency", url="u", detail="d")
    with patch.object(sched, "fetch_wdfw_emergency", AsyncMock(side_effect=err)), caplog.at_level(logging.INFO):
        await sched.job_wdfw_emergency()
    assert _msgs(caplog, "scraper_structure_failure")
    (deg,) = _msgs(caplog, "job_degraded")
    assert deg.job == "wdfw_emergency"


async def test_wdfw_empty_rules_ends_with_job_degraded(caplog):
    import conditions.scheduler as sched
    with patch.object(sched, "fetch_wdfw_emergency", AsyncMock(return_value=[])), caplog.at_level(logging.INFO):
        await sched.job_wdfw_emergency()
    (deg,) = _msgs(caplog, "job_degraded")
    assert deg.job == "wdfw_emergency"


async def test_nwrfc_skips_gauges_without_forecast_product(caplog):
    import conditions.scheduler as sched
    from types import SimpleNamespace
    import uuid
    wbs = [SimpleNamespace(id=uuid.uuid4(), name="Queets River", usgs_site_ids=["12040500"])]  # QUEW1
    fetch = AsyncMock()
    with patch.object(sched, "_water_bodies_with_usgs_ids", AsyncMock(return_value=wbs)), \
         patch.object(sched, "fetch_noaa_nwrfc", fetch), caplog.at_level(logging.INFO):
        await sched.job_noaa_nwrfc()
    fetch.assert_not_called()
    (done,) = _msgs(caplog, "job_done")
    assert done.no_forecast_product == ["QUEW1"]
    assert not _msgs(caplog, "job_stale_fallback")


# ---------------------------------------------------------------------------
# Model tool-call arguments
# ---------------------------------------------------------------------------

def test_malformed_tool_args_logged(caplog):
    from llm.llamacpp import _assemble_streamed_tool_calls, _parse_tool_calls
    with caplog.at_level(logging.WARNING):
        calls = _parse_tool_calls({"tool_calls": [{"function": {"name": "get_spot", "arguments": "{spot_id: 1"}}]})
        _assemble_streamed_tool_calls({0: {"name": "get_spot", "arguments": '{"spot_id": '}})
    assert calls[0]["function"]["arguments"] == {}
    recs = _msgs(caplog, "tool_args_malformed")
    assert [r.tool for r in recs] == ["get_spot", "get_spot"]


def test_valid_tool_args_not_logged(caplog):
    from llm.llamacpp import _parse_tool_calls
    with caplog.at_level(logging.WARNING):
        _parse_tool_calls({"tool_calls": [{"function": {"name": "get_spot", "arguments": '{"spot_id": "x"}'}}]})
    assert not _msgs(caplog, "tool_args_malformed")
