"""add notes.trip_id foreign key (circular FK resolution)

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-04

Both notes and trips must exist before this FK can be added.
See §5.1: op.execute() is used instead of op.add_column with FK argument
because Alembic's FK generation in add_column does not reliably defer
constraint checking.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE notes ADD COLUMN trip_id UUID REFERENCES trips(id)")


def downgrade() -> None:
    op.execute("ALTER TABLE notes DROP COLUMN IF EXISTS trip_id")
