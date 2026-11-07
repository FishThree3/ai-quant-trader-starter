import asyncio
import signal
from typing import Any

import structlog
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from core.broker.base import BrokerStatus
from core.broker.ibkr.broker import IBKRBroker
from core.broker.ibkr.connection import IBKRConnectionConfig
from core.controls.kill_switch import KillSwitch
from core.controls.redis_plane import RedisControlPlane
from core.events.bus import EventBus
from core.execution.order_manager import OrderManager
from core.market.redis_store import RedisQuoteStore
from core.market.resolver import ContractResolutionError, ContractResolver
from core.market.service import MarketDataService
from core.reconciliation.models import ReconciliationResult
from database.broker_state import record_state_event
from database.order_repository import SqlAlchemyOrderRepository
from database.reconciliation_store import load_local_snapshot, persist_broker_snapshot
from database.session import SessionLocal
from database.system_control import SystemControlStore

log = structlog.get_logger()


def _event_metadata(status: BrokerStatus, result: ReconciliationResult | None) -> dict[str, Any]:
    return {
        "next_order_id": status.next_order_id,
        "account_present": status.account is not None,
        "trading_mode": settings.trading_mode.value,
        "read_only": settings.ibkr_read_only,
        # No strategy/account risk context is wired into this worker yet.
        "submission_enabled": False,
        "reconciliation": result.summary() if result is not None else None,
    }


async def publish_state(
    broker: IBKRBroker, event_type: str, result: ReconciliationResult | None = None
) -> None:
    """Record a transition for readers such as the status API.

    Failure to write is logged but never upgrades broker state: the reconciliation
    gate is what protects trading, not this audit trail.
    """
    status = await broker.status()
    try:
        async with SessionLocal() as session:
            await record_state_event(
                session,
                event_type=event_type,
                state=status.state.value,
                message=status.last_error,
                metadata=_event_metadata(status, result),
            )
    except SQLAlchemyError as exc:
        log.error("state_event_persist_failed", event_type=event_type, error=str(exc))


async def reconcile_broker_state(broker: IBKRBroker) -> None:
    """SYNCING -> reconcile -> READY.

    Local state is persisted before the comparison so that fills the broker already
    reported for known orders repair themselves, while anything unattributable stays
    visible as a blocking finding.
    """
    snapshot = await broker.collect_snapshot()

    try:
        async with SessionLocal() as session:
            await persist_broker_snapshot(session, snapshot)
            local = await load_local_snapshot(session)
    except SQLAlchemyError as exc:
        broker.degrade("local state unavailable; refusing to trade")
        log.error("reconciliation_skipped", reason="local_state_unavailable", error=str(exc))
        await publish_state(broker, "reconciliation.skipped")
        return

    result = broker.apply_reconciliation(snapshot, local)
    status = await broker.status()
    log.info(
        "reconciliation",
        state=status.state.value,
        barrier_complete=snapshot.is_complete,
        missing_barriers=snapshot.missing_barriers,
        broker_rows=(
            len(snapshot.orders),
            len(snapshot.positions),
            len(snapshot.executions),
        ),
        **result.summary(),
    )
    await publish_state(broker, "reconciliation.completed", result)


async def start_market_data(service: MarketDataService) -> None:
    """Open lines for the configured watchlist; unconfigured means no subscriptions."""
    symbols = settings.watched_symbols
    if not symbols:
        log.info("market_data_idle", reason="MARKET_DATA_SYMBOLS is empty")
        return

    resolver = ContractResolver()
    for symbol in symbols:
        try:
            instrument = resolver.resolve(symbol)
        except ContractResolutionError as exc:
            log.error("market_data_symbol_rejected", symbol=symbol, error=str(exc))
            continue

        subscribed = await service.subscribe(instrument, "worker")
        log.info(
            "market_data_watch",
            symbol=instrument.symbol,
            subscribed=subscribed,
            ticker_limit_reached=service.ticker_limit_reached,
        )


async def publish_heartbeat(broker: IBKRBroker, stop: asyncio.Event) -> None:
    """Persist liveness and changes even when no orders or quotes arrive."""
    while not stop.is_set():
        await publish_state(broker, "broker.heartbeat", broker.connection.last_reconciliation)
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=max(1, settings.ibkr_stale_data_seconds / 3)
            )
        except TimeoutError:
            pass


async def guard_trading_gate(gate: KillSwitch, stop: asyncio.Event) -> None:
    """Re-read the gate on a timer, because an unread gate is an unsafe gate."""
    interval = max(2.0, settings.ibkr_stale_data_seconds / 3)
    while not stop.is_set():
        state = await gate.refresh()
        log.info(
            "trading_gate_checked",
            state=state.state.value,
            new_orders_allowed=gate.allows_new_orders(),
            reason=state.reason,
            actor=state.actor,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


async def watch_control_signals(plane: RedisControlPlane, gate: KillSwitch) -> None:
    """Accelerate on a recorded change; the timer above is what guarantees safety."""
    try:
        async for signal in plane.stream():
            state = await gate.refresh()
            log.info("trading_gate_refreshed_by_signal", signal=signal, state=state.state.value)
    except Exception as exc:
        log.warning("control_signal_stream_failed", error=str(exc))


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows fallback
            pass

    broker = IBKRBroker(IBKRConnectionConfig.from_settings(settings))

    # Registered before reconciliation so replayed open orders reach the same
    # state machine as live callbacks.
    bus = EventBus()
    quote_store = RedisQuoteStore.from_url(
        settings.redis_url, ttl_seconds=max(60, settings.ibkr_stale_data_seconds * 3)
    )
    control_plane = RedisControlPlane.from_url(settings.redis_url)
    gate = KillSwitch(
        SystemControlStore(SessionLocal),
        control_plane=control_plane,
        max_age_seconds=float(settings.ibkr_stale_data_seconds) * 3,
    )
    order_manager = OrderManager(
        broker,
        SqlAlchemyOrderRepository(SessionLocal),
        gate=gate,
        trading_mode=settings.trading_mode,
        allow_live_trading=settings.allow_live_trading,
        event_bus=bus,
    )
    market_service = MarketDataService(
        broker,
        stale_seconds=float(settings.ibkr_stale_data_seconds),
        store=quote_store,
        event_bus=bus,
    )
    broker.add_order_listener(order_manager)
    broker.add_order_listener(market_service)

    guard: asyncio.Task | None = None
    signaler: asyncio.Task | None = None
    heartbeat: asyncio.Task | None = None
    try:
        await broker.connect()
        await publish_state(broker, "broker.identified")
        await reconcile_broker_state(broker)
        gate_state = await gate.refresh()
        guard = asyncio.create_task(guard_trading_gate(gate, stop))
        signaler = asyncio.create_task(watch_control_signals(control_plane, gate))
        heartbeat = asyncio.create_task(publish_heartbeat(broker, stop))
        status = await broker.status()
        log.info(
            "worker_started",
            state=status.state.value,
            next_order_id=status.next_order_id,
            trading_mode=settings.trading_mode.value,
            allow_live_trading=settings.allow_live_trading,
            trading_state=gate_state.state.value,
            new_orders_allowed=order_manager.gate.allows_new_orders(),
            gate_reason=gate_state.reason,
            last_error=status.last_error,
        )
        if broker.is_ready():
            broker.set_market_data_type(settings.market_data_type)
            await start_market_data(market_service)
        await stop.wait()
    finally:
        tasks = [task for task in (guard, signaler, heartbeat) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await broker.disconnect()
            await publish_state(broker, "broker.disconnected")
        finally:
            await quote_store.aclose()
            await control_plane.aclose()


if __name__ == "__main__":
    asyncio.run(run())
