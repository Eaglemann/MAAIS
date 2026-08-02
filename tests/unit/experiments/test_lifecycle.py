from datetime import datetime, timezone

import pytest

from maais.domain.enums import ExperimentStatus
from maais.experiments.service import (
    ExperimentLifecycle,
    InvalidExperimentTransition,
)
from tests.unit.experiments.test_manifest import _manifest

NOW = datetime(2026, 8, 2, 11, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("status", "command", "next_status"),
    [
        (ExperimentStatus.CREATED, "start", ExperimentStatus.RUNNING),
        (ExperimentStatus.RUNNING, "pause", ExperimentStatus.PAUSED),
        (ExperimentStatus.PAUSED, "resume", ExperimentStatus.RUNNING),
        (ExperimentStatus.RUNNING, "stop", ExperimentStatus.STOPPED),
    ],
)
def test_valid_transition_emits_one_event(
    status: ExperimentStatus,
    command: str,
    next_status: ExperimentStatus,
) -> None:
    lifecycle = ExperimentLifecycle(_manifest(), status=status, version=3, now=lambda: NOW)

    transition = getattr(lifecycle, command)()

    assert transition.status is next_status
    assert transition.expected_version == 3
    assert len(transition.events) == 1
    assert transition.events[0].payload["previous_status"] == status.value
    assert transition.events[0].payload["status"] == next_status.value


def test_completed_experiment_cannot_resume() -> None:
    lifecycle = ExperimentLifecycle(
        _manifest(),
        ExperimentStatus.COMPLETED,
        version=4,
        now=lambda: NOW,
    )

    with pytest.raises(InvalidExperimentTransition):
        lifecycle.resume()


def test_failure_requires_non_empty_reason() -> None:
    lifecycle = ExperimentLifecycle(
        _manifest(),
        ExperimentStatus.RUNNING,
        version=2,
        now=lambda: NOW,
    )

    with pytest.raises(ValueError, match="failure reason"):
        lifecycle.fail(" ")
