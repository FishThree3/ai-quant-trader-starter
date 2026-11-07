from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, replace
from functools import wraps
from math import isfinite
from typing import Any

import structlog

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
    Finding,
    FindingCode,
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
    # Distinct bases: (object, object) makes class construction fail at import time.
    class EClient:  # type: ignore[no-redef]
        pass

    class EWrapper:  # type: ignore[no-redef]
        pass

    ExecutionFilter = None  # type: ignore[assignment,misc]
    OrderCancel = None  # type: ignore[assignment,misc]

try:
    from ibapi.contract import Contract
    from ibapi.order import Order as IBKRWireOrder
except ImportError:
    Contract = None  # type: ignore[assignment,misc]
    IBKRWireOrder = None  # type: ignore[assignment,misc]

EXECUTIONS_REQ_ID = 5001
log = structlog.get_logger()


def _state_guard(method):
    """Serialize synchronous readiness decisions with SDK reader-thread faults."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)

    return guarded


@dataclass(frozen=True, slots=True)
class IBKRConnectionConfig:
    host: str
    port: int
    client_id: int
    timeout_seconds: float = 15
    account: str | None = None
    read_only: bool = True

    def __post_init__(self) -> None:
        if self.port in {7496, 7946, 4001}:
            raise ValueError("Known LIVE IBKR port is forbidden in this paper-only adapter")
        if self.account and not self.account.startswith("DU"):
            raise ValueError("Expected a DU paper account")
        if (
            not 1 <= self.port <= 65535
            or self.client_id < 1
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds < 0
        ):
            raise ValueError("Invalid IBKR connection configuration")

    @classmethod
    def from_settings(cls, settings, *, read_only: bool | None = None, client_id_offset: int = 0):
        return cls(
            host=settings.ibkr_host,
            port=settings.ibkr_port,
            client_id=settings.ibkr_client_id + client_id_offset,
            timeout_seconds=settings.ibkr_connect_timeout_seconds,
            account=settings.ibkr_account,
            read_only=settings.ibkr_read_only if read_only is None else read_only,
        )


class IBKRApp(EWrapper, EClient):  # type: ignore[misc]
    """Thin official-API adapter. No strategy/risk logic belongs here."""

    def __init__(self, owner: "IBKRConnectionManager") -> None:
        if Contract is None or not hasattr(EClient, "connect"):
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
            client_id=clientId,
        )

    def position(self, account, contract, position, avgCost):  # noqa: N802
        self.owner._on_position(account, contract, position, avgCost)

    def positionEnd(self):  # noqa: N802
        self.owner._on_position_end()

    def execDetails(self, reqId, contract, execution):  # noqa: N802
        self.owner._on_exec_details(reqId, contract, execution)

    def execDetailsEnd(self, reqId):  # noqa: N802
        self.owner._on_exec_details_end(reqId)

    def tickPrice(self, reqId, tickType, price, attribs):  # noqa: N802
        self.owner._on_tick(reqId, tickType, price)

    def tickSize(self, reqId, tickType, size):  # noqa: N802
        self.owner._on_tick(reqId, tickType, size)

    def marketDataType(self, reqId, marketDataType):  # noqa: N802
        self.owner._dispatch("apply_market_data_type", reqId, marketDataType)

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
        self._connect_thread: threading.Thread | None = None
        self._connect_abort = threading.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._identified_event = threading.Event()
        self._accounts_event = threading.Event()
        self._account_verified = False
        self._connection_epoch = 0
        self._snapshot_epoch: int | None = None
        self._completed_snapshot: BrokerSnapshot | None = None
        self._collect_lock = asyncio.Lock()
        self._reserved_order_ids: set[int] = set()
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
        async with self._lifecycle_lock:
            await self._connect()

    async def _connect(self) -> None:
        if self._connect_thread is not None and self._connect_thread.is_alive():
            raise RuntimeError("Previous IBKR connect is still stopping; refusing another socket")
        if self._connected() and self.state in {BrokerState.SYNCING, BrokerState.READY}:
            return
        if self.state is BrokerState.CONNECTING:
            raise RuntimeError("IBKR connection already in progress")
        if self._app is not None:
            await self._disconnect()

        self.state = BrokerState.CONNECTING
        self._identified_event.clear()
        self._accounts_event.clear()
        self._account_verified = False
        self.account = None
        self.last_error = None
        self._connection_epoch += 1
        self._reserved_order_ids.clear()
        self._loop = asyncio.get_running_loop()
        try:
            app = self._app = IBKRApp(self)
            completed = threading.Event()
            aborted = self._connect_abort = threading.Event()
            errors: list[Exception] = []

            def connect_socket():
                try:
                    app.connect(self.config.host, self.config.port, self.config.client_id)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    if aborted.is_set():
                        try:
                            app.disconnect()
                        except Exception:
                            log.error("aborted_connect_cleanup_failed")
                    completed.set()

            # SDK connect can block during TCP/server-version negotiation. A daemon
            # owns that call and its cleanup; cancellation cannot orphan a late socket.
            self._connect_thread = threading.Thread(
                target=connect_socket, name="ibkr-connect", daemon=True
            )
            self._connect_thread.start()
            if not await asyncio.to_thread(completed.wait, self.config.timeout_seconds):
                raise TimeoutError("IBKR socket handshake timed out")
            if errors:
                raise errors[0]
            # The official API returns None on successful connect; inspect the socket.
            if not self._connected():
                raise ConnectionError("IBKR socket connection failed")
            self.state = BrokerState.SYNCING
            self._thread = threading.Thread(
                target=self._run_reader, name="ibkr-reader", daemon=True
            )
            self._thread.start()
            identified, accounts_received = await asyncio.gather(
                asyncio.to_thread(self._identified_event.wait, self.config.timeout_seconds),
                asyncio.to_thread(self._accounts_event.wait, self.config.timeout_seconds),
            )
            if not identified or not accounts_received:
                raise TimeoutError("IBKR did not deliver nextValidId and managedAccounts in time")
            if not self._account_verified:
                raise RuntimeError(self.last_error or "Paper account not verified")
            if not self._connected() or self.state is not BrokerState.SYNCING:
                raise ConnectionError("IBKR disconnected during handshake")
        except BaseException:
            self._connect_abort.set()
            await self._disconnect()
            raise

    def _run_reader(self) -> None:
        try:
            assert self._app is not None
            self._app.run()
        except Exception as exc:
            log.error("ibkr_reader_failed", error_type=type(exc).__name__)
        finally:
            self._on_connection_closed()

    def _connected(self) -> bool:
        return bool(self._app is not None and self._app.isConnected())

    async def disconnect(self) -> None:
        async with self._lifecycle_lock:
            await self._disconnect()

    async def _disconnect(self) -> None:
        self._connect_abort.set()
        self._on_connection_closed()
        if self._app is not None:
            await asyncio.to_thread(self._app.disconnect)
        if self._connect_thread is not None and self._connect_thread.is_alive():
            await asyncio.to_thread(self._connect_thread.join, self.config.timeout_seconds)
            if self._connect_thread.is_alive():
                # Retain ownership; its finally closes any connection established late.
                # _connect refuses to overlap it, even if a caller ignores the timeout.
                self.last_error = "IBKR socket connect still stopping"
                return
        self._connect_thread = None
        if self._thread is not None and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, self.config.timeout_seconds)
            if self._thread.is_alive():
                raise RuntimeError("IBKR reader did not stop; refusing a second connection")
        self._thread = None
        self._app = None
        self.account = None
        self._accounts_event.clear()
        self._identified_event.clear()
        self._reserved_order_ids.clear()

    async def collect_snapshot(self) -> BrokerSnapshot:
        async with self._collect_lock, self._lifecycle_lock:
            try:
                return await self._collect_snapshot()
            except BaseException:
                # IB openOrderEnd/positionEnd have no request identity. Cancellation
                # or a send error invalidates this socket, not just this wait.
                await self._disconnect()
                raise

    async def _collect_snapshot(self) -> BrokerSnapshot:
        """Request open orders, positions and executions, then await every barrier.

        A barrier that never arrives leaves its completion flag False, which the
        reconciler treats as blocking; an unanswered request must never be able to
        read as "the account is empty".
        """
        if self._app is None or not getattr(self._app, "isConnected", lambda: False)():
            raise RuntimeError("Cannot reconcile unless the broker socket is connected")
        if self.next_order_id is None:
            raise RuntimeError("Cannot reconcile before nextValidId")

        if not self._account_verified:
            raise RuntimeError("Cannot reconcile before paper account verification")
        with self._state_lock:
            self.state = BrokerState.SYNCING
            self._snapshot_epoch = self._connection_epoch
            self._completed_snapshot = None
        self._reset_snapshot()
        self._request_snapshot()

        timeout = self.config.timeout_seconds
        await asyncio.gather(
            asyncio.to_thread(self._open_orders_end.wait, timeout),
            asyncio.to_thread(self._positions_end.wait, timeout),
            asyncio.to_thread(self._executions_end.wait, timeout),
        )

        with self._snapshot_lock:
            snapshot = BrokerSnapshot(
                account=self.account,
                orders=tuple(self._orders),
                positions=tuple(self._positions),
                executions=tuple(self._executions),
                orders_complete=self._open_orders_end.is_set(),
                positions_complete=self._positions_end.is_set(),
                executions_complete=self._executions_end.is_set(),
            )
        self._completed_snapshot = snapshot
        if not snapshot.is_complete:
            # Untagged position/order end callbacks from a timed-out request could
            # complete a later pass. Require a new socket instead of reusing them.
            await self._disconnect()
        return snapshot

    @_state_guard
    def apply_reconciliation(
        self, snapshot: BrokerSnapshot, local: LocalSnapshot
    ) -> ReconciliationResult:
        """Gate READY on agreement, and DEGRADE on any unexplained difference."""
        result = reconcile(snapshot, local)

        eligible = (
            snapshot is self._completed_snapshot
            and self._snapshot_epoch == self._connection_epoch
            and self.state is BrokerState.SYNCING
            and self._connected()
            and self._account_verified
        )
        if not eligible and result.ok:
            result = replace(
                result,
                findings=(
                    *result.findings,
                    Finding(
                        FindingCode.CONNECTION_CHANGED,
                        "Reconciliation belongs to an invalidated broker connection",
                    ),
                ),
            )
        self.last_reconciliation = result
        if result.ok and eligible:
            self.state = BrokerState.READY
            self.last_error = None
        else:
            self.state = BrokerState.DEGRADED
            self.last_error = (
                _describe_findings(result)
                if not result.ok
                else "Reconciliation belongs to an invalidated broker connection"
            )

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
        execution_filter = ExecutionFilter()
        execution_filter.acctCode = self.account or ""
        self._app.reqExecutions(EXECUTIONS_REQ_ID, execution_filter)

    def _reset_snapshot(self) -> None:
        with self._snapshot_lock:
            self._orders.clear()
            self._positions.clear()
            self._executions.clear()
        self._open_orders_end.clear()
        self._positions_end.clear()
        self._executions_end.clear()

    @_state_guard
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

    @_state_guard
    def allocate_order_id(self) -> int:
        with self._order_id_lock:
            self._require_order_access()
            assert self.next_order_id is not None
            order_id = self.next_order_id
            self.next_order_id += 1
            self._reserved_order_ids.add(order_id)
            return order_id

    def can_submit_orders(self) -> bool:
        return not self.config.read_only and self.is_ready()

    def _require_order_access(self) -> None:
        if not self.is_ready():
            raise RuntimeError("Broker is not READY; refusing order transmission")
        if self.config.read_only:
            raise RuntimeError("IBKR connection is read-only; order mutations are disabled")

    def add_order_listener(self, listener: Any) -> None:
        """Register an order-lifecycle listener (the order manager)."""
        self._order_listeners.append(listener)

    @_state_guard
    def is_ready(self) -> bool:
        # Allocation must be possible too: READY without an order id would let the
        # order manager persist a CREATED row it then cannot transmit.
        return (
            self.state is BrokerState.READY
            and self.next_order_id is not None
            and self._account_verified
            and self._connected()
        )

    @_state_guard
    def place_limit_order(self, intent: Any, broker_order_id: int | None = None) -> int:
        """Transmit a LIMIT order; never trusts this call as proof of acceptance."""
        if Contract is None or IBKRWireOrder is None:
            raise RuntimeError(
                "Official IBKR TWS API Python package (ibapi) is not installed. "
                "Install it from the official TWS API download."
            )
        self._require_order_access()
        order_id = self.allocate_order_id() if broker_order_id is None else broker_order_id
        with self._order_id_lock:
            if order_id not in self._reserved_order_ids:
                raise RuntimeError("Order ID was not reserved or has already been transmitted")
            # Consume before attempting the write: an exception does not prove no send.
            self._reserved_order_ids.remove(order_id)

        contract = _wire_contract(intent.symbol, intent.sec_type, intent.exchange, intent.currency)

        order = IBKRWireOrder()
        order.action = intent.side
        order.orderType = "LMT"
        order.totalQuantity = intent.quantity
        order.lmtPrice = intent.limit_price
        order.tif = intent.time_in_force
        order.account = self.account
        order.orderRef = str(intent.order_uuid)

        self._require_order_access()
        self._app.placeOrder(order_id, contract, order)
        return order_id

    def subscribe(self, instrument: Any, req_id: int) -> None:
        """Open one streaming market-data line for an instrument."""
        if Contract is None or self._app is None:
            raise RuntimeError(
                "Official IBKR TWS API Python package (ibapi) is not installed. "
                "Install it from the official TWS API download."
            )
        contract = _wire_contract(
            instrument.symbol, instrument.sec_type, instrument.exchange, instrument.currency
        )
        self._app.reqMktData(req_id, contract, "", False, False, [])

    def unsubscribe(self, req_id: int) -> None:
        if self._app is None:
            return
        self._app.cancelMktData(req_id)

    def set_market_data_type(self, mode: int) -> None:
        if self._app is not None:
            self._app.reqMarketDataType(mode)

    @_state_guard
    def cancel_order(self, broker_order_id: int) -> None:
        # Allow risk-reducing cancellation while DEGRADED, but never in read-only mode.
        if self.config.read_only or not self._account_verified or not self._connected():
            raise RuntimeError("Cancellation requires a verified writable paper connection")
        if OrderCancel is None or self._app is None:
            raise RuntimeError(
                "Official IBKR TWS API Python package (ibapi) is not installed. "
                "Install it from the official TWS API download."
            )
        self._app.cancelOrder(broker_order_id, OrderCancel())

    def _dispatch(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        """Deliver a reader-thread event to listeners without blocking the reader."""
        if not self._order_listeners or self._loop is None:
            return

        for listener in self._order_listeners:
            handler = getattr(listener, method_name, None)
            if handler is None:
                continue
            coroutine = handler(*args, **kwargs)
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
            except RuntimeError:  # loop already closed during shutdown
                coroutine.close()
                self.degrade("Broker event could not be dispatched")
                continue
            future.add_done_callback(self._dispatch_completed)

    def _dispatch_completed(self, future) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            self.degrade("Broker event processing failed; reconciliation required")
            log.error("broker_listener_failed", error_type=type(error).__name__)

    @_state_guard
    def _on_next_valid_id(self, order_id: int) -> None:
        with self._order_id_lock:
            if self.next_order_id is None or order_id > self.next_order_id:
                self.next_order_id = order_id
        # IDs arriving after a connectivity error cannot erase the fault.
        if self.state is BrokerState.CONNECTING:
            self.state = BrokerState.SYNCING
        self._identified_event.set()

    @_state_guard
    def _on_managed_accounts(self, accounts: list[str]) -> None:
        unique = set(accounts)
        selected = self.config.account
        if any(not account.startswith("DU") for account in unique):
            reason = "Non-paper account reported by IBKR; connection refused"
        elif not unique:
            reason = "IBKR reported no managed paper accounts"
        elif selected and selected not in unique:
            reason = "Configured paper account was not reported by IBKR"
        elif not selected and len(unique) != 1:
            reason = "Multiple paper accounts require explicit IBKR_ACCOUNT selection"
        else:
            candidate = selected or next(iter(unique))
            if self.account is not None and self.account != candidate:
                self._account_verified = False
                self._connection_epoch += 1
                self.degrade("Managed paper account changed; reconnect required")
                self._accounts_event.set()
                return
            self.account = candidate
            self._account_verified = True
            self._accounts_event.set()
            return
        self.account = None
        self._account_verified = False
        self.degrade(reason)
        self._accounts_event.set()

    def _on_open_order(self, orderId, contract, order, orderState) -> None:  # noqa: N802
        account = getattr(order, "account", None)
        if self.account and account and account != self.account:
            return
        converted = broker_order(orderId, contract, order, orderState)
        foreign_client = getattr(order, "clientId", self.config.client_id) != self.config.client_id
        if foreign_client:
            converted = replace(converted, broker_order_id=None)
        with self._snapshot_lock:
            self._orders.append(converted)
        if foreign_client:
            return
        self._dispatch(
            "apply_order_update",
            broker_order_id=converted.broker_order_id,
            perm_id=converted.perm_id,
            status_raw=converted.status,
            filled=converted.filled,
            remaining=converted.quantity - converted.filled,
        )

    def _on_order_status(
        self, *, orderId, status, filled, remaining, perm_id, avg_fill_price, client_id=None
    ) -> None:
        if client_id is not None and client_id != self.config.client_id:
            return
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
        if self.account and account != self.account:
            return
        with self._snapshot_lock:
            self._positions.append(broker_position(account, contract, position, avgCost))

    def _on_position_end(self) -> None:
        self._positions_end.set()

    def _on_exec_details(self, reqId, contract, execution) -> None:  # noqa: N802
        converted = broker_execution(reqId, contract, execution)
        # orderId is client-local. Master-client feeds may include another client's
        # executions; those may only match a known global permId, never our orderId.
        if converted.client_id != self.config.client_id:
            converted = replace(converted, broker_order_id=None)
        if self.account and converted.account and converted.account != self.account:
            return

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

    def _on_tick(self, req_id: int, tick_type: int, value: Any) -> None:
        self._dispatch("apply_tick", int(req_id), int(tick_type), value)

    @_state_guard
    def _on_connection_closed(self) -> None:
        self._connection_epoch += 1
        self.state = BrokerState.DISCONNECTED
        self._account_verified = False
        self._identified_event.clear()

    @_state_guard
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
            self._connection_epoch += 1
            self.state = BrokerState.DEGRADED
            self._identified_event.clear()
        elif code in self.CONNECTIVITY_RESTORED_CODES:
            # Do not jump straight to READY: reconciliation must succeed first.
            self.state = BrokerState.SYNCING

        self._dispatch("apply_error", req_id, code, message)


def _wire_contract(symbol: str, sec_type: str, exchange: str, currency: str):
    """Build the ibapi Contract used by both market data and orders."""
    contract = Contract()
    contract.symbol = symbol
    contract.secType = sec_type
    contract.exchange = exchange
    contract.currency = currency
    return contract


def _describe_findings(result: ReconciliationResult) -> str:
    codes = sorted({f.code.value for f in result.blocking})
    return f"Reconciliation blocked: {', '.join(codes)}"
