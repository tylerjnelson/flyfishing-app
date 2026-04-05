# Fingerprint: .emergency-rule-item, table.rule-table, #emergency-rules — validated YYYY-MM-DD
# NOTE: validate selector against https://wdfw.wa.gov/fishing/regulations/emergency-rules
#       at Phase 1 exit and update the date above.
"""
WDFW Emergency Rules scraper — 2-hour interval via APScheduler.

This fetcher is EXEMPT from the circuit breaker (§4.4).  Failures log at
WARNING and the last cached emergency_closures rows are served unmodified.
A CircuitBreakerError must never silently suppress closure data.

On ScraperStructureError (page structure changed): log CRITICAL.
"""

import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from exceptions import ScraperStructureError

log = logging.getLogger(__name__)

WDFW_EMERGENCY_URL = "https://wdfw.wa.gov/fishing/regulations/emergency-rules"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)

# Structural fingerprint — at least one of these selectors must be present
_FINGERPRINT_SELECTORS = ".emergency-rule-item, table.rule-table, #emergency-rules"


async def fetch_wdfw_emergency() -> list[dict]:
    """
    Scrape active WDFW emergency closure rules.

    Returns a list of dicts:
      {rule_text, effective (date str | None), expires (date str | None), source_url}

    Raises ScraperStructureError if the page structure has changed.
    Raises httpx.HTTPError on connection / HTTP failures (caller logs WARNING).
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(WDFW_EMERGENCY_URL)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    if not soup.select_one(_FINGERPRINT_SELECTORS):
        raise ScraperStructureError(
            source="wdfw_emergency",
            url=WDFW_EMERGENCY_URL,
            detail="Expected emergency rule selector not found — page structure may have changed",
        )

    rules = []

    # Try structured rule items first
    for item in soup.select(".emergency-rule-item"):
        rule_text = item.get_text(separator=" ", strip=True)
        effective, expires = _extract_dates(rule_text)
        rules.append({
            "rule_text": rule_text,
            "effective": effective,
            "expires": expires,
            "source_url": WDFW_EMERGENCY_URL,
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        })

    # Fallback: table rows
    if not rules:
        for row in soup.select("table.rule-table tr, #emergency-rules tr"):
            cells = [td.get_text(strip=True) for td in row.select("td")]
            if not cells:
                continue
            rule_text = " | ".join(cells)
            effective, expires = _extract_dates(rule_text)
            rules.append({
                "rule_text": rule_text,
                "effective": effective,
                "expires": expires,
                "source_url": WDFW_EMERGENCY_URL,
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            })

    if not rules:
        log.warning(
            "wdfw_emergency_no_rules_found",
            extra={"url": WDFW_EMERGENCY_URL},
        )

    return rules


# ---------------------------------------------------------------------------
# Date extraction helpers
# ---------------------------------------------------------------------------

_DATE_PATTERN = re.compile(
    r"(?:effective|from|starting|begins?)[:\s]+([A-Z][a-z]+ \d{1,2},? \d{4})"
    r"|([A-Z][a-z]+ \d{1,2},? \d{4})\s*[-–]\s*([A-Z][a-z]+ \d{1,2},? \d{4})",
    re.IGNORECASE,
)
_EXPIRY_PATTERN = re.compile(
    r"(?:expires?|through|until|ends?)[:\s]+([A-Z][a-z]+ \d{1,2},? \d{4})",
    re.IGNORECASE,
)


def _parse_date(s: str) -> str | None:
    """Parse a human-readable date string to ISO format, or return None."""
    if not s:
        return None
    s = s.strip().rstrip(",")
    for fmt in ("%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _extract_dates(text: str) -> tuple[str | None, str | None]:
    """Return (effective_date, expiry_date) extracted from rule text."""
    effective = None
    expires = None

    m = _DATE_PATTERN.search(text)
    if m:
        if m.group(1):
            effective = _parse_date(m.group(1))
        elif m.group(2) and m.group(3):
            effective = _parse_date(m.group(2))
            expires = _parse_date(m.group(3))

    m2 = _EXPIRY_PATTERN.search(text)
    if m2:
        expires = _parse_date(m2.group(1))

    return effective, expires
