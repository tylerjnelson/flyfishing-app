"""here_usage_counter — application-level HERE monthly spend cap

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-15

HERE exposes no server-side spend limit, so we enforce a hard monthly cap on
billable HERE requests (routing + geocoding) inside the app. This table holds
one row per UTC month ("YYYY-MM") with a running `used` count; the counter is
shared across all gunicorn workers and survives restarts. See
conditions/here_budget.py and spec §19.6.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "here_usage_counter",
        sa.Column("period", sa.String(7), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_table("here_usage_counter")
