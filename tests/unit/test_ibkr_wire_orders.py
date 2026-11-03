"""Verifies the real IBKR wire construction without touching a socket.

The order manager's own tests use a fake gateway, which would hide a misspelled
ibapi attribute. These tests drive the actual place_limit_order / cancel_order.
"""


import pytest

from core.broker.base import BrokerState
from core.broker.ibkr.connection import IBKRConnectionConfig, IBKRConnectionManager
from core.execution.orders import LimitOrderIntent

pytest.importorskip("ibapi", reason="these tests exercise the real IBKR wire objects")


class RecordingApp:
    def __init__(self):
        self.placed = []
        self.cancelled = []

    def isConnected(self):  # noqa: N802
        return True

    def placeOrder(self, order_id, contract, order):  # noqa: N802
        self.placed.append((order_id, contract, order))

    def cancelOrder(self, order_id, order_cancel):  # noqa: N802
        self.cancelled.append((order_id, order_cancel))


def ready_manager() -> IBKRConnectionManager:
    manager = IBKRConnectionManager(
        IBKRConnectionConfig(host="127.0.0.1", port=7497, client_id=99)
    )
    manager._app = RecordingApp()
    manager.next_order_id = 7
    manager.state = BrokerState.READY
    return manager


def intent(**overrides) -> LimitOrderIntent:
    base = {
        "symbol": "AAPL",
        "sec_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "side": "BUY",
        "quantity": 3.0,
        "limit_price": 250.5,
    }
    base.update(overrides)
    return LimitOrderIntent(**base)


def test_place_limit_order_builds_the_wire_objects_correctly():
    manager = ready_manager()

    order_id = manager.place_limit_order(intent())

    recorded_id, contract, order = manager._app.placed[0]
    assert recorded_id == order_id == 7
    assert contract.symbol == "AAPL"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"
    assert order.orderType == "LMT"
    assert order.action == "BUY"
    assert float(order.totalQuantity) == 3.0
    assert order.lmtPrice == pytest.approx(250.5)
    assert order.tif == "DAY"


def test_allocated_ids_advance_and_never_reuse():
    manager = ready_manager()

    first = manager.place_limit_order(intent())
    second = manager.place_limit_order(intent())

    assert (first, second) == (7, 8)


def test_place_is_refused_unless_ready():
    manager = ready_manager()
    manager.state = BrokerState.SYNCING

    with pytest.raises(RuntimeError, match="not READY"):
        manager.place_limit_order(intent())

    assert manager._app.placed == []


def test_place_is_refused_before_next_valid_id():
    manager = ready_manager()
    manager.next_order_id = None

    with pytest.raises(RuntimeError, match="not READY"):
        manager.place_limit_order(intent())


def test_degraded_broker_cannot_place():
    manager = ready_manager()
    manager.state = BrokerState.DEGRADED

    with pytest.raises(RuntimeError, match="not READY"):
        manager.place_limit_order(intent())


def test_is_ready_only_for_ready_state():
    manager = ready_manager()
    assert manager.is_ready() is True

    for state in (BrokerState.DISCONNECTED, BrokerState.CONNECTING, BrokerState.SYNCING):
        manager.state = state
        assert manager.is_ready() is False

    manager.state = BrokerState.READY
    manager.next_order_id = None
    assert manager.is_ready() is False, "READY without an allocatable order id is not ready"


def test_sell_side_is_transmitted_as_is():
    manager = ready_manager()

    manager.place_limit_order(intent(side="SELL"))

    _, _, order = manager._app.placed[0]
    assert order.action == "SELL"


def test_cancel_passes_an_order_cancel_object():
    manager = ready_manager()

    manager.cancel_order(7)

    order_id, order_cancel = manager._app.cancelled[0]
    assert order_id == 7
    assert order_cancel is not None


def test_market_style_intent_cannot_reach_the_wire():
    manager = ready_manager()

    with pytest.raises(ValueError, match="MARKET"):
        intent(limit_price=0.0)

    assert manager._app.placed == []
