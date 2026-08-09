"""Immutable artifact contracts and storage adapters."""

from maais.artifacts.models import (
    ArtifactPutDisposition,
    ArtifactPutResult,
    ArtifactWriteRequest,
    RetentionRequest,
    StoreCapabilities,
    StoredArtifact,
)
from maais.artifacts.store import (
    ArtifactCollisionError,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactVerificationError,
    MissingArtifactError,
    StoreCapabilityError,
)

__all__ = [
    "ArtifactCollisionError",
    "ArtifactPutDisposition",
    "ArtifactPutResult",
    "ArtifactStore",
    "ArtifactStoreError",
    "ArtifactVerificationError",
    "ArtifactWriteRequest",
    "MissingArtifactError",
    "RetentionRequest",
    "StoreCapabilities",
    "StoreCapabilityError",
    "StoredArtifact",
]
