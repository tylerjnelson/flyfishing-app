"""
WDFW emergency rules as advisories for one water on one trip (TODO-21).

Closures are a hard filter (context_builder._has_active_closure). Every other
rule in effect during the trip (gear and limit changes, bait bans, openings,
a stretch closed on certain days) reaches the model as text, so it can tell
the angler. Advisories never change a spot's score.

A rule reaches a water only when it names that water (rule_applies): FK set
by the job, or one of the water's names as a whole phrase in the rule's
title, Location head or a stretch heading, and never across counties. The
Yakima salmon opener (Richland to Prosser) names "Yakima River", which none
of our Yakima waters is called, so it stays out, as intended.

Rows are EmergencyClosure ORM objects (or anything with the same attributes).
Pure functions; no DB access.
"""

from __future__ import annotations

import re
from datetime import date

from conditions.wdfw_emergency import (
    _MON,
    _MONTH_NUM,
    _closes_fishing,
    _norm,
    county_conflicts,
    phrase_in,
    rule_county,
    rule_names_water,
    section_names_other_water,
    water_phrases,
)

MAX_RULES = 3           # per water, newest first
MAX_ITEMS = 6           # stretch rules per rule
MAX_NOTES = 2           # closure sentences from "Additional information"
ITEM_CHARS = 220
ACTION_CHARS = 450

# "Oct. 12–15: ...", "Sept. 28–Oct. 31: ...", "Sept. 18–Oct. 25, 2026: ...", "Sept. 16: Closed."
_ITEM_DATE_RE = re.compile(
    rf"^{_MON}\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?"
    rf"(?:\s*[-–—]\s*(?:{_MON}\s+)?(\d{{1,2}}))?(?:,\s*(\d{{4}}))?\s*:?\s",
    re.IGNORECASE,
)
_CLOSURE_WORD_RE = re.compile(r"\bclos(?:e|ed|es|ing|ure|ures)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _attr(rule, name):
    return getattr(rule, name, None)


def _details(rule) -> list[dict]:
    d = _attr(rule, "details")
    return d if isinstance(d, list) else []


def _notes(rule) -> str:
    n = _attr(rule, "notes")
    return n if isinstance(n, str) else ""


def item_dates(item: str, year: int) -> tuple[date, date] | None:
    """The date range a stretch rule starts with ("Oct. 12–15: ..."), or None if undated."""
    m = _ITEM_DATE_RE.match(item)
    if not m:
        return None
    mon1, d1, y1, mon2, d2, y2 = m.groups()
    try:
        m1 = _MONTH_NUM[mon1.lower()[:3]]
        m2 = _MONTH_NUM[mon2.lower()[:3]] if mon2 else m1
        y_end = int(y2 or y1 or year)
        y_start = int(y1) if y1 else (y_end - 1 if m2 < m1 else y_end)
        start = date(y_start, m1, int(d1))
        end = date(y_end, m2, int(d2)) if d2 else start
    except (KeyError, ValueError):
        return None
    return start, end


def in_effect(rule, day: date) -> bool:
    eff, exp = _attr(rule, "effective"), _attr(rule, "expires")
    return (eff is None or eff <= day) and (exp is None or exp >= day)


def rule_applies(rule, *, water_body_id, name, aliases=None, county=None) -> bool:
    """The rule names this water (FK or phrase), and not a same-named water in another county."""
    title, location = _attr(rule, "title"), _attr(rule, "location")
    if county_conflicts(rule_county(title, location), county):
        return False  # also guards FKs the job set before the county check existed
    if water_body_id is not None and _attr(rule, "water_body_id") == water_body_id:
        return True
    return rule_names_water(title=title, location=location, details=_details(rule),
                            name=name, aliases=aliases, county=county)


def _dates_text(rule) -> str:
    eff, exp = _attr(rule, "effective"), _attr(rule, "expires")
    if eff and exp:
        if (eff.year, eff.month) == (exp.year, exp.month):
            return f"{eff:%b} {eff.day}–{exp.day}"
        return f"{eff:%b} {eff.day}–{exp:%b} {exp.day}"
    if eff:
        return f"from {eff:%b} {eff.day}, until further notice"
    if exp:
        return f"through {exp:%b} {exp.day}"
    return "dates not stated"


def _clip(text: str, limit: int = ITEM_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _closure_sentences(notes: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(notes) if _CLOSURE_WORD_RE.search(s)]


def _headings(rule) -> set[str]:
    return {_norm(s.get("heading")) for s in _details(rule) if s.get("heading")}


def _drop_restated(rules: list) -> list:
    """
    Drop an older rule when a newer one restates every stretch it covered:
    "Skagit and Cascade River fishing updates" (Oct 1) is replaced by
    "Skagit River fishing updates" (Oct 9) with the same four stretches but a
    new tribal closure schedule. rules must be newest first.
    """
    kept: list = []
    for rule in rules:
        heads = _headings(rule)
        if heads and any(heads <= _headings(newer) for newer in kept):
            continue
        kept.append(rule)
    return kept


def _rule_lines(rule, *, name, aliases, start: date, end: date) -> list[str]:
    action = _attr(rule, "action") or _attr(rule, "title") or ""
    lines = [f"WDFW emergency rule ({_dates_text(rule)}): {_clip(action, ACTION_CHARS)}"]

    year = (_attr(rule, "effective") or start).year
    by_item: dict[str, list[str]] = {}
    for section in _details(rule):
        heading = section.get("heading")
        if section_names_other_water(heading, name, aliases):
            continue  # "Cascade River from mouth ..." in a Skagit rule
        for item in section.get("items") or []:
            span = item_dates(item, year)
            if span and (span[1] < start or span[0] > end):
                continue  # a dated stretch rule outside the trip
            by_item.setdefault(_clip(item), []).append(heading or "")
    for item, heads in list(by_item.items())[:MAX_ITEMS]:
        where = "; ".join(h for h in dict.fromkeys(heads) if h)
        lines.append(f"  - {where}: {item}" if where else f"  - {item}")

    for sentence in _closure_sentences(_notes(rule))[:MAX_NOTES]:
        lines.append(f"  - Note: {_clip(sentence)}")
    lines.append(f"  Applies only to the waters and stretches named; source: {_attr(rule, 'source_url') or 'WDFW'}")
    return lines


def advisory_lines(
    rules: list,
    *,
    water_body_id,
    name: str,
    aliases=None,
    county: str | None = None,
    start: date,
    end: date,
) -> list[str]:
    """
    Context lines for the WDFW rules affecting this water during [start, end]
    (trip dates, Pacific). rules: every stored rule; filtered here.

    Skipped: rules not in effect during the trip, rules with no action or
    stretch rules (errata, statewide invasive-species notices), and closures
    the job marked superseded. A closure in effect on the first day is the
    hard filter's (that water never becomes a candidate); one starting later
    in the trip is listed. Also listed: an "Additional information" sentence in another
    water's rule that names this water as closed (the Stillaguamish rule notes
    the North Fork is "closed to all fishing until Nov. 1").
    """
    if not name:
        return []
    named: list = []
    mentions: list[tuple] = []
    phrases = water_phrases(name, aliases)
    for rule in rules:
        if (_attr(rule, "effective") and _attr(rule, "effective") > end) or \
                (_attr(rule, "expires") and _attr(rule, "expires") < start):
            continue
        if rule_applies(rule, water_body_id=water_body_id, name=name, aliases=aliases, county=county):
            if not (_attr(rule, "action") or _details(rule)):
                continue
            if _closes_fishing(_attr(rule, "action"), _attr(rule, "title")) and not _attr(rule, "closes_fishing"):
                continue  # superseded closure: a later rule reopened the water
            if _attr(rule, "closes_fishing") and in_effect(rule, start):
                continue  # the hard filter's job, not an advisory
            named.append(rule)
            continue
        if county_conflicts(rule_county(_attr(rule, "title"), _attr(rule, "location")), county):
            continue
        for sentence in _closure_sentences(_notes(rule)):
            text = _norm(sentence)
            if any(phrase_in(p, text) for p in phrases):
                mentions.append((rule, sentence))

    named.sort(key=lambda r: _attr(r, "effective") or date.min, reverse=True)
    lines: list[str] = []
    for rule in _drop_restated(named)[:MAX_RULES]:
        lines.extend(_rule_lines(rule, name=name, aliases=aliases, start=start, end=end))
    for rule, sentence in mentions[:MAX_NOTES]:
        lines.append(
            f"WDFW note (in the rule \"{_attr(rule, 'title')}\"): {_clip(sentence)} "
            f"Source: {_attr(rule, 'source_url') or 'WDFW'}"
        )
    return lines
