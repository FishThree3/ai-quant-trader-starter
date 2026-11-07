"""Failure-injection matrix for docs Prompt 8.

Each test names one of the required scenarios and asserts the system refuses to trade,
rather than merely surviving it.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from config.settings import TradingMode
from core.broker.base import BrokerState
from core.broker.ibkr.connection import IBKRConnectionConfig, IBKRConnectionManager
from core.controls.kill_switch import (
    AlwaysActive,
    KillSwitch,
    SystemState,
    TradingHalted,
    TradingState,
)
from core.execution.order_manager import BrokerNotReady, OrderManager
from core.execution.orders import LimitOrderIntent
from core.market.models import MarketInstrument
from core.market.service import MarketDataService, NoMarketData, StaleMarketData
from core.reconciliation.models import BrokerExecution, InstrumentKey
from database.models import Base, Execution, Order, OrderStatus
from database.order_repository import SqlAlchemyOrderRepository

T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
AAPL_KEY = InstrumentKey.of("AAPL", "STK", "USD")
AAPL_MI = MarketInstrument("AAPL", "STK", "SMART", "USD")


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


async def seed_live_order(session_factory):
    from database.models import Instrument

    async with session_factory() as session:
        session.add(
            Instrument(
                id=UUID(int=7),
                symbol="AAPL",
                sec_type="STK",
                exchange="SMART",
                currency="USD",
            )
        )
        session.add(
            Order(
                id=UUID(int=8),
                instrument_id=UUID(int=7),
                broker_perm_id=5001,
                broker_order_id=77,
                side="BUY",
                order_type="LMT",
                quantity=10.0,
                limit_price=250.0,
                status="SUBMITTED",
                filled_quantity=0.0,
            )
        )
        await session.commit()
        return UUID(int=8)


class AllowRisk:
    async def authorize(self, intent):
        pass


class BrokerStub:
    """A gateway that can be made unavailable mid-flight, and doubles as the md gateway."""

    def __init__(self, *, ready=True):
        self.ready_flag = ready
        self.placed = []
        self.cancelled = []
        self.subscribed = []
        self.released = []

    def is_ready(self):
        return self.ready_flag

    def allocate_order_id(self):
        return 77

    def place_limit_order(self, intent, broker_order_id):
        self.placed.append(intent)
        return broker_order_id

    def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)

    def subscribe(self, instrument, req_id):
        self.subscribed.append((instrument.key, req_id))

    def unsubscribe(self, req_id):
        self.released.append(req_id)


def manager_for(session_factory, gate=None, broker=None):
    return OrderManager(
        broker or BrokerStub(),
        SqlAlchemyOrderRepository(session_factory),
        gate=gate or AlwaysActive(),
        trading_mode=TradingMode.PAPER,
        allow_live_trading=False,
        risk_check=AllowRisk(),
    )


def intent(**overrides):
    base = {
        "symbol": "AAPL",
        "sec_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "side": "BUY",
        "quantity": 10.0,
        "limit_price": 250.0,
    }
    base.update(overrides)
    return LimitOrderIntent(**base)


# --- 1. stale market data -------------------------------------------------------


async def test_stale_market_data_blocks_a_trade():
    now = {"at": T0}
    svc = MarketDataService(BrokerStub(), stale_seconds=10.0, clock=lambda: now["at"])
    await svc.subscribe(AAPL_MI, "strategy")
    await svc.apply_tick(90_000, 1, 250.0, received_at=now["at"])

    now["at"] = T0 + timedelta(seconds=11)

    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL_MI)


async def test_no_market_data_at_all_is_also_a_refusal():
    svc = MarketDataService(BrokerStub(), stale_seconds=10.0, clock=lambda: T0)

    with pytest.raises(NoMarketData):
        svc.require_fresh(AAPL_MI)


# --- 2. TWS disconnect ------------------------------------------------------------


def test_a_disconnected_broker_refuses_order_ids_and_submission():
    manager = IBKRConnectionManager(
        IBKRConnectionConfig(host="h", port=1, client_id=1, timeout_seconds=5)
    )
    manager.next_order_id = 5
    manager.state = BrokerState.READY

    manager._on_connection_closed()

    assert manager.state is BrokerState.DISCONNECTED
    assert manager.is_ready() is False
    with pytest.raises(RuntimeError, match="not READY"):
        manager.allocate_order_id()


async def test_submission_stops_when_the_broker_drops_between_checks(session_factory):
    broker = BrokerStub(ready=True)
    manager = manager_for(session_factory, broker=broker)

    await manager.submit(intent())
    broker.ready_flag = False

    with pytest.raises(BrokerNotReady):
        await manager.submit(intent())

    assert len(broker.placed) == 1, "the second order must not reach a broker we cannot see"


# --- 3. connectivity error codes 1100 / 2110 --------------------------------------


@pytest.mark.parametrize("code", [1100, 2110])
def test_connectivity_loss_degrades_and_freezes_readiness(code):
    manager = IBKRConnectionManager(IBKRConnectionConfig(host="h", port=1, client_id=1))
    manager.state = BrokerState.READY
    manager.next_order_id = 9

    manager._on_error(req_id=-1, code=code, message="Connection broken with TWS", reject="")

    assert manager.state is BrokerState.DEGRADED
    assert manager.is_ready() is False


def test_connectivity_restored_goes_to_syncing_not_ready():
    """Reconnection must re-earn READY through reconciliation, never by assumption."""
    manager = IBKRConnectionManager(IBKRConnectionConfig(host="h", port=1, client_id=1))
    manager.state = BrokerState.DEGRADED

    manager._on_error(req_id=-1, code=1101, message="Connected to TWS", reject="")

    assert manager.state is BrokerState.SYNCING


# --- 4. order reject ---------------------------------------------------------------


async def test_an_order_reject_lands_as_rejected_not_submitted(session_factory):
    manager = manager_for(session_factory)
    order_uuid = await manager.submit(intent())

    await manager.apply_error(77, 201, "Order rejected - reason: price out of range")

    async with session_factory() as session:
        row = await session.get(Order, order_uuid)
    assert row.status == OrderStatus.REJECTED


# --- 5. duplicate callbacks --------------------------------------------------------


async def test_duplicate_status_callbacks_change_nothing_the_second_time(session_factory):
    manager = manager_for(session_factory)
    order_uuid = await manager.submit(intent())

    first = await manager.apply_order_update(
        broker_order_id=77, perm_id=5001, status_raw="Submitted", filled=0.0, remaining=10.0
    )
    second = await manager.apply_order_update(
        broker_order_id=77, perm_id=5001, status_raw="Submitted", filled=0.0, remaining=10.0
    )

    assert first.status == second.status == OrderStatus.SUBMITTED
    assert first.order_uuid == second.order_uuid == order_uuid, (
        "both callbacks must resolve to the one local order"
    )
    async with session_factory() as session:
        rows = (await session.execute(select(func.count()).select_from(Order))).scalar_one()
    assert rows == 1, "a redelivered callback must not mint a second order"


# --- 6. duplicate execDetails ------------------------------------------------------


async def test_duplicate_exec_details_cannot_double_count_a_fill(session_factory):
    manager = manager_for(session_factory)
    order_uuid = await manager.submit(intent())
    await seed_perm_link(session_factory, order_uuid)
    fill = BrokerExecution("EX-DUP", 5001, AAPL_KEY, "BOT", 4.0, 250.0, "ISLAND")

    recorded = await manager.apply_execution(fill)
    again = await manager.apply_execution(fill)

    assert recorded is True and again is False
    async with session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(Execution))).scalar_one()
    assert count == 1


async def seed_perm_link(session_factory, order_uuid):
    async with session_factory() as session:
        order = await session.get(Order, order_uuid)
        order.broker_perm_id = 5001
        await session.commit()


# --- 7. process restart ------------------------------------------------------------


async def test_a_restarted_process_reconciles_and_creates_no_orders(session_factory):
    from core.reconciliation.engine import reconcile
    from core.reconciliation.models import BrokerOrder, BrokerSnapshot
    from database.reconciliation_store import load_local_snapshot, persist_broker_snapshot

    await seed_live_order(session_factory)
    snapshot = BrokerSnapshot(
        account="DUR1",
        orders=(BrokerOrder(5001, 77, AAPL_KEY, "BOT", 10.0, 0.0, "Submitted"),),
        orders_complete=True,
        positions_complete=True,
        executions_complete=True,
    )

    async with session_factory() as session:
        await persist_broker_snapshot(session, snapshot)
        local = await load_local_snapshot(session)

    result = reconcile(snapshot, local)

    assert result.ok, result.summary()
    assert result.orders_matched == 1
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Order))).scalar_one() == 1


async def test_a_restart_with_an_unseen_broker_order_still_blocks_trading(session_factory):
    from core.reconciliation.engine import reconcile
    from core.reconciliation.models import BrokerOrder, BrokerSnapshot
    from database.reconciliation_store import load_local_snapshot

    async with session_factory() as session:
        local = await load_local_snapshot(session)

    snapshot = BrokerSnapshot(
        account="DUR1",
        orders=(BrokerOrder(9999, 4242, AAPL_KEY, "BOT", 5.0, 0.0, "Submitted"),),
        orders_complete=True,
        positions_complete=True,
        executions_complete=True,
    )

    result = reconcile(snapshot, local)

    assert not result.ok


# --- 8. database temporarily unavailable -------------------------------------------


async def test_a_database_write_failure_blocks_submission_before_transmission(session_factory):
    class BrokenRepository(SqlAlchemyOrderRepository):
        async def insert_created_order(self, intent, instrument_id):
            raise RuntimeError("database temporarily unavailable")

    gate = AlwaysActive()
    broker = BrokerStub()
    manager = OrderManager(
        broker,
        BrokenRepository(session_factory),
        gate=gate,
        trading_mode=TradingMode.PAPER,
        allow_live_trading=False,
        risk_check=AllowRisk(),
    )

    with pytest.raises(Exception, match="temporarily unavailable"):
        await manager.submit(intent())

    assert broker.placed == [], "an order that could not be recorded must never be transmitted"


async def test_an_unreadable_gate_blocks_submission(session_factory):
    class UnreadableStore:
        async def read(self):
            raise ConnectionError("db gone")

        async def write(self, state, *, reason, actor):
            raise ConnectionError("db gone")

    gate = KillSwitch(UnreadableStore(), clock=lambda: T0)
    await gate.refresh()
    manager = manager_for(session_factory, gate=gate)

    with pytest.raises(TradingHalted):
        await manager.submit(intent())


# --- 9. Redis temporarily unavailable -----------------------------------------------


class FlakyPlane:
    def __init__(self):
        self.up = True

    async def healthy(self):
        if not self.up:
            raise ConnectionError("redis gone")
        return True


async def test_losing_redis_stops_new_orders_then_recovers(session_factory):
    plane = FlakyPlane()
    gate = KillSwitch(_ActiveStore(), control_plane=plane, clock=lambda: T0, max_age_seconds=30.0)
    manager = manager_for(session_factory, gate=gate)
    await gate.refresh()
    assert gate.allows_new_orders() is True

    plane.up = False
    await gate.refresh()
    assert gate.allows_new_orders() is False
    with pytest.raises(TradingHalted):
        await manager.submit(intent())

    plane.up = True
    await gate.refresh()
    order_uuid = await manager.submit(intent())
    assert order_uuid is not None


class _ActiveStore:
    async def read(self):
        return SystemState(TradingState.ACTIVE, "ok", "operator", T0)

    async def write(self, state, *, reason, actor):
        return SystemState(state, reason, actor, T0)
