from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue


class EventStreamModel(Base):
    __tablename__ = "event_streams"
    __table_args__ = (
        UniqueConstraint("aggregate_type", "aggregate_id", name="uq_event_stream_aggregate"),
        CheckConstraint("current_version >= 0", name="ck_event_stream_version_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DomainEventModel(Base):
    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint("stream_id", "stream_version", name="uq_domain_event_stream_version"),
        UniqueConstraint("global_position", name="uq_domain_event_global_position"),
        CheckConstraint("stream_version > 0", name="ck_domain_event_stream_version_positive"),
        CheckConstraint("event_version > 0", name="ck_domain_event_version_positive"),
        Index("ix_domain_events_aggregate_time", "aggregate_type", "aggregate_id", "occurred_at"),
        Index("ix_domain_events_type_time", "event_type", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    global_position: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    stream_id: Mapped[UUID] = mapped_column(
        ForeignKey("event_streams.id", ondelete="RESTRICT"), nullable=False
    )
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    stream_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    metadata_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEventModel(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("cursor", name="uq_outbox_cursor"),
        UniqueConstraint("domain_event_id", name="uq_outbox_domain_event"),
        CheckConstraint("publish_attempts >= 0", name="ck_outbox_publish_attempts_nonnegative"),
        Index("ix_outbox_unpublished_cursor", "published_at", "cursor"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    cursor: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    domain_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("domain_events.id", ondelete="RESTRICT"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1000))
