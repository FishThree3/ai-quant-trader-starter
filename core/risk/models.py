from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class RiskDecisionType(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class TradeIntent:
    symbol: str
    side: str
    quantity: float
    reference_price: float
    stop_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    intent_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision: RiskDecisionType
    reason: str
    rule: str
