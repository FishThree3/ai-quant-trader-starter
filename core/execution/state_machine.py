from __future__ import annotations

from database.models import OrderStatus

TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)

# Duplicate and out-of-order broker callbacks are normal, so every state may
# "transition" to itself; that keeps handling idempotent.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {OrderStatus.CREATED, OrderStatus.PENDING_SUBMIT, OrderStatus.REJECTED, OrderStatus.UNKNOWN}
    ),
    OrderStatus.PENDING_SUBMIT: frozenset(
        {
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    # UNKNOWN is the recovery state: reconciliation may resolve it either way.
    OrderStatus.UNKNOWN: frozenset(OrderStatus),
}

for _terminal in TERMINAL_STATUSES:
    ALLOWED_TRANSITIONS[_terminal] = frozenset({_terminal})


class InvalidTransition(Exception):
    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(f"Illegal order status transition {current} -> {target}")
        self.current = current
        self.target = target


def is_terminal(status: OrderStatus | str) -> bool:
    return OrderStatus(status) in TERMINAL_STATUSES


def can_transition(current: OrderStatus | str, target: OrderStatus | str) -> bool:
    current_status = OrderStatus(current)
    target_status = OrderStatus(target)
    return target_status in ALLOWED_TRANSITIONS.get(current_status, frozenset())


def ensure_transition(current: OrderStatus | str, target: OrderStatus | str) -> OrderStatus:
    """Gate every status change, including the ones the broker tries to push on us."""
    current_status = OrderStatus(current)
    target_status = OrderStatus(target)

    if not can_transition(current_status, target_status):
        raise InvalidTransition(current_status, target_status)

    return target_status
