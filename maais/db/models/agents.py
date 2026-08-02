from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base


class AgentWeightModel(Base):
    """Legacy learned-weight projection retained for schema fidelity."""

    __tablename__ = "agent_weights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    weight: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        server_default=text("1.0"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
