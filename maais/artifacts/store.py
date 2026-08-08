from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from maais.artifacts.models import (
    ArtifactPutResult,
    ArtifactWriteRequest,
    StoreCapabilities,
    StoredArtifact,
)


class ArtifactStoreError(RuntimeError):
    """Base class for stable, secret-free artifact store failures."""


class ArtifactVerificationError(ArtifactStoreError):
    pass


class ArtifactCollisionError(ArtifactStoreError):
    pass


class MissingArtifactError(ArtifactStoreError):
    pass


class StoreCapabilityError(ArtifactStoreError):
    pass


@runtime_checkable
class ArtifactStore(Protocol):
    async def capabilities(self) -> StoreCapabilities: ...

    async def put_verified(self, request: ArtifactWriteRequest) -> ArtifactPutResult: ...

    async def head(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> StoredArtifact: ...

    def read_chunks(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> AsyncIterator[bytes]: ...
