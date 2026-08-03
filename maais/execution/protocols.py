from typing import Protocol

from maais.execution.schemas import OrderRequest


class AuthenticatedExecutionClient(Protocol):
    """Minimum signed venue interface used only for protocol smoke tests."""

    async def set_leverage(self, symbol: str, leverage: int) -> int: ...

    async def place_order(self, request: OrderRequest) -> dict[str, object]: ...

    async def get_order(self, symbol: str, order_id: str) -> dict[str, object]: ...

    async def get_funding_payments(
        self, symbol: str, limit: int = 50
    ) -> list[dict[str, object]]: ...
