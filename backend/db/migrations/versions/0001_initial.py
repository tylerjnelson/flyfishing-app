"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text, unique=True, nullable=False),
        sa.Column("display_name", sa.Text),
        sa.Column("preferences", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("is_active", sa.Boolean, server_default="true"),
    )

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id")),
        sa.Column("refresh_token", sa.Text, unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active", sa.DateTime(timezone=True)),
        sa.Column("device_hint", sa.Text),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"])

    # ------------------------------------------------------------------
    # magic_link_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "magic_link_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index(op.f("ix_magic_link_tokens_token_hash"), "magic_link_tokens", ["token_hash"])
    op.create_index("ix_magic_link_tokens_email_used_at", "magic_link_tokens", ["email", "used_at"])

    # ------------------------------------------------------------------
    # spots
    # ------------------------------------------------------------------
    op.create_table(
        "spots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text)),
        sa.Column("type", sa.String,
                  sa.CheckConstraint("type IN ('river','lake','creek','coastal')")),
        sa.Column("latitude", sa.Numeric(9, 6)),
        sa.Column("longitude", sa.Numeric(9, 6)),
        sa.Column("elevation_ft", sa.Integer),
        sa.Column("is_alpine", sa.Boolean, server_default="false"),
        sa.Column("county", sa.Text),
        sa.Column("is_public", sa.Boolean, server_default="true"),
        sa.Column("permit_required", sa.Boolean, server_default="false"),
        sa.Column("permit_url", sa.Text),
        sa.Column("source", sa.Text),
        sa.Column("seed_confidence", sa.String,
                  sa.CheckConstraint("seed_confidence IN ('confirmed','probable','unvalidated')"),
                  server_default="unvalidated"),
        sa.Column("usgs_site_ids", postgresql.ARRAY(sa.Text)),
        sa.Column("noaa_station_id", sa.Text),
        sa.Column("snotel_station_id", sa.Text),
        sa.Column("wdfw_water_id", sa.Text),
        sa.Column("wta_trail_url", sa.Text),
        sa.Column("has_realtime_conditions", sa.Boolean, server_default="false"),
        sa.Column("species_primary", postgresql.ARRAY(sa.Text)),
        sa.Column("min_cfs", sa.Integer),
        sa.Column("max_cfs", sa.Integer),
        sa.Column("min_temp_f", sa.Numeric, server_default="40"),
        sa.Column("max_temp_f", sa.Numeric),
        sa.Column("fishing_regs", postgresql.JSONB),
        sa.Column("fly_fishing_legal", sa.Boolean, server_default="true"),
        sa.Column("name_embedding", sa.Text),  # typed as vector(768) below
        sa.Column("last_stocked_date", sa.Date),
        sa.Column("last_stocked_species", postgresql.ARRAY(sa.Text)),
        sa.Column("score", sa.Numeric, server_default="0"),
        sa.Column("score_updated", sa.DateTime(timezone=True)),
        sa.Column("last_visited", sa.Date),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    # Retype name_embedding from Text to vector(768)
    op.execute("ALTER TABLE spots ALTER COLUMN name_embedding TYPE vector(768) USING NULL")
    op.execute(
        "CREATE INDEX ON spots USING hnsw (name_embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute("CREATE INDEX ON spots USING gin(name gin_trgm_ops)")
    op.execute("CREATE INDEX ON spots USING gin(aliases gin_trgm_ops)")

    # ------------------------------------------------------------------
    # trips  (must exist before notes so notes can FK to it in 0002)
    # ------------------------------------------------------------------
    op.create_table(
        "trips",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id")),
        sa.Column("spot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("spots.id")),
        sa.Column("trip_date", sa.Date),
        sa.Column("departure_time", sa.DateTime(timezone=True)),
        sa.Column("return_time", sa.DateTime(timezone=True)),
        sa.Column("session_intake", postgresql.JSONB, server_default="{}"),
        sa.Column("state", sa.String,
                  sa.CheckConstraint(
                      "state IN ('PLANNED','IMMINENT','IN_WINDOW','POST_TRIP','DEBRIEFED')"
                  ),
                  server_default="PLANNED"),
        sa.Column("conditions_snapshot", postgresql.JSONB),
        # debrief_note_id added after notes table exists (below)
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # ------------------------------------------------------------------
    # notes  — trip_id column intentionally omitted here; added in 0002
    # ------------------------------------------------------------------
    op.create_table(
        "notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("spot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("spots.id")),
        sa.Column("note_date", sa.Date),
        sa.Column("title", sa.Text),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("source_type", sa.String,
                  sa.CheckConstraint(
                      "source_type IN ('handwritten','map','debrief','typed')"
                  )),
        sa.Column("image_path", sa.Text),
        sa.Column("species", postgresql.ARRAY(sa.Text)),
        sa.Column("flies", postgresql.ARRAY(sa.Text)),
        sa.Column("outcome", sa.String,
                  sa.CheckConstraint("outcome IN ('positive','neutral','negative')")),
        sa.Column("negative_reason", sa.String,
                  sa.CheckConstraint(
                      "negative_reason IN "
                      "('conditions','access','fish_absence','gear','unknown')"
                  )),
        sa.Column("approx_cfs", sa.Integer),
        sa.Column("approx_temp", sa.Numeric),
        sa.Column("time_of_day", sa.Text),
        sa.Column("embedding", sa.Text),  # typed as vector(768) below
        sa.Column("author_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id")),
        sa.Column("parent_note_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("notes.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("processing_notes", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    # fts: generated tsvector column
    op.execute(
        "ALTER TABLE notes ADD COLUMN fts tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', coalesce(content,''))) STORED"
    )
    # Retype embedding from Text to vector(768)
    op.execute("ALTER TABLE notes ALTER COLUMN embedding TYPE vector(768) USING NULL")
    op.execute(
        "CREATE INDEX ON notes USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute("CREATE INDEX ON notes USING gin(fts)")

    # Add debrief_note_id to trips now that notes table exists
    op.execute(
        "ALTER TABLE trips ADD COLUMN debrief_note_id UUID REFERENCES notes(id)"
    )

    # ------------------------------------------------------------------
    # conversations
    # ------------------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id")),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trips.id")),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("last_active", sa.DateTime(timezone=True)),
        sa.Column("session_candidates", postgresql.JSONB),
        sa.Column("excluded_spot_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
        sa.Column("surfaced_spot_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
        sa.Column("pending_filter_update", postgresql.JSONB),
    )

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id")),
        sa.Column("role", sa.String,
                  sa.CheckConstraint("role IN ('user','assistant')"),
                  nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # ------------------------------------------------------------------
    # conditions_cache
    # ------------------------------------------------------------------
    op.create_table(
        "conditions_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("spot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("spots.id")),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("data", postgresql.JSONB, nullable=False),
        sa.Column("data_hash", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_conditions_cache_spot_source_fetched",
        "conditions_cache",
        ["spot_id", "source", sa.text("fetched_at DESC")],
    )

    # ------------------------------------------------------------------
    # response_cache
    # ------------------------------------------------------------------
    op.create_table(
        "response_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("spot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("spots.id")),
        sa.Column("conditions_hash", sa.Text, nullable=False),
        sa.Column("response_text", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("spot_id", "conditions_hash"),
    )

    # ------------------------------------------------------------------
    # stocking_events
    # ------------------------------------------------------------------
    op.create_table(
        "stocking_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("spot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("spots.id")),
        sa.Column("stocked_date", sa.Date),
        sa.Column("species", sa.Text),
        sa.Column("count", sa.Integer),
        sa.Column("size_description", sa.Text),
        sa.Column("source_record_id", sa.Text),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # ------------------------------------------------------------------
    # emergency_closures
    # ------------------------------------------------------------------
    op.create_table(
        "emergency_closures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("spot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("spots.id")),
        sa.Column("rule_text", sa.Text, nullable=False),
        sa.Column("effective", sa.Date),
        sa.Column("expires", sa.Date),
        sa.Column("source_url", sa.Text),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_emergency_closures_spot_expires",
        "emergency_closures",
        ["spot_id", "expires"],
    )

    # ------------------------------------------------------------------
    # saved_spots
    # ------------------------------------------------------------------
    op.create_table(
        "saved_spots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id")),
        sa.Column("spot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("spots.id")),
        sa.Column("personal_notes", sa.Text),
        sa.Column("saved_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("user_id", "spot_id"),
    )

    # ------------------------------------------------------------------
    # backup_checksums
    # ------------------------------------------------------------------
    op.create_table(
        "backup_checksums",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("spots_count", sa.Integer, nullable=False),
        sa.Column("notes_count", sa.Integer, nullable=False),
        sa.Column("notes_hash", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("backup_checksums")
    op.drop_table("saved_spots")
    op.drop_table("emergency_closures")
    op.drop_table("stocking_events")
    op.drop_table("response_cache")
    op.drop_table("conditions_cache")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.execute("ALTER TABLE trips DROP COLUMN IF EXISTS debrief_note_id")
    op.drop_table("notes")
    op.drop_table("trips")
    op.drop_table("spots")
    op.drop_table("magic_link_tokens")
    op.drop_table("sessions")
    op.drop_table("users")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
