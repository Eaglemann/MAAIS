"""Local operator entry points for preparing and running live paper experiments."""

from __future__ import annotations

import asyncio
import json
import secrets
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maais.config.constants import TRADING_PAIRS
from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.db.unit_of_work import UnitOfWork
from maais.domain.json import content_hash
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.prepare import (
    capture_repository_identity,
    prepare_live_paper_manifest,
)
from maais.market_data.connectors.binance_rest import BinanceRestConnector
from maais.market_data.connectors.binance_spot import BinanceSpotConnector
from maais.market_data.connectors.bybit_spot import BybitSpotConnector
from maais.market_data.public_runtime import PublicMarketDataRuntime
from maais.orchestration.bootstrap import restore_live_paper_runtime
from maais.orchestration.composition import assemble_live_paper_application
from maais.orchestration.supervisor import PaperWorkerSupervisor, PaperWorkerSupervisorState

StatusWriter = Callable[[dict[str, object]], None]


class PaperLiveConfigurationError(ValueError):
    """Expected operator/candidate mismatch that must refuse worker startup."""


async def prepare_live_manifest_file(
    *,
    repository_root: Path,
    output: Path,
    name: str,
    overwrite: bool = False,
    observed_now: Callable[[], datetime] | None = None,
) -> ExperimentManifest:
    if output.exists() and not overwrite:
        raise FileExistsError(f"manifest already exists: {output}")
    now = observed_now or (lambda: datetime.now(timezone.utc))
    repository = capture_repository_identity(repository_root)
    async with AsyncExitStack() as stack:
        futures = await stack.enter_async_context(BinanceRestConnector(observed_now=now))
        primary = await stack.enter_async_context(BinanceSpotConnector(observed_now=now))
        secondary = await stack.enter_async_context(BybitSpotConnector(observed_now=now))
        futures_preflight, primary_preflight, secondary_mappings = await asyncio.gather(
            futures.preflight(TRADING_PAIRS),
            primary.preflight(TRADING_PAIRS),
            secondary.preflight(TRADING_PAIRS),
        )
    manifest = prepare_live_paper_manifest(
        name=name,
        experiment_id=uuid4(),
        created_at=now(),
        repository=repository,
        exchange_filters=futures_preflight.exchange_filters,
        primary_mapping_hash=content_hash(
            [asdict(mapping) for mapping in primary_preflight.mappings]
        ),
        secondary_mapping_hash=content_hash([asdict(mapping) for mapping in secondary_mappings]),
    )
    _write_manifest(output, manifest, overwrite=overwrite)
    return manifest


async def run_live_paper_manifest(
    manifest: ExperimentManifest,
    *,
    settings: Settings,
    stop_event: asyncio.Event | None = None,
    status_writer: StatusWriter | None = None,
) -> None:
    if manifest.mode is not RunMode.PAPER_LIVE or settings.run_mode is not RunMode.PAPER_LIVE:
        raise PaperLiveConfigurationError(
            "paper-live command requires manifest and RUN_MODE=paper_live"
        )
    if settings.binance_demo_api_key_value or settings.binance_demo_api_secret_value:
        raise PaperLiveConfigurationError("paper-live refuses configured exchange credentials")
    writer = status_writer or _print_status
    engine = create_async_engine(
        settings.database_url_value,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )
    uow = UnitOfWork(async_sessionmaker(engine, expire_on_commit=False))
    try:
        async with uow.begin() as transaction:
            try:
                stored = await transaction.experiments.get_manifest(manifest.experiment_id)
            except LookupError:
                await transaction.experiments.create(manifest)
            else:
                if stored != manifest or stored.manifest_hash != manifest.manifest_hash:
                    raise PaperLiveConfigurationError(
                        "database experiment differs from the requested manifest"
                    )
        snapshot = await restore_live_paper_runtime(uow, manifest)
        async with AsyncExitStack() as stack:
            futures = await stack.enter_async_context(BinanceRestConnector())
            primary = await stack.enter_async_context(BinanceSpotConnector())
            secondary = await stack.enter_async_context(BybitSpotConnector())
            public_data = PublicMarketDataRuntime(
                manifest.symbols,
                futures_rest=futures,
                primary_spot=primary,
                secondary_spot=secondary,
                funding_start_at=manifest.created_at,
            )
            application = await assemble_live_paper_application(
                uow=uow,
                snapshot=snapshot,
                worker_id=uuid4(),
                futures_rest=futures,
                public_data=public_data,
                signing_key=secrets.token_bytes(32),
            )
            await application.supervisor.start()
            writer(
                {
                    "event": "paper_live_started",
                    "experiment_id": str(manifest.experiment_id),
                    "manifest_hash": manifest.manifest_hash,
                    "worker_state": application.supervisor.state,
                    "symbols": manifest.symbols,
                    "database_schema_revision": snapshot.database_schema_revision,
                    "live_money": False,
                }
            )
            local_stop = stop_event or asyncio.Event()
            remove_signals = _install_signal_handlers(local_stop) if stop_event is None else None
            try:
                await _wait_for_supervisor_end(application.supervisor, local_stop)
            finally:
                if remove_signals is not None:
                    remove_signals()
                if application.supervisor.state in {
                    PaperWorkerSupervisorState.RUNNING,
                    PaperWorkerSupervisorState.STANDBY,
                }:
                    await application.supervisor.stop()
            writer(
                {
                    "event": "paper_live_stopped",
                    "experiment_id": str(manifest.experiment_id),
                    "worker_state": application.supervisor.state,
                    "live_money": False,
                }
            )
    finally:
        await engine.dispose()


async def _wait_for_supervisor_end(
    supervisor: PaperWorkerSupervisor,
    local_stop: asyncio.Event,
) -> None:
    monitor = asyncio.create_task(
        supervisor.wait_closed(),
        name="paper_live_worker_monitor",
    )
    stopped = asyncio.create_task(local_stop.wait(), name="paper_live_stop_signal")
    operator_stop = asyncio.create_task(
        supervisor.operator_stop_requested.wait(),
        name="paper_live_operator_stop",
    )
    try:
        done, _ = await asyncio.wait(
            (monitor, stopped, operator_stop),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if monitor in done:
            await monitor
            return
        await supervisor.stop()
        await monitor
    finally:
        for task in (monitor, stopped, operator_stop):
            if not task.done():
                task.cancel()
        await asyncio.gather(monitor, stopped, operator_stop, return_exceptions=True)


def load_manifest_file(path: Path) -> ExperimentManifest:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("manifest file must contain a JSON object")
    return ExperimentManifest.from_dict(value)


def _write_manifest(
    path: Path,
    manifest: ExperimentManifest,
    *,
    overwrite: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    with path.open(mode, encoding="utf-8") as stream:
        json.dump(manifest.to_dict(), stream, indent=2, sort_keys=True)
        stream.write("\n")


def _install_signal_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for item in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(item, stop_event.set)
        except NotImplementedError:
            continue
        installed.append(item)

    def remove() -> None:
        for item in installed:
            loop.remove_signal_handler(item)

    return remove


def _print_status(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True, default=str), flush=True)
