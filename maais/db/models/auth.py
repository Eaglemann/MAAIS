"""Private single-operator authentication projections."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, SmallInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base


class OperatorSessionModel(Base):
    __tablename__ = "operator_sessions"
    __table_args__ = (
        CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$' AND csrf_hash ~ '^[0-9a-f]{64}$' "
            "AND token_hash <> csrf_hash",
            name="ck_operator_session_hashes",
        ),
        CheckConstraint(
            "actor <> '' AND actor = btrim(actor)",
            name="ck_operator_session_actor",
        ),
        CheckConstraint("version >= 1", name="ck_operator_session_version"),
        CheckConstraint(
            "created_at <= last_seen_at AND last_seen_at <= expires_at AND "
            "expires_at = created_at + INTERVAL '12 hours' AND "
            "(revoked_at IS NULL OR revoked_at >= last_seen_at)",
            name="ck_operator_session_time_order",
        ),
        Index(
            "uq_operator_sessions_token_hash",
            "token_hash",
            unique=True,
        ),
        Index(
            "uq_operator_sessions_csrf_hash",
            "csrf_hash",
            unique=True,
        ),
        Index(
            "ix_operator_sessions_active_expiry",
            "expires_at",
            "last_seen_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        {"schema": "maais_auth"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class OperatorAuthStateModel(Base):
    __tablename__ = "operator_auth_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_operator_auth_state_singleton"),
        CheckConstraint(
            "failed_attempts BETWEEN 0 AND 5 AND version >= 1",
            name="ck_operator_auth_state_counts",
        ),
        CheckConstraint(
            "(failed_attempts = 0 AND window_started_at IS NULL AND locked_until IS NULL) OR "
            "(failed_attempts BETWEEN 1 AND 4 AND window_started_at IS NOT NULL AND "
            "updated_at >= window_started_at AND locked_until IS NULL) OR "
            "(failed_attempts = 5 AND window_started_at IS NOT NULL AND "
            "updated_at >= window_started_at AND locked_until IS NOT NULL AND "
            "locked_until = updated_at + INTERVAL '30 minutes')",
            name="ck_operator_auth_state_lifecycle",
        ),
        {"schema": "maais_auth"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
