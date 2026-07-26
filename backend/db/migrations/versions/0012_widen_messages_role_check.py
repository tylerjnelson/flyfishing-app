"""widen messages.role CHECK to include 'tool' — fix agentic tool-turn persistence

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-25

0011 added role="tool" transcript rows for the agentic loop but wrongly assumed
there was no CHECK constraint on messages.role (see its docstring: "role gains
'tool' (no CHECK constraint on role, so nothing to alter there)"). There is one:
0001 created

    messages_role_check = CHECK (role IN ('user', 'assistant'))

So every agentic turn that persists a tool turn — `_persist_tool_turn`
(chat/router.py) writes one role="tool" row per tool result — raises
asyncpg CheckViolationError, 500s the request, and kills the turn after the
tool_status events (no answer tokens, no `done`). Surfaced by the full-cohort
contract-compliance batch (2026-07-25): 7/32 turns (all tool-invoking turns)
died this way; the no-tool opening path used for the prod cutover smoke never
hit it.

Widen the constraint to include 'tool'. Purely additive and safe to apply live:
no existing row is affected because the bug PREVENTED any role="tool" row from
ever being written.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("messages_role_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_role_check",
        "messages",
        "role IN ('user', 'assistant', 'tool')",
    )


def downgrade():
    op.drop_constraint("messages_role_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_role_check",
        "messages",
        "role IN ('user', 'assistant')",
    )
