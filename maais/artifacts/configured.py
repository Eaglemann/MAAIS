from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from maais.artifacts.publisher import ArtifactPublisher
from maais.artifacts.s3 import S3ArtifactStore
from maais.config.artifacts import ArtifactSettings, ArtifactStoreMode
from maais.config.settings import Settings
from maais.db.unit_of_work import UnitOfWork


@dataclass(slots=True, repr=False)
class ConfiguredArtifactRuntime:
    engine: AsyncEngine
    uow_factory: UnitOfWork
    replica_store: S3ArtifactStore
    canonical_store: S3ArtifactStore
    publisher: ArtifactPublisher

    async def close(self) -> None:
        await self.engine.dispose()


def build_configured_artifact_runtime(
    settings: Settings,
    *,
    client_factory: Callable[..., Any] = boto3.client,
) -> ConfiguredArtifactRuntime:
    artifacts = settings.artifacts
    if artifacts.mode is not ArtifactStoreMode.DUAL_S3:
        raise ValueError("configured artifact runtime requires dual_s3 mode")
    replica = S3ArtifactStore(
        client=_s3_client(client_factory, artifacts, canonical=False),
        bucket=artifacts.replica_bucket,
        canonical=False,
        store_name="railway_replica",
    )
    canonical = S3ArtifactStore(
        client=_s3_client(client_factory, artifacts, canonical=True),
        bucket=artifacts.canonical_bucket,
        canonical=True,
        store_name="worm_canonical",
    )
    engine = create_async_engine(
        settings.database_url_value,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    uow_factory = UnitOfWork(async_sessionmaker(engine, expire_on_commit=False))
    return ConfiguredArtifactRuntime(
        engine=engine,
        uow_factory=uow_factory,
        replica_store=replica,
        canonical_store=canonical,
        publisher=ArtifactPublisher(
            replica=replica,
            canonical=canonical,
            uow_factory=uow_factory,
        ),
    )


def _s3_client(
    client_factory: Callable[..., Any],
    settings: ArtifactSettings,
    *,
    canonical: bool,
) -> Any:
    prefix = "canonical" if canonical else "replica"
    session_token = getattr(settings, f"{prefix}_session_token_value")
    parameters: dict[str, Any] = {
        "endpoint_url": getattr(settings, f"{prefix}_endpoint_url"),
        "region_name": getattr(settings, f"{prefix}_region"),
        "aws_access_key_id": getattr(settings, f"{prefix}_access_key_value"),
        "aws_secret_access_key": getattr(settings, f"{prefix}_secret_key_value"),
        "config": Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
            signature_version="s3v4",
        ),
    }
    if session_token:
        parameters["aws_session_token"] = session_token
    return client_factory("s3", **parameters)
