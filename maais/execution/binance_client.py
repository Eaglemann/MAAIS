"""Async Binance USDT-Perpetual Futures REST client.

Covers: order placement (market/limit/stop-limit), cancellation,
order status query, leverage setting, and funding payment history.

Authentication: HMAC-SHA256 signature on all signed endpoints.
Base URL: https://fapi.binance.com
"""

from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from maais.config.settings import get_settings
from maais.core.logging import get_logger
from maais.execution.schemas import FillRecord, OrderRequest, OrderResult, OrderSide, OrderStatus, OrderType

logger = get_logger(__name__)

_BASE_URL = "https://fapi.binance.com"


class BinanceFuturesClient:
    """Async Binance Futures REST client. Use as async context manager."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.binance_api_key
        self._api_secret = settings.binance_api_secret
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "BinanceFuturesClient":
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"X-MBX-APIKEY": self._api_key},
            timeout=10.0,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        sig = hmac.new(
            self._api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = sig
        return params

    async def _get(self, path: str, params: dict | None = None, signed: bool = False) -> dict:
        p = self._sign(params or {}) if signed else (params or {})
        resp = await self._client.get(path, params=p)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, params: dict) -> dict:
        signed = self._sign(params)
        resp = await self._client.post(path, params=signed)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str, params: dict) -> dict:
        signed = self._sign(params)
        resp = await self._client.delete(path, params=signed)
        resp.raise_for_status()
        return resp.json()

    # ── Public API ────────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> int:
        """Set leverage for a symbol. Returns the confirmed leverage."""
        data = await self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
        confirmed = int(data["leverage"])
        logger.info("leverage_confirmed", symbol=symbol, leverage=confirmed)
        return confirmed

    async def place_order(self, request: OrderRequest) -> dict:
        """Place an order. Returns raw Binance response dict."""
        params: dict = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type.value,
            "quantity": str(request.quantity),
        }
        if request.reduce_only:
            params["reduceOnly"] = "true"
        if request.order_type == OrderType.LIMIT:
            if request.price is None:
                raise ValueError("LIMIT order requires a price")
            params["price"] = str(request.price)
            params["timeInForce"] = "GTC"
        if request.order_type in (OrderType.STOP, OrderType.STOP_MARKET):
            if request.stop_price is None:
                raise ValueError("STOP order requires a stop_price")
            params["stopPrice"] = str(request.stop_price)
            if request.price is not None:
                params["price"] = str(request.price)
                params["type"] = "STOP"
            else:
                params["type"] = "STOP_MARKET"

        logger.info("placing_order", symbol=request.symbol, side=request.side.value,
                    type=request.order_type.value, qty=str(request.quantity))
        return await self._post("/fapi/v1/order", params)

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        """Cancel an open order."""
        return await self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    async def get_order(self, symbol: str, order_id: str) -> dict:
        """Query order status."""
        return await self._get("/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    async def get_funding_payments(self, symbol: str, limit: int = 50) -> list[dict]:
        """Fetch recent funding payment history for a symbol."""
        return await self._get(
            "/fapi/v1/income",
            {"symbol": symbol, "incomeType": "FUNDING_FEE", "limit": limit},
            signed=True,
        )

    async def get_account(self) -> dict:
        """Fetch account info (balance, positions)."""
        return await self._get("/fapi/v2/account", signed=True)
