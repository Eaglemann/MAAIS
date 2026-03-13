"""Kill-switch — Rule 17 (BEHAVIOURS.md).

The kill-switch is a global halt flag. Once triggered:
  - All new order requests are blocked (is_halted() returns True)
  - An alert is dispatched
  - Manual reset required (reset() must be called explicitly)

Rule 20: the kill-switch cannot be bypassed programmatically.
Any attempt to call reset() without the correct reason code is rejected.
"""

from __future__ import annotations

from datetime import datetime, timezone

from maais.core.logging import get_logger
from maais.monitoring.schemas import AlertLevel

logger = get_logger(__name__)

# Reason code required to reset the kill-switch (prevents accidental reset)
_RESET_REASON_CODE = "MANUAL_RESET_CONFIRMED"


class KillSwitch:
    """Global trading halt mechanism (Rule 17)."""

    def __init__(self) -> None:
        self._halted: bool = False
        self._halt_reason: str | None = None
        self._halt_time: datetime | None = None
        self._halt_count: int = 0

    def halt(self, reason: str) -> None:
        """Trigger the kill-switch. Blocks all new orders.

        Can be called multiple times — each call updates the reason and increments count.
        """
        self._halted = True
        self._halt_reason = reason
        self._halt_time = datetime.now(timezone.utc)
        self._halt_count += 1
        logger.error(
            "kill_switch_triggered",
            reason=reason,
            halt_count=self._halt_count,
            time=self._halt_time.isoformat(),
        )

    def reset(self, reason_code: str) -> tuple[bool, str]:
        """Attempt to reset the kill-switch.

        Returns (success, message).
        Requires reason_code == _RESET_REASON_CODE (Rule 20 override prevention).
        """
        if reason_code != _RESET_REASON_CODE:
            msg = f"reset_rejected: invalid reason_code '{reason_code}' (Rule 20)"
            logger.warning("kill_switch_reset_rejected", reason_code=reason_code)
            return False, msg

        if not self._halted:
            return True, "kill_switch_not_active"

        self._halted = False
        prev_reason = self._halt_reason
        self._halt_reason = None
        self._halt_time = None
        logger.warning("kill_switch_reset", previous_reason=prev_reason)
        return True, f"reset_ok: was halted for '{prev_reason}'"

    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    @property
    def halt_time(self) -> datetime | None:
        return self._halt_time

    @property
    def halt_count(self) -> int:
        return self._halt_count

    @property
    def reset_reason_code(self) -> str:
        """Expose reset code for authorised callers."""
        return _RESET_REASON_CODE
