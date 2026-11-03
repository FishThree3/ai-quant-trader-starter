from __future__ import annotations

import structlog
from uuid import UUID

from config.settings import TradingMode
from core.events.models import DomainEvent, EventType, utcnow
from core.execution.gateway import OrderGateway
from core.execution.orders import LimitOrderIntent, map_broker_status
from core.execution.repository import OrderRecord, OrderRepository
from core.execution.state_machine import InvalidTransition, ensure_transition, is_terminal
from core.reconciliation.models import BrokerExecution
from database.models import OrderStatus

log = structlog.get_logger()

# IBKR order-related error codes with a state meaning of their own.
ORDER_FAILED = 201
ORDER_CANCELLED = 202
CANCEL_REQUEST_FAILED = 203

_LOCAL_ONLY_CANCELLABLE = {OrderStatus.CREATED, OrderStatus.PENDING_SUBMIT}


class LiveTradingRefused(RuntimeError):
    pass


class BrokerNotReady(RuntimeError):
    pass


class OrderNotFoundError(LookupError):
    pass


class OrderManager:
    """Paper-only LIMIT order submission and lifecycle tracking.

    Nothing here trusts a call's return value as evidence of execution: acceptance,
    fills and cancels are only ever learned from broker callbacks, and any state the
    callbacks cannot explain is recorded as UNKNOWN, which reconciliation then blocks
    trading on.
    """

    def __init__(
        self,
        gateway: OrderGateway,
        repository: OrderRepository,
        *,
        trading_mode: TradingMode,
        allow_live_trading: bool,
        event_bus=None,
    ) -> None:
        if trading_mode is TradingMode.LIVE and not allow_live_trading:
            raise LiveTradingRefused("OrderManager refuses LIVE mode while disabled")

        self.gateway = gateway
        self.repository = repository
        self.trading_mode = trading_mode
        self.event_bus = event_bus

    async def submit(self, intent: LimitOrderIntent) -> UUID:
        """Persist CREATED, then transmit. A broker failure leaves the order UNKNOWN."""
        # Checked again per submission, not just at construction: a live flag flipped
        # at runtime must not be able to reach the socket.
        if self.trading_mode is TradingMode.LIVE:
            raise LiveTradingRefused("Order submission is not permitted in LIVE mode")
        if not self.gateway.is_ready():
            raise BrokerNotReady("Broker is not READY; refusing to allocate an order id")

        instrument_id = await self.repository.ensure_instrument(intent)
        await self.repository.insert_created_order(intent, instrument_id)
        await self._audit("order.created", str(intent.order_uuid), None, self._intent_view(intent))
        await self._publish(EventType.ORDER_CREATED, {"order_uuid": str(intent.order_uuid)})

        try:
            broker_order_id = self.gateway.place_limit_order(intent)
        except Exception as exc:
            # The request may or may not have left the process, so claim ignorance
            # rather than marking it rejected or submitted.
            await self.repository.update_order(intent.order_uuid, status=OrderStatus.UNKNOWN)
            await self._audit(
                "order.transmission_unknown", str(intent.order_uuid), None, {"error": str(exc)}
            )
            log.error("order_transmission_unknown", order=str(intent.order_uuid), error=str(exc))
            raise

        await self.repository.update_order(
            intent.order_uuid,
            broker_order_id=broker_order_id,
            status=OrderStatus.PENDING_SUBMIT,
            submitted_at=utcnow(),
        )
        await self._audit(
            "order.submitted",
            str(intent.order_uuid),
            {"status": OrderStatus.CREATED},
            {"status": OrderStatus.PENDING_SUBMIT, "broker_order_id": broker_order_id},
        )
        log.info("order_submitted", order=str(intent.order_uuid), broker_order_id=broker_order_id)
        return intent.order_uuid

    async def cancel(self, order_uuid: UUID) -> str:
        """Idempotent cancellation: repeating it never re-transmits."""
        order = await self.repository.get_order(order_uuid)
        if order is None:
            raise OrderNotFoundError(str(order_uuid))

        status = OrderStatus(order.status)

        if status is OrderStatus.CANCELLED:
            return "already_cancelled"
        if status is OrderStatus.CANCEL_PENDING:
            # A cancel is already in flight; re-transmitting would race the broker.
            return "cancel_pending"
        if is_terminal(status):
            return "not_cancellable"
        if status in _LOCAL_ONLY_CANCELLABLE and order.broker_order_id is None:
            await self._transition(order, OrderStatus.CANCELLED, reason="cancelled before send")
            return "cancelled_locally"
        if order.broker_order_id is None:
            return "not_transmitted"

        await self._transition(order, OrderStatus.CANCEL_PENDING, reason="cancel requested")
        self.gateway.cancel_order(order.broker_order_id)
        return "cancel_requested"

    async def apply_order_update(
        self,
        *,
        broker_order_id: int | None,
        perm_id: int | None,
        status_raw: str | None,
        filled: float,
        remaining: float | None = None,
    ) -> OrderRecord | None:
        """Fold a broker order callback into the local record.

        A callback for an order we have no record of is deliberately not adopted;
        reconciliation reports broker-only orders instead of the manager inventing
        local ones.
        """
        order = await self._locate(broker_order_id, perm_id)
        if order is None:
            log.warning(
                "order_update_for_unknown_order",
                broker_order_id=broker_order_id,
                perm_id=perm_id,
                status=status_raw,
            )
            return None

        target = map_broker_status(status_raw, filled=filled, remaining=remaining)
        fields: dict[str, object] = {
            "filled_quantity": filled,
            "status": target,
        }
        if perm_id is not None:
            fields["broker_perm_id"] = perm_id
        if broker_order_id is not None:
            fields["broker_order_id"] = broker_order_id
        if is_terminal(target):
            fields["completed_at"] = utcnow()

        try:
            ensure_transition(order.status, target)
        except InvalidTransition as exc:
            # An update we cannot explain is never applied as-is.
            log.error("order_update_rejected", order=str(order.order_uuid), error=str(exc))
            await self.repository.update_order(order.order_uuid, status=OrderStatus.UNKNOWN)
            await self._audit(
                "order.update_rejected",
                str(order.order_uuid),
                {"status": order.status},
                {"attempted": str(target), "error": str(exc)},
            )
            return await self.repository.get_order(order.order_uuid)

        await self.repository.update_order(order.order_uuid, **fields)
        updated = await self.repository.get_order(order.order_uuid)
        await self._audit(
            "order.updated",
            str(order.order_uuid),
            {"status": order.status},
            {"status": str(target), "filled": filled},
        )
        await self._publish(
            EventType.ORDER_SUBMITTED if target is OrderStatus.SUBMITTED else EventType.ORDER_UPDATED,
            {"order_uuid": str(order.order_uuid), "status": str(target)},
        )
        return updated

    async def apply_execution(self, execution: BrokerExecution) -> bool:
        """Record a fill exactly once per IBKR execId."""
        order = await self._locate(None, execution.perm_id)
        if order is None:
            log.warning(
                "execution_for_unknown_order",
                exec_id=execution.exec_id,
                perm_id=execution.perm_id,
            )
            return False

        inserted = await self.repository.insert_execution(
            order_uuid=order.order_uuid,
            exec_id=execution.exec_id,
            quantity=execution.quantity,
            price=execution.price,
            exchange=execution.exchange,
            executed_at=execution.executed_at or utcnow(),
        )
        if not inserted:
            log.info("execution_duplicate_ignored", exec_id=execution.exec_id)
            return False

        await self._recalculate_average_fill(order)
        await self._audit(
            "execution.recorded",
            str(order.order_uuid),
            None,
            {"exec_id": execution.exec_id, "quantity": execution.quantity},
        )
        await self._publish(
            EventType.EXECUTION_RECEIVED,
            {"order_uuid": str(order.order_uuid), "exec_id": execution.exec_id},
        )
        return True

    async def apply_error(self, broker_order_id: int | None, code: int, message: str) -> None:
        """Apply the three order-related IBKR codes that carry state meaning."""
        if broker_order_id is None or broker_order_id < 0:
            return

        order = await self._locate(broker_order_id, None)
        if order is None:
            return

        if code == ORDER_FAILED:
            await self._transition(
                order, OrderStatus.REJECTED, reason=f"IBKR {code}: {message}"
            )
        elif code == ORDER_CANCELLED:
            await self._transition(order, OrderStatus.CANCELLED, reason=f"IBKR {code}: {message}")
        elif code == CANCEL_REQUEST_FAILED:
            # The order is still working but its true state is now uncertain.
            await self._transition(order, OrderStatus.UNKNOWN, reason=f"IBKR {code}: {message}")

    async def _transition(self, order: OrderRecord, target: OrderStatus, *, reason: str) -> None:
        try:
            ensure_transition(order.status, target)
        except InvalidTransition as exc:
            log.error("transition_refused", order=str(order.order_uuid), error=str(exc))
            return

        fields: dict[str, object] = {"status": target}
        if is_terminal(target):
            fields["completed_at"] = utcnow()

        await self.repository.update_order(order.order_uuid, **fields)
        await self._audit(
            f"order.{str(target).lower()}",
            str(order.order_uuid),
            {"status": order.status},
            {"status": str(target), "reason": reason},
        )

    async def _recalculate_average_fill(self, order: OrderRecord) -> None:
        rows = await self.repository.executions_for(order.order_uuid)
        quantity = sum(float(r["quantity"]) for r in rows)
        if quantity <= 0:
            return

        notional = sum(float(r["quantity"]) * float(r["price"]) for r in rows)
        await self.repository.update_order(order.order_uuid, filled_quantity=quantity, avg_fill_price=notional / quantity)

    async def _locate(self, broker_order_id: int | None, perm_id: int | None) -> OrderRecord | None:
        if broker_order_id is not None:
            found = await self.repository.find_by_broker_order_id(broker_order_id)
            if found is not None:
                return found
        if perm_id is not None:
            return await self.repository.find_by_perm_id(perm_id)
        return None

    async def _audit(self, action: str, object_id: str, before, after) -> None:
        await self.repository.append_audit(
            actor="order_manager",
            action=action,
            object_id=object_id,
            before=before,
            after=after,
        )

    async def _publish(self, event_type: EventType, payload: dict) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.publish(DomainEvent(type=event_type, payload=payload))

    @staticmethod
    def _intent_view(intent: LimitOrderIntent) -> dict:
        return {
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "order_type": "LMT",
        }
