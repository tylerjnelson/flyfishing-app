"""
Unit tests for exceptions.py
"""

import pytest

from exceptions import ScraperStructureError


class TestScraperStructureError:
    def test_attributes_stored(self):
        exc = ScraperStructureError(
            source="wdfw_emergency",
            url="https://wdfw.wa.gov/fishing/regulations/emergency-rules",
            detail="Selector not found",
        )
        assert exc.source == "wdfw_emergency"
        assert exc.url == "https://wdfw.wa.gov/fishing/regulations/emergency-rules"
        assert exc.detail == "Selector not found"

    def test_is_exception(self):
        exc = ScraperStructureError(source="wta", url="https://wta.org", detail="missing")
        assert isinstance(exc, Exception)

    def test_message_includes_source_and_url(self):
        exc = ScraperStructureError(source="wta", url="https://wta.org/hike", detail="gone")
        assert "wta" in str(exc)
        assert "https://wta.org/hike" in str(exc)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(ScraperStructureError) as exc_info:
            raise ScraperStructureError(source="x", url="http://x.com", detail="test")
        assert exc_info.value.source == "x"
