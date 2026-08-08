"""Canonical, secret-free identity for one deployable MAAIS candidate."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

from maais.config.cloud import ServiceRole
from maais.config.constants import ALL_AGENTS
from maais.domain.json import JsonValue, MutableJsonValue, content_hash, to_json_data

_SCHEMA_VERSION = 1
_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "git_sha",
        "source_clean",
        "uv_lock_sha256",
        "dashboard_lock_sha256",
        "schema_revision",
        "agent_implementation_hashes",
        "dashboard_asset_manifest_sha256",
        "build_definition_sha256",
    }
)
_DOCUMENT_KEYS = _PAYLOAD_KEYS | {"descriptor_hash"}


@dataclass(frozen=True, slots=True)
class CandidateDescriptor:
    schema_version: int
    git_sha: str
    source_clean: bool
    uv_lock_sha256: str
    dashboard_lock_sha256: str
    schema_revision: str
    agent_implementation_hashes: Mapping[str, str]
    dashboard_asset_manifest_sha256: str
    build_definition_sha256: str
    descriptor_hash: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {_SCHEMA_VERSION}")
        _hex_hash("git_sha", self.git_sha, length=40)
        if type(self.source_clean) is not bool or not self.source_clean:
            raise ValueError("official candidate requires a clean source assertion")
        _hex_hash("uv_lock_sha256", self.uv_lock_sha256)
        _hex_hash("dashboard_lock_sha256", self.dashboard_lock_sha256)
        if (
            not isinstance(self.schema_revision, str)
            or len(self.schema_revision) != 4
            or not self.schema_revision.isascii()
            or not self.schema_revision.isdecimal()
        ):
            raise ValueError("schema_revision must be four ASCII decimal digits")
        if not isinstance(self.agent_implementation_hashes, Mapping):
            raise ValueError("agent_implementation_hashes must be an object")
        agent_hashes = dict(self.agent_implementation_hashes)
        if set(agent_hashes) != set(ALL_AGENTS):
            raise ValueError("candidate descriptor requires exact agent implementation hashes")
        for name, value in agent_hashes.items():
            _hex_hash(f"agent implementation hash for {name}", value)
        object.__setattr__(
            self,
            "agent_implementation_hashes",
            MappingProxyType(dict(sorted(agent_hashes.items()))),
        )
        _hex_hash(
            "dashboard_asset_manifest_sha256",
            self.dashboard_asset_manifest_sha256,
        )
        _hex_hash("build_definition_sha256", self.build_definition_sha256)
        _hex_hash("descriptor_hash", self.descriptor_hash)
        expected_hash = content_hash(_descriptor_payload(self))
        if self.descriptor_hash != expected_hash:
            raise ValueError("descriptor_hash does not match canonical candidate payload")

    @classmethod
    def build(
        cls,
        *,
        git_sha: str,
        source_clean: bool,
        uv_lock_sha256: str,
        dashboard_lock_sha256: str,
        schema_revision: str,
        agent_implementation_hashes: Mapping[str, str],
        dashboard_asset_manifest_sha256: str,
        build_definition_sha256: str,
    ) -> CandidateDescriptor:
        provisional = {
            "schema_version": _SCHEMA_VERSION,
            "git_sha": git_sha,
            "source_clean": source_clean,
            "uv_lock_sha256": uv_lock_sha256,
            "dashboard_lock_sha256": dashboard_lock_sha256,
            "schema_revision": schema_revision,
            "agent_implementation_hashes": dict(sorted(agent_implementation_hashes.items())),
            "dashboard_asset_manifest_sha256": dashboard_asset_manifest_sha256,
            "build_definition_sha256": build_definition_sha256,
        }
        return cls(
            schema_version=_SCHEMA_VERSION,
            git_sha=git_sha,
            source_clean=source_clean,
            uv_lock_sha256=uv_lock_sha256,
            dashboard_lock_sha256=dashboard_lock_sha256,
            schema_revision=schema_revision,
            agent_implementation_hashes=agent_implementation_hashes,
            dashboard_asset_manifest_sha256=dashboard_asset_manifest_sha256,
            build_definition_sha256=build_definition_sha256,
            descriptor_hash=content_hash(provisional),
        )

    @classmethod
    def from_path(cls, path: Path) -> CandidateDescriptor:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"candidate descriptor is not readable valid JSON: {path}") from exc
        return cls.from_json_data(raw)

    @classmethod
    def from_json_data(cls, raw: object) -> CandidateDescriptor:
        if not isinstance(raw, dict) or set(raw) != _DOCUMENT_KEYS:
            raise ValueError("candidate descriptor must contain exact keys")
        agent_hashes = raw["agent_implementation_hashes"]
        if not isinstance(agent_hashes, dict) or not all(
            isinstance(name, str) and isinstance(value, str) for name, value in agent_hashes.items()
        ):
            raise ValueError("agent_implementation_hashes must be a string mapping")
        scalar_string_keys = _DOCUMENT_KEYS - {
            "schema_version",
            "source_clean",
            "agent_implementation_hashes",
        }
        if not all(isinstance(raw[name], str) for name in scalar_string_keys):
            raise ValueError("candidate descriptor string fields must be strings")
        if type(raw["schema_version"]) is not int or type(raw["source_clean"]) is not bool:
            raise ValueError("candidate descriptor scalar types are invalid")
        return cls(
            schema_version=raw["schema_version"],
            git_sha=raw["git_sha"],
            source_clean=raw["source_clean"],
            uv_lock_sha256=raw["uv_lock_sha256"],
            dashboard_lock_sha256=raw["dashboard_lock_sha256"],
            schema_revision=raw["schema_revision"],
            agent_implementation_hashes=agent_hashes,
            dashboard_asset_manifest_sha256=raw["dashboard_asset_manifest_sha256"],
            build_definition_sha256=raw["build_definition_sha256"],
            descriptor_hash=raw["descriptor_hash"],
        )

    def to_json_data(self) -> dict[str, JsonValue]:
        return {**_descriptor_payload(self), "descriptor_hash": self.descriptor_hash}


@dataclass(frozen=True, slots=True)
class RailwayRuntimeIdentity:
    """Public Railway identity frozen once for the lifetime of one process boot."""

    project_id: str
    environment_id: str
    service_id: str
    deployment_id: str
    snapshot_id: str | None
    replica_id: str
    region: str
    service_role: ServiceRole
    boot_id: UUID
    candidate_hash: str
    started_at: datetime

    def __post_init__(self) -> None:
        for field in (
            "project_id",
            "environment_id",
            "service_id",
            "deployment_id",
            "replica_id",
            "region",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field} must be nonempty and trimmed")
        if self.snapshot_id is not None and (
            not self.snapshot_id or self.snapshot_id != self.snapshot_id.strip()
        ):
            raise ValueError("snapshot_id must be null or nonempty and trimmed")
        if not isinstance(self.service_role, ServiceRole):
            raise ValueError("service_role must be a ServiceRole")
        if not isinstance(self.boot_id, UUID) or self.boot_id.int == 0:
            raise ValueError("boot_id must be a non-nil UUID")
        _hex_hash("candidate_hash", self.candidate_hash)
        _require_utc(self.started_at, "started_at")

    def to_json_data(self) -> dict[str, MutableJsonValue]:
        normalized = to_json_data(
            {
                "project_id": self.project_id,
                "environment_id": self.environment_id,
                "service_id": self.service_id,
                "deployment_id": self.deployment_id,
                "snapshot_id": self.snapshot_id,
                "replica_id": self.replica_id,
                "region": self.region,
                "service_role": self.service_role,
                "boot_id": self.boot_id,
                "candidate_hash": self.candidate_hash,
                "started_at": self.started_at,
            }
        )
        if not isinstance(normalized, dict):
            raise TypeError("Railway runtime identity must serialize as an object")
        return normalized


def _descriptor_payload(descriptor: CandidateDescriptor) -> dict[str, JsonValue]:
    return {
        "schema_version": descriptor.schema_version,
        "git_sha": descriptor.git_sha,
        "source_clean": descriptor.source_clean,
        "uv_lock_sha256": descriptor.uv_lock_sha256,
        "dashboard_lock_sha256": descriptor.dashboard_lock_sha256,
        "schema_revision": descriptor.schema_revision,
        "agent_implementation_hashes": dict(sorted(descriptor.agent_implementation_hashes.items())),
        "dashboard_asset_manifest_sha256": descriptor.dashboard_asset_manifest_sha256,
        "build_definition_sha256": descriptor.build_definition_sha256,
    }


def _hex_hash(name: str, value: object, *, length: int = 64) -> None:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be {length} lowercase hexadecimal characters")


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"candidate descriptor contains duplicate key: {key}")
        result[key] = value
    return result
