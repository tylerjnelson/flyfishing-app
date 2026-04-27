# Fingerprint: h3 a[href*="trip_report"] in @@related_tripreport_listing — validated 2026-04-23
# Body text: article p on each individual report detail page — validated 2026-04-23
# Trail conditions: div.trail-issues in @@related_tripreport_listing — validated 2026-04-26
# NOTE: validate selectors against https://www.wta.org/go-hiking/hikes/{slug}/@@related_tripreport_listing
#       and update the date above if structure changes.
"""
WTA trail report scraper — daily 3AM Pacific via APScheduler.

For each spot with a wta_trail_url, fetches recent trip reports and runs
them through the WTA fishing-intent classifier (§18.7).  Reports with no
fishing signal are discarded entirely — no location extraction attempted.

Also extracts structured trail condition data (div.trail-issues) from the
listing page — road access, snow, bugs, trail obstacles — at no extra HTTP
cost since the listing page is already fetched.

The @@related_tripreport_listing URL returns heading links only (no body text).
Body text is fetched from each individual report detail page (article p selector).

Wrapped with the wta_breaker circuit breaker.
Raises ScraperStructureError if the page structure has changed.
"""

import logging
import re
from datetime import datetime, timezone

import httpx
import pybreaker
from bs4 import BeautifulSoup

from conditions.circuit_breaker import wta_breaker
from exceptions import ScraperStructureError

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)
_REPORT_LIMIT = 5  # max recent reports per trail (listing page shows 5 per page)

# Fingerprint: trip report headings link to URLs containing /trip_report
_FINGERPRINT_SELECTOR = 'h3 a[href*="trip_report"]'

# Date pattern embedded in report headings: "Trail Name — Mar. 26, 2026"
_DATE_RE = re.compile(r"—\s+(\w+\.?\s+\d+,\s+\d{4})")

# ---------------------------------------------------------------------------
# Keyword-based fishing-intent classifier
# ---------------------------------------------------------------------------

# Checked first — any match short-circuits to False
_NEG_RE = re.compile(
    r"\bno\s+fishing\b|\bfishing\s+(?:is\s+)?(?:prohibited|closed|not\s+allowed)\b"
    r"|\bfishing\s+prohibited\b|\bfishing\s+closure\b",
    re.IGNORECASE,
)

# Any match → True immediately
_STRONG_RE = re.compile(
    r"\bfishing\b|\bfly\s+rod\b|\bfly\s+reel\b|\bangling\b"
    r"|\bhooked\s+(?:a|the|into)\b|\blanded\s+(?:a|the)\b"
    r"|\bfish\s+on\b|\bdry\s+fly\b|\bwet\s+fly\b"
    r"|\bstreamer\b|\bnymph\b|\blure\b|\bspinner\b|\bfly\s+hatch\b",
    re.IGNORECASE,
)

# Species word + action verb within ±60 chars → True
_SPECIES_RE = re.compile(
    r"\b(?:trout|salmon|steelhead|rainbow|cutthroat|kokanee|bass|"
    r"dolly\s+varden|bull\s+trout|brook\s+trout)\b",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"\b(?:caught|hooked|landed|targeted|fished|casting)\b",
    re.IGNORECASE,
)


def _has_fishing_intent(text: str) -> bool:
    if _NEG_RE.search(text):
        return False
    if _STRONG_RE.search(text):
        return True
    for m in _SPECIES_RE.finditer(text):
        window_start = max(0, m.start() - 60)
        window_end = min(len(text), m.end() + 60)
        if _ACTION_RE.search(text[window_start:window_end]):
            return True
    return False


def _extract_trail_conditions(soup: BeautifulSoup, total_reports: int) -> dict:
    """
    Aggregate condition keywords from div.trail-issues elements on the listing page.

    Returns counts per category and the total report count examined so the
    caller can compute ratios (e.g. "road flagged in 2 of 5 recent reports").
    """
    counts = {"road": 0, "snow": 0, "bugs": 0, "trail": 0}
    for div in soup.select("div.trail-issues"):
        text = div.get_text(" ", strip=True).lower()
        if re.search(r"\broad\b", text):
            counts["road"] += 1
        if re.search(r"\bsnow\b", text):
            counts["snow"] += 1
        if re.search(r"\bbug\b", text):
            counts["bugs"] += 1
        if re.search(r"\btrail\b", text):
            counts["trail"] += 1
    counts["total_reports"] = total_reports
    return counts


async def fetch_wta_reports(wta_trail_url: str) -> dict | None:
    """
    Fetch and classify recent trip reports for a single WTA trail URL.
    Also extracts structured trail condition data from the listing page.

    Returns a dict:
      {
        "fishing_reports": [{report_text, note_date, fishing_intent, confidence,
                             evidence, source_url, fetched_at}, ...],
        "trail_conditions": {"road": N, "snow": N, "bugs": N, "trail": N,
                             "total_reports": N},
      }

    Returns None when the circuit is open.
    Raises ScraperStructureError if the page structure has changed.
    """
    try:
        raw_reports, trail_conditions = await _scrape_reports(wta_trail_url)
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "wta", "url": wta_trail_url})
        return None

    fishing_reports = []
    for report in raw_reports:
        if not _has_fishing_intent(report["text"]):
            continue
        fishing_reports.append({
            "report_text": report["text"],
            "note_date": report.get("date"),
            "source_url": wta_trail_url,
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        })

    return {"fishing_reports": fishing_reports, "trail_conditions": trail_conditions}


@wta_breaker
async def _scrape_reports(url: str) -> tuple[list[dict], dict]:
    """
    Fetch the listing page and each report detail page.

    Returns (raw_reports, trail_conditions).
    raw_reports: [{text, date}] — body text from detail pages.
    trail_conditions: aggregated div.trail-issues counts from the listing page.
    """
    listing_url = url.rstrip("/") + "/@@related_tripreport_listing"

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(listing_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        if not soup.select_one(_FINGERPRINT_SELECTOR):
            raise ScraperStructureError(
                source="wta",
                url=listing_url,
                detail="Expected 'h3 a[href*=trip_report]' not found — WTA listing structure may have changed",
            )

        # Extract report links and dates from the listing page
        link_items = []
        for a in soup.select(_FINGERPRINT_SELECTOR)[:_REPORT_LIMIT]:
            href = a.get("href", "")
            if not href:
                continue
            date_str = None
            m = _DATE_RE.search(a.get_text(separator=" ", strip=True))
            if m:
                date_str = m.group(1)
            link_items.append({"href": href, "date": date_str})

        # Extract trail conditions from listing page (no extra HTTP requests)
        trail_conditions = _extract_trail_conditions(soup, total_reports=len(link_items))

        # Fetch each report detail page for body text (article p)
        reports = []
        for item in link_items:
            try:
                r = await client.get(item["href"])
                r.raise_for_status()
                detail = BeautifulSoup(r.text, "html.parser")
                body_text = " ".join(
                    p.get_text(separator=" ", strip=True)
                    for p in detail.select("article p")
                ).strip()
                if body_text:
                    reports.append({"text": body_text, "date": item["date"]})
            except Exception as exc:
                log.debug("wta_detail_fetch_failed", extra={"href": item["href"], "error": str(exc)})
                continue

    return reports, trail_conditions


