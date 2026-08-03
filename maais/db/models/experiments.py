from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue


class ExperimentModel(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("manifest_hash", name="uq_experiment_manifest_hash"),
        CheckConstraint("initial_capital > 0", name="ck_experiment_initial_capital_positive"),
        CheckConstraint(
            "manifest_schema_version > 0", name="ck_experiment_manifest_version_positive"
        ),
        CheckConstraint(
            "mode IN ('replay', 'paper_live', 'testnet_smoke')", name="ck_experiment_mode"
        ),
        CheckConstraint(
            "status IN ('created', 'running', 'paused', 'stopped', 'completed', 'failed')",
            name="ck_experiment_status",
        ),
        Index("ix_experiments_status_created", "status", "created_at"),
        Index("ix_experiments_mode_created", "mode", "created_at"),
        Index("ix_experiments_config_hash", "config_hash"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    git_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    worktree_hash: Mapped[str | None] = mapped_column(String(64))
    lock_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_revision: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(1000))


class StrategyVersionModel(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_key", "version", name="uq_strategy_version_identity"),
        CheckConstraint(
            "stage IN ('research', 'simulation', 'pilot', 'full_production')",
            name="ck_strategy_version_stage",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    implementation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameter_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentVersionModel(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_name", "version", name="uq_agent_version_identity"),
        CheckConstraint(
            "maturity IN ('implemented', 'proxy', 'disabled')",
            name="ck_agent_version_maturity",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    maturity: Mapped[str] = mapped_column(String(32), nullable=False)
    implementation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameter_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    data_dependencies_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(
        JSONB, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
