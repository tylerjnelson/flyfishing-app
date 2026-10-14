"""WDFW emergency rules: per-stretch rules and notes; Icicle River alias

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-14

Non-closure rules now reach the model as advisories (TODO-21), so the rule's
substance has to be stored, not just its Action line:

  details  [{"heading": "Skagit River from Gilligan Creek to Baker River",
             "items": ["Oct. 12–15: Closed to fishing for all species.", ...]}]
           The bullet lists under each stretch heading, which the paragraph
           scrape never read.
  notes    The "Additional information" paragraphs ("Fish Lake is closed
           beginning Oct. 18.").

Both are filled by the next job_wdfw_emergency run (it replaces every row).

WDFW calls Icicle Creek "Icicle River"; the alias lets its rules link.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE emergency_closures ADD COLUMN details JSONB")
    op.execute("ALTER TABLE emergency_closures ADD COLUMN notes TEXT")
    op.execute("""
        UPDATE water_bodies
        SET aliases = array_append(COALESCE(aliases, ARRAY[]::text[]), 'Icicle River')
        WHERE name = 'Icicle Creek'
          AND NOT ('Icicle River' = ANY(COALESCE(aliases, ARRAY[]::text[])))
    """)


def downgrade():
    op.execute("UPDATE water_bodies SET aliases = array_remove(aliases, 'Icicle River') WHERE name = 'Icicle Creek'")
    op.execute("ALTER TABLE emergency_closures DROP COLUMN notes")
    op.execute("ALTER TABLE emergency_closures DROP COLUMN details")
