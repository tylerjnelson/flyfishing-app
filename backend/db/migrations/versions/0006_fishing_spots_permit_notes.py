"""add permit_notes to fishing_spots

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-22

PERMIT-REQUIRED-BEHAVIOR: permit_required is no longer a hard filter.
Permit-required spots remain in the recommendation pool; the LLM surfaces
the permit requirement as an advisory. permit_notes holds prose describing
the specific permit (e.g. "Colville Tribal Day Permit — $30, available online").
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("fishing_spots", sa.Column("permit_notes", sa.Text, nullable=True))


def downgrade():
    op.drop_column("fishing_spots", "permit_notes")
