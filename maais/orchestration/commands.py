from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from uuid import UUID

from maais.config.constants import ALL_AGENTS
from maais.experiments.manifest import ExperimentManifest
from maais.market_data.frames import CausalMinuteFrame
from maais.market_data.integrity.state_machine import IntegrityAssessment


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


@dataclass(frozen=True, slots=True)
class OrchestrationCommand:
    """All immutable identities required to decide one causal frame."""

    frame: CausalMinuteFrame
    integrity: IntegrityAssessment
    manifest: ExperimentManifest
    agent_version_ids: Mapping[str, UUID]
    evaluated_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.evaluated_at, "evaluated_at")
        _require_utc(self.completed_at, "completed_at")
        if self.integrity.frame_id != self.frame.frame_id:
            raise ValueError("integrity assessment belongs to another frame")
        if self.manifest.experiment_id != self.frame.key.experiment_id:
            raise ValueError("manifest and frame experiment differ")
        if self.frame.key.symbol not in self.manifest.symbols:
            raise ValueError("frame symbol is not pinned by the experiment manifest")
        if self.evaluated_at < self.frame.cutoff_at or self.completed_at < self.evaluated_at:
            raise ValueError("orchestration times are not causally ordered")
        version_ids = dict(self.agent_version_ids)
        if set(version_ids) != set(ALL_AGENTS) or any(
            value.int == 0 for value in version_ids.values()
        ):
            raise ValueError("agent version IDs must contain exactly eight non-nil identities")
        object.__setattr__(self, "agent_version_ids", MappingProxyType(version_ids))

    @property
    def feature_version(self) -> str:
        version = self.manifest.component_versions.get("features")
        if not isinstance(version, str) or not version:
            raise ValueError("manifest must pin a feature version")
        return version
