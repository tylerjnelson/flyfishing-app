# Fingerprint: .view-content, .views-field-title — validated 2026-04-10
# NOTE: validate selector against https://wdfw.wa.gov/fishing/regulations/emergency-rules
#       and update the date above after each validation.
"""
WDFW Emergency Rules scraper — 2-hour interval via APScheduler.

This fetcher is EXEMPT from the circuit breaker (§4.4).  Failures log at
WARNING and the last cached emergency_closures rows are served unmodified.
A CircuitBreakerError must never silently suppress closure data.

On ScraperStructureError (page structure changed): log CRITICAL.

Page structure (validated 2026-04-10):
  .view-content
    .item-list
      h2        ← month heading, e.g. "April 2026"
      ul
        li
          .views-field-title
            a[href=/fishing/regulations/emergency-rules/{slug}]

Detail pages at /fishing/regulations/emergency-rules/{slug}:
  p.posted-date  ← "Posted: April 1, 2026"
  .field-item p  ← rule body paragraphs (Action, Species, Dates, Location)
"""

import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from exceptions import ScraperStructureError

log = logging.getLogger(__name__)

WDFW_EMERGENCY_URL = "https://wdfw.wa.gov/fishing/regulations/emergency-rules"
_WDFW_BASE = "https://wdfw.wa.gov"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)

# Structural fingerprint — at least one of these selectors must be present
_FINGERPRINT_SELECTORS = ".view-content, .views-field-title"


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

        # Collect (title, href, fallback_month_str) from listing page
        rule_links: list[tuple[str, str, str | None]] = []
        for item_list in soup.select(".view-content .item-list"):
            month_heading = item_list.find("h2")
            month_str = month_heading.get_text(strip=True) if month_heading else None
            for li in item_list.select("li"):
                a = li.select_one(".views-field-title a")
                if not a:
                    continue
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = _WDFW_BASE + href
                rule_links.append((title, href, month_str))

        if not rule_links:
            log.warning("wdfw_emergency_no_rule_links", extra={"url": WDFW_EMERGENCY_URL})
            return []

        rules = []
        fetched_at = datetime.now(tz=timezone.utc).isoformat()

        for title, href, month_str in rule_links:
            try:
                detail_resp = await client.get(href)
                detail_resp.raise_for_status()
                detail_soup = BeautifulSoup(detail_resp.text, "html.parser")

                # Collect rule body from .field-item paragraphs
                paragraphs = [
                    p.get_text(separator=" ", strip=True)
                    for p in detail_soup.select(".field-item p")
                    if p.get_text(strip=True)
                ]
                rule_text = title + ". " + " ".join(paragraphs) if paragraphs else title

                # Prefer posted-date on detail page; fall back to month heading
                posted = detail_soup.select_one("p.posted-date")
                posted_str = posted.get_text(strip=True).replace("Posted:", "").strip() if posted else None
                effective, expires = _extract_dates(rule_text)
                if not effective and posted_str:
                    effective = _parse_date(posted_str)
                if not effective and month_str:
                    effective = _parse_month(month_str)

                rules.append({
                    "rule_text": rule_text,
                    "effective": effective,
                    "expires": expires,
                    "source_url": href,
                    "fetched_at": fetched_at,
                })
            except httpx.HTTPError as exc:
                log.warning("wdfw_emergency_detail_fetch_failed", extra={"url": href, "error": str(exc)})
                # Still include the listing-level title so the rule isn't silently dropped
                effective = _parse_month(month_str) if month_str else None
                rules.append({
                    "rule_text": title,
                    "effective": effective,
                    "expires": None,
                    "source_url": href,
                    "fetched_at": fetched_at,
                })

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


def _parse_month(s: str) -> str | None:
    """
    Parse a month heading like 'April 2026' to the first of that month in ISO format.
    Returns None if unparseable.
    """
    if not s:
        return None
    s = s.strip()
    for fmt in ("%B %Y", "%b %Y"):
        try:
            return datetime.strptime(s, fmt).date().replace(day=1).isoformat()
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
