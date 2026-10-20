"""
Annual WDFW seasons (TODO-22): pamphlet dates, name matching, the season
status over a trip, the context builder's season filter, and the page parser.
The last block checks the shipped data/wdfw_seasons.json on waters whose
2026-27 seasons were read by hand from the pamphlet.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from chat.context_builder import _season_filter
from conditions import wdfw_seasons as ws
from scripts.build_wdfw_seasons import parse_page


# --- pamphlet dates ---------------------------------------------------------

@pytest.mark.parametrize("text,year,expected", [
    ("Sat. before Memorial Day", 2026, date(2026, 5, 23)),     # Memorial Day 2026 is May 25
    ("Fri. before Memorial Day", 2027, date(2027, 5, 28)),     # May 31, 2027
    ("4th Sat. in Apr.", 2027, date(2027, 4, 24)),
    ("2nd Sat. in March", 2027, date(2027, 3, 13)),
    ("First Sat. in June", 2027, date(2027, 6, 5)),
    ("3rd Sun. in Oct", 2026, date(2026, 10, 18)),
    ("1st Fri. after Thanksgiving", 2026, date(2026, 11, 27)),
    ("Last Day of Feb.", 2028, date(2028, 2, 29)),
    ("Sept. 19", 2026, date(2026, 9, 19)),
    ("First Sat. after Labor Day", 2026, date(2026, 9, 12)),
])
def test_date_points(text, year, expected):
    assert ws._point(text)(year) == expected


@pytest.mark.parametrize("text,day,expected", [
    ("Year-round", date(2027, 2, 1), True),
    ("Sat. before Memorial Day-Oct. 31", date(2026, 10, 31), True),
    ("Sat. before Memorial Day-Oct. 31", date(2026, 11, 1), False),
    ("Sat. before Memorial Day-Oct. 31", date(2027, 5, 21), False),
    ("Sat. before Memorial Day-Oct. 31", date(2027, 5, 29), True),
    ("Dec. 1-Last Day of Feb.", date(2027, 1, 15), True),            # wraps the new year
    ("Dec. 1-Last Day of Feb.", date(2026, 11, 30), False),
    ("Sat. before Memorial Day-Mar. 31", date(2027, 1, 10), True),
    ("Sat. before Memorial Day-Mar. 31", date(2027, 4, 15), False),
    ("Mar. 1-Day before opening of general waterfowl season", date(2026, 10, 1), None),  # unreadable
])
def test_in_range(text, day, expected):
    assert ws.in_range(text, day) is expected


def test_pamphlet_year():
    assert ws.pamphlet_year(date(2026, 7, 1)) == "2026-27"
    assert ws.pamphlet_year(date(2027, 6, 30)) == "2026-27"
    assert ws.pamphlet_year(date(2026, 6, 30)) == "2025-26"


# --- names ------------------------------------------------------------------

@pytest.mark.parametrize("ours,pamphlet", [
    ("North Fork Stillaguamish River", "STILLAGUAMISH RIVER, NORTH FORK"),
    ("Lake Sammamish", "SAMMAMISH, LAKE"),
    ("WOODLAND CR  13.0006", "WOODLAND CREEK"),
    ("NEWAUKUM R-NF23.0887", "NEWAUKUM RIVER, NORTH FORK"),
    ("SATSOP R -EF 22.0360", "SATSOP RIVER AND EAST FORK"),
    ("Icicle Creek", "ICICLE CREEK AND TRIBUTARIES"),
    ("Elwha River", "ELWHA RIVER AND TRIBUTARIES"),
    ("South Teal Lake", "TEAL LAKE, NORTH & SOUTH"),
    ("Duwamish River", "GREEN (DUWAMISH) RIVER"),
    ("Green River", "GREEN (DUWAMISH) RIVER"),
    ("Lake Spokane", "LONG LAKE (LAKE SPOKANE)"),
    ("MEDICAL LK (SPOK)", "MEDICAL LAKE"),
    ("Upper Caliche Lake", "CALICHE LAKE, UPPER"),
])
def test_name_keys_meet(ours, pamphlet):
    assert ws.name_keys(ours) & ws.name_keys(pamphlet)


def test_name_keys_keep_forks_apart():
    assert not ws.name_keys("North Fork Stillaguamish River") & ws.name_keys("STILLAGUAMISH RIVER")
    assert not ws.name_keys("Lewis River") & ws.name_keys("LEWIS RIVER, EAST FORK")


# --- matching ---------------------------------------------------------------

def _row(species, dates, rules="Statewide min. size/daily limit."):
    return {"species": species, "dates": dates, "rules": rules}


def _pamphlet(*entries, year="2026-27"):
    return ws.from_json({"year": year, "entries": [
        {"name": n, "counties": c, "kind": k, "reaches": r} for n, c, k, r in entries]})


FISH_LAKES = _pamphlet(
    ("FISH LAKE", ["SPOKANE"], "lake", [{"desc": "", "rows": [_row("All game fish", "4th Sat. in Apr.-Sept. 30")]}]),
    ("FISH LAKE", ["OKANOGAN"], "lake", [{"desc": "", "rows": [_row("All game fish", "Year-round")]}]),
    ("FISH CREEK", ["CHELAN"], "river", [{"desc": "", "rows": [_row("Trout", "Year-round")]}]),
)


def test_match_needs_the_county_when_a_name_repeats():
    assert ws.match("Fish Lake", None, "Spokane", "lake", pamphlet=FISH_LAKES).counties == {"SPOKANE"}
    assert ws.match("Fish Lake", None, None, "lake", pamphlet=FISH_LAKES) is None
    assert ws.match("Fish Lake", None, "Chelan", "lake", pamphlet=FISH_LAKES) is None   # a different lake


def test_match_respects_water_type():
    assert ws.match("Fish Creek", None, "Chelan", "lake", pamphlet=FISH_LAKES) is None
    assert ws.match("Fish Creek", None, "Chelan", "creek", pamphlet=FISH_LAKES).name == "FISH CREEK"


def test_same_county_duplicates_merge():
    p = _pamphlet(
        ("NASELLE RIVER", ["PACIFIC"], "river", [{"desc": "a", "rows": [_row("Trout", "Nov. 1-Nov. 30")]}]),
        ("NASELLE RIVER", ["PACIFIC", "WAHKIAKUM"], "river", [{"desc": "b", "rows": [_row("Trout", "June 1-June 30")]}]),
    )
    e = ws.match("Naselle River", None, "Pacific", "river", pamphlet=p)
    assert [r["desc"] for r in e.reaches] == ["a", "b"]


# --- status -----------------------------------------------------------------

STILLY = _pamphlet(
    ("STILLAGUAMISH RIVER, NORTH FORK", ["SKAGIT", "SNOHOMISH"], "river", [
        {"desc": "from mouth to Hwy. 530 Bridge CRC (872)", "rows": [
            _row("All species", "", "Motors prohibited."),
            _row("All species", "Aug. 29-Nov. 30", "Fly fishing only. Night closure."),
            _row("Trout", "Aug. 29-Nov. 30", "Catch and Release."),
            _row("Trout", "Dec. 1-Jan. 31"),
        ]},
        {"desc": "from Hwy. 530 Bridge to French Creek CRC (872)", "rows": [
            _row("All species", "Nov. 1-Nov. 30", "Night closure. Fly fishing only."),
            _row("Trout", "Nov. 1-Nov. 30", "Catch and Release."),
            _row("Trout", "Dec. 1-Jan. 31"),
        ]},
        {"desc": "from the falls upstream", "rows": [_row("All species", "", "CLOSED WATERS.")]},
    ]),
    ("SKYKOMISH RIVER", ["SNOHOMISH"], "river", [
        {"desc": "from mouth to forks", "rows": [
            _row("All species", "Nov. 1-Nov. 30", "Selective gear rules."),
            _row("Other trout", "Nov. 1-Jan. 31"),
            _row("Salmon", "Nov. 1-Nov. 30", "Daily limit 2."),
        ]},
    ]),
    ("WENATCHEE RIVER", ["CHELAN"], "river", [
        {"desc": "from mouth to Icicle Rd. Bridge", "rows": [_row("All game fish", "Year-round", "Closed.")]},
        {"desc": "from Icicle Rd. Bridge upstream", "rows": [_row("All species", "", "CLOSED WATERS.")]},
    ]),
    ("SAMMAMISH, LAKE", ["KING"], "lake", [{"desc": "CRC (763)", "rows": [
        _row("All species", "Aug. 16-Nov. 30", "CLOSED to fishing within 100 yards of the mouth of Issaquah Creek."),
        _row("Other game fish", "July 1-Nov. 30"),
        _row("Other game fish", "Dec. 1-June 30"),
    ]}]),
    ("STEVENS CREEK", ["GRAYS HARBOR"], "river", [{"desc": "", "rows": [
        _row("All species", "Oct. 1-Nov. 30", "Closed."),
        _row("Trout", "Sat. before Memorial Day-Nov. 30"),
    ]}]),
    ("COOT LAKE", ["GRANT"], "lake", [{"desc": "", "rows": [_row("All game fish", "4th Sat. in Apr.-Sept. 30")]}]),
)


def _status(name, county, typ, start, end=None, p=STILLY):
    return ws.status(name, None, county, typ, start, end, pamphlet=p)


def test_listed_river_closed_until_its_season():
    assert _status("Skykomish River", "Snohomish", "river", date(2026, 10, 20)).gate
    assert not _status("Skykomish River", "Snohomish", "river", date(2026, 11, 2)).gate


def test_closed_all_year_and_closed_waters():
    assert _status("Wenatchee River", "Chelan", "river", date(2026, 7, 15)).gate


def test_dated_closure_beats_a_species_season():
    assert _status("STEVENS CR   22.0064", "Grays Harbor", "river", date(2026, 10, 20)).gate
    assert not _status("STEVENS CR   22.0064", "Grays Harbor", "river", date(2026, 9, 20)).gate


def test_partial_closure_text_does_not_close_the_lake():
    # "CLOSED to fishing within 100 yards of the mouth" closed all of Lake Sammamish in a draft.
    st = _status("Lake Sammamish", "King", "lake", date(2026, 10, 20))
    assert not st.gate and st.lines == []


def test_some_stretches_open_names_them_with_rules():
    st = _status("North Fork Stillaguamish River", "Snohomish", "river", date(2026, 10, 20))
    assert not st.gate
    assert len(st.lines) == 1
    line = st.lines[0]
    assert "open only from mouth to Hwy. 530 Bridge" in line
    assert "Fly fishing only." in line and "Catch and Release." in line
    assert "CRC" not in line and "Motors prohibited" not in line


def test_trip_that_reaches_the_opener_is_kept():
    st = _status("Skykomish River", "Snohomish", "river", date(2026, 10, 30), date(2026, 11, 2))
    assert not st.gate
    assert any("closed until Nov 1" in line for line in st.lines)


def test_unlisted_waters_follow_statewide_rules():
    assert not _status("Pine Lake", "King", "lake", date(2027, 1, 15)).gate
    assert not _status("Tahuya River", "Mason", "river", date(2026, 10, 31)).gate
    assert _status("Tahuya River", "Mason", "river", date(2026, 11, 1)).gate
    st = _status("Tahuya River", "Mason", "river", date(2026, 10, 31), date(2026, 11, 2))
    assert not st.gate and "closes after Oct 31" in st.lines[0]


def test_lake_with_a_spring_opener():
    assert _status("Coot Lake", "Grant", "lake", date(2026, 10, 20)).gate
    assert not _status("Coot Lake", "Grant", "lake", date(2027, 4, 24)).gate


def test_trip_outside_the_pamphlet_year_is_never_gated():
    st = _status("Skykomish River", "Snohomish", "river", date(2027, 8, 1))
    assert not st.gate and "outside the 2026-27" in st.lines[0]
    assert not _status("Coot Lake", "Grant", "lake", date(2027, 10, 20)).gate


def test_coastal_and_unknown_types_are_never_gated():
    assert not _status("Skykomish River", "Snohomish", "coastal", date(2026, 10, 20)).gate
    assert not _status("Skykomish River", "Snohomish", None, date(2026, 10, 20)).gate


# --- the context builder's filter ---------------------------------------------

def _pair(name, typ="river", county="Snohomish", wb_id=None):
    wb = SimpleNamespace(id=wb_id or name, name=name, aliases=None, county=county, type=typ)
    return (SimpleNamespace(id=f"fs-{name}"), wb)


@pytest.fixture
def stilly_pamphlet(monkeypatch):
    monkeypatch.setattr(ws, "_pamphlet", STILLY)
    monkeypatch.setattr(ws, "_pamphlet_failed", False)


def test_season_filter_drops_closed_and_keeps_lines(stilly_pamphlet):
    pairs = [_pair("Skykomish River"), _pair("North Fork Stillaguamish River"), _pair("Pine Lake", "lake", "King")]
    kept, lines = _season_filter(pairs, [], date(2026, 10, 20), date(2026, 10, 20))
    assert [wb.name for _, wb in kept] == ["North Fork Stillaguamish River", "Pine Lake"]
    assert "Hwy. 530" in lines["North Fork Stillaguamish River"][0]
    assert "Pine Lake" not in lines


def test_season_filter_emergency_opening_wins(stilly_pamphlet):
    rule = SimpleNamespace(title="Skykomish River opens early for coho", action="Opens the Skykomish River to coho retention.",
                           location="Skykomish River (Snohomish County)", details=None, notes=None,
                           water_body_id=None, closes_fishing=False, effective=date(2026, 10, 15), expires=date(2026, 10, 31))
    kept, lines = _season_filter([_pair("Skykomish River")], [rule], date(2026, 10, 20), date(2026, 10, 20))
    assert len(kept) == 1
    assert "emergency rule opens it" in lines["Skykomish River"][0]


def test_season_filter_ignores_rules_for_other_waters(stilly_pamphlet):
    rule = SimpleNamespace(title="Snoqualmie River opens", action="Opens the Snoqualmie River.",
                           location="Snoqualmie River (King County)", details=None, notes=None,
                           water_body_id=None, closes_fishing=False, effective=None, expires=None)
    kept, _ = _season_filter([_pair("Skykomish River")], [rule], date(2026, 10, 20), date(2026, 10, 20))
    assert kept == []


# --- page parser --------------------------------------------------------------

_PAGE = """<div class="content-designer"><table>
<tr><td class="table-h3">S</td></tr>
<tr><td class="table-h4-Puget-Sound-and-CRs" colspan="3"><p><span>STILLAGUAMISH RIVER, NORTH FORK </span>- SKAGIT/SNOHOMISH CO.</p></td></tr>
<tr><td colspan="3"><p><strong>from mouth to Hwy. 530 Bridge CRC (872)</strong></p></td></tr>
<tr><td rowspan="2"><p>All species</p></td><td></td><td><p>Motors prohibited. </p></td></tr>
<tr><td><p>Aug. 29-Nov. 30</p></td><td><p>Fly fishing only. </p></td></tr>
<tr><td rowspan="2"><p>Trout</p></td><td><p>Aug. 29-Nov. 30</p></td><td><p>Catch and Release.</p></td></tr>
<tr><td><p>Dec. 1-Jan. 31</p></td><td><p>Statewide min. size/daily limit.</p></td></tr>
<tr><td class="table-h4-Puget-Sound-and-CRs" colspan="3"><p><span>SULTAN RIVER</span>- SNOHOMISH CO.</p></td></tr>
<tr><td><p>All species</p></td><td></td><td><p>CLOSED WATERS.</p></td></tr>
</table></div>"""


def test_parse_page_follows_rowspans_and_stretches():
    entries = parse_page(_PAGE, "puget-sound-coastal-rivers-special-rules-s-z")
    assert [e["name"] for e in entries] == ["STILLAGUAMISH RIVER, NORTH FORK", "SULTAN RIVER"]
    nf = entries[0]
    assert nf["counties"] == ["SKAGIT", "SNOHOMISH"] and nf["kind"] == "river"
    rows = nf["reaches"][0]["rows"]
    assert nf["reaches"][0]["desc"] == "from mouth to Hwy. 530 Bridge CRC (872)"
    assert [(r["species"], r["dates"]) for r in rows] == [
        ("All species", ""), ("All species", "Aug. 29-Nov. 30"), ("Trout", "Aug. 29-Nov. 30"), ("Trout", "Dec. 1-Jan. 31")]
    assert entries[1]["reaches"][0] == {"desc": "", "rows": [{"species": "All species", "dates": "", "rules": "CLOSED WATERS."}]}


def test_parse_page_refuses_a_changed_layout():
    with pytest.raises(SystemExit):
        parse_page("<div class='content-designer'><table></table></div>", "westside-lakes-special-rules-a-e")


# --- the shipped 2026-27 data ---------------------------------------------------

@pytest.fixture
def shipped(monkeypatch):
    monkeypatch.setattr(ws, "_pamphlet", None)
    monkeypatch.setattr(ws, "_pamphlet_failed", False)
    p = ws.load()
    assert p is not None and p.year == "2026-27" and len(p.entries) > 900
    return p


@pytest.mark.parametrize("name,aliases,county,typ,day,gated", [
    ("Skykomish River", None, "Snohomish", "river", date(2026, 10, 20), True),       # opens Nov. 1
    ("Skykomish River", None, "Snohomish", "river", date(2026, 11, 15), False),
    ("Cedar River", None, "King", "river", date(2026, 10, 20), True),                # closes Aug. 31
    ("Wenatchee River", None, "Chelan", "river", date(2026, 10, 20), True),          # closed year-round
    ("Lake Sammamish", None, "King", "lake", date(2026, 10, 20), False),
    ("Coot Lake", None, "Grant", "lake", date(2026, 10, 20), True),                  # 4th Sat. in Apr.-Sept. 30
    ("Yakima River", ["Yakima River"], None, "river", date(2027, 1, 15), False),     # open year-round in stretches
    ("North Fork Stillaguamish River", None, "Snohomish", "river", date(2026, 10, 20), False),
])
def test_shipped_pamphlet(shipped, name, aliases, county, typ, day, gated):
    assert ws.status(name, aliases, county, typ, day).gate is gated


def test_shipped_pamphlet_names_the_open_stilly_stretch(shipped):
    st = ws.status("North Fork Stillaguamish River", None, "Snohomish", "river", date(2026, 10, 20))
    assert "from mouth to Hwy. 530 Bridge" in st.lines[0]
