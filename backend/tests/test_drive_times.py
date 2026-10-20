"""TODO-19: road-network estimates, ferries, winter passes, HERE only for the cards.

No live HERE anywhere: get_route, the budget and the cache are stubbed.
"""

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from chat import drive_times
from conditions import ferries, pass_closures, road_network
from conditions.routing import Route

SEATTLE = (47.6062, -122.3321)


class TestFerryDemand:
    @pytest.mark.parametrize("day,level", [
        (date(2026, 7, 11), "peak"),      # summer Saturday
        (date(2026, 7, 8), "summer"),     # summer Wednesday
        (date(2026, 10, 21), "normal"),
        (date(2026, 11, 27), "peak"),     # day after Thanksgiving
        (date(2026, 5, 22), "peak"),      # Friday of Memorial Day weekend
        (date(2026, 5, 20), "normal"),    # the Wednesday before it
        (date(2026, 9, 7), "peak"),       # Labor Day
        (date(2026, 9, 9), "normal"),
    ])
    def test_levels(self, day, level):
        assert ferries.demand(day) == level

    def test_wait_follows_the_date(self):
        ek = ferries.BY_ID["ed-king"]
        assert ferries.expected_wait(ek, date(2026, 10, 21)) < ferries.expected_wait(ek, date(2026, 7, 11))

    def test_line_names_route_date_and_wait(self):
        s = ferries.ferry_summary(ferries.BY_ID["ed-king"], date(2026, 7, 11))
        line = ferries.ferry_line([s])
        assert line.startswith("Ferry: Edmonds–Kingston (Washington State Ferries), ~30 min crossing")
        assert "Sat Jul 11 is a peak day" in line
        assert "expect ~90 min to board" in line
        assert "check the sailing schedule" in line

    def test_reservation_routes_say_so(self):
        s = ferries.ferry_summary(ferries.BY_ID["pt-cou"], date(2026, 10, 21))
        assert s["reservations_url"]
        assert "vehicle reservations recommended" in ferries.ferry_line([s])

    def test_score_penalty_heavier_on_peak_days(self):
        ek = ferries.BY_ID["ed-king"]
        off = ferries.score_penalty([ferries.ferry_summary(ek, date(2026, 10, 21))])
        peak = ferries.score_penalty([ferries.ferry_summary(ek, date(2026, 7, 11))])
        assert 0 < off < peak
        assert ferries.score_penalty([]) == 0

    def test_terminal_pair_either_direction(self):
        assert ferries.match_terminal_pair(47.8134, -122.3855, 47.7947, -122.4948).id == "ed-king"
        assert ferries.match_terminal_pair(47.7947, -122.4948, 47.8134, -122.3855).id == "ed-king"
        assert ferries.match_terminal_pair(47.0, -122.0, 47.1, -122.1) is None


class TestPassClosures:
    @pytest.mark.parametrize("day,status", [
        (date(2027, 1, 15), "closed"),
        (date(2026, 11, 5), "uncertain"),
        (date(2026, 11, 25), "closed"),
        (date(2027, 5, 15), "uncertain"),
        (date(2026, 7, 1), "open"),
        (date(2026, 10, 21), "open"),
    ])
    def test_sr20(self, day, status):
        assert pass_closures.status(pass_closures.BY_ID["sr20"], day) == status

    def test_here_avoids_closed_passes_only(self):
        assert pass_closures.here_avoid_areas(date(2026, 7, 1)) is None
        avoid = pass_closures.here_avoid_areas(date(2027, 1, 15))
        assert "bbox:-121.06,48.45,-120.5,48.76" in avoid
        assert avoid.count("bbox:") == len(pass_closures.CLOSURES)


@pytest.mark.skipif(not road_network.available(), reason="data/road_graph.npz not built")
class TestRoadNetwork:
    def _est(self, dest, day):
        return road_network.drive_field(*SEATTLE, day, 400).to_point(*dest)

    def test_interstate_drive_close_to_typical(self):
        ellensburg = self._est((46.9965, -120.5478), date(2026, 10, 21))
        spokane = self._est((47.6588, -117.4260), date(2026, 10, 21))
        assert 90 <= ellensburg.minutes <= 125      # ~105 typical
        assert 235 <= spokane.minutes <= 295        # ~265 typical
        assert not ellensburg.ferries and not spokane.ferries

    def test_ferry_used_off_peak(self):
        forks = self._est((47.9504, -124.3855), date(2026, 10, 21))
        assert forks.ferries and forks.ferries[0] in ("sea-bi", "ed-king")

    def test_peak_wait_can_make_driving_around_faster(self):
        off = self._est((47.9504, -124.3855), date(2026, 10, 21))
        peak = self._est((47.9504, -124.3855), date(2026, 7, 11))
        assert peak.minutes > off.minutes

    def test_winter_closes_north_cascades_highway(self):
        twisp = (48.3654, -120.1223)
        summer = self._est(twisp, date(2026, 7, 15))
        winter = self._est(twisp, date(2027, 1, 15))
        assert "sr20" in summer.passes
        assert "sr20" not in winter.passes
        assert winter.minutes > summer.minutes        # around via Stevens Pass

    def test_limit_makes_far_waters_unreachable(self):
        f = road_network.drive_field(*SEATTLE, date(2026, 10, 21), 60)
        assert f.to_point(47.6588, -117.4260) is None


def _cand(name, minutes, *, lat=47.0, lon=-121.0):
    return {"spot_id": name, "water_body_name": name, "drive_minutes": minutes,
            "drive_estimated": True, "route_to": [lat, lon], "ferries": []}


class TestVerifyTop:
    """HERE (stubbed) only for the top three, after ranking."""

    @pytest.fixture(autouse=True)
    def _stubs(self):
        from config import settings
        self.cache_get = AsyncMock(return_value=None)
        self.cache_put = AsyncMock()
        self.reserve = AsyncMock(return_value=1)
        self.here = {}

        async def route(olat, olon, lat, lon, departure, avoid=None):
            self.calls.append((lat, lon, avoid))
            r = self.here.get((lat, lon))
            return r if r is not None else Route(minutes=100, is_fallback=False)

        self.calls = []
        with patch.object(settings, "here_disabled", False), \
             patch.object(drive_times.drive_time_cache, "get", self.cache_get), \
             patch.object(drive_times.drive_time_cache, "put", self.cache_put), \
             patch.object(drive_times.here_budget, "reserve", self.reserve), \
             patch.object(drive_times, "get_route", route):
            yield

    async def _run(self, cands, max_minutes=180, day=date(2026, 10, 21)):
        return await drive_times.verify_top(cands, *SEATTLE, datetime(2026, 10, 21, tzinfo=timezone.utc),
                                            day, max_minutes)

    async def test_only_top_three_call_here(self):
        cands = [_cand(f"w{i}", 90 + i, lat=47.0 + i) for i in range(8)]
        out, unavailable = await self._run(cands)
        assert len(self.calls) == 3
        assert [c["drive_estimated"] for c in out] == [False] * 3 + [True] * 5
        assert out[0]["drive_minutes"] == 100
        assert not unavailable
        assert self.cache_put.await_count == 3

    async def test_card_past_the_limit_is_replaced(self):
        cands = [_cand(f"w{i}", 170, lat=47.0 + i) for i in range(5)]
        self.here[(48.0, -121.0)] = Route(minutes=200, is_fallback=False)   # w1
        out, _ = await self._run(cands)
        assert [c["spot_id"] for c in out] == ["w0", "w2", "w3", "w4"]
        assert len(self.calls) == 4

    async def test_unverified_estimates_past_the_limit_drop(self):
        cands = [_cand(f"w{i}", 100, lat=47.0 + i) for i in range(3)] + [_cand("far", 190), _cand("ok", 175)]
        out, _ = await self._run(cands)
        assert [c["spot_id"] for c in out] == ["w0", "w1", "w2", "ok"]

    async def test_calls_capped(self):
        cands = [_cand(f"w{i}", 170, lat=47.0 + i) for i in range(12)]
        for i in range(12):
            self.here[(47.0 + i, -121.0)] = Route(minutes=300, is_fallback=False)
        out, unavailable = await self._run(cands)
        assert len(self.calls) == drive_times.MAX_HERE_CALLS
        assert not unavailable                   # out of calls: the rest keep estimates, no banner
        assert all(c["drive_estimated"] for c in out)

    async def test_cache_hit_skips_here(self):
        self.cache_get.return_value = Route(minutes=120, is_fallback=False)
        out, _ = await self._run([_cand("w0", 110)])
        assert self.calls == [] and self.reserve.await_count == 0
        assert out[0]["drive_minutes"] == 120 and out[0]["drive_estimated"] is False

    async def test_here_down_keeps_estimate_and_flags(self):
        self.here[(47.0, -121.0)] = Route(minutes=77, is_fallback=True)
        out, unavailable = await self._run([_cand("w0", 110)])
        assert unavailable
        assert out[0]["drive_minutes"] == 110 and out[0]["drive_estimated"] is True
        assert self.cache_put.await_count == 0

    async def test_budget_exhausted_no_call(self):
        self.reserve.return_value = 0
        out, unavailable = await self._run([_cand("w0", 110)])
        assert self.calls == [] and unavailable and out[0]["drive_estimated"]

    async def test_ferry_wait_added_to_here_time(self):
        # HERE: 150 min total, with an Edmonds -> Kingston crossing of 30 min (no wait counted).
        self.here[(47.0, -121.0)] = Route(minutes=150, is_fallback=False,
                                          ferries=[(47.8134, -122.3855, 47.7947, -122.4948, 30)])
        out, _ = await self._run([_cand("w0", 160)], max_minutes=300, day=date(2026, 7, 11))
        assert out[0]["drive_minutes"] == 150 + 90
        assert out[0]["ferries"][0]["id"] == "ed-king" and out[0]["ferries"][0]["demand"] == "peak"

    async def test_winter_route_avoids_closed_passes(self):
        await self._run([_cand("w0", 100)], day=date(2027, 1, 15))
        assert "bbox:" in self.calls[0][2]

    async def test_here_disabled_never_calls(self):
        from config import settings
        with patch.object(settings, "here_disabled", True):
            out, unavailable = await self._run([_cand("w0", 100), _cand("far", 200)])
        assert self.calls == [] and self.cache_get.await_count == 0
        assert [c["spot_id"] for c in out] == ["w0"] and not unavailable


class TestRouteWarnings:
    def test_pass_advisory_only_near_the_closure_window(self):
        assert drive_times.pass_warnings(["sr20"], date(2026, 7, 1)) == []
        w = drive_times.pass_warnings(["sr20"], date(2026, 11, 7))
        assert w and w[0].startswith("Route uses SR 20 North Cascades Highway")


class TestCacheKey:
    def test_exact_decimals(self):
        from decimal import Decimal
        from conditions.drive_time_cache import _key
        k = _key(47.60621, -122.33209, 47.1, -121.1, None)
        assert k == {"olat": Decimal("47.606"), "olon": Decimal("-122.332"),
                     "dlat": Decimal("47.10000"), "dlon": Decimal("-121.10000"), "avoid": ""}


class TestHereSections:
    async def test_total_is_every_section_and_ferries_are_reported(self):
        from config import settings
        from conditions import routing

        body = {"routes": [{"sections": [
            {"type": "vehicle", "summary": {"duration": 1200}},
            {"type": "ferry", "transport": {"mode": "ferry"}, "summary": {"duration": 1800},
             "departure": {"place": {"location": {"lat": 47.8134, "lng": -122.3855}}},
             "arrival": {"place": {"location": {"lat": 47.7947, "lng": -122.4948}}}},
            {"type": "vehicle", "summary": {"duration": 3600}},
        ]}]}
        resp = httpx.Response(200, json=body, request=httpx.Request("GET", "https://router.hereapi.com/v8/routes"))
        get = AsyncMock(return_value=resp)
        with patch.object(settings, "here_disabled", False), patch.object(httpx.AsyncClient, "get", get):
            route = await routing.get_route(47.6, -122.3, 47.9, -124.4, None, avoid_areas="bbox:1,2,3,4")
        assert route.minutes == 110 and not route.is_fallback
        assert route.ferries == [(47.8134, -122.3855, 47.7947, -122.4948, 30)]
        assert get.await_args.kwargs["params"]["avoid[areas]"] == "bbox:1,2,3,4"


class TestRendering:
    def test_estimate_labeled_for_the_model(self):
        from chat.context_builder import _format_compact_menu, _render_spot_conditions_lines

        s = ferries.ferry_summary(ferries.BY_ID["sea-bi"], date(2026, 10, 21))
        c = {"water_body_name": "Sol Duc River", "fishing_spot_id": "x", "drive_minutes": 230,
             "drive_estimated": True, "ferries": [s]}
        lines = _render_spot_conditions_lines(c, is_future_trip=False, departure_time=None)
        assert "Drive time: ~230 min (estimate, not a live route)" in lines
        assert any(l.startswith("Ferry: Seattle–Bainbridge") for l in lines)

        menu = _format_compact_menu([{}, {}, {}, {**c, "spot_name": "Sol Duc River", "spot_type": "river"}])
        assert "~230 min est. incl. ferry" in menu


class TestEstimate:
    @pytest.mark.skipif(not road_network.available(), reason="data/road_graph.npz not built")
    def test_unreachable_and_margin(self):
        wb_near, wb_far = SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())
        near = SimpleNamespace(id=uuid.uuid4(), latitude=47.75, longitude=-122.25)
        far = SimpleNamespace(id=uuid.uuid4(), latitude=47.6588, longitude=-117.4260)   # Spokane
        est = drive_times.estimate([(near, wb_near), (far, wb_far)], {}, *SEATTLE, date(2026, 10, 21), 90)
        assert drive_times.within_margin(est[near.id], 90)
        assert not drive_times.within_margin(est[far.id], 90)

    def test_no_origin_no_minutes(self):
        wb = SimpleNamespace(id=uuid.uuid4())
        spot = SimpleNamespace(id=uuid.uuid4(), latitude=47.75, longitude=-122.25)
        est = drive_times.estimate([(spot, wb)], {}, None, None, date(2026, 10, 21), 90)
        assert est[spot.id].minutes is None and drive_times.within_margin(est[spot.id], 90)
