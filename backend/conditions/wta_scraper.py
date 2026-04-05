# Fingerprint: .trip-report-list, .trip-report, article.trip-report — validated YYYY-MM-DD
# NOTE: validate selector against https://www.wta.org/go-hiking/hikes/{slug}
#       at Phase 1 exit and update the date above.
"""
WTA trail report scraper — daily 3AM Pacific via APScheduler.

For each spot with a wta_trail_url, fetches recent trip reports and runs
them through the WTA fishing-intent classifier (§18.7).  Reports with no
fishing signal are discarded entirely — no location extraction attempted.

Wrapped with the wta_breaker circuit breaker.
Raises ScraperStructureError if the page structure has changed.
"""

import logging
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

# Structural fingerprints — at least one must be present
_FINGERPRINT_SELECTORS = ".trip-report-list, .trip-report, article.trip-report"

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
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    if not soup.select_one(_FINGERPRINT_SELECTORS):
        raise ScraperStructureError(
            source="wta",
            url=url,
            detail="Expected trip report selector not found — WTA page structure may have changed",
        )

    reports = []
    for item in soup.select(".trip-report, article.trip-report")[:_REPORT_LIMIT]:
        text = item.get_text(separator=" ", strip=True)
        if not text:
            continue
        date_tag = item.select_one("time, .date, .trip-report-date")
        date_str = date_tag.get("datetime") or date_tag.get_text(strip=True) if date_tag else None
        reports.append({"text": text, "date": date_str})

    return reports


async def _classify(report_text: str) -> dict:
    prompt = WTA_FISHING_INTENT_PROMPT.format(report_text=report_text)
    return await call_json_llm(
        prompt=prompt,
        model=CHAT_MODEL,
        default=_WTA_FISHING_INTENT_DEFAULT,
    )
