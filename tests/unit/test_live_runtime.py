import asyncio

from maais.live import _wait_for_supervisor_end


class _Supervisor:
    def __init__(self) -> None:
        self.operator_stop_requested = asyncio.Event()
        self.closed = asyncio.Event()
        self.stop_calls = 0

    async def wait_closed(self) -> None:
        await self.closed.wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.closed.set()


async def test_audited_stop_command_ends_the_local_worker_process() -> None:
    supervisor = _Supervisor()
    os_stop = asyncio.Event()
    supervisor.operator_stop_requested.set()

    await _wait_for_supervisor_end(supervisor, os_stop)  # type: ignore[arg-type]

    assert supervisor.stop_calls == 1
