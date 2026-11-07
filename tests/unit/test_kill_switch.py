from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from core.controls.kill_switch import (
    KillSwitch,
    SystemState,
    TradingHalted,
    TradingState,
)
from core.execution.order_manager import OrderManager
from database.models import Base, SystemControl
from database.system_control import SystemControlStore

T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


class StubStore:
    def __init__(self, state=None, *, fail=False):
        self.state = state
        self.fail = fail
        self.written = []

    async def read(self):
        if self.fail:
            raise RuntimeError("database is down")
        return self.state

    async def write(self, state, *, reason, actor):
        self.written.append((state, reason, actor))
        self.state = SystemState(state, reason, actor, T0)
        return self.state


class StubPlane:
    def __init__(self, healthy=True, *, fail=False):
        self.healthy_flag = healthy
        self.fail = fail

    async def healthy(self):
        if self.fail:
            raise ConnectionError("redis refused")
        return self.healthy_flag


def state_at(state=TradingState.ACTIVE, age_seconds=0.0) -> SystemState:
    return SystemState(state, "operator decision", "kvn", T0 - timedelta(seconds=age_seconds))


def switch(store=None, plane=None, **overrides):
    kwargs = {
        "max_age_seconds": 30.0,
        "clock": lambda: T0,
    }
    kwargs.update(overrides)
    return KillSwitch(
        store if store is not None else StubStore(state_at()), control_plane=plane, **kwargs
    )


# --- the ACTIVE verdict needs all three proofs at once -------------------------


async def test_active_state_with_healthy_control_plane_allows_orders():
    gate = switch(plane=StubPlane(True))

    await gate.refresh()

    assert gate.allows_new_orders() is True


async def test_no_durable_record_blocks():
    gate = switch(StubStore(None), plane=StubPlane(True))

    state = await gate.refresh()

    assert gate.allows_new_orders() is False
    assert "no durable trading state recorded" in state.reason


async def test_database_outage_blocks_rather_than_reusing_the_last_active_answer():
    gate = switch(StubStore(state_at()), plane=StubPlane(True))
    await gate.refresh()
    assert gate.allows_new_orders() is True

    gate.store.fail = True
    broken = await gate.refresh()

    assert gate.allows_new_orders() is False
    assert "state store unavailable" in broken.reason


async def test_redis_loss_blocks_new_orders():
    gate = switch(plane=StubPlane(False))

    state = await gate.refresh()

    assert gate.allows_new_orders() is False
    assert "control plane unreachable" in state.reason


async def test_a_control_plane_probe_that_throws_blocks_like_any_other_outage():
    gate = switch(plane=StubPlane(fail=True))

    state = await gate.refresh()

    assert "control plane probe failed: ConnectionError" in state.reason


async def test_a_stale_active_record_is_not_treated_as_current():
    gate = switch(StubStore(state_at(age_seconds=120)), clock=lambda: T0)

    state = await gate.refresh()

    assert "120s old" in state.reason
    assert gate.allows_new_orders() is False


# --- non-active states ---------------------------------------------------------


@pytest.mark.parametrize("state", [TradingState.PAUSED, TradingState.KILLED])
async def test_blocked_states_are_honoured_not_softened(state):
    gate = switch(StubStore(state_at(state)), plane=StubPlane(True))

    recorded = await gate.refresh()

    assert gate.allows_new_orders() is False
    assert recorded.state is state


async def test_require_raises_with_the_reasons_an_operator_can_act_on():
    gate = switch(plane=StubPlane(False))
    await gate.refresh()

    with pytest.raises(TradingHalted) as caught:
        gate.require_new_orders_allowed()

    assert caught.value.state is TradingState.PAUSED
    assert any("control plane" in reason for reason in caught.value.reasons)


# --- changing the gate ----------------------------------------------------------


async def test_engage_writes_durably_and_is_adopted_at_once():
    store = StubStore(state_at())
    gate = switch(store, plane=StubPlane(True))
    await gate.refresh()

    written = await gate.engage(TradingState.KILLED, reason="manual kill", actor="operator")

    assert store.written == [(TradingState.KILLED, "manual kill", "operator")]
    assert written.state is TradingState.KILLED
    assert gate.allows_new_orders() is False


async def test_resume_requires_a_durable_write_too():
    store = StubStore(state_at(TradingState.KILLED))
    gate = switch(store, plane=StubPlane(True))
    await gate.refresh()

    await gate.engage(TradingState.ACTIVE, reason="incident closed", actor="operator")

    assert gate.allows_new_orders() is True


async def test_gate_cannot_be_moved_without_a_durable_store():
    gate = KillSwitch(None, control_plane=StubPlane(True))

    with pytest.raises(TradingHalted):
        await gate.engage(TradingState.ACTIVE, reason="no store", actor="operator")


# --- durable store over a real database ----------------------------------------


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


async def test_state_survives_a_process_restart(session_factory):
    store = SystemControlStore(session_factory)
    await store.write(TradingState.KILLED, reason="kill before restart", actor="operator")

    restored = await store.read()

    assert restored.state is TradingState.KILLED
    assert restored.reason == "kill before restart"
    assert restored.actor == "operator"
    # SQLite returns naive timestamps; age maths must not compare naive to aware.
    assert restored.updated_at.tzinfo is not None


async def test_unwritten_gate_reads_as_none_so_the_switch_blocks(session_factory):
    assert await SystemControlStore(session_factory).read() is None


async def test_write_updates_the_single_row_rather_than_appending(session_factory):
    store = SystemControlStore(session_factory)

    await store.write(TradingState.KILLED, reason="first", actor="a")
    await store.write(TradingState.ACTIVE, reason="second", actor="b")

    async with session_factory() as session:
        rows = (await session.execute(SystemControl.__table__.select())).all()
    assert len(rows) == 1
    assert (await store.read()).reason == "second"


async def test_switch_backed_by_the_database_starts_blocked_then_opens(session_factory):
    gate = switch(SystemControlStore(session_factory), plane=StubPlane(True))

    blocked = await gate.refresh()
    assert gate.allows_new_orders() is False
    assert "no durable trading state recorded" in blocked.reason

    await gate.engage(TradingState.ACTIVE, reason="go", actor="operator")
    assert gate.allows_new_orders() is True

    await gate.refresh()
    assert gate.allows_new_orders() is True


# --- enforcement at the order gate ---------------------------------------------


async def build_gated_manager(session_factory, gate):
    from config.settings import TradingMode
    from database.order_repository import SqlAlchemyOrderRepository

    class ReadyGateway:
        def __init__(self):
            self.placed = []
            self.cancelled = []

        def is_ready(self):
            return True

        def allocate_order_id(self):
            return 77

        def place_limit_order(self, intent, broker_order_id):
            self.placed.append(intent)
            return broker_order_id

        def cancel_order(self, broker_order_id):
            self.cancelled.append(broker_order_id)

    class AllowRisk:
        async def authorize(self, intent):
            pass

    manager = OrderManager(
        ReadyGateway(),
        SqlAlchemyOrderRepository(session_factory),
        gate=gate,
        trading_mode=TradingMode.PAPER,
        allow_live_trading=False,
        risk_check=AllowRisk(),
    )
    return manager


async def test_a_killed_gate_blocks_submission_before_anything_is_persisted(session_factory):
    from core.execution.orders import LimitOrderIntent

    gate = switch(StubStore(state_at(TradingState.KILLED)), plane=StubPlane(True))
    await gate.refresh()
    manager = await build_gated_manager(session_factory, gate)

    with pytest.raises(TradingHalted):
        await manager.submit(
            LimitOrderIntent(
                symbol="AAPL",
                sec_type="STK",
                exchange="SMART",
                currency="USD",
                side="BUY",
                quantity=1.0,
                limit_price=250.0,
            )
        )

    async with session_factory() as session:
        rows = (await session.execute(Base.metadata.tables["orders"].select())).all()
    assert rows == [], "a blocked submission must not leave a CREATED row behind"
    assert manager.gateway.placed == []


async def test_cancellation_still_works_while_trading_is_killed(session_factory):
    """The gate blocks new business only; reducing exposure must always be allowed."""
    from core.execution.orders import LimitOrderIntent

    gate = switch(StubStore(state_at()), plane=StubPlane(True))
    await gate.refresh()
    manager = await build_gated_manager(session_factory, gate)
    order_uuid = await manager.submit(
        LimitOrderIntent(
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            currency="USD",
            side="BUY",
            quantity=1.0,
            limit_price=250.0,
        )
    )
    await manager.apply_order_update(
        broker_order_id=77, perm_id=5, status_raw="Submitted", filled=0.0, remaining=1.0
    )

    await gate.engage(TradingState.KILLED, reason="kill", actor="operator")
    outcome = await manager.cancel(order_uuid)

    assert outcome == "cancel_requested"
    assert manager.gateway.cancelled == [77]
