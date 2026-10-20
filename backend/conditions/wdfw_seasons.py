"""
Annual WDFW fishing seasons from the sport fishing pamphlet (TODO-22).

The pamphlet's Special Rules list, per water and per stretch ("from mouth to
Hwy. 530 Bridge"), every open season by species. A listed stretch is open
only on the dates it lists ("Areas listed within Special Rules show all game
fish and salmon fishing opportunities"); a water the pamphlet doesn't list
follows the statewide rules: lakes, ponds and reservoirs open year-round,
rivers, streams and beaver ponds the Saturday before Memorial Day through
Oct. 31.

  scripts/build_wdfw_seasons.py   pamphlet pages -> data/wdfw_seasons.json
  match(...)                      a water body -> its pamphlet entry (or None)
  status(...)                     open / partly open / closed over a trip

The context builder drops waters that are closed for the whole trip (a hard
filter, like an emergency closure) unless an emergency rule opens them, and
tells the model when only some stretches are open, or when the season opens
or closes during the trip.

The pamphlet runs July 1 to June 30 (2026-27). A trip outside that window is
not gated: the dates may have changed, and the model is told so.

Pure functions plus a lazily loaded JSON file; no DB access.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "wdfw_seasons.json"
PAMPHLET_URL = "https://www.eregulations.com/washington/fishing"

RIVER_TYPES = ("river", "creek")
LAKE_TYPES = ("lake",)

# Species whose season makes a water worth recommending. Sturgeon (catch and
# release only), shad, carp and the marine species are left out: a stretch
# open only for them is not open for this app's anglers.
_GAME_SPECIES = re.compile(
    r"\b(?:all species|all game fish|trout|other game fish|steelhead|salmon|kokanee|whitefish|"
    r"bass|walleye|catfish|crappie|perch|bluegill|grayling|dolly varden|char)\b",
    re.IGNORECASE,
)
# A dated row that closes the stretch outright. "CLOSED to fishing within 100
# yards of the mouth", "CLOSED Sundays and Mondays" close part of it: not a closure.
_CLOSED_RULE = re.compile(r"^\s*closed(?: waters)?\.?\s*$", re.IGNORECASE)

STATEWIDE_RIVER_SEASON = "Sat. before Memorial Day-Oct. 31"
STATEWIDE_LAKE_SEASON = "Year-round"


# ---------------------------------------------------------------------------
# Pamphlet dates
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ORD = {"first": 1, "1st": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3, "4th": 4, "fourth": 4}
_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _month(tok: str) -> int | None:
    return _MONTHS.get(tok.strip(". ").lower()[:3])


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _memorial_day(year: int) -> date:
    d = date(year, 5, 31)
    return d - timedelta(days=d.weekday())


def _labor_day(year: int) -> date:
    return _nth_weekday(year, 9, 0, 1)


def _thanksgiving(year: int) -> date:
    return _nth_weekday(year, 11, 3, 4)


_MDAY = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2})$")
_NTH_IN = re.compile(r"^(first|\d(?:st|nd|rd|th))\s+([A-Za-z]{3})[a-z]*\.?\s+in\s+([A-Za-z]+)\.?$", re.IGNORECASE)
_BEFORE_MEMORIAL = re.compile(r"^(sat|fri)[a-z]*\.?\s+before\s+memorial\s+day$", re.IGNORECASE)
_AFTER_THANKSGIVING = re.compile(r"^(first|\d(?:st|nd|rd|th))\s+fri[a-z]*\.?\s+after\s+thanksgiving$", re.IGNORECASE)
_AFTER_LABOR = re.compile(r"^first\s+sat[a-z]*\.?\s+after\s+labor\s+day$", re.IGNORECASE)
_FRI_AFTER_LABOR_SAT = re.compile(r"^fri[a-z]*\.?\s+following\s+first\s+sat[a-z]*\.?\s+after\s+labor\s+day$", re.IGNORECASE)


def _point(text: str):
    """A function year -> date for one end of a pamphlet range, or None if unknown."""
    t = " ".join(text.split()).strip()
    m = _MDAY.match(t)
    if m and _month(m.group(1)):
        mon, day = _month(m.group(1)), int(m.group(2))
        return lambda y: date(y, mon, min(day, calendar.monthrange(y, mon)[1]))
    if re.match(r"^last day of feb", t, re.IGNORECASE):
        return lambda y: date(y, 2, calendar.monthrange(y, 2)[1])
    m = _BEFORE_MEMORIAL.match(t)
    if m:
        back = 2 if m.group(1).lower() == "sat" else 3
        return lambda y: _memorial_day(y) - timedelta(days=back)
    m = _NTH_IN.match(t)
    if m and _month(m.group(3)) and m.group(2).lower() in _WEEKDAY:
        n, wd, mon = _ORD[m.group(1).lower()], _WEEKDAY[m.group(2).lower()], _month(m.group(3))
        return lambda y: _nth_weekday(y, mon, wd, n)
    m = _AFTER_THANKSGIVING.match(t)
    if m:
        n = _ORD[m.group(1).lower()]
        return lambda y: _thanksgiving(y) + timedelta(days=1 + 7 * (n - 1))
    if _AFTER_LABOR.match(t):
        return lambda y: _labor_day(y) + timedelta(days=5)
    if _FRI_AFTER_LABOR_SAT.match(t):
        return lambda y: _labor_day(y) + timedelta(days=11)
    return None


@lru_cache(maxsize=4096)
def parse_range(text: str):
    """("year-round",) | (start_fn, end_fn) | None for a pamphlet date cell."""
    t = " ".join((text or "").split())
    if not t:
        return None
    if t.lower() == "year-round":
        return ("year-round",)
    parts = re.split(r"\s*[-–—]\s*", t)
    if len(parts) != 2:
        return None
    a, b = _point(parts[0]), _point(parts[1])
    if a is None or b is None:
        return None
    return (a, b)


def in_range(text: str, day: date) -> bool | None:
    """True/False for a parsed range, None when the pamphlet text can't be read."""
    r = parse_range(text)
    if r is None:
        return None
    if r[0] == "year-round":
        return True
    start, end = r
    for y in (day.year - 1, day.year):
        s = start(y)
        e = end(y)
        if e < s:
            e = end(y + 1)
        if s <= day <= e:
            return True
    return False


def pamphlet_year(day: date) -> str:
    """The pamphlet in force on a day: '2026-27' from July 1, 2026 to June 30, 2027."""
    y = day.year if day.month >= 7 else day.year - 1
    return f"{y}-{str(y + 1)[2:]}"


# ---------------------------------------------------------------------------
# Water names
# ---------------------------------------------------------------------------

_ABBR = {"LK": "LAKE", "LKS": "LAKES", "R": "RIVER", "CR": "CREEK", "PD": "POND", "RES": "RESERVOIR",
         "C": "CREEK", "MT": "MOUNT", "ST": "SAINT"}
_FORK = {"NF": "NORTH FORK", "SF": "SOUTH FORK", "EF": "EAST FORK", "WF": "WEST FORK", "MF": "MIDDLE FORK"}
_STOCK_CODE = re.compile(r"\s*\d{2}\.\d{4}$")
_COUNTY_ABBR = re.compile(r"\s*\([A-Z]{3,4}\)$")
_FORK_SUFFIX = re.compile(r"\s*-\s*(NF|SF|EF|WF|MF)\b")


def _clean(s: str) -> str:
    s = s.upper().replace("&", " AND ").replace("’", "'")
    s = re.sub(r"[.'`]", "", s)
    s = re.sub(r"[^A-Z0-9(),/ -]", " ", s)
    return " ".join(s.split())


def _expand(s: str) -> str:
    return " ".join(_ABBR.get(w, w) for w in s.split())


def _paren_variants(s: str) -> list[str]:
    """'GREEN (DUWAMISH) RIVER' -> GREEN RIVER, DUWAMISH RIVER; 'LONG LAKE (LAKE SPOKANE)' -> LONG LAKE, LAKE SPOKANE."""
    m = re.search(r"\(([^)]*)\)", s)
    if not m:
        return [s]
    before, inner, after = s[:m.start()].strip(), m.group(1).strip(), s[m.end():].strip()
    out = [" ".join(f"{before} {after}".split())]
    if after:
        words = before.split()
        out.append(" ".join([*words[:-1], inner, after]))
    elif inner:
        out.append(inner)
    return [v for o in out for v in _paren_variants(o)]


_AND_WHOLE = re.compile(r"\s+AND\s+(?:TRIBUTARIES|ALL FORKS|PONDS|OUTLET|INLET AND OUTLET STREAMS)$")


def _and_variants(s: str) -> list[str]:
    """'ICICLE RIVER AND TRIBUTARIES' -> ICICLE RIVER; 'SATSOP RIVER AND EAST FORK' ->
    SATSOP RIVER, EAST FORK SATSOP RIVER; 'ROYAL LAKE AND ROYAL SLOUGH' -> both."""
    if "," in s or " AND " not in s:
        return [s]
    if _AND_WHOLE.search(s):
        return [_AND_WHOLE.sub("", s)]
    head, tail = (x.strip() for x in s.split(" AND ", 1))
    if tail.endswith("FORK"):
        return [head, f"{tail} {head}"]
    return [head, tail]


def _comma_variants(s: str) -> list[str]:
    """Pamphlet order 'STILLAGUAMISH RIVER, NORTH FORK' -> 'NORTH FORK STILLAGUAMISH RIVER';
    'TEAL LAKE, NORTH AND SOUTH' -> NORTH TEAL LAKE, SOUTH TEAL LAKE;
    'NEWAUKUM RIVER, INCLUDING SOUTH FORK' -> NEWAUKUM RIVER, SOUTH FORK NEWAUKUM RIVER."""
    if "," not in s:
        return [s]
    base, rest = (x.strip() for x in s.split(",", 1))
    out = []
    if rest.startswith("INCLUDING "):
        out.append(base)
        rest = rest[len("INCLUDING "):]
    for part in re.split(r"\s+AND\s+", rest):
        part = part.strip()
        if not part:
            continue
        if part == "LAKE":
            out.append(f"LAKE {base}")
        elif part in ("FORK",) or part.endswith("FORK") and len(part.split()) == 1:
            out.append(f"{part} {base}")
        else:
            out.append(f"{part} {base}")
            # 'DICKEY RIVER, EAST FORK AND WEST FORK'; 'GREEN LAKES, LOWER AND UPPER'
            if base.endswith("LAKES"):
                out.append(f"{part} {base[:-1]}")
    return out


def _lake_variants(s: str) -> list[str]:
    """LAKE X <-> X LAKE (the pamphlet, the stocking report and our names disagree)."""
    out = [s]
    if s.startswith("LAKE ") and len(s.split()) > 1:
        out.append(f"{s[5:]} LAKE")
    elif s.endswith(" LAKE"):
        out.append(f"LAKE {s[:-5]}")
    return out


def name_keys(name: str | None) -> set[str]:
    """Comparable forms of a water's name, from our names, stocking codes or the pamphlet."""
    if not name:
        return set()
    s = _STOCK_CODE.sub("", " ".join(name.split()))
    s = re.sub(r"(?<=[A-Z])\d{2}\.\d{4}$", "", s)        # 'NEWAUKUM R-NF23.0887'
    s = _clean(s)
    s = _COUNTY_ABBR.sub("", s).strip()
    fork = _FORK_SUFFIX.search(s)
    if fork:
        s = f"{_FORK[fork.group(1)]} {_FORK_SUFFIX.sub('', s)}"
    s = re.sub(r"(R|CR)-(NF|SF|EF|WF|MF)$", lambda m: f"{m.group(1)} -{m.group(2)}", s)
    fork = _FORK_SUFFIX.search(s)
    if fork:
        s = f"{_FORK[fork.group(1)]} {_FORK_SUFFIX.sub('', s)}"
    out: set[str] = set()
    for v in _paren_variants(s):
        for c in (c for a in _and_variants(v) for c in _comma_variants(a)):
            for x in _lake_variants(_expand(" ".join(c.replace(",", " ").split()))):
                if x:
                    out.add(x)
    return out


def _counties(text: str) -> set[str]:
    """{'SKAGIT', 'SNOHOMISH'} from '- SKAGIT/SNOHOMISH CO.'."""
    m = re.search(r"-\s*([A-Z][A-Z /.]+?)\s+CO\.?", text or "")
    if not m:
        return set()
    return {c.strip() for c in m.group(1).split("/") if c.strip()}


# ---------------------------------------------------------------------------
# Pamphlet data and matching
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    name: str                       # pamphlet heading, e.g. 'STILLAGUAMISH RIVER, NORTH FORK'
    counties: set[str]
    kind: str                       # 'river' | 'lake'
    reaches: list[dict]             # [{desc, rows: [{species, dates, rules}]}]
    page: str = ""
    keys: set[str] = field(default_factory=set)


@dataclass
class Pamphlet:
    year: str                       # '2026-27'
    fetched: str
    entries: list[Entry]
    index: dict[str, list[int]]     # name key -> entry indexes


_pamphlet: Pamphlet | None = None
_pamphlet_failed = False


def load(path: Path = DATA_PATH) -> Pamphlet | None:
    global _pamphlet, _pamphlet_failed
    if _pamphlet is None and not _pamphlet_failed:
        try:
            _pamphlet = from_json(json.loads(path.read_text()))
            log.info("wdfw_seasons_loaded", extra={"entries": len(_pamphlet.entries), "year": _pamphlet.year})
        except Exception as exc:
            _pamphlet_failed = True
            log.warning("wdfw_seasons_unavailable", extra={"reason": type(exc).__name__, "detail": str(exc)[:160]})
    return _pamphlet


def from_json(data: dict) -> Pamphlet:
    entries, index = [], {}
    for i, e in enumerate(data["entries"]):
        entry = Entry(name=e["name"], counties=set(e.get("counties") or []), kind=e["kind"],
                      reaches=e["reaches"], page=e.get("page", ""))
        entry.keys = name_keys(entry.name)
        entries.append(entry)
        for k in entry.keys:
            index.setdefault(k, []).append(i)
    return Pamphlet(year=data["year"], fetched=data.get("fetched", ""), entries=entries, index=index)


# Our water bodies whose names the pamphlet spells differently. Value: the
# pamphlet heading, or None when the pamphlet has no entry for it.
OVERRIDES: dict[str, str | None] = {
    "Rowland, South": "ROWLAND LAKE, SOUTH",
    "LEWIS R -NF  27.0168": "LEWIS RIVER",     # the pamphlet lists the North Fork's stretches under it
}


def _kind(water_type: str | None) -> str | None:
    if water_type in RIVER_TYPES:
        return "river"
    if water_type in LAKE_TYPES:
        return "lake"
    return None


def match(name: str, aliases=None, county: str | None = None, water_type: str | None = None,
          pamphlet: Pamphlet | None = None) -> Entry | None:
    """The pamphlet entry for a water, or None (not listed: statewide rules apply).
    A name the pamphlet lists in several counties needs our county to pick one;
    a listed name in a different county is a different water."""
    p = pamphlet or load()
    kind = _kind(water_type)
    if p is None or kind is None:
        return None
    if name in OVERRIDES:
        heading = OVERRIDES[name]
        return next((e for e in p.entries if e.name == heading), None) if heading else None
    keys: set[str] = set()
    for n in [name, *(aliases or [])]:
        keys |= name_keys(n)
    found = {i for k in keys for i in p.index.get(k, ())}
    cands = [p.entries[i] for i in sorted(found) if p.entries[i].kind == kind]
    cty = (county or "").strip().upper().removesuffix(" COUNTY") or None
    if cty:
        cands = [e for e in cands if not e.counties or cty in e.counties]
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1 and cty:
        # The same name listed twice in our county ('NASELLE RIVER - PACIFIC' and
        # '- PACIFIC/WAHKIAKUM'): one water split across two headings, or two
        # same-named waters. Either way, open when any of them is.
        return Entry(name=cands[0].name, counties=set().union(*(e.counties for e in cands)), kind=kind,
                     reaches=[r for e in cands for r in e.reaches], page=cands[0].page)
    return None


# ---------------------------------------------------------------------------
# Season status
# ---------------------------------------------------------------------------

def _reach_open(reach: dict, day: date, kind: str) -> bool:
    """Open for game fish or salmon on this day. Unreadable dates count as open
    (never drop a water on a parse failure)."""
    rows = [r for r in reach.get("rows") or [] if r.get("species") is not None]
    closed_all = any(_CLOSED_RULE.match(r.get("rules") or "") and not (r.get("dates") or "").strip()
                     for r in rows if r["species"].lower().startswith("all"))
    dated = [r for r in rows if (r.get("dates") or "").strip()]
    # "All species | Nov. 2-Nov. 30 | Closed." closes the stretch for those dates.
    for r in dated:
        if r["species"].lower().startswith("all") and _CLOSED_RULE.match(r.get("rules") or ""):
            if in_range(r["dates"], day):
                return False
    specific = [r for r in dated if _GAME_SPECIES.search(r["species"]) and r["species"].lower() != "all species"]
    general = [r for r in dated if r["species"].lower() == "all species"]
    # An "All species" date row is an opening only where no species has its
    # own dates; otherwise it is a gear or night-closure period inside them.
    seasons = specific or general
    if not seasons:
        if closed_all:
            return False
        return bool(in_range(STATEWIDE_RIVER_SEASON if kind == "river" else STATEWIDE_LAKE_SEASON, day))
    for r in seasons:
        if _CLOSED_RULE.match(r.get("rules") or ""):
            continue
        hit = in_range(r["dates"], day)
        if hit is None or hit:
            return True
    return False


def _reaches(entry: Entry) -> list[dict]:
    """Stretches with rules; a heading with no rows at all means the statewide season."""
    return [r for r in entry.reaches if r.get("rows")] or [{"desc": "", "rows": []}]


def open_reaches(entry: Entry, day: date) -> list[dict]:
    return [r for r in _reaches(entry) if _reach_open(r, day, entry.kind)]


@dataclass
class SeasonStatus:
    gate: bool                      # closed for every day of the trip
    lines: list[str]                # for the model; empty when nothing to say
    entry: str | None = None        # pamphlet heading, or None (statewide rules)
    open_days: int = 0


_MAX_REACHES = 3
_REACH_CHARS = 110


def _reach_label(r: dict) -> str:
    d = " ".join((r.get("desc") or "").split())
    d = re.sub(r"\s*CRC \(\d+\)\s*$", "", d)
    if len(d) > _REACH_CHARS:
        d = d[:_REACH_CHARS - 1].rstrip() + "…"
    return d or "listed stretch"


def _trip_days(start: date, end: date | None) -> list[date]:
    end = max(end or start, start)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)][:14]


def status(name: str, aliases, county, water_type, start: date, end: date | None = None,
           pamphlet: Pamphlet | None = None) -> SeasonStatus:
    """The water's annual season over the trip. Waters we can't place (coastal,
    an unknown type) and trips outside the pamphlet's year are never gated."""
    p = pamphlet or load()
    kind = _kind(water_type)
    if kind is None:
        return SeasonStatus(gate=False, lines=[])
    days = _trip_days(start, end)
    if p is None or pamphlet_year(start) != p.year:
        if kind == "river":
            return SeasonStatus(gate=False, lines=[
                f"Annual season: this trip falls outside the {p.year if p else 'loaded'} WDFW pamphlet; "
                f"check the current rules ({PAMPHLET_URL})."])
        return SeasonStatus(gate=False, lines=[])
    entry = match(name, aliases, county, water_type, pamphlet=p)
    if entry is None:
        if kind == "lake":
            return SeasonStatus(gate=False, lines=[], open_days=len(days))
        open_days = [d for d in days if in_range(STATEWIDE_RIVER_SEASON, d)]
        if not open_days:
            return SeasonStatus(gate=True, lines=[], open_days=0)
        lines = []
        if len(open_days) < len(days):
            lines.append(_partial_trip_line(days, open_days, "the statewide stream season (Sat. before Memorial Day-Oct. 31)"))
        return SeasonStatus(gate=False, lines=lines, open_days=len(open_days))

    by_day = {d: open_reaches(entry, d) for d in days}
    open_days = [d for d in days if by_day[d]]
    if not open_days:
        return SeasonStatus(gate=True, lines=[], entry=entry.name)
    lines = []
    if len(open_days) < len(days):
        lines.append(_partial_trip_line(days, open_days, f"WDFW special rules for {entry.name.title()}"))
    first = open_days[0]
    lines.extend(_stretch_lines(entry, by_day[first], first, p.year))
    return SeasonStatus(gate=False, lines=lines, entry=entry.name, open_days=len(open_days))


def _day_rules(reach: dict, day: date) -> str:
    """The rules of the rows in season that day: 'Fly fishing only. Night closure. Catch and Release.'"""
    out: list[str] = []
    for r in reach.get("rows") or []:
        rules = " ".join((r.get("rules") or "").split())
        dated = (r.get("dates") or "").strip()
        if not rules or (dated and not in_range(dated, day)):
            continue
        if not dated and not r["species"].lower().startswith("all"):
            continue
        for sentence in re.split(r"(?<=\.)\s+", rules):
            if re.match(r"(?i)(fly fishing only|selective gear|catch[- ]and[- ]release|night closure|no bait|"
                        r"anti-snagging)", sentence) and sentence not in out:
                out.append(sentence)
    return " ".join(out)


def _stretch_lines(entry: Entry, open_on_day: list[dict], day: date, year: str) -> list[str]:
    """Which stretches are open on the first fishable day, when not all are. The
    shorter list is named: the open stretches, or the closed ones."""
    reaches = _reaches(entry)
    if len(open_on_day) == len(reaches):
        return []
    closed = [r for r in reaches if r not in open_on_day]
    head = f"Annual season ({year} WDFW pamphlet) on {day:%b} {day.day}:"
    if len(closed) < len(open_on_day):
        names = "; ".join(_reach_label(r) for r in closed[:_MAX_REACHES])
        more = f" (+{len(closed) - _MAX_REACHES} more)" if len(closed) > _MAX_REACHES else ""
        return [f"{head} closed {names}{more}; open elsewhere."]
    parts = []
    for r in open_on_day[:_MAX_REACHES]:
        rules = _day_rules(r, day)
        parts.append(f"{_reach_label(r)}{f' ({rules})' if rules else ''}")
    more = f" (+{len(open_on_day) - _MAX_REACHES} more)" if len(open_on_day) > _MAX_REACHES else ""
    return [f"{head} open only {'; '.join(parts)}{more}. Closed elsewhere; "
            f"send the angler to an open stretch."]


def _partial_trip_line(days: list[date], open_days: list[date], source: str) -> str:
    od = set(open_days)
    if days[0] in od:
        last = max(d for d in days if d in od and all(x in od for x in days[:days.index(d) + 1]))
        return f"Annual season: closes after {last:%b} {last.day} under {source}; fish it before then."
    return f"Annual season: closed until {open_days[0]:%b} {open_days[0].day} under {source}; not fishable before then."
