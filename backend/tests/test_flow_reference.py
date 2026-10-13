"""
Flow references (TODO-18): per-gauge normals, published ranges, flood stages.

Pure-function tests plus the loader's response parsing (shapes copied from
live USGS statistics / NWPS responses on 2026-10-13). No network.
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from conditions import flow_reference as fr
from conditions.gauge_reference import parse_flood_stages, parse_normals
from conditions.normalizer import normalize_noaa_nwrfc

NORMALS = {5: 100.0, 10: 150.0, 25: 300.0, 50: 500.0, 75: 800.0, 90: 1500.0, 95: 2500.0}


class TestPercentiles:
    def test_exact_and_interpolated(self):
        assert fr.percentile_of(500, NORMALS) == 50
        assert fr.percentile_of(650, NORMALS) == pytest.approx(62.5)

    def test_tails_extrapolate_toward_0_and_100(self):
        assert 0 < fr.percentile_of(50, NORMALS) < 5
        assert 95 < fr.percentile_of(5000, NORMALS) < 100

    def test_labels(self):
        assert fr.normal_label(5) == "very low"
        assert fr.normal_label(20) == "below normal"
        assert fr.normal_label(50) == "normal"
        assert fr.normal_label(85) == "above normal"
        assert fr.normal_label(97) == "very high"

    def test_prob_in_range(self):
        assert fr.prob_in_range(300, 800, NORMALS) == pytest.approx(0.5)
        assert fr.prob_in_range(None, None, NORMALS) == 1.0
        assert fr.prob_in_range(5000, 9000, NORMALS) < 0.05


class TestSmoothing:
    def test_averages_the_window(self):
        rows = {"10-12": [1, 2, 3, 4, 5, 6, 7], "10-13": [3, 4, 5, 6, 7, 8, 9]}
        out = fr.smooth_normals(rows, date(2026, 10, 13))
        assert out[5] == 2 and out[95] == 8

    def test_window_wraps_the_year(self):
        keys = fr.window_keys(date(2027, 1, 2))
        assert "12-27" in keys and "01-09" in keys and len(keys) == 15

    def test_no_rows_in_window(self):
        assert fr.smooth_normals({"06-01": [1] * 7}, date(2026, 10, 13)) is None


class TestFloodStatus:
    STAGES = {"action_ft": 12.2, "minor_ft": 15.0}

    def test_levels(self):
        assert fr.flood_status(15.0, self.STAGES) == "flood"
        assert fr.flood_status(13.0, self.STAGES) == "action"
        assert fr.flood_status(4.5, self.STAGES) is None

    def test_missing_data(self):
        assert fr.flood_status(None, self.STAGES) is None
        assert fr.flood_status(20.0, None) is None
        assert fr.flood_status(20.0, {"action_ft": None, "minor_ft": None}) is None


class TestAssessFlow:
    def test_context_names_gauge_source_and_modes(self):
        bands = [
            {"gauge_site_id": "B", "gauge_name": "Gauge B", "mode": "wade", "min_cfs": 700, "max_cfs": 1500,
             "source": "Emerald Water Anglers"},
        ]
        delta, warnings, context = fr.assess_flow(
            cfs=500, normals=NORMALS, target=date(2026, 10, 13), bands=bands, band_cfs={"B": 900},
        )
        assert delta == fr.BAND_IN_RANGE_BONUS and warnings == []
        assert context[0].startswith("Flow vs. this gauge's history: normal for Oct 13")
        assert context[1] == "Emerald Water Anglers at Gauge B (900 cfs): Wading 700–1,500 cfs: in range"

    def test_one_sided_band(self):
        bands = [{"gauge_site_id": "A", "mode": "fish", "min_cfs": None, "max_cfs": 1600, "source": "s"}]
        delta, warnings, _ = fr.assess_flow(cfs=2000, normals=None, target=date(2026, 1, 1),
                                            bands=bands, band_cfs={"A": 2000})
        assert delta == fr.BAND_HIGH_PENALTY and "up to 1,600 cfs" in warnings[0]


class TestReliability:
    def test_best_band_wins(self):
        bands = [
            {"gauge_site_id": "A", "min_cfs": 2000, "max_cfs": 3000},   # rarely
            {"gauge_site_id": "A", "min_cfs": 300, "max_cfs": 800},     # half the time
        ]
        assert fr.best_band_reliability(bands, {"A": NORMALS}) == pytest.approx(0.5)

    def test_no_normals(self):
        assert fr.best_band_reliability([{"gauge_site_id": "A", "min_cfs": 1, "max_cfs": 2}], {}) is None

    @pytest.mark.asyncio
    async def test_scorer_neutral_without_bands(self):
        from spots.scorer import RELIABILITY_NEUTRAL, _conditions_reliability
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        assert await _conditions_reliability(db, "wb") == RELIABILITY_NEUTRAL


def _normals_page(values, next_url=None, statistic="00003", tdate="10-13"):
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "monitoring_location_id": "USGS-12134500",
                "data": [{
                    "parameter_code": "00060",
                    "parent_statistic_id": statistic,
                    "values": [{
                        "time_of_year": tdate,
                        "time_of_year_type": "day_of_year",
                        "values": values,
                        "percentiles": ["5", "10", "25", "50", "75", "90", "95"],
                        "sample_count": 98,
                        "computation": "percentile",
                    }],
                }],
            },
        }],
        "next": next_url,
    }


class TestLoaderParsing:
    def test_parse_normals(self):
        rows = parse_normals([_normals_page(["386.0", "486.0", "651.0", "1405.0", "2922.0", "5897.0", "9644.0"])])
        assert rows == [{"time_of_year": "10-13", "values": [386.0, 486.0, 651.0, 1405.0, 2922.0, 5897.0, 9644.0],
                         "sample_count": 98}]

    def test_parse_normals_skips_other_series_and_bad_rows(self):
        pages = [
            _normals_page(["1"] * 7, statistic="00001"),                       # max series, not mean
            _normals_page(["5", "4", "3", "2", "1", "0", "0"], tdate="10-14"),  # not monotonic
            _normals_page(["1", "2", None, "4", "5", "6", "7"], tdate="10-15"),  # missing value
        ]
        assert parse_normals(pages) == []

    def test_parse_flood_stages(self):
        meta = {"flood": {"categories": {
            "major": {"stage": 19, "flow": -9999}, "moderate": {"stage": 17, "flow": -9999},
            "minor": {"stage": 15, "flow": -9999}, "action": {"stage": 12.2, "flow": -9999},
        }}}
        assert parse_flood_stages(meta) == {"action_ft": 12.2, "minor_ft": 15.0, "moderate_ft": 17.0, "major_ft": 19.0}

    def test_parse_flood_stages_none_published(self):
        meta = {"flood": {"categories": {k: {"stage": -9999, "flow": -9999}
                                         for k in ("major", "moderate", "minor", "action")}}}
        assert parse_flood_stages(meta) is None


def test_nwrfc_forecast_carries_stage_whatever_the_field_order():
    fetched = datetime(2026, 10, 13, tzinfo=timezone.utc)
    stage_first = {"forecast": {"primaryName": "Stage", "primaryUnits": "ft", "secondaryName": "Flow",
                                "secondaryUnits": "kcfs",
                                "data": [{"validTime": "2026-10-14T00:00:00Z", "primary": 4.5, "secondary": 1.2}]}}
    flow_first = {"forecast": {"primaryName": "River Discharge", "primaryUnits": "kcfs", "secondaryName": "Stage",
                               "secondaryUnits": "ft",
                               "data": [{"validTime": "2026-10-14T00:00:00Z", "primary": 0.4, "secondary": 57.2}]}}
    a = normalize_noaa_nwrfc(stage_first, "GLBW1", fetched)["forecast"][0]
    b = normalize_noaa_nwrfc(flow_first, "AUBW1", fetched)["forecast"][0]
    assert (a["flow_cfs"], a["stage_ft"]) == (1200.0, 4.5)
    assert (b["flow_cfs"], b["stage_ft"]) == (400.0, 57.2)
