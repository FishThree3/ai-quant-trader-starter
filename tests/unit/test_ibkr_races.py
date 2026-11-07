"""Deterministic lifecycle races; all transports here are in-process fakes."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from core.broker.base import BrokerState
from core.broker.ibkr.connection import (
    IBKRConnectionConfig,
    IBKRConnectionManager,
)
from core.broker.ibkr.snapshot import broker_position
from core.reconciliation.models import BrokerSnapshot, LocalSnapshot


def connection(**kwargs):
    return IBKRConnectionManager(IBKRConnectionConfig("localhost", 7497, 71, **kwargs))


class SnapshotApp:
    def __init__(self):
        self.connected = True
        self.requested = asyncio.Event()

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def reqOpenOrders(self):
        self.requested.set()

    def reqPositions(self):
        pass

    def reqExecutions(self, *args):
        pass


@pytest.mark.parametrize("send_fails", [False, True])
async def test_abandoned_snapshot_disconnects_before_another_pass(send_fails):
    conn = connection(timeout_seconds=0.05)
    app = conn._app = SnapshotApp()
    conn._on_managed_accounts(["DU123"])
    conn.next_order_id = 1
    if send_fails:

        def fail():
            raise OSError("send interrupted")

        app.reqPositions = fail
        with pytest.raises(OSError):
            await conn.collect_snapshot()
    else:
        task = asyncio.create_task(conn.collect_snapshot())
        await app.requested.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not app.connected
    assert conn._app is None
    # Late callbacks cannot make another request on the abandoned socket valid.
    conn._on_open_order_end()
    conn._on_position_end()
    conn._on_exec_details_end(5001)
    with pytest.raises(RuntimeError, match="socket"):
        await conn.collect_snapshot()
    assert not conn.is_ready()


def test_account_replacement_cannot_preserve_ready():
    conn = connection()
    conn._on_managed_accounts(["DU123"])
    conn.state = BrokerState.READY
    conn._on_managed_accounts(["DU456"])
    assert conn.state is BrokerState.DEGRADED
    assert not conn._account_verified
    assert conn.account == "DU123"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), None, "unknown"])
def test_invalid_position_quantity_cannot_become_zero(value):
    contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD")
    with pytest.raises(ValueError, match="broker numeric"):
        broker_position("DU123", contract, value, 100)


@pytest.mark.parametrize("client_id,expected_id", [(71, 7), (72, None), (None, None)])
def test_only_own_client_execution_uses_local_order_id(client_id, expected_id):
    conn = connection()
    conn._on_managed_accounts(["DU123"])
    forwarded = []
    conn._dispatch = lambda method, execution: forwarded.append(execution)
    contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD")
    execution = SimpleNamespace(
        execId="e1",
        orderId=7,
        permId=55,
        clientId=client_id,
        acctNumber="DU123",
        shares=1,
        price=100,
        side="BOT",
    )
    conn._on_exec_details(-1, contract, execution)
    assert forwarded[0].broker_order_id == expected_id
    assert forwarded[0].client_id == client_id


def test_reconciliation_and_reader_fault_share_state_lock():
    conn = connection(read_only=False)
    conn._on_managed_accounts(["DU123"])
    conn.next_order_id = 1
    conn.state = BrokerState.SYNCING
    snapshot = BrokerSnapshot(
        "DU123", orders_complete=True, positions_complete=True, executions_complete=True
    )
    conn._completed_snapshot = snapshot
    conn._snapshot_epoch = conn._connection_epoch
    arrived = threading.Event()
    fault_done = threading.Event()
    threads = []

    def error():
        arrived.set()
        conn._on_error(-1, 1100, "connection lost", "")
        fault_done.set()

    def is_connected():
        if not threads:
            thread = threading.Thread(target=error)
            threads.append(thread)
            thread.start()
            assert arrived.wait(1)
            # The reader has reached the fault handler but cannot interleave the
            # eligibility-to-READY transaction. It must run immediately afterward.
            assert not fault_done.is_set()
        return True

    conn._app = SimpleNamespace(isConnected=is_connected)
    conn.apply_reconciliation(snapshot, LocalSnapshot())
    threads[0].join(1)
    assert fault_done.is_set()
    assert conn.state is BrokerState.DEGRADED
    assert not conn.is_ready()
    with pytest.raises(RuntimeError, match="not READY"):
        conn.allocate_order_id()


@pytest.mark.parametrize("cancel", [False, True])
async def test_late_connect_cannot_orphan_socket_or_overlap_new_connect(monkeypatch, cancel):
    started = threading.Event()
    release = threading.Event()
    instances = []

    class SlowApp:
        def __init__(self, owner):
            self.owner = owner
            self.connected = False
            instances.append(self)

        def connect(self, *args):
            started.set()
            release.wait(2)
            self.connected = True

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

    monkeypatch.setattr("core.broker.ibkr.connection.IBKRApp", SlowApp)
    conn = connection(timeout_seconds=0.03)
    task = asyncio.create_task(conn.connect())
    await asyncio.to_thread(started.wait, 1)
    if cancel:
        task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        with pytest.raises(RuntimeError, match="still stopping"):
            await conn.connect()
        assert len(instances) == 1
    finally:
        release.set()
        await asyncio.to_thread(conn._connect_thread.join, 1)
        assert not instances[0].connected
        await conn.disconnect()
    assert conn._app is None
    assert conn.state is BrokerState.DISCONNECTED
