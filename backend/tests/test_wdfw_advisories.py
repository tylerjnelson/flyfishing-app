"""
TODO-21: non-closure WDFW emergency rules reach the model as advisories.

Page snippets keep the structure of the live rule pages (2026-10-13): stretch
headings as paragraphs ending in ":", the stretch rules as bullet lists, an
"Additional information" paragraph, then the boilerplate footer. No network.
"""

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bs4 import BeautifulSoup

from conditions.wdfw_advisories import advisory_lines, item_dates
from conditions.wdfw_emergency import _body_blocks, parse_rule_body

SKAGIT_PAGE = """
<div class="field-item">
<p><strong>Action:</strong> Maintains previously announced bait prohibitions, single-point barbless hooks
requirement for salmon, and reduced daily limit of two coho on the upper Skagit River.</p>
<p><strong>Effective date:</strong> Oct. 9–31, 2026.</p>
<p><strong>Species affected:</strong> All species, coho salmon.</p>
<p><strong>Specific locations and rules:</strong></p>
<p>Cascade River from mouth to Rockport-Cascade Road Bridge:</p>
<ul><li>Salmon daily limit four coho.</li><li>Closed to all fishing Sundays, Mondays, and Tuesdays.</li></ul>
<p>Skagit River from the mouth to Gilligan Creek east of Sedro-Woolley:</p>
<ul><li>Salmon daily limit two coho. Bait prohibited and single-point barbless hooks required for salmon.</li></ul>
<p>Skagit River from Gilligan Creek to Baker River:</p>
<ul>
<li>Oct. 9–11: Salmon daily limit two coho.</li>
<li>Oct. 12–15: Closed to fishing for all species.</li>
<li>Oct. 16–31: Salmon daily limit two coho.</li>
</ul>
<p>Skagit River from Baker River to Marblemount Bridge (Cascade River Road):</p>
<ul><li>Salmon daily limit two coho. Bait prohibited and single-point barbless hooks required for salmon.</li></ul>
<p><strong>Reason for action</strong>: The closure period has been reduced for the tribal fishery.</p>
<p><strong>Additional information on sport fisheries:</strong> Identify your catch.</p>
<p>Dolly Varden/bull trout remain closed to targeting or retention under the annual regulations,
and a night closure is in effect in Skagit and Cascade rivers during open periods.</p>
<p>Fishery managers encourage anglers to sign up for Fishing Rule Change email notifications.</p>
<p>Information contact: North Puget Sound Region, 425-775-1311.</p>
</div>
<div class="field-item"><ul><li>WDFW on Facebook</li></ul></div>
"""

FISH_LAKE_PAGE = """
<div class="field-item">
<p>Site search</p>
<p><strong>Action:</strong> Opens fishing with no size restrictions or daily limit for all species.</p>
<p><strong>Location:</strong> Fish Lake (Spokane County)</p>
<p><strong>Rules:</strong></p>
<ul><li>Open for fishing with no size restriction or daily limit for all species.</li></ul>
<p><strong>Additional information:</strong> Fish Lake is closed beginning Oct. 18.</p>
<p>Fishery managers encourage anglers to check for emergency rules.</p>
</div>
"""


def _parse(html):
    return parse_rule_body(_body_blocks(BeautifulSoup(html, "html.parser")))


class TestRuleBody:
    def test_stretch_rules_and_notes(self):
        details, notes = _parse(SKAGIT_PAGE)
        assert [d["heading"] for d in details] == [
            "Cascade River from mouth to Rockport-Cascade Road Bridge",
            "Skagit River from the mouth to Gilligan Creek east of Sedro-Woolley",
            "Skagit River from Gilligan Creek to Baker River",
            "Skagit River from Baker River to Marblemount Bridge (Cascade River Road)",
        ]
        assert details[2]["items"][1] == "Oct. 12–15: Closed to fishing for all species."
        assert notes.startswith("Identify your catch. Dolly Varden/bull trout remain closed")
        assert "Fishery managers" not in notes and "Facebook" not in str(details)

    def test_rules_label_and_location(self):
        details, notes = _parse(FISH_LAKE_PAGE)
        assert details == [{"heading": "Rules",
                            "items": ["Open for fishing with no size restriction or daily limit for all species."]}]
        assert notes == "Fish Lake is closed beginning Oct. 18."

    def test_boilerplate_only_notes(self):
        html = ('<div class="field-item"><p>Rules: Closed to fishing.</p>'
                '<p>Additional information: Fishery managers encourage anglers to sign up.</p></div>')
        details, notes = _parse(html)
        assert details == [{"heading": "Rules", "items": ["Closed to fishing."]}] and notes is None

    def test_long_landmark_heading(self):
        html = ('<div class="field-item"><p>From a north-south line through Buoy 10 to the Rocky Point/Tongue '
                'Point line (a projected line from Rocky Point on the Washington bank):</p>'
                '<ul><li>Oct. 1–31: Daily limit six.</li></ul></div>')
        (section,), _ = _parse(html)
        assert section["heading"].startswith("From a north-south line") and section["items"]


def test_item_dates():
    assert item_dates("Oct. 12–15: Closed.", 2026) == (date(2026, 10, 12), date(2026, 10, 15))
    assert item_dates("Sept. 28–Oct. 31: Limit six.", 2026) == (date(2026, 9, 28), date(2026, 10, 31))
    assert item_dates("Sept. 18–Oct. 25, 2026: Open.", 2025) == (date(2026, 9, 18), date(2026, 10, 25))
    assert item_dates("Sept. 3–Oct. 25, 2026 Open Thursday", 2026) == (date(2026, 9, 3), date(2026, 10, 25))
    assert item_dates("Sept. 16: Closed.", 2026) == (date(2026, 9, 16), date(2026, 9, 16))
    assert item_dates("Dec. 20–Jan. 5: Closed.", 2026) == (date(2025, 12, 20), date(2026, 1, 5))
    assert item_dates("Salmon daily limit two coho.", 2026) is None


# ---------------------------------------------------------------------------
# advisory_lines
# ---------------------------------------------------------------------------

SKAGIT = uuid.uuid4()
SKAGIT_DETAILS, SKAGIT_NOTES = _parse(SKAGIT_PAGE)


def _row(title, action, *, effective, expires=None, location=None, details=None, notes=None,
         closes_fishing=False, water_body_id=None, url="https://wdfw.wa.gov/r"):
    return SimpleNamespace(title=title, action=action, location=location, details=details, notes=notes,
                           closes_fishing=closes_fishing, water_body_id=water_body_id,
                           effective=effective, expires=expires, source_url=url)


SKAGIT_UPDATE = _row("Skagit River fishing updates", "Maintains bait prohibitions.",
                     effective=date(2026, 10, 9), expires=date(2026, 10, 31),
                     details=SKAGIT_DETAILS, notes=SKAGIT_NOTES, water_body_id=SKAGIT)
SKAGIT_EARLIER = _row("Skagit and Cascade River fishing updates", "Opens fishing in portions of Skagit River.",
                      effective=date(2026, 10, 1), expires=date(2026, 10, 31),
                      details=[{"heading": d["heading"], "items": ["Oct. 13–17: Closed to fishing."]}
                               for d in SKAGIT_DETAILS])


def _skagit(day, rules=(SKAGIT_UPDATE, SKAGIT_EARLIER), end=None):
    return advisory_lines(list(rules), water_body_id=SKAGIT, name="SKAGIT R     03.0176", aliases=None,
                          county="Skagit", start=day, end=end or day)


class TestAdvisoryLines:
    def test_skagit_on_a_closed_day(self):
        text = "\n".join(_skagit(date(2026, 10, 13)))
        assert text.startswith("WDFW emergency rule (Oct 9–31): Maintains bait prohibitions.")
        assert "Skagit River from Gilligan Creek to Baker River: Oct. 12–15: Closed to fishing for all species." \
            in text
        assert "Oct. 16–31" not in text and "Oct. 9–11" not in text
        assert "Cascade River from mouth" not in text            # another water's stretch
        assert "Opens fishing in portions" not in text           # older rule restated by the newer one
        # identical stretch rules are listed once, with every stretch they cover
        assert ("Skagit River from the mouth to Gilligan Creek east of Sedro-Woolley; Skagit River from Baker "
                "River to Marblemount Bridge (Cascade River Road): Salmon daily limit two coho.") in text
        assert "Note: Identify your catch." not in text and "night closure" in text
        assert "Applies only to the waters and stretches named; source: https://wdfw.wa.gov/r" in text

    def test_skagit_after_the_closure(self):
        text = "\n".join(_skagit(date(2026, 10, 20)))
        assert "Oct. 16–31: Salmon daily limit two coho." in text and "Oct. 12–15" not in text

    def test_rule_outside_the_trip_is_skipped(self):
        assert _skagit(date(2026, 11, 2)) == []

    def test_other_county_never_reaches_our_water(self):
        """The job linked "Fish Lake (Spokane County)" to our Fish Lake in Chelan County."""
        ours = uuid.uuid4()
        details, notes = _parse(FISH_LAKE_PAGE)
        rule = _row("Daily limits removed from Fish Lake ahead of rehabilitation", "Opens fishing.",
                    effective=date(2026, 9, 1), expires=date(2026, 10, 17), location="Fish Lake (Spokane County)",
                    details=details, notes=notes, water_body_id=ours)  # stale FK from before the county check
        day = date(2026, 10, 13)
        assert advisory_lines([rule], water_body_id=ours, name="Fish Lake", county="Chelan", start=day, end=day) == []
        lines = advisory_lines([rule], water_body_id=None, name="Fish Lake", county="Spokane", start=day, end=day)
        assert "  - Note: Fish Lake is closed beginning Oct. 18." in lines

    def test_superseded_closure_and_empty_rules_skipped(self):
        day = date(2026, 10, 13)
        superseded = _row("Quillayute River closing to all fishing", "Closes fishing.",
                          effective=date(2026, 9, 9), location="Quillayute River (Clallam County)")
        reopen = _row("Quillayute River reopens to fishing", "Reopens fishing with reduced limits.",
                      effective=date(2026, 10, 6), location="Quillayute River (Clallam County)")
        errata = _row("Quillayute River errata", None, effective=date(2026, 9, 28))
        lines = advisory_lines([superseded, reopen, errata], water_body_id=None, name="Quillayute River",
                               county="Clallam", start=day, end=day)
        assert [ln for ln in lines if ln.startswith("WDFW")] == [
            "WDFW emergency rule (from Oct 6, until further notice): Reopens fishing with reduced limits."]

    def test_closure_starting_later_in_the_trip_is_listed(self):
        closure = _row("Snohomish River to close to all fishing Oct. 3", "Closes all fishing.",
                       effective=date(2026, 10, 3), expires=date(2026, 11, 1), closes_fishing=True)
        lines = advisory_lines([closure], water_body_id=None, name="Snohomish River",
                               start=date(2026, 10, 1), end=date(2026, 10, 4))
        assert lines[0] == "WDFW emergency rule (Oct 3–Nov 1): Closes all fishing."

    def test_note_naming_another_water_as_closed(self):
        stilly = _row("Stillaguamish River to close to all fishing Oct. 9", "Closes recreational fishing.",
                      effective=date(2026, 10, 9), expires=date(2026, 11, 1), closes_fishing=True,
                      notes="The upper North Fork Stillaguamish River from the Highway 530 Bridge to North Fork "
                            "Falls is closed to all fishing until Nov. 1 under the annual fishing rules.")
        day = date(2026, 10, 13)
        lines = advisory_lines([stilly], water_body_id=None, name="North Fork Stillaguamish",
                               start=day, end=day)
        assert lines == [
            'WDFW note (in the rule "Stillaguamish River to close to all fishing Oct. 9"): The upper North Fork '
            "Stillaguamish River from the Highway 530 Bridge to North Fork Falls is closed to all fishing until "
            "Nov. 1 under the annual fishing rules. Source: https://wdfw.wa.gov/r"
        ]
        assert advisory_lines([stilly], water_body_id=None, name="Pilchuck River", start=day, end=day) == []


def test_rules_render_in_the_spot_conditions():
    from chat.context_builder import _render_spot_conditions_lines

    c = {
        "fishing_spot_id": "s1", "spot_name": "Skagit", "water_body_name": "Skagit", "spot_type": "river",
        "drive_minutes": 90, "is_haversine": False, "conditions": {},
        "wdfw_rules": ["WDFW emergency rule (Oct 9–31): Maintains bait prohibitions."],
        "warnings": ["Low flow"],
    }
    lines = _render_spot_conditions_lines(c, is_future_trip=False, departure_time=None)
    assert lines.index("WDFW emergency rule (Oct 9–31): Maintains bait prohibitions.") < \
        lines.index("Advisory: Low flow")


# ---------------------------------------------------------------------------
# job_wdfw_emergency linking
# ---------------------------------------------------------------------------

FISH_CHELAN, ICICLE, SKAGIT_WB = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
_WATERS = [
    SimpleNamespace(id=FISH_CHELAN, name="Fish Lake", aliases=None, county="Chelan"),
    SimpleNamespace(id=ICICLE, name="Icicle Creek", aliases=["Icicle Creek", "Icicle River"], county="Chelan"),
    SimpleNamespace(id=SKAGIT_WB, name="SKAGIT R     03.0176", aliases=["SKAGIT R     03.0176"], county="Skagit"),
]


async def _link(rules):
    import conditions.scheduler as sched

    added = []
    session = MagicMock()
    session.add = added.append
    lookup = MagicMock()
    lookup.all.return_value = _WATERS
    session.execute = AsyncMock(return_value=lookup)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=None)
    begin.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin)
    with patch.object(sched, "fetch_wdfw_emergency", AsyncMock(return_value=rules)), \
         patch.object(sched, "AsyncSessionLocal", MagicMock(return_value=ctx)):
        await sched.job_wdfw_emergency()
    return {row.title: row for row in added}


def _scraped(title, location=None, details=None, notes=None):
    return {"rule_text": title, "title": title, "action": "Changes rules.", "location": location,
            "closes_fishing": False, "details": details, "notes": notes, "effective": "2026-10-01",
            "expires": None, "source_url": "https://wdfw.wa.gov/x", "fetched_at": "2026-10-13T00:00:00+00:00"}


async def test_job_links_by_county_alias_code_and_stretch_heading():
    rows = await _link([
        _scraped("Daily limits removed from Fish Lake ahead of rehabilitation", "Fish Lake (Spokane County)"),
        _scraped("Icicle River to open for coho salmon retention", "Icicle River (Chelan County)"),
        _scraped("Skagit River fishing updates", details=SKAGIT_DETAILS, notes=SKAGIT_NOTES),
        _scraped("Skagit and Cascade River fishing updates", details=SKAGIT_DETAILS),
    ])
    assert rows["Daily limits removed from Fish Lake ahead of rehabilitation"].water_body_id is None
    assert rows["Icicle River to open for coho salmon retention"].water_body_id == ICICLE
    assert rows["Skagit River fishing updates"].water_body_id == SKAGIT_WB
    assert rows["Skagit and Cascade River fishing updates"].water_body_id == SKAGIT_WB  # via a stretch heading
    assert rows["Skagit River fishing updates"].details == SKAGIT_DETAILS
    assert rows["Skagit River fishing updates"].notes == SKAGIT_NOTES


def test_closure_in_effect_on_the_first_day_is_left_to_the_hard_filter():
    closure = _row("Green River closed", "Closes all fishing.", effective=date(2026, 9, 16),
                   expires=date(2026, 10, 31), closes_fishing=True)
    day = date(2026, 10, 14)
    assert advisory_lines([closure], water_body_id=None, name="Green River", start=day, end=day) == []


# "Hoh and Quillayute watersheds closing to all fishing" (effective 2026-10-19)
# names its waters only as bullets under a "Locations" heading.
HOH_QUILLAYUTE_DETAILS = [{
    "heading": "Locations (including all tributaries to listed waters)",
    "items": [
        "Bear Creek (Clallam County) a Sol Duc River Tributary.",
        "Bogachiel River (Clallam County) from the mouth to the Olympic National Park boundary.",
        "Hoh River, South Fork (Jefferson County) outside of the Olympic National Park boundary.",
        "Quillayute River (Clallam County) from the Olympic National Park boundary upstream to confluence of "
        "the Sol Duc and Bogachiel rivers.",
        "Sol Duc River (Clallam County) from the mouth upstream to the National Park boundary.",
    ],
}]


def test_location_list_bullets_name_the_closed_waters():
    from chat.context_builder import _has_active_closure

    closure = _row("Hoh and Quillayute watersheds closing to all fishing", "Closes all fishing.",
                   effective=date(2026, 10, 19), closes_fishing=True, details=HOH_QUILLAYUTE_DETAILS)
    assert _has_active_closure("South Fork Hoh", [closure])
    assert _has_active_closure("SOL DUC R   20.0096", [closure], county="Clallam")
    assert _has_active_closure("Bogachiel River", [closure], county="Clallam")
    assert not _has_active_closure("Queets River", [closure], county="Jefferson")
    assert not _has_active_closure("BEAR CR      08.0435", [closure], county="King")  # the bullet says Clallam
    sol_duc = advisory_lines([closure], water_body_id=None, name="Sol Duc River", county="Clallam",
                             start=date(2026, 10, 18), end=date(2026, 10, 19))
    assert not any("Bear Creek" in ln for ln in sol_duc)  # "a Sol Duc River Tributary" is Bear Creek's bullet
    # starting later in a trip: the advisory lists only this water's bullet
    lines = advisory_lines([closure], water_body_id=None, name="Bogachiel River", county="Clallam",
                           start=date(2026, 10, 18), end=date(2026, 10, 19))
    assert lines[0] == "WDFW emergency rule (from Oct 19, until further notice): Closes all fishing."
    assert [ln for ln in lines if ln.startswith("  - ")] == [
        "  - Locations (including all tributaries to listed waters): Bogachiel River (Clallam County) from the "
        "mouth to the Olympic National Park boundary."]


def test_location_head_inverted_fork():
    from conditions.wdfw_emergency import location_head

    assert location_head("Hoh River, South Fork (Jefferson County) outside of the park.") == \
        "South Fork Hoh River outside of the park"
    assert location_head("North Fork Stillaguamish River from the mouth") == "North Fork Stillaguamish River"
    assert location_head("Lake Washington (King County) north of Highway 520 Bridge") == "Lake Washington"
