from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventType(StrEnum):
    BROKER_CONNECTING = "broker.connecting"
    BROKER_READY = "broker.ready"
    BROKER_DISCONNECTED = "broker.disconnected"
    BROKER_DEGRADED = "broker.degraded"
    MARKET_TICK = "market.tick"
    SIGNAL_CREATED = "signal.created"
    RISK_APPROVED = "risk.approved"
    RISK_REJECTED = "risk.rejected"
    ORDER_CREATED = "order.created"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_UPDATED = "order.updated"
    EXECUTION_RECEIVED = "execution.received"
    POSITION_UPDATED = "position.updated"
    KILL_SWITCH_TRIGGERED = "risk.kill_switch"


@dataclass(frozen=True, slots=True)
class DomainEvent:
    type: EventType
    payload: dict[str, Any]
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=utcnow)
