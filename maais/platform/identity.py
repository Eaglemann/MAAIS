"""Canonical, secret-free identity for one deployable MAAIS candidate."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from maais.config.constants import ALL_AGENTS
from maais.domain.json import JsonValue, content_hash

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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"candidate descriptor contains duplicate key: {key}")
        result[key] = value
    return result
