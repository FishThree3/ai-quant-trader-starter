from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import IntegrityError

from core.execution.orders import LimitOrderIntent
from core.execution.repository import OrderRecord
from database.models import Execution, Instrument, Order, AuditLog


class SqlAlchemyOrderRepository:
    """OrderRepository over the ORM session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def ensure_instrument(self, intent: LimitOrderIntent) -> str:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Instrument).where(
                        Instrument.symbol == intent.symbol,
                        Instrument.sec_type == intent.sec_type,
                        Instrument.exchange == intent.exchange,
                        Instrument.currency == intent.currency,
                    )
                )
            ).scalar_one_or_none()

            if row is None:
                row = Instrument(
                    symbol=intent.symbol,
                    sec_type=intent.sec_type,
                    exchange=intent.exchange,
                    currency=intent.currency,
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)

            return str(row.id)

    async def insert_created_order(self, intent: LimitOrderIntent, instrument_id: str) -> None:
        async with self.session_factory() as session:
            session.add(
                Order(
                    id=intent.order_uuid,
                    instrument_id=UUID(instrument_id),
                    side=intent.side,
                    order_type="LMT",
                    quantity=intent.quantity,
                    limit_price=intent.limit_price,
                    time_in_force=intent.time_in_force,
                    status="CREATED",
                    filled_quantity=0.0,
                )
            )
            await session.commit()

    async def get_order(self, order_uuid: UUID) -> OrderRecord | None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Order, Instrument)
                    .join(Instrument, Order.instrument_id == Instrument.id)
                    .where(Order.id == order_uuid)
                )
            ).one_or_none()
            return _to_record(row) if row else None

    async def find_by_broker_order_id(self, broker_order_id: int) -> OrderRecord | None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Order, Instrument)
                    .join(Instrument, Order.instrument_id == Instrument.id)
                    .where(Order.broker_order_id == broker_order_id)
                )
            ).one_or_none()
            return _to_record(row) if row else None

    async def find_by_perm_id(self, perm_id: int) -> OrderRecord | None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Order, Instrument)
                    .join(Instrument, Order.instrument_id == Instrument.id)
                    .where(Order.broker_perm_id == perm_id)
                )
            ).one_or_none()
            return _to_record(row) if row else None

    async def update_order(self, order_uuid: UUID, **fields: Any) -> None:
        async with self.session_factory() as session:
            order = await session.get(Order, order_uuid)
            if order is None:
                return
            for key, value in fields.items():
                setattr(order, key, value)
            await session.commit()

    async def insert_execution(
        self,
        *,
        order_uuid: UUID,
        exec_id: str,
        quantity: float,
        price: float,
        exchange: str | None,
        executed_at: datetime,
    ) -> bool:
        async with self.session_factory() as session:
            duplicate = (
                await session.execute(select(Execution).where(Execution.broker_exec_id == exec_id))
            ).scalar_one_or_none()
            if duplicate is not None:
                _validate_execution_replay(duplicate, order_uuid, quantity, price)
                return False
            session.add(
                Execution(
                    broker_exec_id=exec_id,
                    order_id=order_uuid,
                    quantity=quantity,
                    price=price,
                    exchange=exchange,
                    executed_at=executed_at,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                duplicate = (
                    await session.execute(
                        select(Execution).where(Execution.broker_exec_id == exec_id)
                    )
                ).scalar_one_or_none()
                if duplicate is None:
                    raise
                _validate_execution_replay(duplicate, order_uuid, quantity, price)
                return False
            return True

    async def executions_for(self, order_uuid: UUID) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = (
                (await session.execute(select(Execution).where(Execution.order_id == order_uuid)))
                .scalars()
                .all()
            )
            return [
                {"exec_id": r.broker_exec_id, "quantity": r.quantity, "price": r.price}
                for r in rows
            ]

    async def append_audit(
        self,
        *,
        actor: str,
        action: str,
        object_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                AuditLog(
                    actor=actor,
                    action=action,
                    object_type="order",
                    object_id=object_id,
                    before_json=before,
                    after_json=after,
                )
            )
            await session.commit()


def _validate_execution_replay(
    row: Execution, order_uuid: UUID, quantity: float, price: float
) -> None:
    if row.order_id != order_uuid or row.quantity != quantity or row.price != price:
        raise ValueError("Conflicting execution replay for an existing execId")


def _to_record(row) -> OrderRecord:
    order, instrument = row
    return OrderRecord(
        order_uuid=order.id,
        instrument_id=str(order.instrument_id),
        symbol=instrument.symbol,
        side=order.side,
        quantity=order.quantity,
        limit_price=order.limit_price,
        status=order.status,
        filled_quantity=order.filled_quantity,
        avg_fill_price=order.avg_fill_price,
        broker_order_id=order.broker_order_id,
        broker_perm_id=order.broker_perm_id,
        submitted_at=order.submitted_at,
        completed_at=order.completed_at,
        sec_type=instrument.sec_type,
        exchange=instrument.exchange,
        currency=instrument.currency,
        time_in_force=order.time_in_force,
    )
