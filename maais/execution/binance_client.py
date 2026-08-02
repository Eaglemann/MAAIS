"""Compatibility import for the isolated Binance Demo/Testnet adapter.

New code should import :class:`BinanceDemoFuturesClient` from
``maais.execution.testnet.binance_client``. The legacy name remains temporarily
so the pre-paper execution components can be migrated without a flag day.
"""

from maais.execution.testnet.binance_client import BinanceDemoFuturesClient

BinanceFuturesClient = BinanceDemoFuturesClient

__all__ = ["BinanceFuturesClient"]
