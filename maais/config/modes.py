from enum import Enum


class RunMode(str, Enum):
    """Closed set of execution modes supported by the paper platform."""

    REPLAY = "replay"
    PAPER_LIVE = "paper_live"
    TESTNET_SMOKE = "testnet_smoke"

    @property
    def permits_authenticated_exchange(self) -> bool:
        """Only protocol smoke tests may use signed exchange endpoints."""

        return self is RunMode.TESTNET_SMOKE
