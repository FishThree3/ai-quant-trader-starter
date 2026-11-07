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

    def allocate_order_id(self):
        order_id = self.next_order_id
        self.next_order_id += 1
        return order_id

    def place_limit_order(self, intent, broker_order_id):
        self.journal.append("gateway:place")
        if self.fail_place:
            raise OSError("socket closed mid-write")
        self.placed.append(intent)
        return broker_order_id

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


class AllowRisk:
    async def authorize(self, intent):
        pass


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
        risk_check=AllowRisk(),
    )
    return manager, journal


async def submit_one(session_factory, gateway):
    manager, journal = await build_manager(session_factory, gateway)
    order = intent()
    await manager.submit(order)
    return manager, journal, order


async def test_read_only_gateway_refuses_before_creating_order(session_factory):
    from types import SimpleNamespace
    from core.broker.base import BrokerState
    from core.broker.ibkr.connection import IBKRConnectionConfig, IBKRConnectionManager

    gateway = IBKRConnectionManager(IBKRConnectionConfig("localhost", 7497, 71))
    gateway._on_managed_accounts(["DU123"])
    gateway.state = BrokerState.READY
    gateway.next_order_id = 1
    gateway._app = SimpleNamespace(isConnected=lambda: True)
    manager, _ = await build_manager(session_factory, gateway)
    order = intent()
    with pytest.raises(BrokerNotReady):
        await manager.submit(order)
    assert await fetch_order(session_factory, order.order_uuid) is None


async def test_event_subscriber_can_cancel_without_deadlocking(session_factory):
    import asyncio
    from uuid import UUID

    from core.events.bus import EventBus
    from core.events.models import EventType

    gateway = FakeGateway()
    manager, _ = await build_manager(session_factory, gateway)
    manager.event_bus = EventBus()

    async def cancel_created(event):
        await manager.cancel(UUID(event.payload["order_uuid"]))

    manager.event_bus.subscribe(EventType.ORDER_CREATED, cancel_created)
    await asyncio.wait_for(manager.submit(intent()), timeout=1)
    assert gateway.cancelled == [100]


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
    assert journal[:3] == ["db:created", "db:update:PENDING_SUBMIT", "gateway:place"]

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
    assert row.broker_order_id == 100


# --- state machine ------------------------------------------------------------


async def test_callback_chain_walks_to_filled(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())

    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=0.0, remaining=10.0
    )
    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=4.0, remaining=6.0
    )
    filled = await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Filled", filled=10.0, remaining=0.0
    )

    assert filled.status == str(OrderStatus.FILLED)
    assert filled.completed_at is not None


async def test_perm_id_lands_on_local_order_from_callback(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())

    await manager.apply_order_update(
        broker_order_id=100, perm_id=7777, status_raw="Submitted", filled=0.0, remaining=10.0
    )

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
    await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Filled", filled=10.0, remaining=0.0
    )

    returned = await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Submitted", filled=0.0, remaining=10.0
    )

    assert returned.status == OrderStatus.FILLED
    assert "order.update_ignored" in await audit_actions(session_factory)


async def test_contradiction_on_a_live_order_becomes_unknown(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Submitted", filled=0.0, remaining=10.0
    )

    resolved = await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Created", filled=0.0, remaining=10.0
    )

    assert resolved.status == OrderStatus.UNKNOWN


# --- executions ---------------------------------------------------------------


async def test_duplicate_execid_is_recorded_once(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=4.0, remaining=6.0
    )
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
    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=6.0, remaining=4.0
    )

    await manager.apply_execution(BrokerExecution("EX-1", 5001, AAPL, "BOT", 2.0, 240.0, "ISLAND"))
    await manager.apply_execution(BrokerExecution("EX-2", 5001, AAPL, "BOT", 4.0, 270.0, "ISLAND"))

    row = await fetch_order(session_factory, order.order_uuid)

    assert row.filled_quantity == 6.0
    assert row.avg_fill_price == pytest.approx(260.0)


async def test_execution_for_unknown_order_is_rejected(session_factory):
    manager, _ = await build_manager(session_factory, FakeGateway())

    recorded = await manager.apply_execution(
        BrokerExecution("EX-1", 31337, AAPL, "BOT", 1.0, 250.0)
    )

    assert recorded is False
    async with session_factory() as session:
        assert (await session.execute(select(Execution.id))).all() == []


# --- cancellation -------------------------------------------------------------


async def test_cancellation_is_idempotent(session_factory):
    gateway = FakeGateway()
    manager, _, order = await submit_one(session_factory, gateway)
    await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Submitted", filled=0.0, remaining=10.0
    )

    first = await manager.cancel(order.order_uuid)
    while_pending = await manager.cancel(order.order_uuid)

    assert first == "cancel_requested"
    assert while_pending == "cancel_pending"
    assert gateway.cancelled == [100]

    # Only once the broker confirms does a repeat become "already_cancelled".
    await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Cancelled", filled=0.0, remaining=10.0
    )
    after = await manager.cancel(order.order_uuid)

    assert after == "already_cancelled"
    assert gateway.cancelled == [100]


async def test_cancelling_a_filled_order_is_refused(session_factory):
    gateway = FakeGateway()
    manager, _, order = await submit_one(session_factory, gateway)
    await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Filled", filled=10.0, remaining=0.0
    )

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


@pytest.mark.parametrize("code", [300, 354, 10167])
async def test_market_data_error_cannot_reject_a_colliding_order_id(session_factory, code):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    before = await fetch_order(session_factory, order.order_uuid)
    await manager.apply_error(100, code, "Market data request failed or delayed")
    after = await fetch_order(session_factory, order.order_uuid)
    assert after.status == before.status


async def test_late_error_on_a_settled_order_changes_nothing(session_factory):
    """IBKR sends 161 after a cancel already confirmed; it must not reopen the record."""
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.cancel(order.order_uuid)
    await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Cancelled", filled=0.0, remaining=10.0
    )

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
    await manager.apply_order_update(
        broker_order_id=100, perm_id=1, status_raw="Submitted", filled=0.0, remaining=10.0
    )
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


# --- regression: transmission identity / replay / fail-closed gates ------------


async def test_risk_check_is_required_and_no_order_is_persisted(session_factory):
    from core.execution.order_manager import RiskCheckRequired

    manager, _ = await build_manager(session_factory, FakeGateway())
    manager.risk_check = None
    with pytest.raises(RiskCheckRequired):
        await manager.submit(intent())
    assert manager.gateway.placed == []
    async with session_factory() as session:
        assert (await session.execute(select(Order.id))).all() == []


async def test_same_uuid_replay_never_sends_twice_even_if_gate_closed(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    manager.risk_check = None
    assert await manager.submit(order) == order.order_uuid
    assert len(manager.gateway.placed) == 1


async def test_changed_intent_cannot_reuse_uuid(session_factory):
    from core.execution.order_manager import OrderIntentConflict

    manager, _, order = await submit_one(session_factory, FakeGateway())
    for changed in (
        intent(order_uuid=order.order_uuid, quantity=1),
        intent(order_uuid=order.order_uuid, time_in_force="GTC"),
    ):
        with pytest.raises(OrderIntentConflict):
            await manager.submit(changed)
    assert len(manager.gateway.placed) == 1


async def test_uncertain_transmission_replay_never_retransmits(session_factory):
    gateway = FakeGateway(fail_place=True)
    manager, _ = await build_manager(session_factory, gateway)
    order = intent()
    with pytest.raises(OSError):
        await manager.submit(order)
    gateway.fail_place = False
    assert await manager.submit(order) == order.order_uuid
    assert gateway.placed == []
    row = await fetch_order(session_factory, order.order_uuid)
    assert row.status == OrderStatus.UNKNOWN and row.broker_order_id == 100


async def test_broker_mapping_failure_prevents_transmission(session_factory):
    manager, _ = await build_manager(session_factory, FakeGateway())

    class BrokenMapping(SqlAlchemyOrderRepository):
        async def update_order(self, order_uuid, **fields):
            if "broker_order_id" in fields:
                raise OSError("mapping commit failed")
            await super().update_order(order_uuid, **fields)

    manager.repository = BrokenMapping(session_factory)
    with pytest.raises(OSError):
        await manager.submit(intent())
    assert manager.gateway.placed == []


async def test_gate_is_rechecked_after_database_await(session_factory):
    class Gate:
        active = True

        def require_new_orders_allowed(self):
            if not self.active:
                raise RuntimeError("halted")

    gate = Gate()
    manager, _ = await build_manager(session_factory, FakeGateway(), gate)

    class HaltAfterMapping(SqlAlchemyOrderRepository):
        async def update_order(self, order_uuid, **fields):
            await super().update_order(order_uuid, **fields)
            gate.active = False

    manager.repository = HaltAfterMapping(session_factory)
    with pytest.raises(RuntimeError, match="halted"):
        await manager.submit(intent())
    assert manager.gateway.placed == []


async def test_risk_is_rechecked_immediately_before_send(session_factory):
    class Expires:
        calls = 0

        async def authorize(self, intent):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("risk expired")

    manager, _ = await build_manager(session_factory, FakeGateway())
    manager.risk_check = Expires()
    with pytest.raises(RuntimeError, match="risk expired"):
        await manager.submit(intent())
    assert manager.gateway.placed == []


async def test_concurrent_same_uuid_submissions_send_once(session_factory):
    import asyncio

    manager, _ = await build_manager(session_factory, FakeGateway())
    order = intent()
    assert (
        await asyncio.gather(manager.submit(order), manager.submit(order)) == [order.order_uuid] * 2
    )
    assert len(manager.gateway.placed) == 1


# --- regression: cancellation must truthfully persist its result --------------


async def test_created_cancellation_actually_persists_cancelled(session_factory):
    manager, _ = await build_manager(session_factory, FakeGateway())
    order = intent()
    iid = await manager.repository.ensure_instrument(order)
    await manager.repository.insert_created_order(order, iid)
    assert await manager.cancel(order.order_uuid) == "cancelled_locally"
    assert (await fetch_order(session_factory, order.order_uuid)).status == OrderStatus.CANCELLED


async def test_pending_submit_cancellation_is_idempotent(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    assert await manager.cancel(order.order_uuid) == "cancel_requested"
    assert await manager.cancel(order.order_uuid) == "cancel_pending"
    assert manager.gateway.cancelled == [100]
    assert (
        await fetch_order(session_factory, order.order_uuid)
    ).status == OrderStatus.CANCEL_PENDING


async def test_cancel_transmission_failure_is_unknown_and_retryable(session_factory):
    class BrokenCancel(FakeGateway):
        fail_cancel = True

        def cancel_order(self, broker_order_id):
            if self.fail_cancel:
                raise OSError("cancel socket failed")
            super().cancel_order(broker_order_id)

    gateway = BrokenCancel()
    manager, _, order = await submit_one(session_factory, gateway)
    with pytest.raises(OSError):
        await manager.cancel(order.order_uuid)
    assert (await fetch_order(session_factory, order.order_uuid)).status == OrderStatus.UNKNOWN
    gateway.fail_cancel = False
    assert await manager.cancel(order.order_uuid) == "cancel_requested"
    assert gateway.cancelled == [100]


async def test_working_status_does_not_erase_cancel_request(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.cancel(order.order_uuid)
    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=0, remaining=10
    )
    assert await manager.cancel(order.order_uuid) == "cancel_pending"
    assert manager.gateway.cancelled == [100]


# --- regression: cumulative fills and execution ordering ----------------------


async def test_partial_exec_ledger_never_reduces_completed_fill_total(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Filled", filled=10, remaining=0
    )
    await manager.apply_execution(BrokerExecution("EX-1", 5001, AAPL, "BOT", 4, 250))
    row = await fetch_order(session_factory, order.order_uuid)
    assert row.filled_quantity == 10 and row.status == OrderStatus.FILLED
    assert row.avg_fill_price is None
    await manager.apply_execution(BrokerExecution("EX-2", 5001, AAPL, "BOT", 6, 260))
    row = await fetch_order(session_factory, order.order_uuid)
    assert row.avg_fill_price == pytest.approx(256)


async def test_old_partial_status_does_not_reduce_known_fill(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=6, remaining=4
    )
    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=4, remaining=6
    )
    assert (await fetch_order(session_factory, order.order_uuid)).filled_quantity == 6


async def test_first_execution_can_bind_perm_id_and_complete_without_status(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    assert await manager.apply_execution(
        BrokerExecution("EX-1", 5001, AAPL, "BOT", 10, 250, broker_order_id=100)
    )
    row = await fetch_order(session_factory, order.order_uuid)
    assert row.broker_perm_id == 5001 and row.status == OrderStatus.FILLED
    assert row.filled_quantity == 10 and row.completed_at is not None


async def test_execution_replay_repairs_interrupted_projection(session_factory):
    from datetime import datetime, timezone

    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.repository.update_order(order.order_uuid, broker_perm_id=5001)
    await manager.repository.insert_execution(
        order_uuid=order.order_uuid,
        exec_id="EX-1",
        quantity=10,
        price=250,
        exchange=None,
        executed_at=datetime.now(timezone.utc),
    )
    assert not await manager.apply_execution(BrokerExecution("EX-1", 5001, AAPL, "BOT", 10, 250))
    assert (await fetch_order(session_factory, order.order_uuid)).status == OrderStatus.FILLED


async def test_conflicting_execution_replay_is_not_silently_deduplicated(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_execution(
        BrokerExecution("EX-1", 5001, AAPL, "BOT", 4, 250, broker_order_id=100)
    )
    with pytest.raises(ValueError, match="Conflicting execution"):
        await manager.apply_execution(
            BrokerExecution("EX-1", 5001, AAPL, "BOT", 5, 250, broker_order_id=100)
        )


async def test_conflicting_order_and_perm_ids_cannot_mutate_order(session_factory):
    manager, _, order = await submit_one(session_factory, FakeGateway())
    await manager.apply_order_update(
        broker_order_id=100, perm_id=5001, status_raw="Submitted", filled=0, remaining=10
    )
    with pytest.raises(ValueError, match="Conflicting broker"):
        await manager.apply_order_update(
            broker_order_id=100, perm_id=9999, status_raw="Filled", filled=10, remaining=0
        )
    assert (await fetch_order(session_factory, order.order_uuid)).filled_quantity == 0


@pytest.mark.parametrize("field", ["quantity", "limit_price"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_order_intent_is_rejected(field, value):
    with pytest.raises(ValueError):
        intent(**{field: value})
