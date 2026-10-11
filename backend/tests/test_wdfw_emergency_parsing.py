"""
Unit tests for wdfw_emergency.py date extraction logic.

The scraper's _extract_dates() and _parse_date() helpers contain non-trivial
regex logic — worth testing in isolation without any HTTP calls.
"""

import pytest

from conditions.wdfw_emergency import _extract_dates, _parse_date


class TestParseDate:
    def test_full_month_name(self):
        assert _parse_date("April 5, 2026") == "2026-04-05"

    def test_full_month_no_comma(self):
        assert _parse_date("April 5 2026") == "2026-04-05"

    def test_abbreviated_month(self):
        assert _parse_date("Apr 5, 2026") == "2026-04-05"

    def test_returns_none_for_empty(self):
        assert _parse_date("") is None
        assert _parse_date(None) is None

    def test_returns_none_for_garbage(self):
        assert _parse_date("not a date") is None


class TestExtractDates:
    def test_effective_and_expiry(self):
        text = "Emergency closure effective April 1, 2026 expires April 30, 2026"
        effective, expires = _extract_dates(text)
        assert effective == "2026-04-01"
        assert expires == "2026-04-30"

    def test_date_range_with_dash(self):
        text = "Closed April 1, 2026 - May 15, 2026"
        effective, expires = _extract_dates(text)
        assert effective == "2026-04-01"
        assert expires == "2026-05-15"

    def test_no_dates_returns_none_none(self):
        effective, expires = _extract_dates("No dates in this text at all.")
        assert effective is None
        assert expires is None

    def test_only_expiry(self):
        text = "This closure expires June 1, 2026."
        _, expires = _extract_dates(text)
        assert expires == "2026-06-01"


class TestUserAgent:
    async def test_requests_send_identifying_user_agent(self, monkeypatch):
        """WDFW's CDN 403s the default python-httpx UA (TODO-10, 2026-10-11)."""
        import httpx

        import conditions.wdfw_emergency as w

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["user-agent"])
            return httpx.Response(200, text="<html><div class='view-content'></div></html>")

        real_client = httpx.AsyncClient

        def client(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(w.httpx, "AsyncClient", client)
        assert await w.fetch_wdfw_emergency() == []  # no rule links in stub page
        assert seen and all(not ua.startswith("python-httpx") for ua in seen)
        assert seen[0] == w._HEADERS["User-Agent"]


# ---------------------------------------------------------------------------
# TODO-15: structured fields, effective-date ranges, closure classification.
# Strings are verbatim from wdfw.wa.gov rule pages, 2026-10-11.
# ---------------------------------------------------------------------------

from conditions.wdfw_emergency import (  # noqa: E402
    _closes_fishing,
    _labeled_fields,
    _parse_effective_field,
    location_head,
    reopens_fishing,
)


class TestParseEffectiveField:
    @pytest.mark.parametrize("text,expected", [
        ("Oct. 9–31, 2026.", ("2026-10-09", "2026-10-31")),
        ("Oct. 9 – Nov. 1, 2026.", ("2026-10-09", "2026-11-01")),
        ("Sept. 16–Oct. 31, 2026.", ("2026-09-16", "2026-10-31")),
        ("Oct. 1–31, 2026.", ("2026-10-01", "2026-10-31")),
        ("Oct. 6, 2026, until further notice.", ("2026-10-06", None)),
        ("Sept. 9, 2026, until further notice.", ("2026-09-09", None)),
        ("Aug. 20, 2026, until further notice.", ("2026-08-20", None)),
        ("Dec. 1, 2026 – Jan. 31, 2027", ("2026-12-01", "2027-01-31")),
        ("Dec. 1–Jan. 31, 2027", ("2026-12-01", "2027-01-31")),
    ])
    def test_formats(self, text, expected):
        assert _parse_effective_field(text) == expected

    def test_immediately_through_uses_posted_date(self):
        assert _parse_effective_field("Immediately through Oct. 31, 2026.", "2026-09-18") == \
            ("2026-09-18", "2026-10-31")

    def test_unparseable_or_empty(self):
        assert _parse_effective_field(None) == (None, None)
        assert _parse_effective_field("See below.") == (None, None)


class TestLabeledFields:
    def test_spacing_variants(self):
        fields = _labeled_fields([
            "Site search",
            "Action : Opens the Mineral Lake game fish season through Oct. 31 annually.",
            "Effective date : Oct. 1–31, 2026.",
            "Location : Mineral Lake (Lewis County).",
            "Action: a second action line is ignored",
        ])
        assert fields["action"].startswith("Opens the Mineral Lake")
        assert fields["effective"] == "Oct. 1–31, 2026."
        assert fields["location"] == "Mineral Lake (Lewis County)."

    def test_plural_labels(self):
        fields = _labeled_fields(["Effective dates: Sept. 9, 2026, until further notice.",
                                  "Locations: Stillaguamish River from Marine Drive"])
        assert fields["effective"].startswith("Sept. 9")
        assert fields["location"].startswith("Stillaguamish River")


class TestClosesFishing:
    @pytest.mark.parametrize("action", [
        "Closes all fishing.",
        "Closes fishing.",
        "Closes recreational fishing in the mainstem Stillaguamish River.",
    ])
    def test_closures(self, action):
        assert _closes_fishing(action, "x") is True

    @pytest.mark.parametrize("action", [
        "Closes Chinook salmon retention.",
        "Closes retention of wild (unmarked) fall Chinook.",
        "Opens the Mineral Lake game fish season through Oct. 31 annually.",
        "Returns Lake Washington to permanent rules listed in the Washington Sport Fishing Rules , "
        "reopening salmon fishing.",
        "Reopens fishing in the Quillayute River with reduced salmon retention limits and prohibits bait.",
        "Maintains previously announced bait prohibitions, single-point barbless hooks requirement.",
    ])
    def test_not_closures(self, action):
        assert _closes_fishing(action, "x") is False

    def test_title_fallback_when_no_action(self):
        assert _closes_fishing(None, "Stillaguamish River to close to all fishing Oct. 9") is True
        assert _closes_fishing(None, "Green (Duwamish) River Chinook retention closed") is False


class TestReopensAndLocation:
    def test_reopens(self):
        assert reopens_fishing("Reopens fishing in the Quillayute River with reduced limits.")
        assert reopens_fishing("Opens the Icicle River to retention of coho salmon.")
        assert reopens_fishing("Returns Lake Washington to permanent rules listed in the pamphlet.")
        assert not reopens_fishing("Closes all fishing.")
        assert not reopens_fishing(None)

    def test_location_head_drops_landmarks(self):
        loc = ("Quillayute River (Clallam County), from Olympic National Park boundary upstream to "
               "confluence of Sol Duc and Bogachiel rivers.")
        assert location_head(loc) == "Quillayute River"
        assert location_head("Stillaguamish River from Marine Drive (south of Stanwood) upstream") == \
            "Stillaguamish River"
        assert location_head(None) == ""
