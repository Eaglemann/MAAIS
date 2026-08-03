"""Authenticated Binance Demo/Testnet protocol adapter.

This package is not used by the local paper broker and cannot target production.
"""

from maais.execution.testnet.binance_client import (
    BinanceDemoFuturesClient,
    build_authenticated_execution_client,
)

__all__ = ["BinanceDemoFuturesClient", "build_authenticated_execution_client"]
