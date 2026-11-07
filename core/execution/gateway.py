from __future__ import annotations

from typing import Protocol

from core.execution.orders import LimitOrderIntent


class OrderGateway(Protocol):
    """The slice of the broker the order manager is allowed to touch."""

    def is_ready(self) -> bool:
        """True only when the broker has been identified and reconciled."""
        ...

    def allocate_order_id(self) -> int:
        """Reserve a one-use broker ID without transmitting anything."""
        ...

    def place_limit_order(self, intent: LimitOrderIntent, broker_order_id: int) -> int:
        """Transmit the order and return the broker-assigned order id.

        Success here means only that the request reached the socket; it never
        implies acceptance or a fill.
        """
        ...

    def cancel_order(self, broker_order_id: int) -> None:
        """Request cancellation; the outcome arrives via callbacks."""
        ...


class PreTradeCheck(Protocol):
    async def authorize(self, intent: LimitOrderIntent) -> None:
        """Raise unless fresh risk context authorizes this exact intent."""
        ...
