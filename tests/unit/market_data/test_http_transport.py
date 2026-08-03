import httpx
import pytest
from structlog.testing import capture_logs

from maais.market_data.connectors.http_transport import get_with_transport_retry


async def test_transport_retry_recovers_with_bounded_backoff_and_structured_evidence() -> None:
    requests = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.RemoteProtocolError("server ended HTTP/2 connection", request=request)
        return httpx.Response(200, json={"ok": True})

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(
        base_url="https://public.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        with capture_logs() as logs:
            response = await get_with_transport_retry(
                client,
                "/reference",
                {"symbol": "BTCUSDT"},
                component="secondary_reference",
                sleep=record_sleep,
            )

    assert response.json() == {"ok": True}
    assert requests == 2
    assert sleeps == [0.25]
    assert logs == [
        {
            "attempt": 1,
            "component": "secondary_reference",
            "error": "server ended HTTP/2 connection",
            "error_type": "RemoteProtocolError",
            "event": "public_rest_transport_retry",
            "log_level": "warning",
            "max_attempts": 3,
            "path": "/reference",
            "retry_in_seconds": 0.25,
        }
    ]


async def test_transport_retry_exhaustion_is_visible_and_reraises_original_error() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(
        base_url="https://public.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        with capture_logs() as logs:
            with pytest.raises(httpx.ReadError, match="connection reset"):
                await get_with_transport_retry(
                    client,
                    "/reference",
                    {},
                    component="secondary_reference",
                    sleep=record_sleep,
                )

    assert sleeps == [0.25, 0.5]
    assert [item["event"] for item in logs] == [
        "public_rest_transport_retry",
        "public_rest_transport_retry",
        "public_rest_transport_exhausted",
    ]
    assert logs[-1]["attempt"] == 3
    assert logs[-1]["log_level"] == "error"


async def test_transport_retry_does_not_retry_nontransport_failures() -> None:
    requests = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise RuntimeError("invalid response contract")

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(
        base_url="https://public.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RuntimeError, match="invalid response contract"):
            await get_with_transport_retry(
                client,
                "/reference",
                {},
                component="secondary_reference",
                sleep=record_sleep,
            )

    assert requests == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("max_attempts", "initial_backoff_seconds"),
    ((0, 0.25), (3, -0.1)),
)
async def test_transport_retry_rejects_invalid_policy(
    max_attempts: int,
    initial_backoff_seconds: float,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    async with httpx.AsyncClient(base_url="https://public.example") as client:
        with pytest.raises(ValueError, match="attempts"):
            await get_with_transport_retry(
                client,
                "/reference",
                {},
                component="secondary_reference",
                sleep=no_sleep,
                max_attempts=max_attempts,
                initial_backoff_seconds=initial_backoff_seconds,
            )
