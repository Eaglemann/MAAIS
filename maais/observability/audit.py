"""Immutable, hash-bound operational audit and health evidence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID, uuid5

from maais.domain.json import (
    JsonValue,
    MutableJsonValue,
    canonical_json_bytes,
    content_hash,
    freeze_json,
    to_json_data,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}:[0-9a-f]{32}$")
_EVENT_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_CHECK_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_MAX_EVIDENCE_BYTES = 65_536
_MAX_COMPONENT_BYTES = 131_072
_AUDIT_EVENT_NAMESPACE = UUID("b778489d-47ab-4bc9-9c26-f488c8fa1dc9")
_FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "access_key",
        "account_equity",
        "authorization",
        "balance",
        "body",
        "client_secret",
        "cookie",
        "credentials",
        "csrf",
        "database_url",
        "headers",
        "local_storage",
        "order_quantity",
        "password",
        "position",
        "positions",
        "private_key",
        "quantity",
        "raw_response",
        "request_body",
        "response_body",
        "secret",
        "session_storage",
        "session_token",
        "token",
    }
)


class AuditSourceRole(StrEnum):
    WEB = "web"
    WORKER = "worker"
    OPERATIONS = "operations"
    MIGRATOR = "migrator"
    VERIFIER = "verifier"


AUDIT_EVENT_CODES_BY_SOURCE: Mapping[AuditSourceRole, frozenset[str]] = MappingProxyType(
    {
        AuditSourceRole.WEB: frozenset(
            {
                "auth.csrf.rejected",
                "auth.login.locked",
                "auth.login.rejected",
                "auth.login.succeeded",
                "auth.logout",
                "auth.session.expired",
                "auth.session.revoked",
                "operator.command.enqueued",
                "service.booted",
                "service.stopped",
            }
        ),
        AuditSourceRole.WORKER: frozenset(
            {
                "operator.command.accepted",
                "operator.command.completed",
                "operator.command.rejected",
                "run.completed",
                "run.invalidated",
                "run.started",
                "service.booted",
                "service.stopped",
            }
        ),
        AuditSourceRole.OPERATIONS: frozenset(
            {
                "artifact.publication_failed",
                "artifact.published",
                "backup.failed",
                "backup.succeeded",
                "daily_close.failed",
                "daily_close.succeeded",
                "health.evaluated",
                "readiness.verdict",
                "restore.failed",
                "restore.succeeded",
                "service.booted",
                "service.stopped",
            }
        ),
        AuditSourceRole.MIGRATOR: frozenset(
            {
                "migration.completed",
                "migration.started",
                "service.booted",
                "service.stopped",
            }
        ),
        AuditSourceRole.VERIFIER: frozenset(
            {
                "service.booted",
                "service.stopped",
            }
        ),
    }
)


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"


class HealthSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: UUID
    sequence: int
    previous_hash: str | None
    source_role: AuditSourceRole
    actor_reference: str
    session_reference: str | None
    event_code: str
    reason_code: str | None
    evidence: Mapping[str, JsonValue]
    run_id: UUID | None
    service_boot_id: UUID | None
    occurred_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        _require_uuid(self.event_id, "audit event")
        if self.sequence < 1:
            raise ValueError("audit sequence must be positive")
        if (self.sequence == 1) != (self.previous_hash is None):
            raise ValueError("only the genesis audit event can omit previous_hash")
        if self.previous_hash is not None:
            _require_sha256(self.previous_hash, "audit previous_hash")
        if self.event_code not in AUDIT_EVENT_CODES_BY_SOURCE[self.source_role]:
            raise ValueError("audit event code is not approved for its source role")
        _require_reference(self.actor_reference, "audit actor_reference")
        if self.session_reference is not None:
            _require_reference(self.session_reference, "audit session_reference")
            if not self.session_reference.startswith("session:"):
                raise ValueError("audit session_reference must use the session namespace")
        _require_code(self.event_code, "audit event_code", _EVENT_CODE_PATTERN)
        if self.reason_code is not None:
            _require_code(self.reason_code, "audit reason_code", _REASON_CODE_PATTERN)
        _require_utc(self.occurred_at, "audit occurred_at")
        if self.run_id is not None:
            _require_uuid(self.run_id, "audit run")
        if self.service_boot_id is not None:
            _require_uuid(self.service_boot_id, "audit service boot")
        _require_sha256(self.content_hash, "audit content_hash")
        if content_hash(_audit_hash_payload(self)) != self.content_hash:
            raise ValueError("audit content_hash does not match immutable event state")

    @classmethod
    def create(
        cls,
        *,
        event_id: UUID,
        sequence: int,
        previous_hash: str | None,
        source_role: AuditSourceRole,
        actor_reference: str,
        session_reference: str | None,
        event_code: str,
        reason_code: str | None,
        evidence: Mapping[str, object],
        run_id: UUID | None,
        service_boot_id: UUID | None,
        occurred_at: datetime,
    ) -> AuditEvent:
        frozen_evidence = _evidence_object(evidence, maximum_bytes=_MAX_EVIDENCE_BYTES)
        payload = {
            "actor_reference": actor_reference,
            "event_code": event_code,
            "event_id": str(event_id),
            "evidence": frozen_evidence,
            "occurred_at": occurred_at,
            "previous_hash": previous_hash,
            "reason_code": reason_code,
            "run_id": run_id,
            "sequence": sequence,
            "service_boot_id": service_boot_id,
            "session_reference": session_reference,
            "source_role": source_role.value,
        }
        return cls(
            event_id=event_id,
            sequence=sequence,
            previous_hash=previous_hash,
            source_role=source_role,
            actor_reference=actor_reference,
            session_reference=session_reference,
            event_code=event_code,
            reason_code=reason_code,
            evidence=frozen_evidence,
            run_id=run_id,
            service_boot_id=service_boot_id,
            occurred_at=occurred_at,
            content_hash=content_hash(payload),
        )

    def to_json_data(self) -> dict[str, object]:
        return {
            **_mutable_object(_audit_hash_payload(self)),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class AuditChainVerification:
    ok: bool
    event_count: int
    terminal_hash: str | None
    errors: tuple[str, ...]


def verify_audit_chain(events: Iterable[AuditEvent]) -> AuditChainVerification:
    ordered = tuple(events)
    errors: list[str] = []
    previous_hash: str | None = None
    expected_sequence = 1
    for event in ordered:
        if event.sequence != expected_sequence:
            errors.append(
                f"audit_sequence_gap:expected={expected_sequence}:actual={event.sequence}"
            )
        if event.previous_hash != previous_hash:
            errors.append(f"audit_previous_hash_mismatch:sequence={event.sequence}")
        if content_hash(_audit_hash_payload(event)) != event.content_hash:
            errors.append(f"audit_content_hash_mismatch:sequence={event.sequence}")
        previous_hash = event.content_hash
        expected_sequence = event.sequence + 1
    return AuditChainVerification(
        ok=not errors,
        event_count=len(ordered),
        terminal_hash=ordered[-1].content_hash if ordered else None,
        errors=tuple(errors),
    )


@dataclass(frozen=True, slots=True)
class HealthEvaluation:
    evaluation_id: UUID
    run_id: UUID
    service_boot_id: UUID
    overall_status: HealthStatus
    failed_check_names: tuple[str, ...]
    severity: HealthSeverity
    deduplication_key: str
    incident_id: UUID | None
    recovery_of_evaluation_id: UUID | None
    recovered_at: datetime | None
    components: Mapping[str, JsonValue]
    checked_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        _require_uuid(self.evaluation_id, "health evaluation")
        _require_uuid(self.run_id, "health run")
        _require_uuid(self.service_boot_id, "health service boot")
        if not self.components:
            raise ValueError("health components cannot be empty")
        if self.failed_check_names != tuple(sorted(set(self.failed_check_names))):
            raise ValueError("health failed checks must be sorted and unique")
        for name in self.failed_check_names:
            _require_code(name, "health failed check", _CHECK_NAME_PATTERN)
        if self.overall_status is HealthStatus.HEALTHY:
            if self.failed_check_names:
                raise ValueError("healthy evaluation cannot contain failed checks")
            if self.severity is not HealthSeverity.INFO:
                raise ValueError("healthy evaluation severity must be info")
        else:
            if not self.failed_check_names:
                raise ValueError("unhealthy evaluation requires failed checks")
            if self.severity.value != self.overall_status.value:
                raise ValueError("health severity must match unhealthy status")
        _require_sha256(self.deduplication_key, "health deduplication_key")
        if self.incident_id is not None:
            _require_uuid(self.incident_id, "health incident")
        if self.recovery_of_evaluation_id is None:
            if self.recovered_at is not None:
                raise ValueError("health recovered_at requires a recovery reference")
        else:
            _require_uuid(self.recovery_of_evaluation_id, "health recovery evaluation")
            if self.recovery_of_evaluation_id == self.evaluation_id:
                raise ValueError("health recovery cannot reference itself")
            if self.overall_status is not HealthStatus.HEALTHY or self.recovered_at is None:
                raise ValueError("health recovery must be a healthy terminal snapshot")
            if self.recovered_at != self.checked_at:
                raise ValueError("health recovery time must equal checked_at")
        if self.recovered_at is not None:
            _require_utc(self.recovered_at, "health recovered_at")
        _require_utc(self.checked_at, "health checked_at")
        _require_sha256(self.content_hash, "health content_hash")
        if content_hash(_health_hash_payload(self)) != self.content_hash:
            raise ValueError("health content_hash does not match immutable evaluation")

    @classmethod
    def create(
        cls,
        *,
        evaluation_id: UUID,
        run_id: UUID,
        service_boot_id: UUID,
        overall_status: HealthStatus,
        failed_check_names: Iterable[str],
        severity: HealthSeverity,
        deduplication_key: str,
        incident_id: UUID | None,
        recovery_of_evaluation_id: UUID | None,
        recovered_at: datetime | None,
        components: Mapping[str, object],
        checked_at: datetime,
    ) -> HealthEvaluation:
        normalized_checks = tuple(sorted(set(failed_check_names)))
        frozen_components = _evidence_object(
            components,
            maximum_bytes=_MAX_COMPONENT_BYTES,
        )
        payload = {
            "checked_at": checked_at,
            "components": frozen_components,
            "deduplication_key": deduplication_key,
            "evaluation_id": str(evaluation_id),
            "failed_check_names": normalized_checks,
            "incident_id": incident_id,
            "overall_status": overall_status.value,
            "recovered_at": recovered_at,
            "recovery_of_evaluation_id": recovery_of_evaluation_id,
            "run_id": str(run_id),
            "service_boot_id": str(service_boot_id),
            "severity": severity.value,
        }
        return cls(
            evaluation_id=evaluation_id,
            run_id=run_id,
            service_boot_id=service_boot_id,
            overall_status=overall_status,
            failed_check_names=normalized_checks,
            severity=severity,
            deduplication_key=deduplication_key,
            incident_id=incident_id,
            recovery_of_evaluation_id=recovery_of_evaluation_id,
            recovered_at=recovered_at,
            components=frozen_components,
            checked_at=checked_at,
            content_hash=content_hash(payload),
        )

    def to_json_data(self) -> dict[str, object]:
        return {
            **_mutable_object(_health_hash_payload(self)),
            "content_hash": self.content_hash,
        }


def pseudonymous_reference(namespace: str, value: object) -> str:
    if _NAMESPACE_PATTERN.fullmatch(namespace) is None:
        raise ValueError("pseudonymous reference namespace is invalid")
    rendered = str(value)
    if not rendered or len(rendered) > 1_024:
        raise ValueError("pseudonymous reference value must be 1-1024 characters")
    digest = hashlib.sha256(f"{namespace}\x00{rendered}".encode()).hexdigest()[:32]
    return f"{namespace}:{digest}"


def deterministic_audit_event_id(event_code: str, identity: object) -> UUID:
    _require_code(event_code, "audit event_code", _EVENT_CODE_PATTERN)
    rendered = str(identity)
    if not rendered or len(rendered) > 2_048:
        raise ValueError("audit event identity must be 1-2048 characters")
    return uuid5(_AUDIT_EVENT_NAMESPACE, f"{event_code}\x00{rendered}")


def bounded_reason_code(value: str, *, fallback: str) -> str:
    """Retain declared machine codes while keeping free-form text out of audit fields."""

    _require_code(fallback, "audit fallback reason_code", _REASON_CODE_PATTERN)
    if isinstance(value, str) and len(value) <= 128 and _REASON_CODE_PATTERN.fullmatch(value):
        return value
    return fallback


def health_deduplication_key(run_id: UUID, failed_check_names: Iterable[str]) -> str:
    _require_uuid(run_id, "health run")
    names = tuple(sorted(set(failed_check_names)))
    for name in names:
        _require_code(name, "health failed check", _CHECK_NAME_PATTERN)
    return content_hash({"failed_check_names": names, "run_id": str(run_id)})


def _audit_hash_payload(event: AuditEvent) -> dict[str, object]:
    return {
        "actor_reference": event.actor_reference,
        "event_code": event.event_code,
        "event_id": str(event.event_id),
        "evidence": event.evidence,
        "occurred_at": event.occurred_at,
        "previous_hash": event.previous_hash,
        "reason_code": event.reason_code,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "service_boot_id": event.service_boot_id,
        "session_reference": event.session_reference,
        "source_role": event.source_role.value,
    }


def _health_hash_payload(evaluation: HealthEvaluation) -> dict[str, object]:
    return {
        "checked_at": evaluation.checked_at,
        "components": evaluation.components,
        "deduplication_key": evaluation.deduplication_key,
        "evaluation_id": str(evaluation.evaluation_id),
        "failed_check_names": evaluation.failed_check_names,
        "incident_id": evaluation.incident_id,
        "overall_status": evaluation.overall_status.value,
        "recovered_at": evaluation.recovered_at,
        "recovery_of_evaluation_id": evaluation.recovery_of_evaluation_id,
        "run_id": str(evaluation.run_id),
        "service_boot_id": str(evaluation.service_boot_id),
        "severity": evaluation.severity.value,
    }


def _evidence_object(
    value: Mapping[str, object],
    *,
    maximum_bytes: int,
) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - Mapping input invariant
        raise TypeError("operational evidence must be a JSON object")
    _reject_sensitive_keys(frozen)
    if len(canonical_json_bytes(frozen)) > maximum_bytes:
        raise ValueError("operational evidence exceeds its size limit")
    return frozen


def _reject_sensitive_keys(value: JsonValue, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            padded = f"_{normalized}_"
            if any(f"_{marker}_" in padded for marker in _FORBIDDEN_EVIDENCE_KEYS):
                location = ".".join((*path, key))
                raise ValueError(f"operational evidence contains forbidden key: {location}")
            _reject_sensitive_keys(child, path=(*path, key))
    elif isinstance(value, tuple):
        for child in value:
            _reject_sensitive_keys(child, path=path)


def _require_reference(value: str, name: str) -> None:
    if _REFERENCE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded pseudonymous reference")


def _mutable_object(value: object) -> dict[str, MutableJsonValue]:
    normalized = to_json_data(value)
    if not isinstance(normalized, dict):  # pragma: no cover - object call contract
        raise TypeError("operational evidence must serialize as an object")
    return normalized


def _require_code(value: str, name: str, pattern: re.Pattern[str]) -> None:
    if len(value) > 128 or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _require_sha256(value: str, name: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")


def _require_uuid(value: UUID, name: str) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{name} identifier is invalid")


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.astimezone(timezone.utc) != value:
        raise ValueError(f"{name} must be normalized to UTC")
