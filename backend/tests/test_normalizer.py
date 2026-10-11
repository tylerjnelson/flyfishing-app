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
    def _ts(code, values, age=timedelta(minutes=30), step=timedelta(minutes=15)):
        """One USGS series; `values` oldest→newest, the newest `age` old."""
        newest = datetime.now(tz=timezone.utc) - age
        n = len(values)
        return {
            "variable": {"variableCode": [{"value": code}], "noDataValue": -999999.0},
            "values": [{"value": [
                {"value": str(v), "dateTime": (newest - step * (n - 1 - i)).isoformat()}
                for i, v in enumerate(values)
            ]}],
        }

    def _make_raw(self, cfs=450.0, gauge_ht=3.5, temp_c=10.0):
        ts = self._ts
        return {"value": {"timeSeries": [ts("00060", [cfs]), ts("00065", [gauge_ht]), ts("00010", [temp_c])]}}

    def test_no_data_sentinel_dropped(self):
        """Klickitat 14113000 reports water temp as -999999 (TODO-11)."""
        raw = {"value": {"timeSeries": [self._ts("00060", [610]), self._ts("00010", [-999999])]}}
        result = normalize_usgs(raw, datetime.now(tz=timezone.utc))
        assert result["cfs"] == 610.0
        assert result["temp_f"] is None
        assert result["dropped"] == {"00010": "no_data_sentinel"}

    def test_stale_reading_dropped(self):
        """Elwha 12044900's latest discharge is from 2011 (TODO-11)."""
        raw = {"value": {"timeSeries": [self._ts("00060", [725], age=timedelta(days=5000))]}}
        result = normalize_usgs(raw, datetime.now(tz=timezone.utc))
        assert result["cfs"] is None
        assert result["dropped"]["00060"].startswith("stale:")

    def test_reading_just_inside_window_kept(self):
        raw = {"value": {"timeSeries": [self._ts("00060", [300], age=timedelta(hours=23))]}}
        assert normalize_usgs(raw, datetime.now(tz=timezone.utc))["cfs"] == 300.0

    def test_trend_ignores_sentinel_values(self):
        raw = {"value": {"timeSeries": [self._ts("00060", [100, -999999, 100, 100, 100, 120])]}}
        result = normalize_usgs(raw, datetime.now(tz=timezone.utc))
        assert result["cfs"] == 120.0
        assert result["trend"] == "rising"

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
        raw = {"value": {"timeSeries": []}}
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


def test_usgs_base_url_host():
    """Was api.waterservices.usgs.gov (nonexistent) until 2026-10-11 (TODO-11)."""
    from urllib.parse import urlparse
    from conditions.usgs import _BASE_URL
    assert urlparse(_BASE_URL).hostname == "waterservices.usgs.gov"


async def test_usgs_request_includes_period_for_trend(monkeypatch):
    """Without `period` USGS returns only the latest value, so trend is always None."""
    import httpx
    import conditions.usgs as usgs

    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json={"value": {"timeSeries": []}})

    real = httpx.AsyncClient
    monkeypatch.setattr(usgs.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    await usgs._fetch("12134500")
    assert seen["period"] == "PT2H"
    assert seen["sites"] == "12134500"
