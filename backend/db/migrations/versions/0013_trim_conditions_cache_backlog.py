"""trim conditions_cache backlog left by the unpruned real-time write path

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-11

The chat context builder's real-time fetch (NWS / AirNow / USGS at session
open) inserted conditions_cache rows without the TTL pruning the scheduler
jobs do, so the table grew to ~28k rows (112 MB), mostly noaa_nws/airnow
history nobody reads. The write path now prunes (conditions/cache.py); this
one-off removes the backlog it left behind.

Every reader uses only the freshest row per (water_body_id, source), and the
stale-fallback path may serve that row however old it is. So keep:
  - the newest row per (water_body_id, source), whatever its age, and
  - every row from the last 48h (the longest per-source TTL).
Delete the rest. Data-only; not reversible (downgrade is a no-op).

stocking_events duplicates are NOT handled here: job_wdfw_stocking now
deletes the year's rows that are absent from each fetch, so the first run
after deploy removes them.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        DELETE FROM conditions_cache c
        USING (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY water_body_id, source
                       ORDER BY fetched_at DESC NULLS LAST, id
                   ) AS rn
            FROM conditions_cache
        ) ranked
        WHERE c.id = ranked.id
          AND ranked.rn > 1
          AND (c.fetched_at IS NULL OR c.fetched_at < now() - interval '48 hours')
        """
    )


def downgrade():
    pass
