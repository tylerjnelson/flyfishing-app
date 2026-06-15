"""response_cache intake_hash column

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-06

Adds intake_hash to the response_cache key so that cached narratives built
for one intake config (water_type / target_species / max_drive_minutes) are
never served for a different config.

Truncates existing cache rows on apply — a clean reset is cheaper than a
backfill and the cache regenerates quickly on the next few requests.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("TRUNCATE TABLE response_cache")
    op.add_column(
        "response_cache",
        sa.Column("intake_hash", sa.String(16), nullable=False, server_default=""),
    )
    op.create_unique_constraint(
        "uq_response_cache_spot_hash_intake",
        "response_cache",
        ["fishing_spot_id", "conditions_hash", "intake_hash"],
    )


def downgrade():
    op.drop_constraint("uq_response_cache_spot_hash_intake", "response_cache")
    op.drop_column("response_cache", "intake_hash")
