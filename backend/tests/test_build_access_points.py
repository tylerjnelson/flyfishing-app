"""The access-point builder's matching rules (TODO-5). Pure functions; no network."""

import pytest

from scripts.build_access_points import (
    AccessPoint,
    Match,
    activation,
    clean_name,
    find_merges,
    lake_key,
    match_all,
    name_variants,
    proper_name,
)


@pytest.mark.parametrize("raw,expected", [
    ("LK DESIRE (KING)", "lake desire"),
    ("SILVER LK (WHAT) 01", "silver lake"),
    ("LIBERTY LK (SPOK) (57)", "liberty lake"),
    ("4TH OF JULY LK(LINC/ADAM)", "fourth of july lake"),
    ("EGG LK (SAN)", "egg lake"),
    ("W MEDICAL LK (SPOK)", "west medical lake"),
    ("I-82 PD #1 (YAKI)", "i 82 pond 1"),
    ("LEWIS R -NF  27.0168", "north fork lewis river"),
    ("NEWAUKUM R-NF23.0887", "north fork newaukum river"),
    ("NORTH NEMAH R24.0460", "north nemah river"),
    ("VAN WINKLE C 22.0240", "van winkle creek"),
    ("GREEN LK LTL (OKAN)", "green lake little"),
])
def test_clean_name(raw, expected):
    assert clean_name(raw) == expected


def test_name_variants_and_keys():
    assert name_variants("HOG (CANYON) LK (SPOK)") == ["hog lake", "canyon lake"]
    assert lake_key("LK DESIRE (KING)") == lake_key("Lake Desire") == lake_key("Desire Lake")
    assert lake_key("Long's Pond") == lake_key("LONGS PD (THUR)")
    assert lake_key("St. Clair Lake") == lake_key("LK ST CLAIR (THUR)")


def _wb(wid, name, county="Okanogan", *, spot=None, source="wdfw_access", stockings=0, type="lake"):
    return {"id": wid, "name": name, "type": type, "county": county, "source": source, "aliases": [name],
            "lat": None, "lon": None, "stocking_events": stockings,
            "spot_id": f"s-{wid}" if spot else None, "spot_name": None,
            "spot_source": source if spot else None,
            "spot_lat": spot[0] if spot else None, "spot_lon": spot[1] if spot else None}


def _shore(name, county, lat, lon, perm):
    return AccessPoint(source="wdfw_shore", source_id=name + str(lat), name=name, kind="shore", lat=lat, lon=lon,
                       county=county, water_name=name,
                       lake={"gnis_id": None, "name": name, "perm_id": perm, "dist_m": 0, "area_km2": 1})


class TestLakeMatching:
    def test_distinct_lakes_sharing_a_name_key_stay_apart(self):
        # Conconully Lake and Conconully Reservoir both have spots: different waters.
        lake = _wb("L", "Conconully Lake", spot=(48.55, -119.75))
        res = _wb("R", "Conconully Reservoir", spot=(48.56, -119.73))
        aps = [_shore("Conconully Lake", "Okanogan", 48.55, -119.75, "p-lake"),
               _shore("Conconully Reservoir", "Okanogan", 48.56, -119.73, "p-res")]
        perms = {"L": "p-lake", "R": "p-res"}
        got = {(m.wb_id, m.ap_index) for m in match_all(aps, [lake, res], {}, perms)}
        assert got == {("L", 0), ("R", 1)}

    def test_stocking_duplicate_joins_its_lake(self):
        access = _wb("A", "Mineral Lake", "Lewis", spot=(46.72, -122.17))
        stocking = _wb("S", "MINERAL LK (LEWI)", "Lewis", source="wdfw_stocking", stockings=16)
        aps = [_shore("Mineral Lake", "Lewis", 46.72, -122.17, "p-min")]
        matches = match_all(aps, [access, stocking], {}, {"A": "p-min"})
        assert {m.wb_id for m in matches} == {"A", "S"}
        merges, conflicts = find_merges([access, stocking], matches, {})
        assert merges == {"S": "A"} and conflicts == []

    def test_same_name_other_county_is_not_matched(self):
        cowlitz = _wb("C", "Silver Lake", "Cowlitz", spot=(46.31, -122.78))
        aps = [_shore("Silver Lake", "Snohomish", 47.89, -122.21, "p-snoh")]
        assert match_all(aps, [cowlitz], {}, {"C": "p-cow"}) == []


class TestMerges:
    def test_two_spotted_rows_keep_the_access_sourced_one(self):
        access = _wb("A", "McCabe Pond", "Kittitas", spot=(47.0, -120.5))
        stocking = _wb("S", "MCCABE POND (KITT)", "Kittitas", spot=(47.0, -120.5), source="wdfw_stocking")
        matches = [Match("A", 0, "name+county", "high"), Match("S", 0, "name+county", "high")]
        merges, conflicts = find_merges([access, stocking], matches, {})
        assert merges == {"S": "A"} and conflicts == []

    def test_without_spots_the_most_stocked_row_is_kept(self):
        a = _wb("A", "Silver Lake", "Snohomish", source="wdfw_stocking", stockings=3)
        b = _wb("B", "SILVER LK (SNOH) T28", "Snohomish", source="wdfw_stocking", stockings=9)
        merges, _ = find_merges([a, b], [Match("A", 0, "m", "high"), Match("B", 0, "m", "high")], {})
        assert merges == {"A": "B"}


class TestActivationAndNames:
    def test_activation(self):
        assert activation(_wb("x", "Twisp River", type="river", source="curated"), 3) == (True, "has public access")
        assert activation(_wb("x", "Twisp River", type="river", source="curated"), 0)[0] is False
        assert activation(_wb("x", "BOEING CR    08.0019", type="river", source="wdfw_stocking"), 1)[0] is False
        assert activation(_wb("x", "WYNOOCHEE R  22.0260", type="river", source="wdfw_stocking"), 3)[0] is True
        assert activation(_wb("x", "Olsen Creek State Forest stream", type="river", source="dnr"), 2)[0] is False

    def test_proper_name_prefers_wdfw_lake_name(self):
        w = _wb("S", "SILVER LK (SNOH) T28", "Snohomish", source="wdfw_stocking")
        aps = [_shore("Silver Lake", "Snohomish", 47.89, -122.21, "p")]
        assert proper_name(w, aps, {}) == "Silver Lake"
        assert proper_name(_wb("Y", "Yakima Basin watershed", type="river", source="dnr"), [], {}) == "Yakima River"
        assert proper_name(_wb("M", "Mineral Lake", "Lewis"), [], {}) is None


# --- TODO-5 review corrections (2026-10-21) ------------------------------------

def test_wdfw_shore_typos_are_corrected():
    from scripts.build_access_points import from_wdfw_shore
    f = {"attributes": {"AccessSiteID": 10639, "LakeName": "Spearfish Lake", "Description": "Boat ramp",
                        "Latitude": 46.626002, "Longitude": -121.128247, "County": "Klickitat"}}
    ap = from_wdfw_shore(f)
    assert (ap.lat, ap.lon) == (45.626002, -121.128247)
    f["attributes"].update(AccessSiteID=1, Latitude=47.1, Longitude=-120.2)
    assert (from_wdfw_shore(f).lat, from_wdfw_shore(f).lon) == (47.1, -120.2)


def test_shipped_file_has_the_review_fixes():
    import json
    from pathlib import Path
    d = json.loads((Path(__file__).resolve().parent.parent / "data" / "access_points.json").read_text())
    aps = {a["key"]: a for a in d["access_points"]}
    assert aps["wdfw_shore:10639"]["lat"] == 45.626002          # Spearfish Lake
    assert aps["wdfw_shore:10607"]["lon"] == -121.375493        # Rowland, South
    assert "wdfw_shore:10344" not in aps and "osm:374042226" not in aps
    used = {x["key"] for e in d["water_bodies"].values() for x in e["access"]}
    assert used <= set(aps)
    assert {m["from_name"] for m in d["merges"]} >= {"COFFEEPOT LK (LINC)", "ICE HOUSE LK (SKAM)",
                                                      "GOLF COURSE PD (ASOT)", "BLACK LK (PACI)"}
    # A merged-away row is deleted by its merge; listing it too makes every later
    # load fail ("water body ... not found"), as BLACK LK (PACI) did after 5c525d2.
    assert not {m["from_id"] for m in d["merges"]} & set(d["water_bodies"])
    # DI-0 deleted four DNR rows that are not waters.
    assert not {"af056b6b-aedf-4cc4-83b1-19c8595e539a", "27b6c050-b748-465d-be50-29937f7957b8",
                "00002512-a267-4f5f-80fb-f1682b732b67", "baaae21f-fa68-40a4-9049-050ef823d4a4"} & set(d["water_bodies"])
