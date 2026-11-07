from types import SimpleNamespace

import pytest

from core.broker.base import BrokerState
from core.broker.ibkr.connection import (
    EXECUTIONS_REQ_ID,
    IBKRConnectionConfig,
    IBKRConnectionManager,
)
from core.reconciliation.models import (
    FindingCode,
    InstrumentKey,
    LocalExecution,
    LocalOrder,
    LocalPosition,
    LocalSnapshot,
)

EMPTY_LOCAL = LocalSnapshot()
AAPL = InstrumentKey.of("AAPL", "STK", "USD")


def make_manager(timeout_seconds: int = 5) -> IBKRConnectionManager:
    return IBKRConnectionManager(
        IBKRConnectionConfig(
            host="127.0.0.1", port=7497, client_id=99, timeout_seconds=timeout_seconds
        )
    )


def contract(symbol="AAPL", sec_type="STK", currency="USD"):
    return SimpleNamespace(symbol=symbol, secType=sec_type, currency=currency)


def open_order(order_id=17, perm_id=1001, action="BOT", total="10", filled="0", status="Submitted"):
    return (
        order_id,
        contract(),
        SimpleNamespace(permId=perm_id, action=action, totalQuantity=total, lmtPrice=250.0),
        SimpleNamespace(filled=filled, status=status),
    )


def position_row(account="DUR123456", qty="10", avg_cost=250.0):
    return (account, contract(), qty, avg_cost)


def exec_row(exec_id="EX-1", perm_id=1001, shares="4", price=250.0):
    return SimpleNamespace(
        execId=exec_id,
        permId=perm_id,
        side="BOT",
        shares=shares,
        price=price,
        exchange="ISLAND",
        time="20261103  09:30:00",
        acctNumber="DUR123456",
    )


class FakeApp:
    """Stands in for EClient so the barrier logic is testable without TWS."""

    def __init__(self, owner, *, orders=None, positions=None, executions=None, hang=False):
        self.owner = owner
        self._orders = orders if orders is not None else []
        self._positions = positions if positions is not None else []
        self._executions = executions if executions is not None else []
        self._hang = hang
        self.executions_requested: list[int] = []

    def isConnected(self):  # noqa: N802
        return True

    def disconnect(self):
        pass

    def reqOpenOrders(self):  # noqa: N802
        for row in self._orders:
            self.owner._on_open_order(*row)
        if not self._hang:
            self.owner._on_open_order_end()

    def reqPositions(self):  # noqa: N802
        for row in self._positions:
            self.owner._on_position(*row)
        if not self._hang:
            self.owner._on_position_end()

    def reqExecutions(self, req_id, execution_filter):  # noqa: N802
        self.executions_requested.append(req_id)
        for row in self._executions:
            self.owner._on_exec_details(req_id, contract(), row)
        if not self._hang:
            self.owner._on_exec_details_end(req_id)


def armed(manager: IBKRConnectionManager) -> IBKRConnectionManager:
    """Simulate the post-handshake state connect() leaves behind."""
    manager.next_order_id = 1
    manager._on_managed_accounts(["DUR123456"])
    manager.state = BrokerState.SYNCING
    return manager


async def test_broker_activity_with_no_local_records_does_not_reach_ready():
    manager = armed(make_manager())
    manager._app = FakeApp(
        manager,
        orders=[open_order(filled="10", status="Filled")],
        positions=[position_row()],
        executions=[exec_row()],
    )

    result = await manager.sync_state(EMPTY_LOCAL)

    assert not result.ok
    assert manager.state is BrokerState.DEGRADED


async def test_reconciled_activity_grants_ready():
    manager = armed(make_manager())
    manager._app = FakeApp(
        manager,
        orders=[open_order(filled="4")],
        positions=[position_row(qty="4")],
        executions=[exec_row()],
    )
    local = LocalSnapshot(
        orders=(LocalOrder("o1", 1001, 17, AAPL, "BUY", 10.0, 4.0, "PARTIALLY_FILLED"),),
        positions=(LocalPosition("p1", AAPL, 4.0, 250.0),),
        executions=(LocalExecution("e1", "EX-1", 1001),),
    )

    result = await manager.sync_state(local)

    assert result.ok
    assert manager.state is BrokerState.READY
    assert result.orders_matched == 1
    assert result.executions_matched == 1


async def test_empty_account_with_empty_local_is_ready():
    manager = armed(make_manager())
    manager._app = FakeApp(manager)

    result = await manager.sync_state(EMPTY_LOCAL)

    assert result.ok
    assert manager.state is BrokerState.READY
    assert manager.last_reconciliation is result


async def test_broker_only_order_degrades_and_records_reason():
    manager = armed(make_manager())
    manager._app = FakeApp(manager, orders=[open_order(perm_id=555)])

    result = await manager.sync_state(EMPTY_LOCAL)

    assert [f.code for f in result.blocking] == [FindingCode.BROKER_ONLY_ORDER]
    assert manager.state is BrokerState.DEGRADED
    assert "BROKER_ONLY_ORDER" in (manager.last_error or "")


async def test_missing_barrier_never_looks_clean():
    manager = armed(make_manager(timeout_seconds=0))
    manager._app = FakeApp(manager, hang=True)

    result = await manager.sync_state(EMPTY_LOCAL)

    assert not result.ok
    assert manager.state is BrokerState.DEGRADED
    codes = {f.code for f in result.blocking}
    assert codes == {FindingCode.RECONCILIATION_INCOMPLETE}
    assert result.blocking[0].details["missing_barriers"] == [
        "open_orders",
        "positions",
        "executions",
    ]


async def test_executions_use_dedicated_request_id():
    manager = armed(make_manager())
    manager._app = FakeApp(manager)

    await manager.sync_state(EMPTY_LOCAL)

    assert manager._app.executions_requested == [EXECUTIONS_REQ_ID]


async def test_foreign_req_id_exec_details_are_ignored():
    manager = armed(make_manager())
    manager._app = FakeApp(manager)
    manager._on_exec_details(EXECUTIONS_REQ_ID + 1, contract(), exec_row(exec_id="OTHER"))

    result = await manager.sync_state(EMPTY_LOCAL)

    assert result.executions_matched == 0
    assert manager.state is BrokerState.READY


async def test_unmatched_exec_id_end_marker_is_ignored():
    manager = armed(make_manager(timeout_seconds=0))
    manager._app = FakeApp(manager, hang=True)
    manager._on_exec_details_end(EXECUTIONS_REQ_ID + 1)

    await manager.sync_state(EMPTY_LOCAL)

    assert not manager._executions_end.is_set()


async def test_sync_state_refuses_without_handshake():
    manager = make_manager()
    manager._app = FakeApp(manager)

    with pytest.raises(RuntimeError, match="nextValidId"):
        await manager.sync_state(EMPTY_LOCAL)


async def test_sync_state_refuses_when_socket_down():
    manager = armed(make_manager())

    with pytest.raises(RuntimeError, match="socket"):
        await manager.sync_state(EMPTY_LOCAL)


async def test_informational_codes_never_appear_as_last_error():
    """IBKR sends 2104/2106/2158 on a perfectly healthy session."""
    manager = armed(make_manager())

    for code in (2104, 2106, 2107, 2108, 2158):
        manager._on_error(req_id=-1, code=code, message="data connection up", reject="")

    assert manager.last_error is None
    assert manager.state is BrokerState.SYNCING


async def test_connectivity_loss_degrades_and_records():
    manager = armed(make_manager())
    manager.state = BrokerState.READY

    manager._on_error(req_id=-1, code=1100, message="Socket connection broken", reject="")

    assert manager.state is BrokerState.DEGRADED
    assert "1100" in (manager.last_error or "")


async def test_connectivity_restored_returns_to_syncing_not_ready():
    manager = armed(make_manager())
    manager.state = BrokerState.DEGRADED

    manager._on_error(req_id=-1, code=1101, message="Connected to TWS", reject="")

    assert manager.state is BrokerState.SYNCING


async def test_real_error_codes_are_still_reported():
    manager = armed(make_manager())

    manager._on_error(req_id=-1, code=201, message="Order failed", reject="")

    assert manager.last_error == "IBKR 201: Order failed"


async def test_snapshot_conversion_handles_ibkr_sentinels():
    manager = armed(make_manager())
    manager._app = FakeApp(
        manager,
        # permId 0 means "not assigned yet"; UNSET_DOUBLE arrives as infinity.
        orders=[open_order(perm_id=0, total=float("inf"), filled="0")],
    )

    result = await manager.sync_state(EMPTY_LOCAL)

    finding = result.blocking[0]
    assert finding.code is FindingCode.BROKER_ONLY_ORDER
    assert finding.details["perm_id"] is None
    assert finding.details["broker_order_id"] == 17
    assert finding.details["quantity"] == 0.0
