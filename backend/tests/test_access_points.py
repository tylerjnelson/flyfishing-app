"""Access points (TODO-5): gate parsing, which point a water is routed to, card/model text."""

from datetime import date
from types import SimpleNamespace

import pytest

from conditions.access_points import (
    access_line,
    access_summary,
    directions_url,
    pick_access_point,
    vehicle_access,
)


def _ap(key, lat, lon, *, source="wdfw_was", kind="boat_ramp", confidence="high", seasonal=None, name=None):
    return SimpleNamespace(
        key=key, source=source, kind=kind, confidence=confidence, seasonal=seasonal,
        name=name or key, latitude=lat, longitude=lon, description=None, parking="1 parking lot",
        facilities=["1 boat ramp (concrete)"], pass_required="Discover Pass or WDFW Vehicle Access Pass",
    )


class TestVehicleAccess:
    @pytest.mark.parametrize("text,day,expected", [
        # Closed window that wraps the new year.
        ("Year-round. Closed to vehicles October 1 - February 28.", date(2026, 1, 10), False),
        ("Year-round. Closed to vehicles October 1 - February 28.", date(2026, 7, 4), True),
        ("Year-round. Closed to vehicles October 1 - February 28.", date(2026, 10, 1), False),
        # Open window with an nth-weekday start (4th Saturday in April 2026 = Apr 25).
        ("4th Saturday in April - October 31", date(2026, 4, 24), False),
        ("4th Saturday in April - October 31", date(2026, 4, 25), True),
        ("4th Saturday in April - October 31", date(2026, 11, 2), False),
        ("Year-round. Open to vehicles 4th Saturday in April - September 15.", date(2026, 9, 16), False),
        # Closed window ending on an nth weekday (4th Friday in April 2026 = Apr 24).
        ("Year-round. Closed to vehicles November 1 - 4th Friday in April. Walk-in allowed when entrance gate is closed.",
         date(2026, 4, 24), False),
        ("Year-round. Closed to vehicles November 1 - 4th Friday in April. Walk-in allowed when entrance gate is closed.",
         date(2026, 4, 25), True),
        ("Year-round.  Closed to all vehicles December 1 - March 15.", date(2026, 3, 16), True),
        ("Year-round. Closed to street-legal vehicles December 1 - March 31.", date(2026, 12, 25), False),
        ("June 1 - August 31", date(2026, 9, 1), False),
    ])
    def test_dated_windows(self, text, day, expected):
        assert vehicle_access(text, day) is expected

    def test_no_text_means_open(self):
        assert vehicle_access(None, date(2026, 1, 1)) is True

    def test_undated_season_is_unknown(self):
        text = "Year-round.  Open to vehicles only during the regulated sport fishing season(s)."
        assert vehicle_access(text, date(2026, 6, 1)) is None


class TestPick:
    SEATTLE = (47.61, -122.33)

    def test_nearest_to_origin(self):
        renton = _ap("wdfw_shore:1", 47.5064, -122.2033)
        kenmore = _ap("wdfw_shore:2", 47.7560, -122.2470)
        assert pick_access_point([renton, kenmore], 47.76, -122.26, date(2026, 6, 1)) is kenmore
        assert pick_access_point([renton, kenmore], 47.48, -122.21, date(2026, 6, 1)) is renton

    def test_gate_closed_on_trip_day_is_skipped(self):
        near = _ap("wdfw_was:1", 47.62, -122.33, seasonal="Year-round. Closed to vehicles October 1 - February 28.")
        far = _ap("wdfw_was:2", 47.80, -122.33)
        assert pick_access_point([near, far], *self.SEATTLE, date(2026, 12, 1)) is far
        assert pick_access_point([near, far], *self.SEATTLE, date(2026, 6, 1)) is near

    def test_all_gated_still_returns_one(self):
        only = _ap("wdfw_was:1", 47.62, -122.33, seasonal="June 1 - August 31")
        assert pick_access_point([only], *self.SEATTLE, date(2026, 12, 1)) is only

    def test_undated_season_is_not_skipped(self):
        near = _ap("wdfw_was:1", 47.62, -122.33,
                   seasonal="Year-round. Open to vehicles only during the regulated sport fishing season(s).")
        far = _ap("wdfw_was:2", 47.80, -122.33)
        assert pick_access_point([near, far], *self.SEATTLE, date(2026, 12, 1)) is near

    def test_official_access_beats_a_slightly_nearer_bridge(self):
        # Bridge ~1 km nearer; it carries +1 (osm) +2 (bridge) +3 (low) minutes.
        bridge = _ap("osm:way/1", 47.70, -122.33, source="osm", kind="bridge", confidence="low")
        wdfw = _ap("wdfw_was:9", 47.709, -122.33)
        assert pick_access_point([bridge, wdfw], *self.SEATTLE, date(2026, 6, 1)) is wdfw

    def test_much_nearer_osm_point_wins(self):
        camp = _ap("osm:way/2", 47.70, -122.33, source="osm", kind="campground", confidence="medium")
        wdfw = _ap("wdfw_was:9", 48.20, -122.33)
        assert pick_access_point([camp, wdfw], *self.SEATTLE, date(2026, 6, 1)) is camp

    def test_no_origin_prefers_confident_official(self):
        osm = _ap("osm:node/1", 47.7, -122.3, source="osm", kind="parking", confidence="medium")
        med = _ap("curated:x", 47.7, -122.3, source="curated", kind="access", confidence="medium")
        high = _ap("wdfw_was:3", 47.9, -122.3)
        assert pick_access_point([osm, med, high], None, None, date(2026, 6, 1)) is high

    def test_gateway_follows_the_road_up_the_valley(self):
        # Icicle Creek from Seattle: Blackpine Horse Camp is the westernmost point,
        # but its road starts at Leavenworth and runs 35 min up the valley.
        gw = dict(gateway_latitude=47.5885, gateway_longitude=-120.6753)
        blackpine = _ap("osm:1", 47.6220, -120.8350, source="osm", kind="campground", confidence="medium")
        blackpine.__dict__.update(gw, gateway_minutes=34.8)
        wdfw = _ap("wdfw_was:icicle", 47.5694, -120.6612, kind="access_area")
        wdfw.__dict__.update(gateway_latitude=47.5979, gateway_longitude=-120.6497, gateway_minutes=3.7)
        assert pick_access_point([blackpine, wdfw], *self.SEATTLE, date(2026, 6, 1)) is wdfw
        # Without gateways the straight line picks Blackpine.
        for p in (blackpine, wdfw):
            p.gateway_minutes = None
        assert pick_access_point([blackpine, wdfw], *self.SEATTLE, date(2026, 6, 1)) is blackpine

    def test_no_points(self):
        assert pick_access_point([], *self.SEATTLE, date(2026, 6, 1)) is None


class TestDisplay:
    def test_summary_and_directions(self):
        ap = _ap("wdfw_was:204", 47.269778, -117.966025, name="Fourth of July Lake",
                 seasonal="Year-round. Closed to vehicles October 1 - February 28.")
        s = access_summary(ap, date(2026, 11, 1))
        assert s["name"] == "Fourth of July Lake (WDFW access)"
        assert s["vehicle_access"] is False
        assert s["directions_url"] == directions_url(47.269778, -117.966025)
        assert "destination=47.269778%2C-117.966025" in s["directions_url"]

    def test_model_line_mentions_gate_and_pass(self):
        ap = _ap("wdfw_was:204", 47.27, -117.97, name="Fourth of July Lake",
                 seasonal="Year-round. Closed to vehicles October 1 - February 28.")
        line = access_line(access_summary(ap, date(2026, 11, 1)))
        assert line.startswith("Access: Fourth of July Lake (WDFW access) (boat ramp)")
        assert "vehicle gate closed on the trip date" in line
        assert "Discover Pass" in line

    def test_shore_site_label(self):
        ap = _ap("wdfw_shore:10001", 48.68, -119.51, source="wdfw_shore", kind="shore", name="Aeneas Lake")
        assert access_summary(ap, date(2026, 6, 1))["name"] == "WDFW shore access"

    def test_stocking_style_curated_name_cleaned(self):
        ap = _ap("curated:1", 47.5, -122.9, source="curated", kind="access", name="LAKE TAHUYA (KITS)")
        assert access_summary(ap, date(2026, 6, 1))["name"] == "Lake Tahuya"
        ok = _ap("curated:2", 47.5, -122.9, source="curated", kind="access", name="Old Monroe Road Access")
        assert access_summary(ok, date(2026, 6, 1))["name"] == "Old Monroe Road Access"

    def test_no_access(self):
        assert access_line(None) is None


class TestRouteDestinations:
    """The context builder routes each candidate to its chosen access point."""

    def test_access_point_used_else_spot_coordinate(self):
        import uuid
        from chat.context_builder import _render_spot_conditions_lines
        from chat.drive_times import estimate

        lake = SimpleNamespace(id=uuid.uuid4())
        river = SimpleNamespace(id=uuid.uuid4())
        lake_spot = SimpleNamespace(id=uuid.uuid4(), latitude=47.5064, longitude=-122.2033)
        river_spot = SimpleNamespace(id=uuid.uuid4(), latitude=48.0, longitude=-121.0)
        kenmore = _ap("wdfw_shore:2", 47.7560, -122.2470, source="wdfw_shore", kind="shore")
        renton = _ap("wdfw_shore:1", 47.5064, -122.2033, source="wdfw_shore", kind="shore")

        est = estimate([(lake_spot, lake), (river_spot, river)], {lake.id: [renton, kenmore]},
                       47.76, -122.26, date(2026, 6, 1), 180)

        assert est[lake_spot.id].dest == (47.7560, -122.2470)   # nearest shore site, not the spot
        assert est[river_spot.id].dest == (48.0, -121.0)        # no access points: the spot
        assert est[lake_spot.id].access["name"] == "WDFW shore access"
        assert est[river_spot.id].access is None
        assert est[lake_spot.id].minutes < est[river_spot.id].minutes

        lines = _render_spot_conditions_lines(
            {"water_body_name": "Lake Washington", "fishing_spot_id": "x", "drive_minutes": 25,
             "access": est[lake_spot.id].access},
            is_future_trip=False, departure_time=None)
        assert "Drive time: 25 min" in lines
        assert any(l.startswith("Access: WDFW shore access (shore)") for l in lines)
