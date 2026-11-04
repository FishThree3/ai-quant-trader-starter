import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from config.settings import TradingMode
from core.controls.kill_switch import AlwaysActive
from core.execution.order_manager import (
    BrokerNotReady,
    LiveTradingRefused,
    OrderManager,
)
from core.execution.orders import LimitOrderIntent, map_broker_status
from core.reconciliation.models import BrokerExecution, InstrumentKey
from database.models import Base, AuditLog, Execution, Order, OrderStatus
from database.order_repository import SqlAlchemyOrderRepository

AAPL = InstrumentKey.of("AAPL", "STK", "USD")


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


class JournalRepository(SqlAlchemyOrderRepository):
    """Records the order of persistence steps so ordering claims are testable."""

    def __init__(self, session_factory, journal):
        super().__init__(session_factory)
        self.journal = journal

    async def insert_created_order(self, intent, instrument_id):
        self.journal.append("db:created")
        await super().insert_created_order(intent, instrument_id)

    async def update_order(self, order_uuid, **fields):
        self.journal.append(f"db:update:{fields.get('status')}")
        await super().update_order(order_uuid, **fields)


class FakeGateway:
    def __init__(self, *, ready=True, journal=None, fail_place=False):
        self.ready = ready
        self.journal = journal if journal is not None else []
        self.fail_place = fail_place
        self.placed = []
        self.cancelled = []
        self.next_order_id = 100

    def is_ready(self):
        return self.ready

    def place_limit_order(self, intent):
        self.journal.append("gateway:place")
        if self.fail_place:
            raise OSError("socket closed mid-write")
        self.placed.append(intent)
        order_id = self.next_order_id
        self.next_order_id += 1
        return order_id

    def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)


def intent(**overrides) -> LimitOrderIntent:
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


async def build_manager(session_factory, gateway, gate=None):
    gate = gate or AlwaysActive()
    journal: list[str] = []
    repository = JournalRepository(session_factory, journal)
    gateway.journal = journal
    manager = OrderManager(
        gateway,
        repository,
        gate=gate,
        trading_mode=TradingMode.PAPER,
        allow_live_trading=False,
    )
    return manager, journal


async def submit_one(session_factory, gateway):
    manager, journal = await build_manager(session_factory, gateway)
    order = intent()
    await manager.submit(order)
    return manager, journal, order


async def fetch_order(session_factory, order_uuid):
    async with session_factory() as session:
        return await session.get(Order, order_uuid)


async def audit_actions(session_factory):
    async with session_factory() as session:
        rows = (await session.execute(select(AuditLog.action).order_by(AuditLog.created_at))).all()
        return [r[0] for r in rows]


# --- mode and readiness gates -------------------------------------------------


def test_live_mode_is_refused_at_construction():
    with pytest.raises(LiveTradingRefused):
        OrderManager(
            FakeGateway(),
            None,
            gate=AlwaysActive(),
            trading_mode=TradingMode.LIVE,
            allow_live_trading=False,
        )


async def test_submit_refuses_live_even_if_mode_changed_after_construction(session_factory):
    manager, _ = await build_manager(session_factory, FakeGateway())
    manager.trading_mode = TradingMode.LIVE

    with pytest.raises(LiveTradingRefused):
        await manager.submit(intent())


async def test_submit_refuses_when_broker_not_ready_and_persists_nothing(session_factory):
    gateway = FakeGateway(ready=False)

    with pytest.raises(BrokerNotReady):
        await submit_one(session_factory, gateway)

    async with session_factory() as session:
        assert (await session.execute(select(Order.id))).all() == []
    assert gateway.placed == []


# --- CREATED before send ------------------------------------------------------


async def test_created_row_is_persisted_before_transmission(session_factory):
    manager, journal, order = await submit_one(session_factory, FakeGateway())

    assert journal.index("db:created") < journal.index("gateway:place")
    assert journal[:3] == ["db:created", "gateway:place", "db:update:PENDING_SUBMIT"]

    row = await fetch_order(session_factory, order.order_uuid)
    assert row.broker_order_id == 100
    assert row.status == OrderStatus.PENDING_SUBMIT


async def test_successful_send_never_implies_fill(session_factory):
    """placeOrder() returning is not evidence of anything except a socket write."""
    _, _, order = await submit_one(session_factory, FakeGateway())

    row = await fetch_order(session_factory, order.order_uuid)

    assert row.status == OrderStatus.PENDING_SUBMIT
    assert row.filled_quantity == 0.0
    assert row.avg_fill_price is None


async def test_internal_uuid_is_assigned_and_unique(session_factory):
    manager, _, first = await submit_one(session_factory, FakeGateway())
    second = intent()
    await manager.submit(second)

    assert first.order_uuid != second.order_uuid
    assert first.order_uuid.int > 0


async def test_order_lookup_returns_its_own_instrument(session_factory):
    """Guards against a missing join: two instruments must not cross-contaminate."""
    from database.models import Instrument
    from uuid import uuid4

    async with session_factory() as session:
        session.add(
            Instrument(
                id=uuid4(),
                symbol="MSFT",
                sec_type="STK",
                exchange="SMART",
                currency="USD",
            )
        )
        await session.commit()

    manager, _, order = await submit_one(session_factory, FakeGateway())
    record = await manager.repository.get_order(order.order_uuid)

    assert record.symbol == "AAPL"


async def test_transmission_failure_leaves_order_unknown(session_factory):
    gateway = FakeGateway(fail_place=True)
    manager, _ = await build_manager(session_factory, gateway)
    order = intent()

    with pytest.raises(OSError):
        await manager.submit(order)

    row = await fetch_order(session_factory, order.order_uuid)

    assert row.status == OrderStatus.UNKNOWN
    assert row.broker_order_id is None


# --- state machine ------------------------------------------------------------


async def test_callback_chain_walks_to_filled(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())

    await manager.apply_order_update(broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=0.0, remaining=10.0)
    await manager.apply_order_update(broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=4.0, remaining=6.0)
    filled = await manager.apply_order_update(broker_order_id=100, perm_id=5001, status_raw="Filled", filled=10.0, remaining=0.0)

    assert filled.status == str(OrderStatus.FILLED)
    assert filled.completed_at is not None


async def test_perm_id_lands_on_local_order_from_callback(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())

    await manager.apply_order_update(broker_order_id=100, perm_id=7777, status_raw="Submitted", filled=0.0, remaining=10.0)

    row = await fetch_order(session_factory, order.order_uuid)
    assert row.broker_perm_id == 7777


async def test_update_for_unknown_order_is_not_adopted(session_factory):
    manager, _ = await build_manager(session_factory, FakeGateway())

    result = await manager.apply_order_update(
        broker_order_id=4242, perm_id=999, status_raw="Submitted", filled=0.0, remaining=5.0
    )

    assert result is None
    async with session_factory() as session:
        assert (await session.execute(select(Order.id))).all() == []


async def test_stale_update_after_fill_is_ignored_not_overwritten(session_factory):
    """A settled order is a fact; late chatter must not rewrite it."""
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(broker_order_id=100, perm_id=1, status_raw="Filled", filled=10.0, remaining=0.0)

    returned = await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Submitted", filled=0.0, remaining=10.0
    )

    assert returned.status == OrderStatus.FILLED
    assert "order.update_ignored" in await audit_actions(session_factory)


async def test_contradiction_on_a_live_order_becomes_unknown(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(broker_order_id=100, perm_id=1, status_raw="Submitted", filled=0.0, remaining=10.0)

    resolved = await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Created", filled=0.0, remaining=10.0
    )

    assert resolved.status == OrderStatus.UNKNOWN


# --- executions ---------------------------------------------------------------


async def test_duplicate_execid_is_recorded_once(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=4.0, remaining=6.0)
    execution = BrokerExecution("EX-1", 5001, AAPL, "BOT", 4.0, 250.0, "ISLAND")

    first = await manager.apply_execution(execution)
    duplicate = await manager.apply_execution(execution)

    assert first is True
    assert duplicate is False
    async with session_factory() as session:
        rows = (await session.execute(select(Execution.id))).all()
        assert len(rows) == 1


async def test_average_fill_price_is_volume_weighted(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=6.0, remaining=4.0)

    await manager.apply_execution(BrokerExecution("EX-1", 5001, AAPL, "BOT", 2.0, 240.0, "ISLAND"))
    await manager.apply_execution(BrokerExecution("EX-2", 5001, AAPL, "BOT", 4.0, 270.0, "ISLAND"))

    row = await fetch_order(session_factory, order.order_uuid)

    assert row.filled_quantity == 6.0
    assert row.avg_fill_price == pytest.approx(260.0)


async def test_execution_for_unknown_order_is_rejected(session_factory):
    manager, _ = await build_manager(session_factory, FakeGateway())

    recorded = await manager.apply_execution(BrokerExecution("EX-1", 31337, AAPL, "BOT", 1.0, 250.0))

    assert recorded is False
    async with session_factory() as session:
        assert (await session.execute(select(Execution.id))).all() == []


# --- cancellation -------------------------------------------------------------


async def test_cancellation_is_idempotent(session_factory):
    gateway = FakeGateway()
    manager, _, order = await submit_one(session_factory, gateway)
    await manager.apply_order_update(broker_order_id=100, perm_id=1, status_raw="Submitted", filled=0.0, remaining=10.0)

    first = await manager.cancel(order.order_uuid)
    while_pending = await manager.cancel(order.order_uuid)

    assert first == "cancel_requested"
    assert while_pending == "cancel_pending"
    assert gateway.cancelled == [100]

    # Only once the broker confirms does a repeat become "already_cancelled".
    await manager.apply_order_update(broker_order_id=100, perm_id=1, status_raw="Cancelled", filled=0.0, remaining=10.0)
    after = await manager.cancel(order.order_uuid)

    assert after == "already_cancelled"
    assert gateway.cancelled == [100]


async def test_cancelling_a_filled_order_is_refused(session_factory):
    gateway = FakeGateway()
    manager, _, order = await submit_one(session_factory, gateway)
    await manager.apply_order_update(broker_order_id=100, perm_id=1, status_raw="Filled", filled=10.0, remaining=0.0)

    outcome = await manager.cancel(order.order_uuid)

    assert outcome == "not_cancellable"
    assert gateway.cancelled == []


async def test_cancelling_an_unsent_order_is_local_only(session_factory):
    gateway = FakeGateway()
    manager, journal = await build_manager(session_factory, gateway)
    order = intent()
    # Persist CREATED without transmitting.
    instrument_id = await manager.repository.ensure_instrument(order)
    await manager.repository.insert_created_order(order, instrument_id)

    outcome = await manager.cancel(order.order_uuid)

    assert outcome == "cancelled_locally"
    assert gateway.cancelled == []


# --- error codes --------------------------------------------------------------


async def test_code_354_no_market_data_rejects_the_order(session_factory):
    """The live case: TWS silently declines orders for contracts without data."""
    manager, _, order = await submit_one(session_factory, FakeGateway())

    await manager.apply_error(100, 354, "No market data for this contract, order not accepted")

    row = await fetch_order(session_factory, order.order_uuid)
    assert row.status == OrderStatus.REJECTED


async def test_late_error_on_a_settled_order_changes_nothing(session_factory):
    """IBKR sends 161 after a cancel already confirmed; it must not reopen the record."""
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.cancel(order.order_uuid)
    await manager.apply_order_update(broker_order_id=100, perm_id=1, status_raw="Cancelled", filled=0.0, remaining=10.0)

    await manager.apply_error(100, 161, "Cancel Request failed since order was not cancellable")

    row = await fetch_order(session_factory, order.order_uuid)
    assert row.status == OrderStatus.CANCELLED
    assert "order.error_ignored" in await audit_actions(session_factory)


async def test_unrecognised_order_code_is_audited_without_guessing(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())

    await manager.apply_error(100, 10197, "Request Market Data farming warning")

    row = await fetch_order(session_factory, order.order_uuid)
    assert row.status == OrderStatus.PENDING_SUBMIT
    assert "order.error_observed" in await audit_actions(session_factory)


async def test_order_failed_becomes_rejected(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())

    await manager.apply_error(100, 201, "Order rejected - reason: price out of range")

    row = await fetch_order(session_factory, order.order_uuid)
    assert row.status == OrderStatus.REJECTED


async def test_cancel_request_failed_marks_unresolved(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.cancel(order.order_uuid)

    await manager.apply_error(100, 203, "Cancel request failed")

    row = await fetch_order(session_factory, order.order_uuid)
    assert row.status == OrderStatus.UNKNOWN


async def test_system_level_errors_do_not_touch_orders(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())

    await manager.apply_error(-1, 2104, "Hardware connection disconnected")

    row = await fetch_order(session_factory, order.order_uuid)
    assert row.status == OrderStatus.PENDING_SUBMIT


# --- audit trail --------------------------------------------------------------


async def test_audit_trail_covers_the_lifecycle(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(broker_order_id=100, perm_id=1, status_raw="Submitted", filled=0.0, remaining=10.0)
    await manager.apply_execution(BrokerExecution("EX-1", 1, AAPL, "BOT", 1.0, 250.0))

    actions = await audit_actions(session_factory)

    assert "order.created" in actions
    assert "order.submitted" in actions
    assert "order.updated" in actions
    assert "execution.recorded" in actions


# --- status mapping -----------------------------------------------------------


def test_status_mapping_prefers_quantity_over_wording():
    assert map_broker_status("Submitted", filled=10.0, remaining=0.0) is OrderStatus.FILLED
    assert map_broker_status("Filled", filled=10.0, remaining=0.0) is OrderStatus.FILLED


def test_inactive_distinguishes_reject_from_post_fill_expiry():
    assert map_broker_status("Inactive", filled=0.0, remaining=10.0) is OrderStatus.REJECTED
    assert map_broker_status("Inactive", filled=4.0, remaining=6.0) is OrderStatus.CANCELLED


def test_unrecognised_status_is_unknown_not_optimistic():
    assert map_broker_status("SomethingNew", filled=0.0) is OrderStatus.UNKNOWN


def test_market_style_intent_is_rejected():
    with pytest.raises(ValueError, match="MARKET"):
        intent(limit_price=0.0)

    with pytest.raises(ValueError, match="side"):
        intent(side="SELL_SHORT")

    with pytest.raises(ValueError, match="quantity"):
        intent(quantity=0.0)
