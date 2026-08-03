from collections.abc import Mapping

import pytest

from maais.market_data.events import ObservedMarketEvent
from maais.market_data.recovery import MarketCursor
from maais.orchestration.composition import HealthAwareDispatchEngine
from maais.orchestration.observations import RuntimeHealthRegistry
from maais.orchestration.worker import CursorKey, DispatchResult
from tests.unit.market_data.test_frame_builder import _bar


class _FailingDispatchEngine:
    @property
    def cursors(self) -> Mapping[CursorKey, MarketCursor]:
        return {}

    async def process(self, event: ObservedMarketEvent) -> DispatchResult:
        del event
        raise RuntimeError("simulated dispatch failure")


async def test_dispatch_failure_marks_every_advertised_component_unhealthy() -> None:
    components = ("market_data", "execution")
    health = RuntimeHealthRegistry(components)
    engine = HealthAwareDispatchEngine(_FailingDispatchEngine(), health, components)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="simulated dispatch failure"):
        await engine.process(_bar())

    observations = {item.component: item for item in health.snapshot()}
    assert set(observations) == set(components)
    assert all(not item.healthy for item in observations.values())
    assert {item.error for item in observations.values()} == {
        "RuntimeError: simulated dispatch failure"
    }
