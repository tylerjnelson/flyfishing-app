"""transcript persistence — tool turns + per-conversation seq + frozen context

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-24

Phase 2 of the agentic-harness migration. Additive/backward-compatible, like
0009/0010 — no existing behaviour changes until the loop (Phase 3) writes the
new tool rows and the freeze (Phase 4) writes frozen_context.

messages:
  - seq (int)        per-conversation monotonic ordinal; the transcript is
                     ordered by this, NOT created_at (which is now() =
                     transaction-start time, byte-identical across all rows an
                     agentic turn commits together). Backfilled for existing
                     rows by (created_at, id) so today's one-row-per-turn
                     history keeps its order. A non-unique index on
                     (conversation_id, seq) serves the ORDER BY.
  - tool_name (text) | tool_calls (jsonb) | digest (text) — hold tool turns;
                     role gains "tool" (no CHECK constraint on role, so nothing
                     to alter there).

conversations:
  - frozen_context (text)         verbatim stable prefix (Phase 4)
  - frozen_context_at (timestamptz) its as-of stamp
  - context_state (text)          Phase 6 budget guard (inert until then)

Schema is fully pinned (plan, 2026-07-23) — these are the only new columns; in
particular there is deliberately NO promoted_spot_ids (the active set is derived
from the transcript).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    # messages — transcript ordinal + tool-turn columns
    op.add_column("messages", sa.Column("seq", sa.Integer(), nullable=True))
    op.add_column("messages", sa.Column("tool_name", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("tool_calls", JSONB(), nullable=True))
    op.add_column("messages", sa.Column("digest", sa.Text(), nullable=True))

    # Backfill seq: dense 0-based ordinal per conversation, ordered by the
    # existing (created_at, id). id is the tiebreak so the backfill is
    # deterministic even where created_at already collides.
    op.execute(
        """
        UPDATE messages AS m
        SET seq = sub.rn
        FROM (
            SELECT id,
                   (row_number() OVER (
                        PARTITION BY conversation_id
                        ORDER BY created_at, id
                    ) - 1) AS rn
            FROM messages
        ) AS sub
        WHERE m.id = sub.id
        """
    )

    op.create_index(
        "ix_messages_conversation_seq",
        "messages",
        ["conversation_id", "seq"],
    )

    # conversations — frozen prefix + freshness stamp + budget guard
    op.add_column("conversations", sa.Column("frozen_context", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("frozen_context_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("conversations", sa.Column("context_state", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("conversations", "context_state")
    op.drop_column("conversations", "frozen_context_at")
    op.drop_column("conversations", "frozen_context")

    op.drop_index("ix_messages_conversation_seq", table_name="messages")
    op.drop_column("messages", "digest")
    op.drop_column("messages", "tool_calls")
    op.drop_column("messages", "tool_name")
    op.drop_column("messages", "seq")
