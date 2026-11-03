from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

from core.broker.base import BrokerState, BrokerStatus
from core.broker.ibkr.snapshot import (
    _num,
    _optional_int,
    broker_execution,
    broker_order,
    broker_position,
)
from core.reconciliation.engine import reconcile
from core.reconciliation.models import (
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    LocalSnapshot,
    ReconciliationResult,
)

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper

    try:
        from ibapi.execution import ExecutionFilter
        from ibapi.order_cancel import OrderCancel
    except ImportError:
        ExecutionFilter = None  # type: ignore[assignment,misc]
        OrderCancel = None  # type: ignore[assignment,misc]
except ImportError:  # Allows API/tests to run before official IBKR API is installed.
    EClient = object  # type: ignore[assignment,misc]
    EWrapper = object  # type: ignore[assignment,misc]
    ExecutionFilter = None  # type: ignore[assignment,misc]
    OrderCancel = None  # type: ignore[assignment,misc]

try:
    from ibapi.contract import Contract
    from ibapi.order import Order as IBKRWireOrder
except ImportError:
    Contract = None  # type: ignore[assignment,misc]
    IBKRWireOrder = None  # type: ignore[assignment,misc]

EXECUTIONS_REQ_ID = 5001


@dataclass(slots=True)
class IBKRConnectionConfig:
    host: str
    port: int
    client_id: int
    timeout_seconds: int = 15


class IBKRApp(EWrapper, EClient):  # type: ignore[misc]
    """Thin official-API adapter. No strategy/risk logic belongs here."""

    def __init__(self, owner: "IBKRConnectionManager") -> None:
        if EClient is object:
            raise RuntimeError(
                "Official IBKR TWS API Python package (ibapi) is not installed. "
                "Install it from the official TWS API download."
            )
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.owner = owner

    def nextValidId(self, orderId: int):  # noqa: N802 - IBKR callback name
        self.owner._on_next_valid_id(orderId)

    def managedAccounts(self, accountsList: str):  # noqa: N802
        accounts = [x.strip() for x in accountsList.split(",") if x.strip()]
        self.owner._on_managed_accounts(accounts)

    def openOrder(self, orderId, contract, order, orderState):  # noqa: N802
        self.owner._on_open_order(orderId, contract, order, orderState)

    def openOrderEnd(self):  # noqa: N802
        self.owner._on_open_order_end()

    def orderStatus(  # noqa: N802
        self,
        orderId,
        status,
        filled,
        remaining,
        avgFillPrice,
        permId,
        parentId,
        lastFillPrice,
        clientId,
        whyHeld,
        mktCapPrice,
    ):
        self.owner._on_order_status(
            orderId=orderId,
            status=status,
            filled=filled,
            remaining=remaining,
            perm_id=permId,
            avg_fill_price=avgFillPrice,
        )

    def position(self, account, contract, position, avgCost):  # noqa: N802
        self.owner._on_position(account, contract, position, avgCost)

    def positionEnd(self):  # noqa: N802
        self.owner._on_position_end()

    def execDetails(self, reqId, contract, execution):  # noqa: N802
        self.owner._on_exec_details(reqId, contract, execution)

    def execDetailsEnd(self, reqId):  # noqa: N802
        self.owner._on_exec_details_end(reqId)

    def connectionClosed(self):  # noqa: N802
        self.owner._on_connection_closed()

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderReject=""):  # noqa: N802
        self.owner._on_error(
            req_id=reqId,
            code=int(errorCode),
            message=str(errorString),
            reject=str(advancedOrderReject or ""),
        )


class IBKRConnectionManager:
    """
    Connection lifecycle gate.

    The socket is only useful once IBKR delivers nextValidId, and the account is
    only trustworthy once a reconciliation pass agrees with local records, so
    READY is granted by sync_state() rather than by the handshake alone.
    """

    CONNECTIVITY_LOST_CODES = {1100, 1300, 2110}
    CONNECTIVITY_RESTORED_CODES = {1101, 1102}
    INFORMATIONAL_CODES = {2104, 2106, 2107, 2108, 2158}

    def __init__(self, config: IBKRConnectionConfig) -> None:
        self.config = config
        self.state = BrokerState.DISCONNECTED
        self.account: str | None = None
        self.next_order_id: int | None = None
        self.last_error: str | None = None
        self.last_reconciliation: ReconciliationResult | None = None
        self._app: IBKRApp | None = None
        self._thread: threading.Thread | None = None
        self._identified_event = threading.Event()
        self._order_id_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._orders: list[BrokerOrder] = []
        self._positions: list[BrokerPosition] = []
        self._executions: list[BrokerExecution] = []
        self._open_orders_end = threading.Event()
        self._positions_end = threading.Event()
        self._executions_end = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._order_listeners: list[Any] = []

    async def connect(self) -> None:
        if self.state in {BrokerState.CONNECTING, BrokerState.SYNCING, BrokerState.READY}:
            return

        self.state = BrokerState.CONNECTING
        self._identified_event.clear()
        # Callbacks arrive on the reader thread and must hand work back here.
        self._loop = asyncio.get_running_loop()
        self._app = IBKRApp(self)

        await asyncio.to_thread(
            self._app.connect,
            self.config.host,
            self.config.port,
            self.config.client_id,
        )
        # ibapi 10.51 EClient.connect() returns None; success is only observable
        # through isConnected(), so the return value must not be treated as a flag.
        if not self._app.isConnected():
            self.state = BrokerState.DISCONNECTED
            raise ConnectionError("IBKR socket connection failed")

        self._thread = threading.Thread(target=self._app.run, name="ibkr-reader", daemon=True)
        self._thread.start()
        self.state = BrokerState.SYNCING

        identified = await asyncio.to_thread(
            self._identified_event.wait,
            self.config.timeout_seconds,
        )
        if not identified:
            await self.disconnect()
            raise TimeoutError("IBKR did not deliver nextValidId before timeout")

    async def disconnect(self) -> None:
        if self._app is not None and getattr(self._app, "isConnected", lambda: False)():
            await asyncio.to_thread(self._app.disconnect)
        self.state = BrokerState.DISCONNECTED
        self._identified_event.clear()

    async def collect_snapshot(self) -> BrokerSnapshot:
        """Request open orders, positions and executions, then await every barrier.

        A barrier that never arrives leaves its completion flag False, which the
        reconciler treats as blocking; an unanswered request must never be able to
        read as "the account is empty".
        """
        if self._app is None or not getattr(self._app, "isConnected", lambda: False)():
            raise RuntimeError("Cannot reconcile unless the broker socket is connected")
        if self.next_order_id is None:
            raise RuntimeError("Cannot reconcile before nextValidId")

        self.state = BrokerState.SYNCING
        self._reset_snapshot()
        await asyncio.to_thread(self._request_snapshot)

        timeout = self.config.timeout_seconds
        await asyncio.to_thread(self._open_orders_end.wait, timeout)
        await asyncio.to_thread(self._positions_end.wait, timeout)
        await asyncio.to_thread(self._executions_end.wait, timeout)

        with self._snapshot_lock:
            return BrokerSnapshot(
                account=self.account,
                orders=tuple(self._orders),
                positions=tuple(self._positions),
                executions=tuple(self._executions),
                orders_complete=self._open_orders_end.is_set(),
                positions_complete=self._positions_end.is_set(),
                executions_complete=self._executions_end.is_set(),
            )

    def apply_reconciliation(
        self, snapshot: BrokerSnapshot, local: LocalSnapshot
    ) -> ReconciliationResult:
        """Gate READY on agreement, and DEGRADE on any unexplained difference."""
        result = reconcile(snapshot, local)
        self.last_reconciliation = result

        if result.ok:
            self.state = BrokerState.READY
            self.last_error = None
        else:
            self.state = BrokerState.DEGRADED
            self.last_error = _describe_findings(result)

        return result

    async def sync_state(self, local: LocalSnapshot) -> ReconciliationResult:
        """Collect the broker view and reconcile it against the supplied local state."""
        snapshot = await self.collect_snapshot()
        return self.apply_reconciliation(snapshot, local)

    def _request_snapshot(self) -> None:
        """Issue every reconciliation request on the reader's socket."""
        if ExecutionFilter is None:
            raise RuntimeError(
                "Official IBKR TWS API Python package (ibapi) is not installed. "
                "Install it from the official TWS API download."
            )
        assert self._app is not None
        self._app.reqOpenOrders()
        self._app.reqPositions()
        self._app.reqExecutions(EXECUTIONS_REQ_ID, ExecutionFilter())

    def _reset_snapshot(self) -> None:
        with self._snapshot_lock:
            self._orders.clear()
            self._positions.clear()
            self._executions.clear()
        self._open_orders_end.clear()
        self._positions_end.clear()
        self._executions_end.clear()

    def degrade(self, reason: str) -> None:
        """Fail closed without a reconciliation pass.

        Used when local state cannot be read at all: agreeing is impossible, so
        READY must not survive the previous pass.
        """
        self.state = BrokerState.DEGRADED
        self.last_error = reason

    async def status(self) -> BrokerStatus:
        connected = bool(
            self._app is not None and getattr(self._app, "isConnected", lambda: False)()
        )
        return BrokerStatus(
            state=self.state,
            connected=connected,
            account=self.account,
            next_order_id=self.next_order_id,
            last_error=self.last_error,
        )

    def allocate_order_id(self) -> int:
        if self.state != BrokerState.READY or self.next_order_id is None:
            raise RuntimeError("Broker is not READY; refusing to allocate order id")
        with self._order_id_lock:
            order_id = self.next_order_id
            self.next_order_id += 1
            return order_id

    def add_order_listener(self, listener: Any) -> None:
        """Register an order-lifecycle listener (the order manager)."""
        self._order_listeners.append(listener)

    def is_ready(self) -> bool:
        return self.state is BrokerState.READY

    def place_limit_order(self, intent: Any) -> int:
        """Transmit a LIMIT order; never trusts this call as proof of acceptance."""
        if Contract is None or IBKRWireOrder is None:
            raise RuntimeError(
                "Official IBKR TWS API Python package (ibapi) is not installed. "
                "Install it from the official TWS API download."
            )
        if self.state is not BrokerState.READY or self._app is None:
            raise RuntimeError("Broker is not READY; refusing to allocate order id")

        order_id = self.allocate_order_id()

        contract = Contract()
        contract.symbol = intent.symbol
        contract.secType = intent.sec_type
        contract.exchange = intent.exchange
        contract.currency = intent.currency

        order = IBKRWireOrder()
        order.action = intent.side
        order.orderType = "LMT"
        order.totalQuantity = intent.quantity
        order.lmtPrice = intent.limit_price
        order.tif = intent.time_in_force

        self._app.placeOrder(order_id, contract, order)
        return order_id

    def cancel_order(self, broker_order_id: int) -> None:
        if OrderCancel is None or self._app is None:
            raise RuntimeError(
                "Official IBKR TWS API Python package (ibapi) is not installed. "
                "Install it from the official TWS API download."
            )
        self._app.cancelOrder(broker_order_id, OrderCancel())

    def _dispatch(self, method_name: str, *args: Any) -> None:
        """Deliver a reader-thread event to listeners without blocking the reader."""
        if not self._order_listeners or self._loop is None:
            return

        for listener in self._order_listeners:
            handler = getattr(listener, method_name, None)
            if handler is None:
                continue
            try:
                future = asyncio.run_coroutine_threadsafe(handler(*args), self._loop)
            except RuntimeError as exc:  # loop already closed during shutdown
                print(f"listener dispatch failed: {exc}")
                continue
            future.add_done_callback(_log_dispatch_failure)

    def _on_next_valid_id(self, order_id: int) -> None:
        with self._order_id_lock:
            if self.next_order_id is None or order_id > self.next_order_id:
                self.next_order_id = order_id
        # SYNCING, not READY: reconciliation still has to agree with local records.
        self.state = BrokerState.SYNCING
        self._identified_event.set()

    def _on_managed_accounts(self, accounts: list[str]) -> None:
        if accounts and self.account is None:
            self.account = accounts[0]

    def _on_open_order(self, orderId, contract, order, orderState) -> None:  # noqa: N802
        converted = broker_order(orderId, contract, order, orderState)
        with self._snapshot_lock:
            self._orders.append(converted)
        self._dispatch(
            "apply_order_update",
            broker_order_id=converted.broker_order_id,
            perm_id=converted.perm_id,
            status_raw=converted.status,
            filled=converted.filled,
            remaining=converted.quantity - converted.filled,
        )

    def _on_order_status(self, *, orderId, status, filled, remaining, perm_id, avg_fill_price) -> None:
        self._dispatch(
            "apply_order_update",
            broker_order_id=_optional_int(orderId),
            perm_id=_optional_int(perm_id),
            status_raw=str(status or ""),
            filled=_num(filled),
            remaining=_num(remaining, default=0.0),
        )

    def _on_open_order_end(self) -> None:
        self._open_orders_end.set()

    def _on_position(self, account, contract, position, avgCost) -> None:  # noqa: N802
        with self._snapshot_lock:
            self._positions.append(broker_position(account, contract, position, avgCost))

    def _on_position_end(self) -> None:
        self._positions_end.set()

    def _on_exec_details(self, reqId, contract, execution) -> None:  # noqa: N802
        converted = broker_execution(reqId, contract, execution)

        if reqId == EXECUTIONS_REQ_ID:
            with self._snapshot_lock:
                self._executions.append(converted)
            return

        # Live fills arrive with reqId -1 and must reach the order manager.
        self._dispatch("apply_execution", converted)

    def _on_exec_details_end(self, reqId) -> None:  # noqa: N802
        if reqId != EXECUTIONS_REQ_ID:
            return
        self._executions_end.set()

    def _on_connection_closed(self) -> None:
        self.state = BrokerState.DISCONNECTED
        self._identified_event.clear()

    def _on_error(self, req_id: int, code: int, message: str, reject: str) -> None:
        if code in self.INFORMATIONAL_CODES:
            # IBKR reports these while everything is healthy (e.g. 2104 per
            # market-data farm); storing them as last_error would make a READY
            # session look degraded.
            return

        detail = f"IBKR {code}: {message}"
        if reject:
            detail += f" reject={reject}"
        self.last_error = detail

        if code in self.CONNECTIVITY_LOST_CODES:
            self.state = BrokerState.DEGRADED
            self._identified_event.clear()
        elif code in self.CONNECTIVITY_RESTORED_CODES:
            # Do not jump straight to READY: reconciliation must succeed first.
            self.state = BrokerState.SYNCING

        self._dispatch("apply_error", req_id, code, message)


def _log_dispatch_failure(future) -> None:
    """Surface listener failures that asyncio futures would otherwise swallow."""
    error = future.exception()
    if error is not None:
        print(f"order listener failed: {type(error).__name__}: {error}")


def _describe_findings(result: ReconciliationResult) -> str:
    codes = sorted({f.code.value for f in result.blocking})
    return f"Reconciliation blocked: {', '.join(codes)}"
