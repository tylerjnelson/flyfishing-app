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
from datetime import date, datetime, timezone

import httpx
from bs4 import BeautifulSoup

from exceptions import ScraperStructureError

log = logging.getLogger(__name__)

WDFW_EMERGENCY_URL = "https://wdfw.wa.gov/fishing/regulations/emergency-rules"
_WDFW_BASE = "https://wdfw.wa.gov"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)
# WDFW's CDN (Varnish) 403s the default "python-httpx/x.y" User-Agent; any
# identifying UA is accepted. Without this every run failed from ~2026-04-29.
_HEADERS = {"User-Agent": "FlyFishWA/1.0 (+https://waflyfish.com)"}

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
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
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
                fields = _labeled_fields(paragraphs)

                # Prefer posted-date on detail page; fall back to month heading
                posted = detail_soup.select_one("p.posted-date")
                posted_str = posted.get_text(strip=True).replace("Posted:", "").strip() if posted else None
                posted_iso = _parse_date(posted_str) if posted_str else None

                # The structured "Effective date(s):" field is authoritative; the
                # free-text regexes only matched the old "Month D, YYYY" wording,
                # so every rule ended up with no expiry (TODO-15).
                effective, expires = _parse_effective_field(fields.get("effective"), posted_iso)
                if not effective and not expires:
                    effective, expires = _extract_dates(rule_text)
                if not effective:
                    effective = posted_iso or (_parse_month(month_str) if month_str else None)

                rules.append({
                    "rule_text": rule_text,
                    "title": title,
                    "action": fields.get("action"),
                    "location": fields.get("location"),
                    "closes_fishing": _closes_fishing(fields.get("action"), title),
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
                    "title": title,
                    "action": None,
                    "location": None,
                    "closes_fishing": _closes_fishing(None, title),
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


# ---------------------------------------------------------------------------
# Structured detail-page fields (TODO-15)
# ---------------------------------------------------------------------------
# Detail pages carry labelled paragraphs, with inconsistent spacing:
#   "Action: Closes all fishing."  /  "Action : Opens the Mineral Lake ..."
#   "Effective date: Oct. 9 – Nov. 1, 2026."  /  "Effective dates: Sept. 9, 2026, until further notice."
#   "Location: Quillayute River (Clallam County), from ..."  /  "Locations: ..."

_FIELD_RE = re.compile(
    r"^\s*(action|effective dates?|species affected|locations?)\s*:\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _labeled_fields(paragraphs: list[str]) -> dict[str, str]:
    """{"action", "effective", "species", "location"} → first value found."""
    out: dict[str, str] = {}
    for p in paragraphs:
        m = _FIELD_RE.match(p)
        if not m:
            continue
        label = m.group(1).lower()
        key = ("effective" if label.startswith("effective")
               else "location" if label.startswith("location")
               else "species" if label.startswith("species")
               else "action")
        out.setdefault(key, " ".join(m.group(2).split()))
    return out


_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
        r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_MONTH_NUM = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
# "Sept. 16–Oct. 31, 2026" / "Oct. 9 – Nov. 1, 2026" / "Dec. 1, 2026 – Jan. 31, 2027"
_RANGE_TWO_MONTHS = re.compile(
    rf"{_MON}\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?\s*-\s*{_MON}\s+(\d{{1,2}}),?\s*(\d{{4}})", re.IGNORECASE)
# "Oct. 9–31, 2026"
_RANGE_ONE_MONTH = re.compile(rf"{_MON}\s+(\d{{1,2}})\s*-\s*(\d{{1,2}}),?\s*(\d{{4}})", re.IGNORECASE)
_THROUGH = re.compile(rf"\b(?:through|until)\s+{_MON}\s+(\d{{1,2}}),?\s*(\d{{4}})", re.IGNORECASE)
_SINGLE = re.compile(rf"\b{_MON}\s+(\d{{1,2}}),?\s*(\d{{4}})", re.IGNORECASE)


def _iso(month_tok: str, day: str, year: str | int) -> str | None:
    try:
        return date(int(year), _MONTH_NUM[month_tok.lower()[:3]], int(day)).isoformat()
    except (KeyError, ValueError):
        return None


def _parse_effective_field(text: str | None, posted_iso: str | None = None) -> tuple[str | None, str | None]:
    """
    Parse a detail page's "Effective date(s):" value into (effective, expires)
    ISO dates. "until further notice" → no expiry. "Immediately through X" →
    effective = posted date. Returns (None, None) when nothing parses.
    """
    if not text:
        return None, None
    t = " ".join(text.replace("\u2013", "-").replace("\u2014", "-").split())

    m = _RANGE_TWO_MONTHS.search(t)
    if m:
        m1, d1, y1, m2, d2, y2 = m.groups()
        if not y1:  # year only on the end date; a Dec→Jan range starts the year before
            y1 = int(y2) - (1 if _MONTH_NUM[m1.lower()[:3]] > _MONTH_NUM[m2.lower()[:3]] else 0)
        return _iso(m1, d1, y1), _iso(m2, d2, y2)

    m = _RANGE_ONE_MONTH.search(t)
    if m:
        mon, d1, d2, y = m.groups()
        return _iso(mon, d1, y), _iso(mon, d2, y)

    expires = None
    through = _THROUGH.search(t)
    if through:
        expires = _iso(*through.groups())
    effective = None
    single = _SINGLE.search(t[: through.start()] if through else t)
    if single:
        effective = _iso(*single.groups())
    elif re.search(r"\bimmediately\b", t, re.IGNORECASE):
        effective = posted_iso
    return effective, expires


_CLOSES_FISHING_RE = re.compile(r"\bclos(?:e|es|ed|ing)\b[^.;]{0,60}?\bfishing\b", re.IGNORECASE)
_REOPENS_RE = re.compile(r"\b(?:re)?opens?\b|\breturns?\b[^.]*\bpermanent rules\b", re.IGNORECASE)


def _closes_fishing(action: str | None, title: str | None) -> bool:
    """
    True only for rules that close fishing outright ("Closes all fishing.",
    "Closes recreational fishing in ...", title "... to close to all fishing").
    Species-retention closures ("Closes Chinook salmon retention"), openings,
    limit and gear changes are advisories, not closures.
    """
    text = action or title or ""
    m = _CLOSES_FISHING_RE.search(text)
    return bool(m) and "retention" not in m.group(0).lower()


def reopens_fishing(action: str | None) -> bool:
    """True for rules that (re)open fishing: used to supersede earlier closures."""
    return bool(action and _REOPENS_RE.search(action))


def location_head(location: str | None) -> str:
    """
    The water named at the start of a Location field, without the boundary
    landmarks that follow: "Quillayute River (Clallam County), from ... the
    confluence of Sol Duc and Bogachiel rivers" → "Quillayute River". Matching
    against the full text linked this rule to the Bogachiel (TODO-15).
    """
    if not location:
        return ""
    s = re.sub(r"\([^)]*\)", " ", location)
    s = re.split(
        r"[,;:.]|\b(?:from|north|south|east|west|upstream|downstream|near|at|between|including|below|above)\b",
        s, maxsplit=1, flags=re.IGNORECASE,
    )[0]
    return " ".join(s.split())


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
