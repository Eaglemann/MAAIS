from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from maais.artifacts.models import ArtifactRecord, ArtifactType
from maais.artifacts.publisher import ArtifactPublisher, PublicationRequest
from maais.artifacts.store import ArtifactVerificationError
from maais.operations.artifact_publication import (
    CloudArtifactIdentity,
    publish_daily_report_bundle,
)
from maais.operations.reporting import write_daily_report_bundle
from tests.unit.operations.test_reporting import _report

NOW = datetime(2026, 8, 8, 20, tzinfo=timezone.utc)


class CapturingPublisher:
    def __init__(self) -> None:
        self.requests: list[PublicationRequest] = []

    async def publish(self, request: PublicationRequest) -> ArtifactRecord:
        self.requests.append(request)
        return cast(ArtifactRecord, object())


def _identity() -> CloudArtifactIdentity:
    return CloudArtifactIdentity(
        environment="qualification",
        candidate_hash="a" * 64,
        experiment_id=UUID("11111111-1111-4111-8111-111111111111"),
        run_id=UUID("22222222-2222-4222-8222-222222222222"),
        operation_id=UUID("33333333-3333-4333-8333-333333333333"),
        generated_at=NOW,
        producing_deployment_id="deployment-1",
        producing_service_id="operations-1",
    )


async def test_cloud_daily_report_preserves_local_bundle_and_binds_full_identity(
    tmp_path: Path,
) -> None:
    report = _report()
    paths = write_daily_report_bundle(report, tmp_path)
    publisher = CapturingPublisher()

    await publish_daily_report_bundle(
        cast(ArtifactPublisher, publisher),
        paths,
        report_id=str(report["report_id"]),
        identity=_identity(),
    )

    assert paths.json_path.is_file()
    assert len(publisher.requests) == 1
    request = publisher.requests[0]
    assert request.artifact_type is ArtifactType.DAILY_REPORT
    assert request.bundle_directory == paths.directory
    assert request.operation_id == _identity().operation_id
    assert request.producing_deployment_id == "deployment-1"


async def test_cloud_daily_report_never_calls_publisher_after_local_tampering(
    tmp_path: Path,
) -> None:
    report = _report()
    paths = write_daily_report_bundle(report, tmp_path)
    paths.json_path.write_text("tampered\n", encoding="utf-8")
    publisher = CapturingPublisher()

    with pytest.raises(ArtifactVerificationError):
        await publish_daily_report_bundle(
            cast(ArtifactPublisher, publisher),
            paths,
            report_id=str(report["report_id"]),
            identity=_identity(),
        )

    assert publisher.requests == []
