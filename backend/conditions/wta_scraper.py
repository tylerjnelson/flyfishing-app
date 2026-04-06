# Fingerprint: h3 a[href*="trip_report"] in @@related_tripreport_listing — validated 2026-04-06
# NOTE: validate selector against https://www.wta.org/go-hiking/hikes/{slug}/@@related_tripreport_listing
#       and update the date above if structure changes.
"""
WTA trail report scraper — daily 3AM Pacific via APScheduler.

For each spot with a wta_trail_url, fetches recent trip reports and runs
them through the WTA fishing-intent classifier (§18.7).  Reports with no
fishing signal are discarded entirely — no location extraction attempted.

Trip reports are fetched from the @@related_tripreport_listing sub-URL
(WTA loads reports via AJAX on the main page; the listing URL returns
pre-rendered HTML directly).

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
from llm.client import CHAT_MODEL, call_json_llm
from prompts.registry import WTA_FISHING_INTENT_PROMPT

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)
_REPORT_LIMIT = 10  # max recent reports to process per trail per run

# Fingerprint: trip report headings link to URLs containing /trip_report (matches trip_report- slugs)
_FINGERPRINT_SELECTOR = 'h3 a[href*="trip_report"]'

# Date pattern embedded in report headings: "Trail Name — Mar. 26, 2026"
_DATE_RE = re.compile(r"—\s+(\w+\.?\s+\d+,\s+\d{4})")

_WTA_FISHING_INTENT_DEFAULT = {"fishing_intent": False}


async def fetch_wta_reports(wta_trail_url: str) -> list[dict] | None:
    """
    Fetch and classify recent trip reports for a single WTA trail URL.

    Returns a list of dicts for reports that passed the fishing-intent filter:
      {report_text, note_date, fishing_intent, confidence, evidence, source_url}

    Returns None when the circuit is open.
    Raises ScraperStructureError if the page structure has changed.
    """
    try:
        reports = await _scrape_reports(wta_trail_url)
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "wta", "url": wta_trail_url})
        return None

    fishing_reports = []
    for report in reports:
        result = await _classify(report["text"])
        if not result.get("fishing_intent", False):
            continue
        fishing_reports.append({
            "report_text": report["text"],
            "note_date": report.get("date"),
            "fishing_intent": True,
            "confidence": result.get("confidence", "low"),
            "evidence": result.get("evidence", ""),
            "source_url": wta_trail_url,
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        })

    return fishing_reports


@wta_breaker
async def _scrape_reports(url: str) -> list[dict]:
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

    reports = []
    headings = soup.select("h3")[:_REPORT_LIMIT]

    for h3 in headings:
        # Extract date from heading text: "Trail Name — Mar. 26, 2026"
        heading_text = h3.get_text(separator=" ", strip=True)
        date_str = None
        m = _DATE_RE.search(heading_text)
        if m:
            date_str = m.group(1)

        # Collect all text from siblings until the next h3
        parts = [heading_text]
        for sibling in h3.next_siblings:
            if getattr(sibling, "name", None) == "h3":
                break
            if hasattr(sibling, "get_text"):
                text = sibling.get_text(separator=" ", strip=True)
                if text:
                    parts.append(text)

        full_text = " ".join(parts)
        if full_text:
            reports.append({"text": full_text, "date": date_str})

    return reports


async def _classify(report_text: str) -> dict:
    prompt = WTA_FISHING_INTENT_PROMPT.format(report_text=report_text)
    return await call_json_llm(
        prompt=prompt,
        model=CHAT_MODEL,
        default=_WTA_FISHING_INTENT_DEFAULT,
    )
