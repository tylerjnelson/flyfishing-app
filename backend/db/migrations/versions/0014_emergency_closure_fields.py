"""emergency_closures: parsed title/action/location + closes_fishing flag

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-11

Every scraped WDFW emergency rule was treated as a closure. The chat context
builder hard-filters any water body with an active rule linked to it, so
openings ("Mineral Lake season extended", "Lake Washington reopens"), limit
changes and species-retention closures removed those spots from
recommendations. No rule ever got an expiry, so they stayed active for as
long as WDFW listed them (TODO-15).

The scraper now parses the rule pages' structured fields. These columns hold
them, and closes_fishing marks the rules that actually close fishing (and
aren't superseded by a later reopening). Only those are hard filters.

Existing rows default to closes_fishing = false. The table is fully rewritten
by the next job_wdfw_emergency run (every 2h, and at startup), so there's no
backfill.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("emergency_closures", sa.Column("title", sa.Text()))
    op.add_column("emergency_closures", sa.Column("action", sa.Text()))
    op.add_column("emergency_closures", sa.Column("location", sa.Text()))
    op.add_column(
        "emergency_closures",
        sa.Column("closes_fishing", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("emergency_closures", "closes_fishing")
    op.drop_column("emergency_closures", "location")
    op.drop_column("emergency_closures", "action")
    op.drop_column("emergency_closures", "title")
