from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Uuid,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Instrument(Base, TimestampMixin):
    __tablename__ = "instruments"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    sec_type: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    broker_contract_id: Mapped[int | None] = mapped_column(Integer)
    multiplier: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        UniqueConstraint("symbol", "sec_type", "exchange", "currency", name="uq_instrument"),
    )


class Signal(Base, TimestampMixin):
    __tablename__ = "signals"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    entry_reference: Mapped[float | None] = mapped_column(Float)
    stop_reference: Mapped[float | None] = mapped_column(Float)
    take_profit_reference: Mapped[float | None] = mapped_column(Float)
    reason_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class RiskDecision(Base, TimestampMixin):
    __tablename__ = "risk_decisions"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    signal_id: Mapped[UUID | None] = mapped_column(ForeignKey("signals.id"))
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rule: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    risk_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    broker_order_id: Mapped[int | None] = mapped_column(Integer)
    broker_perm_id: Mapped[int | None] = mapped_column(Integer)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    signal_id: Mapped[UUID | None] = mapped_column(ForeignKey("signals.id"))
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    time_in_force: Mapped[str] = mapped_column(
        String(16), default="DAY", server_default="DAY", nullable=False
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default=OrderStatus.CREATED, nullable=False)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    avg_fill_price: Mapped[float | None] = mapped_column(Float)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_orders_broker_order_id", "broker_order_id"),
        Index("ix_orders_status", "status"),
        UniqueConstraint("broker_perm_id", name="uq_orders_broker_perm_id"),
    )


class Execution(Base, TimestampMixin):
    __tablename__ = "executions"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    broker_exec_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    commission: Mapped[float | None] = mapped_column(Float)
    exchange: Mapped[str | None] = mapped_column(String(32))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Position(Base):
    __tablename__ = "positions"
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    average_cost: Mapped[float] = mapped_column(Float, nullable=False)
    market_price: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BrokerEvent(Base, TimestampMixin):
    __tablename__ = "broker_events"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str | None] = mapped_column(String(32))
    code: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(String(1024))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(64))
    object_id: Mapped[str | None] = mapped_column(String(128))
    before_json: Mapped[dict | None] = mapped_column(JSON)
    after_json: Mapped[dict | None] = mapped_column(JSON)


class SystemControl(Base):
    """Single-row durable record of the trading gate.

    ``id`` is a fixed constant so the row is an upsert target rather than a history table:
    the history belongs in audit_logs, and readers must never have to guess which row wins.
    """

    __tablename__ = "system_controls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
