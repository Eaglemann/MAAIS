"""Monitoring layer schemas."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AlertLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ComponentName(str, Enum):
    MARKET_DATA = "market_data"
    FEATURE_PIPELINE = "feature_pipeline"
    AGENTS = "agents"
    DECISION = "decision"
    RISK = "risk"
    EXECUTION = "execution"
    MONITORING = "monitoring"


@dataclass
class AlertEvent:
    """A single alert emitted by the monitoring system."""

    level: AlertLevel
    component: str
    title: str
    message: str
    timestamp: datetime = field(
        default_factory=lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
    )
    metadata: dict = field(default_factory=dict)


@dataclass
class HealthStatus:
    """Health record for a single system component."""

    component: str
    is_healthy: bool
    last_heartbeat: datetime | None
    error_count: int = 0
    last_error: str | None = None
