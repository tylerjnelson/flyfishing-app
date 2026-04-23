"""drop legacy spot_id columns and spots table

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-23

Removes the 7 nullable spot_id FK columns left over from the pre-refactor
schema, and drops the now-orphaned spots table.

All active application code uses water_body_id / fishing_spot_id. No writer
has touched any of these columns since the Phase 5 cutover (2026-04-21).
conditions_cache.spot_id rows were backfilled to water_body_id before this
migration ran.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("notes", "spot_id")
    op.drop_column("trips", "spot_id")
    op.drop_column("response_cache", "spot_id")
    op.drop_column("saved_spots", "spot_id")
    op.drop_column("conditions_cache", "spot_id")
    op.drop_column("stocking_events", "spot_id")
    op.drop_column("emergency_closures", "spot_id")
    op.drop_table("spots")


def downgrade():
    raise NotImplementedError("downgrade not supported — spots table data is preserved in water_bodies")
