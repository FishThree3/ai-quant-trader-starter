from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from core.execution.orders import LimitOrderIntent


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order_uuid: UUID
    instrument_id: str
    symbol: str
    side: str
    quantity: float
    limit_price: float | None
    status: str
    filled_quantity: float
    avg_fill_price: float | None
    broker_order_id: int | None
    broker_perm_id: int | None
    submitted_at: datetime | None
    completed_at: datetime | None


class OrderRepository(Protocol):
    """Persistence the order manager needs, kept free of ORM types.

    The broker adapter may push updates at any moment, so lookups are by both
    identity forms: the local UUID and the ids TWS assigns.
    """

    async def ensure_instrument(self, intent: LimitOrderIntent) -> str: ...

    async def insert_created_order(self, intent: LimitOrderIntent, instrument_id: str) -> None: ...

    async def get_order(self, order_uuid: UUID) -> OrderRecord | None: ...

    async def find_by_broker_order_id(self, broker_order_id: int) -> OrderRecord | None: ...

    async def find_by_perm_id(self, perm_id: int) -> OrderRecord | None: ...

    async def update_order(self, order_uuid: UUID, **fields: Any) -> None: ...

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
        """Return False when the execId was already recorded."""
        ...

    async def executions_for(self, order_uuid: UUID) -> list[dict[str, Any]]: ...

    async def append_audit(
        self,
        *,
        actor: str,
        action: str,
        object_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None: ...
