"""Exercise the actual SDK boundary and fail-closed connection lifecycle."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.broker.base import BrokerState
from core.broker.ibkr.connection import IBKRApp, IBKRConnectionConfig, IBKRConnectionManager
from core.broker.ibkr.snapshot import broker_position
from core.reconciliation.models import BrokerSnapshot, LocalSnapshot


def manager(**kwargs):
    return IBKRConnectionManager(IBKRConnectionConfig("127.0.0.1", 7497, 971, **kwargs))


def test_read_only_by_default():
    conn = manager()
    conn._on_managed_accounts(["DU123"])
    conn._app = SimpleNamespace(isConnected=lambda: True)
    conn.state = BrokerState.READY
    conn.next_order_id = 1
    with pytest.raises(RuntimeError, match="read-only"):
        conn.allocate_order_id()
    with pytest.raises(RuntimeError, match="writable"):
        conn.cancel_order(1)


@pytest.mark.parametrize("accounts", [[], ["U123"], ["DU123", "U123"], ["DU1", "DU2"]])
def test_account_selection_fails_closed(accounts):
    conn = manager()
    conn._on_managed_accounts(accounts)
    assert not conn._account_verified
    assert conn.account is None
    assert conn.state is BrokerState.DEGRADED
    assert not any(account in (conn.last_error or "") for account in accounts)


def test_configured_account_must_be_reported():
    conn = manager(account="DU123")
    conn._on_managed_accounts(["DU456"])
    assert not conn._account_verified
    conn._on_managed_accounts(["DU123", "DU456"])
    assert conn._account_verified
    assert conn.account == "DU123"


@pytest.mark.parametrize("port", [7496, 7946, 4001])
def test_known_live_ports_forbidden(port):
    with pytest.raises(ValueError, match="LIVE"):
        IBKRConnectionConfig("127.0.0.1", port, 1)


def test_readiness_requires_verified_account_and_socket():
    conn = manager()
    conn.state = BrokerState.READY
    conn.next_order_id = 1
    conn._app = SimpleNamespace(isConnected=lambda: True)
    assert not conn.is_ready()
    conn._on_managed_accounts(["DU123"])
    assert conn.is_ready()
    conn._app = SimpleNamespace(isConnected=lambda: False)
    assert not conn.is_ready()


def test_old_snapshot_cannot_restore_ready_after_disconnect():
    conn = manager()
    conn._on_managed_accounts(["DU123"])
    conn.state = BrokerState.SYNCING
    snapshot = BrokerSnapshot(
        account="DU123", orders_complete=True, positions_complete=True, executions_complete=True
    )
    conn._completed_snapshot = snapshot
    conn._snapshot_epoch = conn._connection_epoch
    conn._on_connection_closed()
    result = conn.apply_reconciliation(snapshot, LocalSnapshot())
    assert not result.ok
    assert not conn.is_ready()
    assert conn.state is BrokerState.DEGRADED


def test_next_id_does_not_clear_connectivity_error():
    conn = manager()
    conn._on_error(-1, 1100, "lost", "")
    conn._on_next_valid_id(42)
    assert conn.state is BrokerState.DEGRADED


def test_sdk_tick_size_has_official_three_argument_signature():
    events = []
    app = SimpleNamespace(owner=SimpleNamespace(_on_tick=lambda *args: events.append(args)))
    IBKRApp.tickSize(app, 90000, 0, 100)
    assert events == [(90000, 0, 100)]


def test_market_data_type_is_forwarded():
    events = []
    app = SimpleNamespace(owner=SimpleNamespace(_dispatch=lambda *args: events.append(args)))
    IBKRApp.marketDataType(app, 90000, 2)
    assert events == [("apply_market_data_type", 90000, 2)]


def test_real_contract_symbol_survives_snapshot_conversion():
    contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD")
    row = broker_position("DU123", contract, 3, 100)
    assert row.instrument.symbol == "AAPL"


def test_connection_module_imports_without_optional_ibapi():
    # A clean subprocess prevents cached SDK modules from hiding duplicate-base bugs.
    code = """
import builtins
original = builtins.__import__
def no_ibapi(name, *args, **kwargs):
    if name == "ibapi" or name.startswith("ibapi."):
        raise ImportError("SDK intentionally absent")
    return original(name, *args, **kwargs)
builtins.__import__ = no_ibapi
from core.broker.ibkr.connection import IBKRApp, IBKRConnectionConfig, IBKRConnectionManager
manager = IBKRConnectionManager(IBKRConnectionConfig("localhost", 7497, 1))
try:
    IBKRApp(manager)
except RuntimeError as exc:
    assert "not installed" in str(exc)
else:
    raise AssertionError("SDK absence must be explicit")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr


async def test_connect_waits_for_accounts_and_cleans_up(monkeypatch):
    class FakeApp:
        def __init__(self, owner):
            self.owner = owner
            self.connected = False
            self.done = __import__("threading").Event()

        def connect(self, *args):
            self.connected = True

        def isConnected(self):
            return self.connected

        def run(self):
            self.owner._on_next_valid_id(1)
            self.owner._on_managed_accounts(["DU123"])
            self.done.wait(3)

        def disconnect(self):
            self.connected = False
            self.done.set()

    monkeypatch.setattr("core.broker.ibkr.connection.IBKRApp", FakeApp)
    conn = manager(timeout_seconds=1)
    await conn.connect()
    assert conn._account_verified
    assert conn.state is BrokerState.SYNCING
    assert not conn.is_ready(), "Handshake is not reconciliation"
    await conn.disconnect()
    assert conn._thread is None
    assert conn.account is None
    assert conn.state is BrokerState.DISCONNECTED


async def test_missing_account_callback_fails_handshake(monkeypatch):
    class FakeApp:
        def __init__(self, owner):
            self.owner = owner
            self.connected = True

        def connect(self, *args):
            pass

        def isConnected(self):
            return self.connected

        def run(self):
            self.owner._on_next_valid_id(1)

        def disconnect(self):
            self.connected = False

    monkeypatch.setattr("core.broker.ibkr.connection.IBKRApp", FakeApp)
    conn = manager(timeout_seconds=0.01)
    with pytest.raises(TimeoutError):
        await conn.connect()
    assert conn._app is None
    assert not conn.is_ready()
