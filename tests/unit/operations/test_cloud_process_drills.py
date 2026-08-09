from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from maais.operations.cloud_evidence import CloudEvidenceError
from maais.operations.cloud_process_drills import (
    CLOUD_DRILLS,
    CloudDrillObservation,
    CloudProcessDrillSnapshot,
    evaluate_cloud_process_drills,
    load_verified_cloud_process_drills,
    write_cloud_process_drill_bundle,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 9, 20, 0, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-4000-8000-000000000201")
EXPERIMENT_ID = UUID("00000000-0000-4000-8000-000000000202")
OPERATION_ID = UUID("00000000-0000-4000-8000-000000000203")
CANDIDATE_HASH = "1" * 64
MANIFEST_HASH = "2" * 64


def _observations() -> tuple[CloudDrillObservation, ...]:
    observations: list[CloudDrillObservation] = []
    for index, name in enumerate(CLOUD_DRILLS):
        started = NOW + timedelta(minutes=index * 5)
        observations.append(
            CloudDrillObservation(
                name=name,
                started_at=started,
                action_at=started + timedelta(minutes=1),
                recovered_at=started + timedelta(minutes=2),
                before_boot_id=UUID(f"00000000-0000-4000-8000-{index + 1:012d}"),
                after_boot_id=UUID(f"00000000-0000-4000-8000-{index + 101:012d}"),
                before_lease_epoch=3,
                after_lease_epoch=4,
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
        )
    return tuple(observations)


def _snapshot(
    observations: tuple[CloudDrillObservation, ...] | None = None,
) -> CloudProcessDrillSnapshot:
    return CloudProcessDrillSnapshot.create(
        operation_id=OPERATION_ID,
        environment="qualification",
        candidate_hash=CANDIDATE_HASH,
        run_id=RUN_ID,
        experiment_id=EXPERIMENT_ID,
        manifest_hash=MANIFEST_HASH,
        captured_at=NOW + timedelta(hours=1),
        observations=observations or _observations(),
    )


def test_cloud_drill_contract_is_exact() -> None:
    assert CLOUD_DRILLS == (
        "mission_control_replacement",
        "worker_replacement_lease_takeover",
        "operations_daily_close_replacement",
        "database_interruption_fail_closed",
        "railway_artifact_target_failure",
        "worm_artifact_target_failure",
        "sentry_outage_fallback",
        "backup_restore",
    )


def test_complete_ordered_cloud_process_drills_pass() -> None:
    report = evaluate_cloud_process_drills(
        _snapshot(),
        expected_candidate_hash=CANDIDATE_HASH,
        expected_run_id=RUN_ID,
        expected_experiment_id=EXPERIMENT_ID,
        expected_manifest_hash=MANIFEST_HASH,
        expected_environment="qualification",
        evaluated_at=NOW + timedelta(hours=1, minutes=1),
    )

    assert report["passed"] is True
    checks = cast(list[dict[str, object]], report["checks"])
    assert [check["name"] for check in checks] == list(CLOUD_DRILLS)
    assert report["safety"] == {"paper_trading_only": True, "live_money": False}


@pytest.mark.parametrize(
    ("drill", "field"),
    [
        ("mission_control_replacement", "duplicate_decisions"),
        ("worker_replacement_lease_takeover", "after_lease_epoch"),
        ("operations_daily_close_replacement", "daily_report_count"),
        ("database_interruption_fail_closed", "failed_closed"),
        ("railway_artifact_target_failure", "idempotent_retry"),
        ("worm_artifact_target_failure", "alert_recorded"),
        ("sentry_outage_fallback", "incident_recorded"),
        ("backup_restore", "reconciled"),
    ],
)
def test_each_drill_fails_on_its_required_recovery_semantics(drill: str, field: str) -> None:
    observations = list(_observations())
    index = CLOUD_DRILLS.index(drill)
    value: object = 1 if field == "duplicate_decisions" else False
    if field == "daily_report_count":
        value = 0
    if field == "after_lease_epoch":
        value = observations[index].before_lease_epoch
    observations[index] = replace(observations[index], **{field: value})

    report = evaluate_cloud_process_drills(
        _snapshot(tuple(observations)),
        expected_candidate_hash=CANDIDATE_HASH,
        expected_run_id=RUN_ID,
        expected_experiment_id=EXPERIMENT_ID,
        expected_manifest_hash=MANIFEST_HASH,
        expected_environment="qualification",
        evaluated_at=NOW + timedelta(hours=1, minutes=1),
    )

    assert report["passed"] is False
    checks = cast(list[dict[str, object]], report["checks"])
    failed = [check["name"] for check in checks if check["passed"] is False]
    assert failed == [drill]


def test_overlapping_or_reordered_drills_are_rejected() -> None:
    observations = list(_observations())
    observations[1] = replace(
        observations[1],
        started_at=observations[0].recovered_at - timedelta(seconds=1),
    )

    with pytest.raises(CloudEvidenceError, match="strictly ordered"):
        _snapshot(tuple(observations))


def test_process_drill_snapshot_hash_tampering_is_rejected() -> None:
    payload = _snapshot().to_dict()
    payload["environment"] = "production"

    with pytest.raises(CloudEvidenceError, match="snapshot hash"):
        CloudProcessDrillSnapshot.from_dict(payload)


def test_cloud_process_drill_bundle_round_trip_is_verified(tmp_path: Path) -> None:
    report = evaluate_cloud_process_drills(
        _snapshot(),
        expected_candidate_hash=CANDIDATE_HASH,
        expected_run_id=RUN_ID,
        expected_experiment_id=EXPERIMENT_ID,
        expected_manifest_hash=MANIFEST_HASH,
        expected_environment="qualification",
        evaluated_at=NOW + timedelta(hours=1, minutes=1),
    )
    paths = write_cloud_process_drill_bundle(report, tmp_path)

    loaded, verified = load_verified_cloud_process_drills(paths.directory)

    assert verified is True
    assert loaded == report
