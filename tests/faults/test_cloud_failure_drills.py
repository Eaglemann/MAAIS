from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID

from maais.operations.cloud_process_drills import (
    CLOUD_DRILLS,
    CloudDrillObservation,
    CloudProcessDrillSnapshot,
    evaluate_cloud_process_drills,
)

UTC = timezone.utc


def test_recovery_never_hides_duplicate_trade_state() -> None:
    started = datetime(2026, 8, 9, 20, 0, tzinfo=UTC)
    observations = tuple(
        CloudDrillObservation(
            name=name,
            started_at=started + timedelta(minutes=index * 5),
            action_at=started + timedelta(minutes=index * 5 + 1),
            recovered_at=started + timedelta(minutes=index * 5 + 2),
            before_boot_id=UUID(f"00000000-0000-4000-8000-{index + 1:012d}"),
            after_boot_id=UUID(f"00000000-0000-4000-8000-{index + 101:012d}"),
            before_lease_epoch=2,
            after_lease_epoch=3,
            duplicate_decisions=0,
            duplicate_orders=0,
            duplicate_fills=0,
            duplicate_counterfactuals=0,
            daily_report_count=1,
            backup_count=1,
            failed_closed=True,
            incident_recorded=True,
            alert_recorded=True,
            idempotent_retry=True,
            reconciled=True,
            source_hash=f"{index + 3:x}" * 64,
        )
        for index, name in enumerate(CLOUD_DRILLS)
    )
    observations = (
        replace(observations[0], duplicate_orders=1),
        *observations[1:],
    )
    snapshot = CloudProcessDrillSnapshot.create(
        operation_id=UUID("00000000-0000-4000-8000-000000000301"),
        environment="qualification",
        candidate_hash="1" * 64,
        run_id=UUID("00000000-0000-4000-8000-000000000302"),
        experiment_id=UUID("00000000-0000-4000-8000-000000000303"),
        manifest_hash="2" * 64,
        captured_at=started + timedelta(hours=1),
        observations=observations,
    )

    report = evaluate_cloud_process_drills(
        snapshot,
        expected_candidate_hash="1" * 64,
        expected_run_id=UUID("00000000-0000-4000-8000-000000000302"),
        expected_experiment_id=UUID("00000000-0000-4000-8000-000000000303"),
        expected_manifest_hash="2" * 64,
        expected_environment="qualification",
        evaluated_at=started + timedelta(hours=1, minutes=1),
    )

    assert report["passed"] is False
    checks = cast(list[dict[str, object]], report["checks"])
    assert checks[0]["detail"] == "duplicate_trade_state"
