"""migrate FK columns from spots to water_bodies/fishing_spots

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-21

Phase 3 of TWO-TIER-ARCHITECTURE:
- Add water_body_id (nullable) to conditions_cache, stocking_events, emergency_closures
- Add fishing_spot_id (nullable) to notes, trips, response_cache, saved_spots
- Populate new FK columns from old spot_id values using the Phase 1/2 mapping
  (water_bodies.id = spots.id, fishing_spots.water_body_id = water_bodies.id)
- Drop conversations.surfaced_spot_ids (dead column, never used)
- Old spot_id columns left in place (nullable) for application verification before Phase 5
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- conditions_cache: spot_id → water_body_id ---
    op.execute("""
        ALTER TABLE conditions_cache
        ADD COLUMN water_body_id UUID REFERENCES water_bodies(id)
    """)
    op.execute("""
        UPDATE conditions_cache cc
        SET water_body_id = cc.spot_id
        WHERE cc.spot_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM water_bodies wb WHERE wb.id = cc.spot_id)
    """)
    op.execute("CREATE INDEX conditions_cache_water_body_id_idx ON conditions_cache (water_body_id)")

    # --- stocking_events: spot_id → water_body_id ---
    op.execute("""
        ALTER TABLE stocking_events
        ADD COLUMN water_body_id UUID REFERENCES water_bodies(id)
    """)
    op.execute("""
        UPDATE stocking_events se
        SET water_body_id = se.spot_id
        WHERE se.spot_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM water_bodies wb WHERE wb.id = se.spot_id)
    """)
    op.execute("CREATE INDEX stocking_events_water_body_id_idx ON stocking_events (water_body_id)")

    # --- emergency_closures: spot_id → water_body_id ---
    op.execute("""
        ALTER TABLE emergency_closures
        ADD COLUMN water_body_id UUID REFERENCES water_bodies(id)
    """)
    op.execute("""
        UPDATE emergency_closures ec
        SET water_body_id = ec.spot_id
        WHERE ec.spot_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM water_bodies wb WHERE wb.id = ec.spot_id)
    """)
    op.execute("CREATE INDEX emergency_closures_water_body_id_idx ON emergency_closures (water_body_id)")

    # --- notes: spot_id → fishing_spot_id ---
    # Map via: fishing_spots.water_body_id = spots.id (Phase 1/2 identity)
    op.execute("""
        ALTER TABLE notes
        ADD COLUMN fishing_spot_id UUID REFERENCES fishing_spots(id)
    """)
    op.execute("""
        UPDATE notes n
        SET fishing_spot_id = fs.id
        FROM fishing_spots fs
        WHERE fs.water_body_id = n.spot_id
          AND n.spot_id IS NOT NULL
    """)
    op.execute("CREATE INDEX notes_fishing_spot_id_idx ON notes (fishing_spot_id)")

    # --- trips: spot_id → fishing_spot_id ---
    op.execute("""
        ALTER TABLE trips
        ADD COLUMN fishing_spot_id UUID REFERENCES fishing_spots(id)
    """)
    op.execute("""
        UPDATE trips t
        SET fishing_spot_id = fs.id
        FROM fishing_spots fs
        WHERE fs.water_body_id = t.spot_id
          AND t.spot_id IS NOT NULL
    """)
    op.execute("CREATE INDEX trips_fishing_spot_id_idx ON trips (fishing_spot_id)")

    # --- response_cache: spot_id → fishing_spot_id ---
    op.execute("""
        ALTER TABLE response_cache
        ADD COLUMN fishing_spot_id UUID REFERENCES fishing_spots(id)
    """)
    op.execute("""
        UPDATE response_cache rc
        SET fishing_spot_id = fs.id
        FROM fishing_spots fs
        WHERE fs.water_body_id = rc.spot_id
          AND rc.spot_id IS NOT NULL
    """)
    op.execute("CREATE INDEX response_cache_fishing_spot_id_idx ON response_cache (fishing_spot_id)")

    # --- saved_spots: spot_id → fishing_spot_id ---
    op.execute("""
        ALTER TABLE saved_spots
        ADD COLUMN fishing_spot_id UUID REFERENCES fishing_spots(id)
    """)
    op.execute("""
        UPDATE saved_spots ss
        SET fishing_spot_id = fs.id
        FROM fishing_spots fs
        WHERE fs.water_body_id = ss.spot_id
          AND ss.spot_id IS NOT NULL
    """)
    op.execute("CREATE INDEX saved_spots_fishing_spot_id_idx ON saved_spots (fishing_spot_id)")

    # --- Drop dead column conversations.surfaced_spot_ids ---
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS surfaced_spot_ids")


def downgrade() -> None:
    op.execute("ALTER TABLE conversations ADD COLUMN surfaced_spot_ids UUID[]")

    op.execute("DROP INDEX IF EXISTS saved_spots_fishing_spot_id_idx")
    op.execute("ALTER TABLE saved_spots DROP COLUMN IF EXISTS fishing_spot_id")

    op.execute("DROP INDEX IF EXISTS response_cache_fishing_spot_id_idx")
    op.execute("ALTER TABLE response_cache DROP COLUMN IF EXISTS fishing_spot_id")

    op.execute("DROP INDEX IF EXISTS trips_fishing_spot_id_idx")
    op.execute("ALTER TABLE trips DROP COLUMN IF EXISTS fishing_spot_id")

    op.execute("DROP INDEX IF EXISTS notes_fishing_spot_id_idx")
    op.execute("ALTER TABLE notes DROP COLUMN IF EXISTS fishing_spot_id")

    op.execute("DROP INDEX IF EXISTS emergency_closures_water_body_id_idx")
    op.execute("ALTER TABLE emergency_closures DROP COLUMN IF EXISTS water_body_id")

    op.execute("DROP INDEX IF EXISTS stocking_events_water_body_id_idx")
    op.execute("ALTER TABLE stocking_events DROP COLUMN IF EXISTS water_body_id")

    op.execute("DROP INDEX IF EXISTS conditions_cache_water_body_id_idx")
    op.execute("ALTER TABLE conditions_cache DROP COLUMN IF EXISTS water_body_id")
