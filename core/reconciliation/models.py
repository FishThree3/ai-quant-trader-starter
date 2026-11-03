from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class FindingSeverity(StrEnum):
    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"


class FindingCode(StrEnum):
    BROKER_ONLY_ORDER = "BROKER_ONLY_ORDER"
    UNMATCHABLE_BROKER_ORDER = "UNMATCHABLE_BROKER_ORDER"
    LOCAL_ONLY_OPEN_ORDER = "LOCAL_ONLY_OPEN_ORDER"
    LOCAL_ORDER_UNRESOLVED = "LOCAL_ORDER_UNRESOLVED"
    ORDER_SIDE_MISMATCH = "ORDER_SIDE_MISMATCH"
    ORDER_QUANTITY_MISMATCH = "ORDER_QUANTITY_MISMATCH"
    FILL_QUANTITY_MISMATCH = "FILL_QUANTITY_MISMATCH"
    ORDER_STATUS_CONFLICT = "ORDER_STATUS_CONFLICT"
    EXECUTION_NOT_PERSISTED = "EXECUTION_NOT_PERSISTED"
    ORPHAN_EXECUTION = "ORPHAN_EXECUTION"
    POSITION_QUANTITY_MISMATCH = "POSITION_QUANTITY_MISMATCH"
    POSITION_COST_MISMATCH = "POSITION_COST_MISMATCH"
    RECONCILIATION_INCOMPLETE = "RECONCILIATION_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class InstrumentKey:
    """Instrument identity used to match broker and local records.

    Exchange is deliberately excluded: IBKR reports the exchange a request was
    routed through (often SMART) on orders, but the executing venue on fills, so
    it cannot serve as a join key.
    """

    symbol: str
    sec_type: str
    currency: str

    @classmethod
    def of(cls, symbol: str, sec_type: str, currency: str) -> InstrumentKey:
        return cls(
            symbol=(symbol or "").strip().upper(),
            sec_type=(sec_type or "").strip().upper(),
            currency=(currency or "").strip().upper(),
        )


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    perm_id: int | None
    broker_order_id: int | None
    instrument: InstrumentKey
    side: str
    quantity: float
    filled: float
    status: str
    account: str | None = None
    limit_price: float | None = None


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    instrument: InstrumentKey
    quantity: float
    average_cost: float
    account: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerExecution:
    exec_id: str
    perm_id: int | None
    instrument: InstrumentKey
    side: str
    quantity: float
    price: float
    exchange: str | None = None
    executed_at: datetime | None = None
    account: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    """Everything the broker reported for one reconciliation pass.

    The completion flags are what let callers distinguish "the broker said there
    is nothing" from "the broker never answered", which must never look like a
    clean reconciliation.
    """

    account: str | None
    orders: tuple[BrokerOrder, ...] = ()
    positions: tuple[BrokerPosition, ...] = ()
    executions: tuple[BrokerExecution, ...] = ()
    orders_complete: bool = False
    positions_complete: bool = False
    executions_complete: bool = False

    @property
    def is_complete(self) -> bool:
        return self.orders_complete and self.positions_complete and self.executions_complete

    @property
    def missing_barriers(self) -> tuple[str, ...]:
        missing = []
        if not self.orders_complete:
            missing.append("open_orders")
        if not self.positions_complete:
            missing.append("positions")
        if not self.executions_complete:
            missing.append("executions")
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class LocalOrder:
    ref: str
    perm_id: int | None
    broker_order_id: int | None
    instrument: InstrumentKey
    side: str
    quantity: float
    filled_quantity: float
    status: str


@dataclass(frozen=True, slots=True)
class LocalPosition:
    ref: str
    instrument: InstrumentKey
    quantity: float
    average_cost: float


@dataclass(frozen=True, slots=True)
class LocalExecution:
    ref: str
    exec_id: str
    perm_id: int | None


@dataclass(frozen=True, slots=True)
class LocalSnapshot:
    orders: tuple[LocalOrder, ...] = ()
    positions: tuple[LocalPosition, ...] = ()
    executions: tuple[LocalExecution, ...] = ()


@dataclass(frozen=True, slots=True)
class Finding:
    code: FindingCode
    message: str
    severity: FindingSeverity = FindingSeverity.BLOCKING
    details: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    findings: tuple[Finding, ...] = ()
    orders_matched: int = 0
    positions_matched: int = 0
    executions_matched: int = 0

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == FindingSeverity.BLOCKING)

    @property
    def advisory(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == FindingSeverity.ADVISORY)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def summary(self) -> dict:
        return {
            "ok": self.ok,
            "blocking": [f.code for f in self.blocking],
            "advisory": [f.code for f in self.advisory],
            "orders_matched": self.orders_matched,
            "positions_matched": self.positions_matched,
            "executions_matched": self.executions_matched,
        }
