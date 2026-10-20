"""access points: where an angler parks and gets on the water (TODO-5)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-19

A fishing spot was one coordinate per water body, and drive time was routed to
it: for lakes, one WDFW shore site picked arbitrarily (Lake Washington has 28);
for most rivers, nothing, so the river could never be recommended.

  access_points  Every public access point we know for a water body: WDFW
                 Water Access Sites and Shore Fishing Sites, State Parks boat
                 launches, Ecology beach access, hand-placed spots, and
                 OpenStreetMap campgrounds, boat ramps, named parking and road
                 bridges for rivers with little official access. Each carries what it is
                 (kind), parking and facilities, the seasonal vehicle-gate
                 text, the pass it needs, and how sure the match is.

At build time each candidate water is routed to its open access point nearest
the user (conditions/access_points.py). For river points, gateway_* is where the
local roads leave the highway network and the drive from there (precomputed on
OpenStreetMap roads), so "nearest" follows the road up the valley, not a
straight line over the ridge.

Schema only. Rows come from backend/data/access_points.json via
scripts/load_access_points.py (built by scripts/build_access_points.py), which
also merges duplicate water_bodies rows, renames stocking-style names, adds
the lakes the original seed dropped, and gives activated waters a spot.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE access_points (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            water_body_id UUID NOT NULL REFERENCES water_bodies(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            source TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            latitude NUMERIC(9, 6) NOT NULL,
            longitude NUMERIC(9, 6) NOT NULL,
            description TEXT,
            parking TEXT,
            facilities TEXT[],
            seasonal TEXT,
            pass_required TEXT,
            managing_entity TEXT,
            gateway_latitude NUMERIC(9, 6),
            gateway_longitude NUMERIC(9, 6),
            gateway_minutes NUMERIC,
            confidence TEXT NOT NULL,
            match_method TEXT,
            match_note TEXT,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (water_body_id, key)
        )
    """)
    op.execute("CREATE INDEX ix_access_points_water_body_id ON access_points (water_body_id)")


def downgrade():
    op.execute("DROP TABLE access_points")
