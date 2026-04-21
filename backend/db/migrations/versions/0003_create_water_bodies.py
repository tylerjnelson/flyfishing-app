"""create water_bodies table and migrate from spots

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-21

Phase 1 of TWO-TIER-ARCHITECTURE:
- Create water_bodies table with all fishery-level fields
- Copy all spots rows into water_bodies, applying transforms:
  - type: set 'lake' where name matches lake-indicator pattern (fixes STOCKED-LAKE-TYPE)
  - aliases: seed with existing spot name
- water_bodies.id values are kept identical to spots.id to simplify Phase 3 FK population
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create water_bodies table
    op.execute("""
        CREATE TABLE water_bodies (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            parent_id UUID REFERENCES water_bodies(id),
            latitude NUMERIC(9,6),
            longitude NUMERIC(9,6),
            elevation_ft INTEGER,
            is_alpine BOOLEAN DEFAULT FALSE,
            county TEXT,
            aliases TEXT[],
            seed_confidence TEXT DEFAULT 'unvalidated',
            usgs_site_ids TEXT[],
            snotel_station_id TEXT,
            wdfw_water_id TEXT,
            wta_trail_url TEXT,
            noaa_station_id TEXT,
            fishing_regs JSONB,
            fly_fishing_legal BOOLEAN DEFAULT TRUE,
            min_cfs INTEGER,
            max_cfs INTEGER,
            min_temp_f NUMERIC DEFAULT 40,
            max_temp_f NUMERIC,
            species_primary TEXT[],
            has_realtime_conditions BOOLEAN DEFAULT FALSE,
            last_stocked_date DATE,
            last_stocked_species TEXT[],
            score NUMERIC DEFAULT 0,
            score_updated TIMESTAMPTZ,
            source TEXT,
            name_embedding vector(768),
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    # Copy all spots rows into water_bodies
    # - Keep id identical to spots.id (critical for Phase 3 FK population)
    # - Apply lake type fix: set type='lake' where name matches lake/pond/reservoir pattern
    # - Seed aliases array with existing spot name
    op.execute("""
        INSERT INTO water_bodies (
            id, name, type, latitude, longitude, elevation_ft, is_alpine,
            county, aliases, seed_confidence, usgs_site_ids, snotel_station_id,
            wdfw_water_id, wta_trail_url, noaa_station_id, fishing_regs,
            fly_fishing_legal, min_cfs, max_cfs, min_temp_f, max_temp_f,
            species_primary, has_realtime_conditions, last_stocked_date,
            last_stocked_species, score, score_updated, source, name_embedding, created_at
        )
        SELECT
            id,
            name,
            CASE
                WHEN name ~* '\\m(LK|LAKE|POND|PD|RESERVOIR|RES)\\M'
                     AND type != 'lake'
                THEN 'lake'
                ELSE type
            END AS type,
            latitude,
            longitude,
            elevation_ft,
            is_alpine,
            county,
            ARRAY[name] AS aliases,
            seed_confidence,
            usgs_site_ids,
            snotel_station_id,
            wdfw_water_id,
            wta_trail_url,
            noaa_station_id,
            fishing_regs,
            fly_fishing_legal,
            min_cfs,
            max_cfs,
            COALESCE(min_temp_f, 40),
            max_temp_f,
            species_primary,
            has_realtime_conditions,
            last_stocked_date,
            last_stocked_species,
            score,
            score_updated,
            source,
            name_embedding,
            created_at
        FROM spots
    """)

    # Index: name similarity search (trgm) — mirrors spots table index
    op.execute("CREATE INDEX water_bodies_name_trgm_idx ON water_bodies USING gin (name gin_trgm_ops)")
    # Index: score for ordered recommendation queries
    op.execute("CREATE INDEX water_bodies_score_idx ON water_bodies (score DESC)")
    # Index: type filter
    op.execute("CREATE INDEX water_bodies_type_idx ON water_bodies (type)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS water_bodies")
