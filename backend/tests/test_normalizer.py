"""
Unit tests for conditions/normalizer.py

All functions under test are pure (no I/O), so no mocking is needed.
"""

from datetime import datetime, timezone, timedelta

import pytest

from conditions.normalizer import (
    INTERVAL_REALTIME,
    INTERVAL_SCHEDULED,
    _round_down,
    compute_conditions_hash,
    normalize_airnow,
    normalize_inciweb,
    normalize_snotel,
    normalize_usgs,
    normalize_wdfw_stocking,
)


# ---------------------------------------------------------------------------
# _round_down
# ---------------------------------------------------------------------------

class TestRoundDown:
    def test_already_on_boundary(self):
        dt = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        result = _round_down(dt, 15)
        assert result == dt

    def test_rounds_down_to_previous_boundary(self):
        dt = datetime(2026, 4, 5, 12, 14, 59, tzinfo=timezone.utc)
        result = _round_down(dt, 15)
        assert result == datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_120_minute_interval(self):
        dt = datetime(2026, 4, 5, 3, 45, 0, tzinfo=timezone.utc)
        result = _round_down(dt, 120)
        assert result == datetime(2026, 4, 5, 2, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# compute_conditions_hash
# ---------------------------------------------------------------------------

class TestComputeConditionsHash:
    def _hash(self, **kwargs):
        defaults = dict(
            cfs=500.0,
            temp_f=52.0,
            turbidity_fnu=None,
            fetched_at=datetime(2026, 4, 5, 14, 7, 0, tzinfo=timezone.utc),
            interval_minutes=INTERVAL_REALTIME,
        )
        defaults.update(kwargs)
        return compute_conditions_hash(**defaults)

    def test_same_inputs_same_hash(self):
        assert self._hash() == self._hash()

    def test_different_cfs_different_hash(self):
        assert self._hash(cfs=500.0) != self._hash(cfs=501.0)

    def test_different_temp_different_hash(self):
        assert self._hash(temp_f=52.0) != self._hash(temp_f=53.0)

    def test_timestamps_within_same_interval_produce_same_hash(self):
        # Both fall within the same 15-minute boundary (12:00–12:15)
        t1 = datetime(2026, 4, 5, 12, 2, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 4, 5, 12, 14, 0, tzinfo=timezone.utc)
        h1 = compute_conditions_hash(cfs=400.0, temp_f=50.0, turbidity_fnu=None,
                                     fetched_at=t1, interval_minutes=INTERVAL_REALTIME)
        h2 = compute_conditions_hash(cfs=400.0, temp_f=50.0, turbidity_fnu=None,
                                     fetched_at=t2, interval_minutes=INTERVAL_REALTIME)
        assert h1 == h2

    def test_timestamps_crossing_interval_boundary_produce_different_hash(self):
        t1 = datetime(2026, 4, 5, 12, 14, 0, tzinfo=timezone.utc)  # boundary: 12:00
        t2 = datetime(2026, 4, 5, 12, 15, 0, tzinfo=timezone.utc)  # boundary: 12:15
        h1 = compute_conditions_hash(cfs=400.0, temp_f=50.0, turbidity_fnu=None,
                                     fetched_at=t1, interval_minutes=INTERVAL_REALTIME)
        h2 = compute_conditions_hash(cfs=400.0, temp_f=50.0, turbidity_fnu=None,
                                     fetched_at=t2, interval_minutes=INTERVAL_REALTIME)
        assert h1 != h2

    def test_returns_32_char_md5_hex(self):
        result = self._hash()
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_none_values_produce_stable_hash(self):
        h = compute_conditions_hash(
            cfs=None, temp_f=None, turbidity_fnu=None,
            fetched_at=datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc),
            interval_minutes=INTERVAL_SCHEDULED,
        )
        assert len(h) == 32


# ---------------------------------------------------------------------------
# normalize_usgs
# ---------------------------------------------------------------------------

class TestNormalizeUsgs:
    @staticmethod
    def _ts(code, values, age=timedelta(minutes=30), step=timedelta(minutes=15), stat="00011"):
        """OGC `continuous` features for one parameter; `values` oldest→newest, newest `age` old."""
        newest = datetime.now(tz=timezone.utc) - age
        n = len(values)
        return [
            {"type": "Feature", "properties": {
                "monitoring_location_id": "USGS-12134500", "parameter_code": code,
                "statistic_id": stat, "time": (newest - step * (n - 1 - i)).isoformat(),
                "value": None if v is None else str(v),
            }}
            for i, v in enumerate(values)
        ]

    @staticmethod
    def _raw(*series):
        return {"type": "FeatureCollection", "features": [f for s in series for f in s]}

    def _make_raw(self, cfs=450.0, gauge_ht=3.5, temp_c=10.0):
        ts = self._ts
        return self._raw(ts("00060", [cfs]), ts("00065", [gauge_ht]), ts("00010", [temp_c]))

    def test_null_value_dropped(self):
        """Klickitat 14113000 reports water temp as null (-999999 on the legacy API)."""
        raw = self._raw(self._ts("00060", [610]), self._ts("00010", [None]))
        result = normalize_usgs(raw, datetime.now(tz=timezone.utc))
        assert result["cfs"] == 610.0
        assert result["temp_f"] is None
        assert result["dropped"] == {"00010": "no_data_sentinel"}

    def test_legacy_sentinel_still_dropped(self):
        raw = self._raw(self._ts("00010", [-999999]))
        assert normalize_usgs(raw, datetime.now(tz=timezone.utc))["dropped"] == {"00010": "no_data_sentinel"}

    def test_stale_reading_dropped(self):
        """Yakima 12500450's latest water temp is from 2023 (still served by both APIs)."""
        raw = self._raw(self._ts("00060", [725], age=timedelta(days=5000)))
        result = normalize_usgs(raw, datetime.now(tz=timezone.utc))
        assert result["cfs"] is None
        assert result["dropped"]["00060"].startswith("stale:")

    def test_reading_just_inside_window_kept(self):
        raw = self._raw(self._ts("00060", [300], age=timedelta(hours=23)))
        assert normalize_usgs(raw, datetime.now(tz=timezone.utc))["cfs"] == 300.0

    def test_trend_ignores_null_values(self):
        raw = self._raw(self._ts("00060", [100, None, 100, 100, 100, 120]))
        result = normalize_usgs(raw, datetime.now(tz=timezone.utc))
        assert result["cfs"] == 120.0
        assert result["trend"] == "rising"

    def test_trend_from_four_readings(self):
        """USGS posts ~1h late in hourly batches; a 2h window held only 4 readings,
        which the old count-based trend (needed 5) always reported as None."""
        raw = self._raw(self._ts("00060", [100, 100, 100, 90]))
        assert normalize_usgs(raw, datetime.now(tz=timezone.utc))["trend"] == "dropping"

    def test_no_trend_without_reading_an_hour_back(self):
        raw = self._raw(self._ts("00060", [100, 120]))
        assert normalize_usgs(raw, datetime.now(tz=timezone.utc))["trend"] is None

    def test_unordered_features_sorted_by_time(self):
        """The API doesn't guarantee result order."""
        feats = self._ts("00060", [100, 100, 100, 100, 130])
        raw = self._raw(list(reversed(feats)))
        result = normalize_usgs(raw, datetime.now(tz=timezone.utc))
        assert result["cfs"] == 130.0
        assert result["trend"] == "rising"
        assert result["observed_at"] == feats[-1]["properties"]["time"]

    def test_non_instantaneous_statistic_ignored(self):
        raw = self._raw(self._ts("00060", [999], stat="00003"), self._ts("00060", [450]))
        assert normalize_usgs(raw, datetime.now(tz=timezone.utc))["cfs"] == 450.0

    def test_cfs_extracted(self):
        result = normalize_usgs(self._make_raw(cfs=450.0), datetime.now(tz=timezone.utc))
        assert result["cfs"] == 450.0

    def test_temp_converted_c_to_f(self):
        result = normalize_usgs(self._make_raw(temp_c=10.0), datetime.now(tz=timezone.utc))
        assert result["temp_f"] == pytest.approx(50.0, abs=0.1)

    def test_source_field(self):
        result = normalize_usgs(self._make_raw(), datetime.now(tz=timezone.utc))
        assert result["source"] == "usgs"

    def test_stale_defaults_false(self):
        result = normalize_usgs(self._make_raw(), datetime.now(tz=timezone.utc))
        assert result["stale"] is False

    def test_missing_param_returns_none(self):
        raw = {"type": "FeatureCollection", "features": []}
        result = normalize_usgs(raw, datetime.now(tz=timezone.utc))
        assert result["cfs"] is None
        assert result["temp_f"] is None


# ---------------------------------------------------------------------------
# normalize_airnow
# ---------------------------------------------------------------------------

class TestNormalizeAirnow:
    def test_picks_highest_aqi(self):
        raw = [
            {"AQI": 42, "ParameterName": "PM2.5", "Category": {"Name": "Good"}},
            {"AQI": 85, "ParameterName": "O3", "Category": {"Name": "Moderate"}},
        ]
        result = normalize_airnow(raw, datetime.now(tz=timezone.utc))
        assert result["aqi"] == 85
        assert result["pollutant"] == "O3"

    def test_empty_list(self):
        result = normalize_airnow([], datetime.now(tz=timezone.utc))
        assert result["aqi"] is None

    def test_source_field(self):
        result = normalize_airnow([], datetime.now(tz=timezone.utc))
        assert result["source"] == "airnow"


# ---------------------------------------------------------------------------
# normalize_inciweb
# ---------------------------------------------------------------------------

class TestNormalizeInciweb:
    def _incidents(self):
        return [
            {"id": "1", "name": "TestFire", "incident_type": "Wildfire", "state": "WA",
             "latitude": 47.5, "longitude": -120.5, "modified": "2026-04-01"},
            {"id": "2", "name": "OrFire", "incident_type": "Fire", "state": "OR",
             "latitude": 44.0, "longitude": -122.0, "modified": "2026-04-01"},
            {"id": "3", "name": "WAFlood", "incident_type": "Flood", "state": "WA",
             "latitude": 47.0, "longitude": -121.0, "modified": "2026-04-01"},
        ]

    def test_filters_to_wa_fires_only(self):
        result = normalize_inciweb(self._incidents(), datetime.now(tz=timezone.utc))
        fires = result["active_wa_fires"]
        assert len(fires) == 1
        assert fires[0]["name"] == "TestFire"

    def test_source_field(self):
        result = normalize_inciweb([], datetime.now(tz=timezone.utc))
        assert result["source"] == "inciweb"

    def test_empty_list(self):
        result = normalize_inciweb([], datetime.now(tz=timezone.utc))
        assert result["active_wa_fires"] == []


# ---------------------------------------------------------------------------
# normalize_wdfw_stocking
# ---------------------------------------------------------------------------

class TestNormalizeWdfwStocking:
    def test_basic_record(self):
        # Uses dataset 6fex-3r7d field names
        raw = [{"release_location": "Lake X", "county": "King",
                "release_start_date": "2026-03-01T00:00:00.000", "species": "Rainbow Trout",
                "number_released": "500", "lifestage": "Legals", "geo_code": "L1234",
                ":id": "row-abc123"}]
        result = normalize_wdfw_stocking(raw, datetime.now(tz=timezone.utc))
        assert len(result) == 1
        assert result[0]["water_name"] == "Lake X"
        assert result[0]["count"] == 500
        assert result[0]["source_record_id"].endswith("-1")

    def test_record_id_stable_across_republish(self):
        """Socrata :id changes on every republish; our key must not (TODO-12)."""
        rec = {"release_location": "Lake X", "release_start_date": "2026-03-01T00:00:00.000",
               "species": "Rainbow Trout", "number_released": "500"}
        a = normalize_wdfw_stocking([{**rec, ":id": "row-aaa"}], datetime.now(tz=timezone.utc))
        b = normalize_wdfw_stocking([{**rec, ":id": "row-bbb"}], datetime.now(tz=timezone.utc))
        assert a[0]["source_record_id"] == b[0]["source_record_id"]

    def test_identical_records_get_distinct_ids(self):
        rec = {"release_location": "Lake X", "species": "Rainbow Trout", "number_released": "500"}
        result = normalize_wdfw_stocking([dict(rec), dict(rec), {**rec, "number_released": "501"}],
                                         datetime.now(tz=timezone.utc))
        ids = [r["source_record_id"] for r in result]
        assert len(set(ids)) == 3
        assert ids[0].rsplit("-", 1)[0] == ids[1].rsplit("-", 1)[0]
        assert [i.rsplit("-", 1)[1] for i in ids[:2]] == ["1", "2"]

    def test_missing_count_returns_none(self):
        raw = [{"release_location": "Lake Y", "release_start_date": "2026-03-01T00:00:00.000",
                "species": "Cutthroat", "geo_code": "L5678"}]
        result = normalize_wdfw_stocking(raw, datetime.now(tz=timezone.utc))
        assert result[0]["count"] is None

    def test_empty_list(self):
        assert normalize_wdfw_stocking([], datetime.now(tz=timezone.utc)) == []


def test_usgs_uses_water_data_api():
    """Legacy waterservices.usgs.gov is being decommissioned (early 2027)."""
    from urllib.parse import urlparse
    from conditions.usgs import _BASE_URL
    url = urlparse(_BASE_URL)
    assert url.hostname == "api.waterdata.usgs.gov"
    assert url.path == "/ogcapi/v1/collections/continuous/items"


async def _capture_usgs_request(monkeypatch, api_key=""):
    import httpx
    import conditions.usgs as usgs

    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        seen["headers"] = request.headers
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(usgs.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    monkeypatch.setattr(usgs.settings, "usgs_api_key", api_key)
    await usgs._fetch("12134500")
    return seen


async def test_usgs_request_params(monkeypatch):
    seen = await _capture_usgs_request(monkeypatch)
    assert seen["params"]["monitoring_location_id"] == "USGS-12134500"
    assert seen["params"]["parameter_code"] == "00060,00065,00010"
    assert seen["params"]["time"] == "PT3H"  # room for a reading ~1h before the latest
    assert int(seen["params"]["limit"]) >= 36
    assert "x-api-key" not in seen["headers"]


async def test_usgs_api_key_sent_as_header_not_query(monkeypatch):
    seen = await _capture_usgs_request(monkeypatch, api_key="k123")
    assert seen["headers"]["x-api-key"] == "k123"
    assert "api_key" not in seen["params"]


# ---------------------------------------------------------------------------
# NWPS observed-flow fallback for dead USGS gauges (2026-10-11 audit)
# ---------------------------------------------------------------------------

_STAGE_FLOW = {"primaryName": "Stage", "primaryUnits": "ft", "secondaryName": "Flow", "secondaryUnits": "kcfs"}
# AUBW1 / TOLW1 / NISW1 list discharge first (verified 2026-10-11)
_DISCHARGE_STAGE = {"primaryName": "River Discharge", "primaryUnits": "kcfs",
                    "secondaryName": "Stage", "secondaryUnits": "ft"}


def _nwps(points, meta=_STAGE_FLOW, series="observed"):
    """points: (time, stage_ft, flow_kcfs), laid out per `meta`'s field order."""
    stage_first = meta["primaryName"] == "Stage"
    return {series: {**meta, "data": [
        {"validTime": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "primary": stage if stage_first else kcfs, "secondary": kcfs if stage_first else stage}
        for t, stage, kcfs in points
    ]}}


class TestNormalizeNwpsObserved:
    def test_latest_flow_in_cfs_with_trend(self):
        from conditions.normalizer import normalize_nwps_observed
        now = datetime.now(tz=timezone.utc)
        raw = _nwps([(now - timedelta(minutes=75), 14.5, 1.60),
                     (now - timedelta(minutes=60), 14.6, 1.60),
                     (now - timedelta(minutes=10), 14.7, 1.77)])
        r = normalize_nwps_observed(raw, "NACW1", now)
        assert r["cfs"] == 1770.0 and r["gauge_height_ft"] == 14.7
        assert r["trend"] == "rising"
        assert r["provider"] == "noaa_nwps_observed" and r["nwps_gauge_id"] == "NACW1"

    def test_negative_missing_markers_skipped(self):
        from conditions.normalizer import normalize_nwps_observed
        now = datetime.now(tz=timezone.utc)
        raw = _nwps([(now - timedelta(minutes=30), 0.24, 0.495), (now - timedelta(minutes=15), -999, -999)])
        assert normalize_nwps_observed(raw, "ARLW1", now)["cfs"] == 495.0

    def test_discharge_first_field_order(self):
        """Green (AUBW1): primary is discharge, secondary is stage (57.25 ft)."""
        from conditions.normalizer import normalize_nwps_observed
        now = datetime.now(tz=timezone.utc)
        raw = _nwps([(now - timedelta(minutes=15), 57.25, 0.476)], meta=_DISCHARGE_STAGE)
        r = normalize_nwps_observed(raw, "AUBW1", now)
        assert r["cfs"] == 476.0          # not 57,250
        assert r["gauge_height_ft"] == 57.25

    def test_series_without_flow_returns_none(self):
        from conditions.normalizer import normalize_nwps_observed
        now = datetime.now(tz=timezone.utc)
        meta = {"primaryName": "Stage", "primaryUnits": "ft"}
        assert normalize_nwps_observed(_nwps([(now, 1.0, 1.0)], meta=meta), "X", now) is None

    def test_stale_or_empty_returns_none(self):
        from conditions.normalizer import normalize_nwps_observed
        now = datetime.now(tz=timezone.utc)
        assert normalize_nwps_observed(_nwps([(now - timedelta(days=3), 1, 1)]), "X", now) is None
        assert normalize_nwps_observed({}, "X", now) is None


class TestUsgsNwpsFallback:
    async def test_dead_usgs_gauge_falls_back_to_nwps(self):
        from unittest.mock import AsyncMock, patch
        import conditions.usgs as usgs
        nwps = {"cfs": 1770.0, "temp_f": None, "provider": "noaa_nwps_observed"}
        with patch.object(usgs, "_fetch_usgs_reading", AsyncMock(return_value=None)), \
             patch.object(usgs, "fetch_nwps_observed", AsyncMock(return_value=nwps)) as nw:
            r = await usgs.fetch_usgs_gauge("12494000")  # Naches → NACW1
        nw.assert_awaited_once_with("NACW1")
        assert r["cfs"] == 1770.0

    async def test_live_usgs_never_calls_nwps(self):
        from unittest.mock import AsyncMock, patch
        import conditions.usgs as usgs
        with patch.object(usgs, "_fetch_usgs_reading", AsyncMock(return_value={"cfs": 500.0})), \
             patch.object(usgs, "fetch_nwps_observed", AsyncMock()) as nw:
            assert (await usgs.fetch_usgs_gauge("12494000"))["cfs"] == 500.0
        nw.assert_not_awaited()

    async def test_stage_only_usgs_keeps_temp_on_fallback(self):
        from unittest.mock import AsyncMock, patch
        import conditions.usgs as usgs
        with patch.object(usgs, "_fetch_usgs_reading", AsyncMock(return_value={"cfs": None, "temp_f": 52.0})), \
             patch.object(usgs, "fetch_nwps_observed", AsyncMock(return_value={"cfs": 495.0, "temp_f": None})):
            r = await usgs.fetch_usgs_gauge("12167400")  # Stillaguamish → ARLW1
        assert r["cfs"] == 495.0 and r["temp_f"] == 52.0

    async def test_no_lid_returns_usgs_result(self):
        from unittest.mock import AsyncMock, patch
        import conditions.usgs as usgs
        with patch.object(usgs, "_fetch_usgs_reading", AsyncMock(return_value=None)), \
             patch.object(usgs, "fetch_nwps_observed", AsyncMock()) as nw:
            assert await usgs.fetch_usgs_gauge("00000000") is None
        nw.assert_not_awaited()


class TestNwrfcForecastFlowField:
    """normalize_noaa_nwrfc sets flow_cfs from metadata; _nwrfc_cfs_at uses only that."""

    def test_forecast_flow_cfs_both_orders(self):
        from conditions.normalizer import normalize_noaa_nwrfc
        t = datetime.now(tz=timezone.utc) + timedelta(days=2)
        a = normalize_noaa_nwrfc(_nwps([(t, 14.6, 1.6)], series="forecast"), "NACW1", t)
        b = normalize_noaa_nwrfc(_nwps([(t, 57.25, 0.476)], meta=_DISCHARGE_STAGE, series="forecast"), "AUBW1", t)
        assert a["forecast"][0]["flow_cfs"] == 1600.0
        assert b["forecast"][0]["flow_cfs"] == 476.0

    def test_cfs_at_departure_uses_flow_cfs_and_ignores_legacy_rows(self):
        from chat.context_builder import _nwrfc_cfs_at
        dep = datetime.now(tz=timezone.utc) + timedelta(days=2)
        vt = dep.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _nwrfc_cfs_at({"forecast": [{"validTime": vt, "primary": 0.476, "secondary": 57.25,
                                            "flow_cfs": 476.0}]}, dep) == 476.0
        # pre-fix cached row: no flow_cfs → not guessed from "secondary"
        assert _nwrfc_cfs_at({"forecast": [{"validTime": vt, "primary": 0.476, "secondary": 57.25}]}, dep) is None
