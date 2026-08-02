from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from maais.domain.enums import PaperOrderSide
from maais.domain.json import canonical_json_bytes
from maais.execution.paper.clock import require_utc


@dataclass(frozen=True, slots=True)
class AuthorizationClaims:
    experiment_id: UUID
    decision_cycle_id: UUID
    proposal_id: UUID
    gate_chain_hash: str
    symbol: str
    side: PaperOrderSide
    quantity: Decimal
    approved_notional: Decimal
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if len(self.gate_chain_hash) != 64:
            raise ValueError("gate_chain_hash must be a SHA-256 hex digest")
        if not self.symbol:
            raise ValueError("symbol is required")
        for value, field in (
            (self.quantity, "quantity"),
            (self.approved_notional, "approved_notional"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{field} must be a positive finite Decimal")
        require_utc(self.issued_at, "issued_at")
        require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")

    def signing_payload(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "decision_cycle_id": self.decision_cycle_id,
            "proposal_id": self.proposal_id,
            "gate_chain_hash": self.gate_chain_hash,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "approved_notional": self.approved_notional,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class ExecutionCapability:
    claims: AuthorizationClaims
    signature: str


class ExecutionAuthorizer:
    """Issues process-local capabilities bound to an exact approved proposal."""

    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("signing key must contain at least 32 bytes")
        self._signing_key = signing_key

    def _signature(self, claims: AuthorizationClaims) -> str:
        return hmac.new(
            self._signing_key,
            canonical_json_bytes(claims.signing_payload()),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        claims: AuthorizationClaims,
        *,
        all_gates_passed: bool,
    ) -> ExecutionCapability:
        if not all_gates_passed:
            raise PermissionError("execution capability requires a passed gate chain")
        return ExecutionCapability(claims, self._signature(claims))

    def verify(self, capability: ExecutionCapability, *, at: datetime) -> bool:
        require_utc(at, "verification time")
        if at > capability.claims.expires_at or at < capability.claims.issued_at:
            return False
        expected = self._signature(capability.claims)
        return hmac.compare_digest(expected, capability.signature)
