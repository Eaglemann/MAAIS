from __future__ import annotations

from typing import Any

from maais.artifacts.configured import build_configured_artifact_runtime
from maais.config.artifacts import ArtifactStoreMode
from maais.config.settings import Settings


async def test_configured_artifact_runtime_builds_two_distinct_secret_safe_clients() -> None:
    calls: list[dict[str, Any]] = []

    def client_factory(_service: str, **kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql+psycopg://maais:"
            "local-password@localhost:5432/maais"  # pragma: allowlist secret
        ),
        artifact_store_mode=ArtifactStoreMode.DUAL_S3,
        artifact_replica_endpoint_url="https://storage.railway.example",
        artifact_replica_region="auto",
        artifact_replica_bucket="maais-replica",
        artifact_replica_access_key="replica-access-canary",  # pragma: allowlist secret
        artifact_replica_secret_key="replica-secret-canary",  # pragma: allowlist secret
        artifact_canonical_endpoint_url="https://s3.worm-provider.example",
        artifact_canonical_region="eu-central-1",
        artifact_canonical_bucket="maais-canonical",
        artifact_canonical_access_key="canonical-access-canary",  # pragma: allowlist secret
        artifact_canonical_secret_key="canonical-secret-canary",  # pragma: allowlist secret
    )

    runtime = build_configured_artifact_runtime(settings, client_factory=client_factory)
    try:
        assert len(calls) == 2
        assert calls[0]["endpoint_url"] == "https://storage.railway.example"
        assert calls[1]["endpoint_url"] == "https://s3.worm-provider.example"
        assert runtime.replica_store is not runtime.canonical_store
        rendered = repr(runtime)
        for canary in (
            "replica-access-canary",
            "replica-secret-canary",
            "canonical-access-canary",
            "canonical-secret-canary",
        ):
            assert canary not in rendered
    finally:
        await runtime.close()
