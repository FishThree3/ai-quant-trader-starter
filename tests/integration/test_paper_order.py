"""Opt-in IBKR Paper order lifecycle scenario (docs Prompt 9 / P7 order manager).

Skipped unless ENABLE_PAPER_ORDER_TESTS=true, so a normal pytest run never touches
an account. When enabled it submits one BUY of a single share at an absurdly low
price: TWS accepts it, it rests, and it can never fill, so the scenario leaves no
position behind. It is cancelled and confirmed before finishing.
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ENABLE_PAPER_ORDER_TESTS") != "true",
    reason="set ENABLE_PAPER_ORDER_TESTS=true to run against an IBKR Paper account",
)

SYMBOL = "AAPL"
RESTING_PRICE = 0.01  # far below any market, so the order only ever rests
POLL_SECONDS = 0.5
POLL_LIMIT = 40


async def test_paper_limit_order_lifecycle():
    from config.settings import settings

    if settings.trading_mode.value != "PAPER" or settings.allow_live_trading:
        pytest.fail(
            "refusing to run an order scenario unless TRADING_MODE=PAPER and live is disabled"
        )

    from core.broker.ibkr.broker import IBKRBroker
    from core.broker.ibkr.connection import IBKRConnectionConfig
    from core.execution.order_manager import OrderManager
    from core.execution.orders import LimitOrderIntent
    from core.reconciliation.engine import reconcile
    from database.models import OrderStatus
    from core.controls.kill_switch import KillSwitch
    from core.controls.redis_plane import RedisControlPlane
    from database.order_repository import SqlAlchemyOrderRepository
    from database.session import SessionLocal
    from database.system_control import SystemControlStore
    from database.reconciliation_store import load_local_snapshot, persist_broker_snapshot

    broker = IBKRBroker(
        IBKRConnectionConfig(
            host=settings.ibkr_host,
            port=settings.ibkr_port,
            client_id=settings.ibkr_client_id,
            timeout_seconds=settings.ibkr_connect_timeout_seconds,
        )
    )
    gate = KillSwitch(
        SystemControlStore(SessionLocal),
        control_plane=RedisControlPlane.from_url(settings.redis_url),
    )
    await gate.refresh()
    if not gate.allows_new_orders():
        reason = gate.state.reason if gate.state else "gate never read"
        pytest.fail(f"trading gate is not ACTIVE ({reason}); POST /api/system/resume first")

    manager = OrderManager(
        broker,
        SqlAlchemyOrderRepository(SessionLocal),
        gate=gate,
        trading_mode=settings.trading_mode,
        allow_live_trading=settings.allow_live_trading,
    )
    broker.add_order_listener(manager)

    try:
        await broker.connect()
        assert broker.is_ready() is False, "READY must require reconciliation, not just a socket"

        snapshot = await broker.collect_snapshot()
        async with SessionLocal() as session:
            await persist_broker_snapshot(session, snapshot)
            local = await load_local_snapshot(session)
        result = broker.apply_reconciliation(snapshot, local)
        assert result.ok, f"refusing to trade on an unreconciled account: {result.summary()}"

        intent = LimitOrderIntent(
            symbol=SYMBOL,
            sec_type="STK",
            exchange="SMART",
            currency="USD",
            side="BUY",
            quantity=1.0,
            limit_price=RESTING_PRICE,
            time_in_force="DAY",
        )
        order_uuid = await manager.submit(intent)

        # Success of submit() proves only that a write happened, so wait for the
        # broker's own acceptance.
        settled = await wait_for_status(
            manager, order_uuid, {OrderStatus.SUBMITTED, OrderStatus.REJECTED}
        )
        if settled is not None and settled.status == OrderStatus.REJECTED:
            pytest.fail(
                f"TWS rejected the order before it ever worked; check this account's "
                f"market-data entitlements for {SYMBOL} (IBKR code 354 is the usual cause)"
            )
        assert settled is not None, "TWS never accepted or rejected the order"
        assert settled.status == OrderStatus.SUBMITTED
        assert settled.broker_perm_id is not None, "accepted order must carry a permId"

        outcome = await manager.cancel(order_uuid)
        assert outcome == "cancel_requested"

        cancelled = await wait_for_status(manager, order_uuid, {OrderStatus.CANCELLED})
        assert cancelled is not None, "TWS never confirmed the cancellation"

        fills = await manager.repository.executions_for(order_uuid)
        assert fills == [], f"the resting order should never have filled, got {fills}"

        final_snapshot = await broker.collect_snapshot()
        async with SessionLocal() as session:
            final_local = await load_local_snapshot(session)
        invariant = reconcile(final_snapshot, final_local)
        assert invariant.ok, f"invariant broken after the lifecycle: {invariant.summary()}"
    finally:
        await broker.disconnect()


async def wait_for_status(manager, order_uuid, wanted):
    """Poll the local record until a broker callback moves it into `wanted`."""
    for _ in range(POLL_LIMIT):
        record = await manager.repository.get_order(order_uuid)
        if record is not None and record.status in wanted:
            return record
        await asyncio.sleep(POLL_SECONDS)
    return None
