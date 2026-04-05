import uuid
from typing import Optional

from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.types import UserDefinedType


# ---------------------------------------------------------------------------
# Custom pgvector type — renders as vector(N) in DDL; no extra package needed
# ---------------------------------------------------------------------------

class Vector(UserDefinedType):
    cache_ok = True

    def __init__(self, dim: int):
        self.dim = dim

    def get_col_spec(self, **kw):
        return f"vector({self.dim})"

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return f"[{','.join(str(float(v)) for v in value)}]"
        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            return [float(x) for x in value.strip("[]").split(",")]
        return process


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    email = Column(Text, unique=True, nullable=False)
    display_name = Column(Text)
    preferences = Column(JSON, default=dict)  # profile-scoped intake answers
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_active = Column(Boolean, default=True)

    sessions = relationship("Session", back_populates="user")
    notes = relationship("Note", back_populates="author")
    trips = relationship("Trip", back_populates="user")
    saved_spots = relationship("SavedSpot", back_populates="user")
    conversations = relationship("Conversation", back_populates="user")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(), ForeignKey("users.id"))
    refresh_token = Column(Text, unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_active = Column(DateTime(timezone=True))
    device_hint = Column(Text)

    user = relationship("User", back_populates="sessions")


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    email = Column(Text, nullable=False)
    token_hash = Column(Text, unique=True, nullable=False)  # SHA-256, never plaintext
    expires_at = Column(DateTime(timezone=True), nullable=False)  # NOW() + 15 min
    used_at = Column(DateTime(timezone=True))  # null = unused; set on first valid click


class Spot(Base):
    __tablename__ = "spots"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False)
    aliases = Column(ARRAY(Text))
    type = Column(String, nullable=False)  # river | lake | creek | coastal
    latitude = Column(Numeric(9, 6))
    longitude = Column(Numeric(9, 6))
    elevation_ft = Column(Integer)
    is_alpine = Column(Boolean, default=False)
    county = Column(Text)
    is_public = Column(Boolean, default=True)
    permit_required = Column(Boolean, default=False)
    permit_url = Column(Text)
    source = Column(Text)  # wdfw_stocking | wdfw_access | wta | notes | debrief | user
    seed_confidence = Column(String, default="unvalidated")
    # confirmed=1.0, probable=0.6, unvalidated=0.2 scorer multiplier
    usgs_site_ids = Column(ARRAY(Text))
    noaa_station_id = Column(Text)
    snotel_station_id = Column(Text)  # e.g. '679:WA:SNTL'
    wdfw_water_id = Column(Text)
    wta_trail_url = Column(Text)
    has_realtime_conditions = Column(Boolean, default=False)
    species_primary = Column(ARRAY(Text))
    min_cfs = Column(Integer)
    max_cfs = Column(Integer)
    min_temp_f = Column(Numeric, default=40)
    max_temp_f = Column(Numeric)
    fishing_regs = Column(JSONB)
    # { open_dates, gear, size_limits, bag_limits, special_rules, year_round_closed }
    fly_fishing_legal = Column(Boolean, default=True)
    # false when gear = 'bait_only'; pre-LLM hard filter
    name_embedding = Column(Vector(768))
    # embedding of: name || aliases || county — used in spot entity resolution
    last_stocked_date = Column(Date)
    last_stocked_species = Column(ARRAY(Text))
    score = Column(Numeric, default=0)
    score_updated = Column(DateTime(timezone=True))
    last_visited = Column(Date)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    notes = relationship("Note", back_populates="spot")
    trips = relationship("Trip", back_populates="spot")
    conditions_cache = relationship("ConditionsCache", back_populates="spot")
    response_cache = relationship("ResponseCache", back_populates="spot")
    stocking_events = relationship("StockingEvent", back_populates="spot")
    emergency_closures = relationship("EmergencyClosure", back_populates="spot")
    saved_spots = relationship("SavedSpot", back_populates="spot")


class Note(Base):
    __tablename__ = "notes"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    spot_id = Column(Uuid(), ForeignKey("spots.id"))
    note_date = Column(Date)
    title = Column(Text)
    content = Column(Text, nullable=False)
    source_type = Column(String)  # handwritten | map | debrief | typed
    image_path = Column(Text)
    species = Column(ARRAY(Text))
    flies = Column(ARRAY(Text))
    outcome = Column(String)  # positive | neutral | negative
    negative_reason = Column(String)
    # conditions | access | fish_absence | gear | unknown
    approx_cfs = Column(Integer)
    approx_temp = Column(Numeric)
    time_of_day = Column(Text)
    embedding = Column(Vector(768))
    # fts is a GENERATED column — defined in migration, not mapped here
    author_id = Column(Uuid(), ForeignKey("users.id"))
    # trip_id FK is added by migration 0002 (circular FK resolution)
    trip_id: Optional[Column] = Column(Uuid(), ForeignKey("trips.id"), nullable=True)
    parent_note_id = Column(Uuid(), ForeignKey("notes.id"), nullable=True)
    # set on extracted map records; points to source handwritten note
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    processing_notes = Column(Text)
    # 'low_quality_scan' when contrast < 30 or min_dimension < 200px; null otherwise
    updated_at = Column(DateTime(timezone=True))

    spot = relationship("Spot", back_populates="notes")
    author = relationship("User", back_populates="notes")
    trip = relationship("Trip", back_populates="notes", foreign_keys=[trip_id])
    child_notes = relationship("Note", foreign_keys=[parent_note_id])


class Trip(Base):
    __tablename__ = "trips"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(), ForeignKey("users.id"))
    spot_id = Column(Uuid(), ForeignKey("spots.id"))
    trip_date = Column(Date)
    departure_time = Column(DateTime(timezone=True))
    return_time = Column(DateTime(timezone=True))
    session_intake = Column(JSONB, default=dict)
    state = Column(String, default="PLANNED")
    # PLANNED | IMMINENT | IN_WINDOW | POST_TRIP | DEBRIEFED
    conditions_snapshot = Column(JSONB)
    debrief_note_id = Column(Uuid(), ForeignKey("notes.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="trips")
    spot = relationship("Spot", back_populates="trips")
    notes = relationship("Note", back_populates="trip", foreign_keys="Note.trip_id")
    debrief_note = relationship("Note", foreign_keys=[debrief_note_id])
    conversations = relationship("Conversation", back_populates="trip")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(), ForeignKey("users.id"))
    trip_id = Column(Uuid(), ForeignKey("trips.id"))
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    last_active = Column(DateTime(timezone=True))
    session_candidates = Column(JSONB)
    excluded_spot_ids = Column(ARRAY(Uuid()))
    surfaced_spot_ids = Column(ARRAY(Uuid()))
    pending_filter_update = Column(JSONB)
    # written by streaming handler when [FILTER_UPDATE] intercepted;
    # read by POST /chat/confirm-filter; cleared after Yes or No

    user = relationship("User", back_populates="conversations")
    trip = relationship("Trip", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(Uuid(), ForeignKey("conversations.id"))
    role = Column(String, nullable=False)  # user | assistant
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    conversation = relationship("Conversation", back_populates="messages")


class ConditionsCache(Base):
    __tablename__ = "conditions_cache"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    spot_id = Column(Uuid(), ForeignKey("spots.id"))
    source = Column(Text, nullable=False)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    data = Column(JSONB, nullable=False)
    data_hash = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True))

    spot = relationship("Spot", back_populates="conditions_cache")


class ResponseCache(Base):
    __tablename__ = "response_cache"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    spot_id = Column(Uuid(), ForeignKey("spots.id"))
    conditions_hash = Column(Text, nullable=False)
    response_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("spot_id", "conditions_hash"),)

    spot = relationship("Spot", back_populates="response_cache")


class StockingEvent(Base):
    __tablename__ = "stocking_events"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    spot_id = Column(Uuid(), ForeignKey("spots.id"))
    stocked_date = Column(Date)
    species = Column(Text)
    count = Column(Integer)
    size_description = Column(Text)
    source_record_id = Column(Text)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())

    spot = relationship("Spot", back_populates="stocking_events")


class EmergencyClosure(Base):
    __tablename__ = "emergency_closures"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    spot_id = Column(Uuid(), ForeignKey("spots.id"))
    rule_text = Column(Text, nullable=False)
    effective = Column(Date)
    expires = Column(Date)
    source_url = Column(Text)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())

    spot = relationship("Spot", back_populates="emergency_closures")


class SavedSpot(Base):
    __tablename__ = "saved_spots"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(), ForeignKey("users.id"))
    spot_id = Column(Uuid(), ForeignKey("spots.id"))
    personal_notes = Column(Text)
    saved_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "spot_id"),)

    user = relationship("User", back_populates="saved_spots")
    spot = relationship("Spot", back_populates="saved_spots")


class BackupChecksum(Base):
    __tablename__ = "backup_checksums"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now())
    spots_count = Column(Integer, nullable=False)
    notes_count = Column(Integer, nullable=False)
    notes_hash = Column(Text, nullable=False)
    # MD5 of string_agg of note IDs ordered by created_at
