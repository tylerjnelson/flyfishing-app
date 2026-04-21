"""create fishing_spots table and seed defaults from water_bodies

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-21

Phase 2 of TWO-TIER-ARCHITECTURE:
- Create fishing_spots table (the recommendation entity)
- For each water_body with coordinates: insert one default fishing_spot
  with the same lat/lon. name=NULL (display inherits water_body.name).
- water_bodies without coords (stocking-carrier rows, unresolved rivers)
  get no fishing_spot; they are excluded from recommendations automatically.
- Result: 523 water bodies with coords → 523 default fishing_spots.

fishing_spots.id is a new UUID (not derived from water_bodies.id).
The spots.id → water_body.id identity from Phase 1 handles FK population in Phase 3.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create fishing_spots table
    op.execute("""
        CREATE TABLE fishing_spots (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            water_body_id UUID NOT NULL REFERENCES water_bodies(id),
            name TEXT,
            latitude NUMERIC(9,6) NOT NULL,
            longitude NUMERIC(9,6) NOT NULL,
            is_public BOOLEAN DEFAULT TRUE,
            permit_required BOOLEAN DEFAULT FALSE,
            permit_url TEXT,
            last_visited DATE,
            name_embedding vector(768),
            source TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    # Seed one default fishing_spot for each water_body that has coordinates.
    # - name = NULL: display inherits water_body.name (single-spot water)
    # - Copy permit_required, permit_url, last_visited from spots (via water_bodies identity)
    # - Copy name_embedding from water_bodies for initial RAG continuity
    op.execute("""
        INSERT INTO fishing_spots (
            id, water_body_id, name, latitude, longitude,
            is_public, permit_required, permit_url, last_visited,
            name_embedding, source, created_at
        )
        SELECT
            gen_random_uuid(),
            wb.id AS water_body_id,
            NULL AS name,
            wb.latitude,
            wb.longitude,
            COALESCE(s.is_public, TRUE) AS is_public,
            COALESCE(s.permit_required, FALSE) AS permit_required,
            s.permit_url,
            s.last_visited,
            wb.name_embedding,
            wb.source,
            wb.created_at
        FROM water_bodies wb
        JOIN spots s ON s.id = wb.id
        WHERE wb.latitude IS NOT NULL
          AND wb.longitude IS NOT NULL
    """)

    # Index: water_body_id for JOIN queries
    op.execute("CREATE INDEX fishing_spots_water_body_id_idx ON fishing_spots (water_body_id)")
    # Index: coordinates for geo queries
    op.execute("CREATE INDEX fishing_spots_coords_idx ON fishing_spots (latitude, longitude)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fishing_spots")
