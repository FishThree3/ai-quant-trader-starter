from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from core.reconciliation.engine import reconcile
from core.reconciliation.models import (
    BrokerExecution,
    BrokerOrder,
    BrokerSnapshot,
    FindingCode,
    InstrumentKey,
)
from database.models import Base, Execution, Instrument, Order
from database.reconciliation_store import load_local_snapshot, persist_broker_snapshot

AAPL = InstrumentKey.of("AAPL", "STK", "USD")
EXEC_TIME = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        s.add(Instrument(id=_uuid(1), symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD"))
        await s.commit()
        yield s
    await engine.dispose()


def _uuid(n: int):
    from uuid import UUID

    return UUID(int=n)


async def seed_order(session, *, perm_id=1001, broker_order_id=17, status="SUBMITTED", filled=0.0):
    order = Order(
        id=_uuid(50),
        instrument_id=_uuid(1),
        broker_perm_id=perm_id,
        broker_order_id=broker_order_id,
        side="BUY",
        order_type="LMT",
        quantity=10.0,
        limit_price=250.0,
        status=status,
        filled_quantity=filled,
    )
    session.add(order)
    await session.commit()
    return order


def snapshot_with_execution(exec_id="EX-1", perm_id=1001, quantity=4.0):
    return BrokerSnapshot(
        account="DUR123456",
        orders=(BrokerOrder(perm_id, 17, AAPL, "BOT", 10.0, quantity, "Submitted"),),
        positions=(),
        executions=(BrokerExecution(exec_id, perm_id, AAPL, "BOT", quantity, 250.0, "ISLAND", EXEC_TIME),),
        orders_complete=True,
        positions_complete=True,
        executions_complete=True,
    )


async def test_execution_is_persisted_once_per_exec_id(session):
    await seed_order(session)
    snapshot = snapshot_with_execution()

    first = await persist_broker_snapshot(session, snapshot)
    second = await persist_broker_snapshot(session, snapshot)

    assert first.executions_inserted == 1
    assert second.executions_inserted == 0
    assert second.executions_deduplicated == 1

    rows = (await session.execute(select_exec_count())).scalar_one()
    assert rows == 1


async def test_duplicate_exec_id_cannot_create_duplicate_rows(session):
    """The unique constraint is the last line of defence against redelivered fills."""
    await seed_order(session)
    snapshot = BrokerSnapshot(
        account="DUR123456",
        orders=(BrokerOrder(1001, 17, AAPL, "BOT", 10.0, 4.0, "Submitted"),),
        executions=(
            BrokerExecution("EX-1", 1001, AAPL, "BOT", 4.0, 250.0, "ISLAND", EXEC_TIME),
            BrokerExecution("EX-1", 1001, AAPL, "BOT", 4.0, 250.0, "ISLAND", EXEC_TIME),
        ),
        orders_complete=True,
        positions_complete=True,
        executions_complete=True,
    )

    report = await persist_broker_snapshot(session, snapshot)

    assert report.executions_inserted == 1
    assert report.executions_deduplicated == 1
    assert (await session.execute(select_exec_count())).scalar_one() == 1


async def test_execution_without_local_order_is_not_persisted(session):
    """No local order means we cannot attribute the fill; inventing one is forbidden."""
    report = await persist_broker_snapshot(session, snapshot_with_execution(perm_id=999))

    assert report.executions_unmatched == 1
    assert report.executions_inserted == 0
    assert (await session.execute(select_exec_count())).scalar_one() == 0


async def test_load_local_snapshot_reflects_persisted_rows(session):
    await seed_order(session, status="PARTIALLY_FILLED", filled=4.0)
    await persist_broker_snapshot(session, snapshot_with_execution(quantity=4.0))

    local = await load_local_snapshot(session)

    assert local.orders[0].perm_id == 1001
    assert local.orders[0].instrument == AAPL
    assert local.executions[0].exec_id == "EX-1"
    assert local.executions[0].perm_id == 1001


async def test_restart_after_persist_reconciles_clean(session):
    """The integration case: restart with an existing broker order and partial fill."""
    await seed_order(session, status="PARTIALLY_FILLED", filled=4.0)
    snapshot = snapshot_with_execution(quantity=4.0)

    await persist_broker_snapshot(session, snapshot)
    local = await load_local_snapshot(session)
    result = reconcile(snapshot, local)

    assert result.ok, result.summary()
    assert result.executions_matched == 1


async def test_unpersisted_broker_fill_blocks_until_repaired(session):
    await seed_order(session, status="PARTIALLY_FILLED", filled=4.0)
    snapshot = snapshot_with_execution(quantity=4.0)
    local = await load_local_snapshot(session)

    result = reconcile(snapshot, local)

    codes = [f.code for f in result.blocking]
    assert FindingCode.EXECUTION_NOT_PERSISTED in codes


def select_exec_count():
    from sqlalchemy import func, select

    return select(func.count()).select_from(Execution)
