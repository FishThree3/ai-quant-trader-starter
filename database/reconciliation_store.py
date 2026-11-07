from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Execution, Instrument, Order, Position
from core.reconciliation.models import (
    BrokerSnapshot,
    InstrumentKey,
    LocalExecution,
    LocalOrder,
    LocalPosition,
    LocalSnapshot,
)


@dataclass(frozen=True, slots=True)
class PersistReport:
    executions_inserted: int = 0
    executions_deduplicated: int = 0
    executions_unmatched: int = 0

    def summary(self) -> dict:
        return {
            "executions_inserted": self.executions_inserted,
            "executions_deduplicated": self.executions_deduplicated,
            "executions_unmatched": self.executions_unmatched,
        }


def _key(instrument: Instrument) -> InstrumentKey:
    return InstrumentKey.of(instrument.symbol, instrument.sec_type, instrument.currency)


async def load_local_snapshot(session: AsyncSession) -> LocalSnapshot:
    """Read local records as reconciliation inputs, keyed the way IBKR keys them."""
    order_rows = (
        await session.execute(
            select(Order, Instrument).join(Instrument, Order.instrument_id == Instrument.id)
        )
    ).all()
    position_rows = (
        await session.execute(
            select(Position, Instrument).join(Instrument, Position.instrument_id == Instrument.id)
        )
    ).all()
    execution_rows = (
        await session.execute(select(Execution, Order).join(Order, Execution.order_id == Order.id))
    ).all()

    return LocalSnapshot(
        orders=tuple(
            LocalOrder(
                ref=str(order.id),
                perm_id=order.broker_perm_id,
                broker_order_id=order.broker_order_id,
                instrument=_key(instrument),
                side=order.side,
                quantity=order.quantity,
                filled_quantity=order.filled_quantity,
                status=order.status,
            )
            for order, instrument in order_rows
        ),
        positions=tuple(
            LocalPosition(
                ref=str(instrument.id),
                instrument=_key(instrument),
                quantity=position.quantity,
                average_cost=position.average_cost,
            )
            for position, instrument in position_rows
        ),
        executions=tuple(
            LocalExecution(
                ref=str(execution.id),
                exec_id=execution.broker_exec_id,
                perm_id=order.broker_perm_id,
                quantity=execution.quantity,
                price=execution.price,
            )
            for execution, order in execution_rows
        ),
    )


async def persist_broker_snapshot(session: AsyncSession, snapshot: BrokerSnapshot) -> PersistReport:
    """Idempotently record broker executions, keyed by IBKR execId.

    Orders are never created here: a broker-only order is an unexplained state that
    reconciliation must surface, not something to paper over with a synthetic row.
    """
    existing = {
        row.broker_exec_id: row
        for row in (await session.execute(select(Execution))).scalars().all()
    }
    orders_by_perm = {
        order.broker_perm_id: order.id
        for order in (await session.execute(select(Order.id, Order.broker_perm_id))).all()
        if order.broker_perm_id is not None
    }

    inserted = deduplicated = unmatched = 0

    for execution in snapshot.executions:
        if (
            not execution.exec_id
            or not isfinite(execution.quantity)
            or execution.quantity <= 0
            or not isfinite(execution.price)
            or execution.price <= 0
            or (execution.account is not None and execution.account != snapshot.account)
        ):
            unmatched += 1
            continue
        order_id = orders_by_perm.get(execution.perm_id) if execution.perm_id is not None else None
        if execution.exec_id in existing:
            prior = existing[execution.exec_id]
            if (
                prior.order_id != order_id
                or prior.quantity != execution.quantity
                or prior.price != execution.price
            ):
                raise ValueError("Conflicting execution replay for an existing execId")
            deduplicated += 1
            continue

        if order_id is None or execution.executed_at is None:
            # A fill we cannot attribute to a known order, or cannot timestamp, is
            # left unpersisted so reconciliation reports it instead of hiding it.
            unmatched += 1
            continue

        row = Execution(
            broker_exec_id=execution.exec_id,
            order_id=order_id,
            quantity=execution.quantity,
            price=execution.price,
            exchange=execution.exchange,
            executed_at=execution.executed_at,
        )
        session.add(row)
        existing[execution.exec_id] = row
        inserted += 1

    await session.commit()
    return PersistReport(
        executions_inserted=inserted,
        executions_deduplicated=deduplicated,
        executions_unmatched=unmatched,
    )
