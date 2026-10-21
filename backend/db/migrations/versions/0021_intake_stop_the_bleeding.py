"""DATA-INTAKE DI-0 data fixes: Snoqualmie legality, DNR rows, last stocking, confidence

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-21

- The Snoqualmie River was marked not fly-fishing-legal by an LLM read of a DNR
  forest page, so the candidate query never returned it. Legality now comes only
  from WDFW (the pamphlet and emergency rules); the flag goes back to true.
- Four DNR rows are not waters ("Loup Loup State Forest's Sportsmen's Camp",
  "Middle Fork Road stream", "Olsen Creek State Forest stream", "Upper
  Clearwater"): no spot, no access point, no stocking, nothing references them.
  Deleted (their entries left data/access_points.json in the same commit).
- Counties for rivers and creeks that had none, from the OSM county polygon
  containing the spot (or, with no spot, the access points). Season, rule and
  stocking matching all use county. Yakima and Nisqually stay blank: they run
  through several counties, and one county would make a rule about another
  stretch look like a different water (wdfw_emergency.county_conflicts) until
  reaches exist (DI-8).
- last_stocked_date/species recomputed from stocking_events; the daily job only
  ever moved the date, so the species was April's on 59 of 224 stocked waters.
  (conditions/scheduler.py REFRESH_LAST_STOCKED_SQL does this daily from now on.)
- seed_confidence: Sauk and Skykomish were 'unvalidated' (score x0.2) only
  because the WTA seed created them; seven DNR rivers the WDFW pamphlet lists
  were 'probable' (x0.6) for the same reason. All are WDFW-listed rivers with a
  spot: 'confirmed'. (Derived confidence is DI-6.)
- The removed December regulations job's row leaves the APScheduler job store,
  so the restart doesn't log a traceback restoring it.

Downgrade restores all but the stocking recompute (the old values were wrong)
and the deleted rows' name embeddings (re-embedded only if needed).
"""

from alembic import op
from sqlalchemy import text

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

SNOQUALMIE = "ebd46277-388a-4a16-8fd7-fdf2a9c99d14"

# id: (name, type, fly_fishing_legal)
NON_WATERS = {
    "af056b6b-aedf-4cc4-83b1-19c8595e539a": ("Loup Loup State Forest's Sportsmen's Camp", "river", True),
    "27b6c050-b748-465d-be50-29937f7957b8": ("Middle Fork Road stream", "river", False),
    "00002512-a267-4f5f-80fb-f1682b732b67": ("Olsen Creek State Forest stream", "river", True),
    "baaae21f-fa68-40a4-9049-050ef823d4a4": ("Upper Clearwater", "river", True),
}

COUNTIES = {
    "1c4b3342-46a7-42e3-a7a3-68d677490b63": "Clallam",       # Elwha River
    "ebd46277-388a-4a16-8fd7-fdf2a9c99d14": "King",          # Snoqualmie River
    "48c206cd-5279-4b12-a73d-099bf2c7d392": "King",          # Raging River
    "4b9d7db0-2cd4-4538-a77f-4d11ad0c4ef1": "Yakima",        # South Fork Ahtanum Creek
    "c59f64ef-5869-4573-b82e-2f93d6dec1a6": "Jefferson",     # South Fork Hoh River
    "669259f6-fbe4-48bb-899e-02157be0c1f0": "Mason",         # Tahuya River
    "77dbf978-11f7-4038-bcbb-1c691e2a0c7b": "Kittitas",      # West Fork Teanaway River
    "5a00c983-61ff-4c0c-a512-23333aa12f75": "Wahkiakum",     # Elochoman River
    "694dfa93-8c7f-4854-bbab-6dbe18a25fab": "Wahkiakum",     # Grays River
    "4d2c59ee-6a99-4cd2-b431-4ac89d61737c": "Lewis",         # North Fork Newaukum River
    "c05574fd-a7fa-4930-abe4-f1cfdc5796d1": "Yakima",        # Ahtanum Creek (access points)
    "ccacbb45-4d59-4bda-8aff-a250db408239": "Stevens",       # Flodelle Creek (access point)
    "8578b2a8-577b-4bf4-9dfe-867d344e4fbd": "Thurston",      # McLane Creek (access points)
    "d0a5b26c-95e8-4d2e-bab0-c13d507098f1": "Whatcom",       # Olsen Creek (access point)
    "088ca598-607d-4886-952d-3997ef9ed798": "Clallam",       # Sadie Creek (access point)
    "7cfa64b0-cf55-47d6-b1e1-6deff2975498": "Pend Oreille",  # Tacoma Creek (access points)
}

# id: confidence before
CONFIRM = {
    "a5736703-b9de-4ea4-b561-0b22b98b3dc2": "unvalidated",  # Sauk River
    "07bcd49c-9e7d-42b0-bee2-80542fd20b31": "unvalidated",  # Skykomish River
    "ebd46277-388a-4a16-8fd7-fdf2a9c99d14": "probable",     # Snoqualmie River
    "f0715b1d-a9bb-4d31-917f-3a4170e1fa6e": "probable",     # Yakima River
    "1c4b3342-46a7-42e3-a7a3-68d677490b63": "probable",     # Elwha River
    "8807029e-75c6-488f-9706-865267f94e9a": "probable",     # Nisqually River
    "48c206cd-5279-4b12-a73d-099bf2c7d392": "probable",     # Raging River
    "669259f6-fbe4-48bb-899e-02157be0c1f0": "probable",     # Tahuya River
    "c59f64ef-5869-4573-b82e-2f93d6dec1a6": "probable",     # South Fork Hoh River
}

# Same as conditions/scheduler.py REFRESH_LAST_STOCKED_SQL (migrations don't import app code).
REFRESH_LAST_STOCKED_SQL = """
    WITH latest AS (
        SELECT water_body_id, max(stocked_date) AS d
          FROM stocking_events
         WHERE water_body_id IS NOT NULL AND stocked_date IS NOT NULL
         GROUP BY water_body_id
    ), last AS (
        SELECT l.water_body_id, l.d,
               array_agg(DISTINCT s.species ORDER BY s.species)
                   FILTER (WHERE s.species IS NOT NULL) AS species
          FROM latest l
          JOIN stocking_events s ON s.water_body_id = l.water_body_id AND s.stocked_date = l.d
         GROUP BY l.water_body_id, l.d
    )
    UPDATE water_bodies w
       SET last_stocked_date = last.d, last_stocked_species = last.species
      FROM last
     WHERE w.id = last.water_body_id
       AND (w.last_stocked_date IS DISTINCT FROM last.d
            OR w.last_stocked_species IS DISTINCT FROM last.species)
"""

_UNREFERENCED = """
    NOT EXISTS (SELECT 1 FROM fishing_spots x WHERE x.water_body_id = w.id)
    AND NOT EXISTS (SELECT 1 FROM stocking_events x WHERE x.water_body_id = w.id)
    AND NOT EXISTS (SELECT 1 FROM emergency_closures x WHERE x.water_body_id = w.id)
    AND NOT EXISTS (SELECT 1 FROM access_points x WHERE x.water_body_id = w.id)
    AND NOT EXISTS (SELECT 1 FROM flow_bands x WHERE x.water_body_id = w.id)
    AND NOT EXISTS (SELECT 1 FROM water_bodies x WHERE x.parent_id = w.id)
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text(
        "UPDATE water_bodies SET fly_fishing_legal = true WHERE id = CAST(:id AS uuid) AND fly_fishing_legal = false"),
        {"id": SNOQUALMIE})

    for wid, (name, _, _) in NON_WATERS.items():
        conn.execute(text("DELETE FROM conditions_cache WHERE water_body_id = CAST(:id AS uuid)"), {"id": wid})
        conn.execute(text(
            f"DELETE FROM water_bodies w WHERE w.id = CAST(:id AS uuid) AND w.name = :n AND {_UNREFERENCED}"),
            {"id": wid, "n": name})

    for wid, county in COUNTIES.items():
        conn.execute(text(
            "UPDATE water_bodies SET county = :c WHERE id = CAST(:id AS uuid) AND county IS NULL"),
            {"id": wid, "c": county})

    conn.execute(text(REFRESH_LAST_STOCKED_SQL))

    for wid, before in CONFIRM.items():
        conn.execute(text(
            "UPDATE water_bodies SET seed_confidence = 'confirmed' WHERE id = CAST(:id AS uuid) "
            "AND seed_confidence = :b"), {"id": wid, "b": before})

    if conn.execute(text("SELECT to_regclass('apscheduler_jobs')")).scalar() is not None:
        conn.execute(text("DELETE FROM apscheduler_jobs WHERE id = 'wdfw_regulations'"))


def downgrade() -> None:
    conn = op.get_bind()
    for wid, before in CONFIRM.items():
        conn.execute(text(
            "UPDATE water_bodies SET seed_confidence = :b WHERE id = CAST(:id AS uuid) "
            "AND seed_confidence = 'confirmed'"), {"id": wid, "b": before})

    for wid, county in COUNTIES.items():
        conn.execute(text(
            "UPDATE water_bodies SET county = NULL WHERE id = CAST(:id AS uuid) AND county = :c"),
            {"id": wid, "c": county})

    for wid, (name, typ, legal) in NON_WATERS.items():
        conn.execute(text("""
            INSERT INTO water_bodies (id, name, type, aliases, source, seed_confidence, fly_fishing_legal,
                                      min_temp_f, score)
            VALUES (CAST(:id AS uuid), :n, :t, ARRAY[:n], 'dnr', 'probable', :legal, 40, 4)
            ON CONFLICT (id) DO NOTHING
        """), {"id": wid, "n": name, "t": typ, "legal": legal})

    conn.execute(text(
        "UPDATE water_bodies SET fly_fishing_legal = false WHERE id = CAST(:id AS uuid) AND fly_fishing_legal = true"),
        {"id": SNOQUALMIE})
