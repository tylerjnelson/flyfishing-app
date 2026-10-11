"""fix USGS gauge mappings found wrong or dead in the 2026-10-11 audit

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-11

scripts/populate_usgs_ids.py matched river names by substring and used gauge
IDs that were never checked against USGS's own station records:
  - Sol Duc River → 12044900 = ELWHA RIVER ABOVE LAKE MILLS (discontinued 2011)
  - Kalama River → 14221500 = CEDAR CREEK NEAR ARIEL (no live data)
  - Little Wenatchee → 12462500 = Wenatchee mainstem at Monitor, 50 km downstream
    ("wenatchee" substring)
  - North Fork Stillaguamish → 12167400 (mainstem at Arlington, no longer reports)
  - Yakima → 12505000 (Parker, no longer reports to USGS)
USGS has no active gauge on the Sol Duc, Kalama or Little Wenatchee, so those
get none (no live flow, which is better than another river's). NF Stillaguamish
→ 12167000 (its own live gauge). Yakima → 12500450 only (Union Gap, live, at
the spot). Parker (12505000 / PARW1) is dropped: it sits below the Sunnyside
diversion (392 cfs vs Union Gap's 1,490 on 2026-10-11), so its NWRFC forecast
would have flagged every future Yakima trip as "low flow". Elwha → 12045500
(Elwha at McDonald Bridge, live).

Naches (12494000) and the Stillaguamish mainstem (12167400) keep their dead USGS
IDs: fetch_usgs_gauge falls back to NWPS observed flow (NACW1 / ARLW1).

The seed script carries the same corrections, so a re-run won't undo this.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# (name regex, new ids, has_realtime_conditions, old ids for downgrade)
_CHANGES = [
    (r"sol ?duc", [], False, ["12044900"]),
    (r"kalama", [], False, ["14221500"]),
    (r"little wenatchee", [], False, ["12462500"]),
    (r"north fork stillaguamish", ["12167000"], True, ["12167400"]),
    (r"yakima", ["12500450"], True, ["12505000"]),
    (r"elwha", ["12045500"], True, None),
]


def _array(ids):
    return "ARRAY[" + ",".join(f"'{i}'" for i in ids) + "]::text[]" if ids else "ARRAY[]::text[]"


def upgrade():
    for pattern, ids, realtime, _ in _CHANGES:
        op.execute(
            f"UPDATE water_bodies SET usgs_site_ids = {_array(ids)}, "
            f"has_realtime_conditions = {str(realtime).lower()} "
            f"WHERE type IN ('river', 'creek') AND lower(name) ~ '{pattern}'"
        )


def downgrade():
    for pattern, _, _, old in _CHANGES:
        old_sql = _array(old) if old is not None else "NULL"
        op.execute(
            f"UPDATE water_bodies SET usgs_site_ids = {old_sql}, "
            f"has_realtime_conditions = {'true' if old else 'false'} "
            f"WHERE type IN ('river', 'creek') AND lower(name) ~ '{pattern}'"
        )
