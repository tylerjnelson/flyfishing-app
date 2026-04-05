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
