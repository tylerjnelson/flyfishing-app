"""Move twelve misplaced lakes to where they are (TODO-5 review)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-21

The TODO-5 review checked every lake's spot and access points against
OpenStreetMap. Twelve lakes sat 3 to 300 km from the water: WDFW's shore-access
layer has five with a one-digit typo (Spearfish Lake at 46.6 N lands on Rimrock
Lake; it is at 45.6 N), and seven hand-placed spots were never on their lake
(Pass Lake was on Big Lake, Yahoo Lake near Loon Lake in Stevens County). Their
weather, drive times and directions all pointed at the wrong place.

Each water body and its fishing spot moves to the lake's access point (WDFW's
corrected site, or the OSM boat launch / campground); a row only moves if it
still has the wrong coordinates. Also: three wrong counties, and Omak Lake's
Colville Tribes permit. Cached conditions for the moved lakes are dropped so
the next fetch uses the right place.

The access_points rows are fixed by re-running scripts/load_access_points.py
with the corrected data/access_points.json (same commit).
"""

from decimal import Decimal

from alembic import op
from sqlalchemy import text

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# water body id: ((wrong lat, lon), (right lat, lon), why)
MOVES = {
    "fb7fed99-e363-427d-b9e7-85c4738ec7fa": ((47.118180, -119.270060), (47.460367, -119.519047), "Lake Lenore: was on Moses Lake"),
    "da636369-bf82-4d27-9b7f-bc1a4672c366": ((47.426260, -119.288830), (46.837891, -119.832542), "Lenice Lake: was near Stratford"),
    "79898420-716a-4a00-acce-33f5c3e2ddd3": ((47.527130, -122.684210), (47.567803, -122.836230), "Tahuya Lake: 12 km east"),
    "cd91aa51-29ea-4b5e-be06-c857313dbc90": ((48.047100, -122.462930), (48.023913, -122.460856), "Lone Lake: 2.6 km north"),
    "a51ea8ff-3187-4bf9-baa8-611897780b75": ((48.399170, -122.237820), (48.417000, -122.644260), "Pass Lake: was on Big Lake"),
    "85306d59-7002-4777-8f76-ae62d0971205": ((48.055130, -117.612400), (47.676010, -124.019470), "Yahoo Lake: was in Stevens County"),
    "18040f89-552a-42fc-bb6c-6ffe86242bc1": ((48.410780, -119.527240), (48.282700, -119.386800), "Omak Lake: 17 km off"),
    "3e970d9f-1df1-4313-b2d8-933be7640c9c": ((46.626002, -121.128247), (45.626002, -121.128247), "Spearfish Lake: WDFW latitude typo"),
    "3ac4a1aa-6736-40c5-9b2e-3a2dd8c498b4": ((46.661441, -121.906395), (45.661441, -121.906395), "Icehouse Lake: WDFW latitude typo"),
    "15c70d9a-cb6c-458f-a302-fb626ef9532b": ((46.724113, -122.726510), (46.578210, -122.726590), "Carlisle Lake: 16 km north"),
    "f8e4cee4-0e4f-4290-a498-349d1df713c4": ((45.709121, -122.375493), (45.709121, -121.375493), "Rowland, South: WDFW longitude typo"),
    "fe36c2ec-7a11-49f2-b3ac-79cc620c9013": ((45.617709, -121.617709), (45.717709, -121.617709), "Tunnel Lake: WDFW latitude typo"),
}

# water body id: (wrong county, right county)
COUNTIES = {
    "85306d59-7002-4777-8f76-ae62d0971205": (None, "Jefferson"),        # Yahoo Lake
    "bd42a7db-4ede-489c-801a-705f2bee25c0": ("Columbia", "Asotin"),     # Golf Course Pond, Clarkston
    "8aab8d6f-a8eb-49bc-bf89-b34485d54e9b": ("Pend Oreille", "Stevens"),  # Hatch Lake, near Colville
}

OMAK = "18040f89-552a-42fc-bb6c-6ffe86242bc1"
OMAK_PERMIT = ("Omak Lake is on the Colville Reservation: a Colville Tribes fishing permit is required "
               "(a state license is not valid).")


def _move(conn, frm, to):
    for wid, (a, b, _) in MOVES.items():
        old, new = (a, b) if (frm, to) == ("wrong", "right") else (b, a)
        # Exact decimals: a float bound against NUMERIC compares as its binary expansion.
        params = {"id": wid, **{k: Decimal(f"{v:.6f}") for k, v in
                                (("olat", old[0]), ("olon", old[1]), ("nlat", new[0]), ("nlon", new[1]))}}
        for table, col in (("water_bodies", "id"), ("fishing_spots", "water_body_id")):
            conn.execute(text(
                f"UPDATE {table} SET latitude = :nlat, longitude = :nlon "
                f"WHERE {col} = CAST(:id AS uuid) AND latitude = :olat AND longitude = :olon"), params)
        conn.execute(text("DELETE FROM conditions_cache WHERE water_body_id = CAST(:id AS uuid)"), params)


def upgrade() -> None:
    conn = op.get_bind()
    _move(conn, "wrong", "right")
    for wid, (old, new) in COUNTIES.items():
        conn.execute(text(
            "UPDATE water_bodies SET county = :new WHERE id = CAST(:id AS uuid) "
            "AND county IS NOT DISTINCT FROM :old"), {"id": wid, "old": old, "new": new})
    conn.execute(text(
        "UPDATE fishing_spots SET permit_required = true, permit_notes = :n "
        "WHERE water_body_id = CAST(:id AS uuid) AND permit_notes IS NULL"), {"id": OMAK, "n": OMAK_PERMIT})


def downgrade() -> None:
    conn = op.get_bind()
    _move(conn, "right", "wrong")
    for wid, (old, new) in COUNTIES.items():
        conn.execute(text(
            "UPDATE water_bodies SET county = :old WHERE id = CAST(:id AS uuid) AND county = :new"),
            {"id": wid, "old": old, "new": new})
    conn.execute(text(
        "UPDATE fishing_spots SET permit_required = false, permit_notes = NULL "
        "WHERE water_body_id = CAST(:id AS uuid) AND permit_notes = :n"), {"id": OMAK, "n": OMAK_PERMIT})
