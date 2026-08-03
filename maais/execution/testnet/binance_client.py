"""Signed Binance Demo Futures client for isolated protocol smoke tests."""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.core.logging import get_logger
from maais.execution.schemas import OrderRequest, OrderType

logger = get_logger(__name__)

DEMO_FUTURES_BASE_URL = "https://demo-fapi.binance.com"
QueryValue = str | int | float


class BinanceDemoFuturesClient:
    """Async client pinned to Binance Demo Futures.

    Credentials are explicit constructor inputs so merely importing this module
    cannot load or use an exchange account.
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise ValueError("both Binance Demo credentials are required")
        self._api_key = api_key
        self._api_secret = api_secret
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> BinanceDemoFuturesClient:
        self._client = httpx.AsyncClient(
            base_url=DEMO_FUTURES_BASE_URL,
            headers={"X-MBX-APIKEY": self._api_key},
            timeout=10.0,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("BinanceDemoFuturesClient must be used as an async context manager")
        return self._client

    def _sign(self, params: dict[str, QueryValue]) -> dict[str, QueryValue]:
        signed = dict(params)
        signed["timestamp"] = int(time.time() * 1000)
        query = urlencode(signed)
        signed["signature"] = hmac.new(
            self._api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        return signed

    async def _get(
        self,
        path: str,
        params: dict[str, QueryValue] | None = None,
        *,
        signed: bool = False,
    ) -> dict[str, object]:
        request_params = self._sign(params or {}) if signed else (params or {})
        response = await self._http().get(path, params=request_params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("expected object response from Binance Demo Futures")
        return data

    async def _post(self, path: str, params: dict[str, QueryValue]) -> dict[str, object]:
        response = await self._http().post(path, params=self._sign(params))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("expected object response from Binance Demo Futures")
        return data

    async def _delete(self, path: str, params: dict[str, QueryValue]) -> dict[str, object]:
        response = await self._http().delete(path, params=self._sign(params))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("expected object response from Binance Demo Futures")
        return data

    async def set_leverage(self, symbol: str, leverage: int) -> int:
        data = await self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
        confirmed = int(str(data["leverage"]))
        logger.info("demo_leverage_confirmed", symbol=symbol, leverage=confirmed)
        return confirmed

    async def place_order(self, request: OrderRequest) -> dict[str, object]:
        params: dict[str, QueryValue] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type.value,
            "quantity": str(request.quantity),
        }
        if request.reduce_only:
            params["reduceOnly"] = "true"
        if request.order_type is OrderType.LIMIT:
            if request.price is None:
                raise ValueError("LIMIT order requires a price")
            params.update(price=str(request.price), timeInForce="GTC")
        if request.order_type in (OrderType.STOP, OrderType.STOP_MARKET):
            if request.stop_price is None:
                raise ValueError("STOP order requires a stop_price")
            params["stopPrice"] = str(request.stop_price)
            if request.price is not None:
                params.update(price=str(request.price), type="STOP")
            else:
                params["type"] = "STOP_MARKET"

        logger.info(
            "placing_demo_order",
            symbol=request.symbol,
            side=request.side.value,
            type=request.order_type.value,
            qty=str(request.quantity),
        )
        return await self._post("/fapi/v1/order", params)

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, object]:
        return await self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    async def get_order(self, symbol: str, order_id: str) -> dict[str, object]:
        return await self._get(
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )

    async def get_funding_payments(self, symbol: str, limit: int = 50) -> list[dict[str, object]]:
        response = await self._http().get(
            "/fapi/v1/income",
            params=self._sign({"symbol": symbol, "incomeType": "FUNDING_FEE", "limit": limit}),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise TypeError("expected list response from Binance Demo Futures")
        return data

    async def get_account(self) -> dict[str, object]:
        return await self._get("/fapi/v2/account", signed=True)


def build_authenticated_execution_client(settings: Settings) -> BinanceDemoFuturesClient:
    """Construct signed execution only in explicit Testnet-smoke mode."""

    if settings.run_mode is not RunMode.TESTNET_SMOKE:
        raise ValueError("authenticated execution is limited to testnet_smoke")
    if not settings.binance_demo_api_key or not settings.binance_demo_api_secret:
        raise ValueError("both Binance Demo credentials are required")
    return BinanceDemoFuturesClient(
        api_key=settings.binance_demo_api_key,
        api_secret=settings.binance_demo_api_secret,
    )
