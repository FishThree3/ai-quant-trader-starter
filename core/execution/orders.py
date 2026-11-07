from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from uuid import UUID, uuid4

from database.models import OrderStatus

BUY = "BUY"
SELL = "SELL"
SIDES = frozenset({BUY, SELL})

# TWS/Gateway status strings, per the official OrderStatus enum.
_ACTIVE_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "Active", "Worked"}
_CANCELLED_STATUSES = {"Cancelled", "ApiCancelled"}
_PENDING_CANCEL = {"PendingCancel"}
_FILLED_STATUSES = {"Filled"}
_INACTIVE = {"Inactive"}
_EXPIRED = {"Expired"}


@dataclass(frozen=True, slots=True)
class LimitOrderIntent:
    """A LIMIT order to submit. MARKET orders are deliberately unsupported here."""

    symbol: str
    sec_type: str
    exchange: str
    currency: str
    side: str
    quantity: float
    limit_price: float
    time_in_force: str = "DAY"
    order_uuid: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {sorted(SIDES)}")
        if not isfinite(self.quantity) or self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if not isfinite(self.limit_price) or self.limit_price <= 0:
            raise ValueError("limit_price must be positive; MARKET orders are not supported")
        if not self.symbol or not self.sec_type or not self.exchange or not self.currency:
            raise ValueError("symbol, sec_type, exchange and currency are required")


def map_broker_status(
    raw_status: str | None,
    *,
    filled: float = 0.0,
    remaining: float | None = None,
) -> OrderStatus:
    """Translate a TWS status string into the local state machine's vocabulary.

    Quantity wins over wording: a callback that says Submitted while reporting the
    whole size filled is a fill, and trusting the string would understate exposure.
    """
    status = (raw_status or "").strip()

    if status in _FILLED_STATUSES:
        return OrderStatus.FILLED
    if status in _CANCELLED_STATUSES:
        return OrderStatus.CANCELLED
    if status in _PENDING_CANCEL:
        return OrderStatus.CANCEL_PENDING
    if status in _EXPIRED:
        return OrderStatus.EXPIRED

    if remaining is not None and remaining <= 0 and filled > 0:
        return OrderStatus.FILLED

    if status in _ACTIVE_STATUSES:
        return OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.SUBMITTED

    if status in _INACTIVE:
        # TWS uses Inactive for rejects and for post-fill expiries, so the fill
        # count is the only way to tell them apart.
        return OrderStatus.CANCELLED if filled > 0 else OrderStatus.REJECTED

    return OrderStatus.UNKNOWN
