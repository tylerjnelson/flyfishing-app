"""drive_time_cache: HERE drive times, reused for 90 days (TODO-19)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-20

HERE is now called only for the three cards a user sees (the rest of the
candidates get the road-network estimate). Planning-mode routes have no
traffic and no date, so the answer for an origin and an access point changes
only when the roads do: cache it.

  origin_*   the user's origin rounded to 3 decimals (~100 m)
  dest_*     the access point (or spot) routed to, rounded to 5 decimals
  avoid      the HERE avoid[areas] sent ('' when none): a winter route around
             a closed pass is a different answer from the summer one
  minutes    HERE total, every section (ferry crossings included, waits not)
  ferries    the ferry crossings on the route, [[lat, lon, lat, lon, minutes]]

The wait to board a ferry depends on the trip date, so it is added at read
time (conditions/ferries.py), never stored.
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE drive_time_cache (
            origin_lat NUMERIC(7, 3) NOT NULL,
            origin_lon NUMERIC(7, 3) NOT NULL,
            dest_lat NUMERIC(8, 5) NOT NULL,
            dest_lon NUMERIC(8, 5) NOT NULL,
            avoid TEXT NOT NULL DEFAULT '',
            minutes INTEGER NOT NULL,
            ferries JSONB NOT NULL DEFAULT '[]'::jsonb,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (origin_lat, origin_lon, dest_lat, dest_lon, avoid)
        )
    """)


def downgrade():
    op.execute("DROP TABLE drive_time_cache")
