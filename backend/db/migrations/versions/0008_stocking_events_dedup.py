"""stocking_events deduplication

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-23

source_record_id was previously populated from geo_code (a geographic location
code shared across multiple stocking events), not from the Socrata :id field
(the true per-row unique identifier).  This caused the daily stocking job to
accumulate ~40 copies of every record with no way to deduplicate.

Fix: truncate the dirty data and add a unique constraint so the updated job
can use ON CONFLICT DO NOTHING going forward.
"""

from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("TRUNCATE TABLE stocking_events")
    op.create_unique_constraint(
        "uq_stocking_events_source_record_id",
        "stocking_events",
        ["source_record_id"],
    )


def downgrade():
    op.drop_constraint("uq_stocking_events_source_record_id", "stocking_events")
