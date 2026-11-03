import asyncio
import signal
from typing import Any

import structlog
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from core.broker.base import BrokerStatus
from core.broker.ibkr.broker import IBKRBroker
from core.broker.ibkr.connection import IBKRConnectionConfig
from core.reconciliation.models import ReconciliationResult
from database.broker_state import record_state_event
from database.reconciliation_store import load_local_snapshot, persist_broker_snapshot
from database.session import SessionLocal

log = structlog.get_logger()


def _event_metadata(status: BrokerStatus, result: ReconciliationResult | None) -> dict[str, Any]:
    return {
        "next_order_id": status.next_order_id,
        "account_present": status.account is not None,
        "trading_mode": settings.trading_mode.value,
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


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows fallback
            pass

    broker = IBKRBroker(
        IBKRConnectionConfig(
            host=settings.ibkr_host,
            port=settings.ibkr_port,
            client_id=settings.ibkr_client_id,
            timeout_seconds=settings.ibkr_connect_timeout_seconds,
        )
    )

    try:
        await broker.connect()
        await publish_state(broker, "broker.identified")
        await reconcile_broker_state(broker)
        status = await broker.status()
        log.info(
            "worker_started",
            state=status.state.value,
            next_order_id=status.next_order_id,
            trading_mode=settings.trading_mode.value,
            allow_live_trading=settings.allow_live_trading,
            last_error=status.last_error,
        )
        await stop.wait()
    finally:
        await broker.disconnect()
        await publish_state(broker, "broker.disconnected")


if __name__ == "__main__":
    asyncio.run(run())
