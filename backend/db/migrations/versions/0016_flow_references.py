"""flow references: per-gauge normals, flood stages, and sourced fishable ranges

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-13

water_bodies.min_cfs / max_cfs held one hand-entered range per river,
compared against whatever gauge the river used. Most were unsourced, and the
sourced ones were written for a different gauge (Emerald's Yakima range is
for Umtanum, but we read Union Gap, which runs about 2x higher). TODO-18.

  gauge_flow_normals  USGS day-of-year flow percentiles (p5–p95) per gauge.
                      Filled by the gauge_reference job (weekly + startup).
  gauge_flood_stages  NWS action / flood stages per gauge (NWPS metadata).
                      Same job.
  flow_bands          Published fishable ranges, each with the gauge its
                      source wrote it for and the mode (wade / float / fish).
                      Seeded below. Each row cites its source and the gauge
                      that source linked.

Sources:
  - Emerald Water Anglers "Current River Levels" sidebar, archived 2025-06-22
    (the CFS page now redirects to it). Each range linked a USGS gauge.
  - Emerald's older "CFS – Washington River Flows" page, unchanged
    2016–2021, for Green, Klickitat, Wenatchee and Methow (dropped from the
    2025 list).

South Fork Hoh is read on the Hoh mainstem gauge, so it gets the mainstem
range, labeled as such. Rivers with no published range (Elwha, Nisqually,
NF Stillaguamish, Pilchuck, Tolt, Stillaguamish, Naches) are judged on
normals alone. min_cfs / max_cfs are left in place (the spots API still
returns them) but nothing scores on them any more.
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_EWA_2025 = (
    "Emerald Water Anglers",
    "https://web.archive.org/web/20250622182339/https://emeraldwateranglers.com/blogs/fishing-reports",
    "2025-06-22",
)
_EWA_2021 = (
    "Emerald Water Anglers",
    "https://web.archive.org/web/20211102145843/https://emeraldwateranglers.com/cfs-washington-rivers/",
    "2021-11-02",
)

# (water body name, gauge site, gauge name, mode, min, max, source, note)
_BANDS = [
    ("Yakima Basin watershed", "12484500", "Yakima River at Umtanum", "wade", 700, 1500, _EWA_2025, None),
    ("Yakima Basin watershed", "12484500", "Yakima River at Umtanum", "float", 1000, 3500, _EWA_2025, None),
    ("Snoqualmie Corridor", "12144500", "Snoqualmie River near Snoqualmie", "wade", 300, 1500, _EWA_2025, None),
    ("Snoqualmie Corridor", "12144500", "Snoqualmie River near Snoqualmie", "float", 750, 1500, _EWA_2025, None),
    ("Skykomish River", "12134500", "Skykomish River near Gold Bar", "fish", 700, 7000, _EWA_2025, None),
    ("SKAGIT R     03.0176", "12181000", "Skagit River at Marblemount", "fish", 2000, 9000, _EWA_2025, None),
    ("Sauk River", "12186000", "Sauk River above White Chuck River", "fish", 600, 3000, _EWA_2025, None),
    ("Queets River", "12040500", "Queets River near Clearwater", "fish", 1500, 4000, _EWA_2025, None),
    ("South Fork Hoh", "12041200", "Hoh River at US 101 (mainstem)", "fish", 1200, 3500, _EWA_2025,
     "Range is for the Hoh mainstem"),
    ("Green River", "12113000", "Green River near Auburn", "fish", 700, 1600, _EWA_2021, None),
    ("Klickitat River", "14113000", "Klickitat River near Pitt", "fish", 600, 1500, _EWA_2021, None),
    ("Wenatchee River", "12459000", "Wenatchee River at Peshastin", "fish", 600, 1200, _EWA_2021, None),
    ("Methow River", "12448500", "Methow River at Winthrop", "fish", 200, 600, _EWA_2021, None),
]


def _q(s):
    return "NULL" if s is None else "'" + s.replace("'", "''") + "'"


def upgrade():
    op.execute("""
        CREATE TABLE gauge_flow_normals (
            site_id       TEXT NOT NULL,
            time_of_year  TEXT NOT NULL,          -- 'MM-DD'
            p05 REAL NOT NULL, p10 REAL NOT NULL, p25 REAL NOT NULL, p50 REAL NOT NULL,
            p75 REAL NOT NULL, p90 REAL NOT NULL, p95 REAL NOT NULL,
            sample_count  INTEGER,
            loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (site_id, time_of_year)
        )
    """)
    op.execute("""
        CREATE TABLE gauge_flood_stages (
            site_id      TEXT PRIMARY KEY,
            nwps_lid     TEXT NOT NULL,
            action_ft    DOUBLE PRECISION,       -- NULL: NWS publishes none
            minor_ft     DOUBLE PRECISION,
            moderate_ft  DOUBLE PRECISION,
            major_ft     DOUBLE PRECISION,
            loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE flow_bands (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            water_body_id  UUID NOT NULL REFERENCES water_bodies(id) ON DELETE CASCADE,
            gauge_site_id  TEXT NOT NULL,
            gauge_name     TEXT,
            mode           TEXT NOT NULL CHECK (mode IN ('wade', 'float', 'fish')),
            min_cfs        INTEGER,
            max_cfs        INTEGER,
            source         TEXT NOT NULL,
            source_url     TEXT,
            source_date    DATE,
            note           TEXT,
            CHECK (min_cfs IS NOT NULL OR max_cfs IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX flow_bands_water_body_id_idx ON flow_bands (water_body_id)")

    for name, site, gauge_name, mode, lo, hi, (source, url, as_of), note in _BANDS:
        op.execute(
            "INSERT INTO flow_bands (water_body_id, gauge_site_id, gauge_name, mode, min_cfs, max_cfs, "
            "source, source_url, source_date, note) "
            f"SELECT id, {_q(site)}, {_q(gauge_name)}, {_q(mode)}, {lo}, {hi}, {_q(source)}, {_q(url)}, "
            f"{_q(as_of)}::date, {_q(note)} FROM water_bodies WHERE name = {_q(name)}"
        )


def downgrade():
    op.execute("DROP TABLE flow_bands")
    op.execute("DROP TABLE gauge_flood_stages")
    op.execute("DROP TABLE gauge_flow_normals")
