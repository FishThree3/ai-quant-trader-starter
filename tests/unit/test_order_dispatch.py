import asyncio
from types import SimpleNamespace

from core.broker.base import BrokerState
from core.broker.ibkr.connection import (
    EXECUTIONS_REQ_ID,
    IBKRConnectionConfig,
    IBKRConnectionManager,
)
from core.reconciliation.models import BrokerExecution


class RecordingListener:
    def __init__(self):
        self.updates = []
        self.executions = []
        self.errors = []

    async def apply_order_update(self, **kwargs):
        self.updates.append(kwargs)

    async def apply_execution(self, execution):
        self.executions.append(execution)

    async def apply_error(self, broker_order_id, code, message):
        self.errors.append((broker_order_id, code, message))


def make_manager() -> IBKRConnectionManager:
    manager = IBKRConnectionManager(
        IBKRConnectionConfig(host="127.0.0.1", port=7497, client_id=99, timeout_seconds=5)
    )
    manager.state = BrokerState.SYNCING
    manager.next_order_id = 1
    # connect() normally captures the loop; tests drive callbacks directly.
    manager._loop = asyncio.get_running_loop()
    return manager


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def contract():
    return SimpleNamespace(code="AAPL", secType="STK", currency="USD")


def order(perm_id=1001, action="BOT", total=10.0, lmt=250.0):
    return SimpleNamespace(permId=perm_id, action=action, totalQuantity=total, lmtPrice=lmt)


def order_state(filled=0.0, status="Submitted"):
    return SimpleNamespace(filled=filled, status=status)


def execution(exec_id="EX-1", perm_id=1001, shares=4.0, price=250.0):
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


async def test_open_order_callback_reaches_listener_and_snapshot():
    manager = make_manager()
    listener = RecordingListener()
    manager.add_order_listener(listener)

    manager._on_open_order(17, contract(), order(), order_state())
    await settle()

    assert listener.updates == [
        {
            "broker_order_id": 17,
            "perm_id": 1001,
            "status_raw": "Submitted",
            "filled": 0.0,
            "remaining": 10.0,
        }
    ]
    assert len(manager._orders) == 1


async def test_order_status_callback_forwards_perm_id():
    manager = make_manager()
    listener = RecordingListener()
    manager.add_order_listener(listener)

    manager._on_order_status(
        orderId=17, status="Filled", filled=10, remaining=0, perm_id=4242, avg_fill_price=250.0
    )
    await settle()

    assert listener.updates[0]["perm_id"] == 4242
    assert listener.updates[0]["status_raw"] == "Filled"


async def test_live_fill_reaches_listener_without_polluting_snapshot():
    """Live fills arrive with reqId -1 and must not be read as a reconciliation row."""
    manager = make_manager()
    listener = RecordingListener()
    manager.add_order_listener(listener)

    manager._on_exec_details(-1, contract(), execution())
    await settle()

    assert len(listener.executions) == 1
    assert isinstance(listener.executions[0], BrokerExecution)
    assert listener.executions[0].exec_id == "EX-1"
    assert manager._executions == []


async def test_snapshot_execution_does_not_notify_listener():
    manager = make_manager()
    listener = RecordingListener()
    manager.add_order_listener(listener)

    manager._on_exec_details(EXECUTIONS_REQ_ID, contract(), execution())
    await settle()

    assert listener.executions == []
    assert len(manager._executions) == 1


async def test_order_error_reaches_listener():
    manager = make_manager()
    listener = RecordingListener()
    manager.add_order_listener(listener)

    manager._on_error(req_id=17, code=201, message="Order failed", reject="")
    await settle()

    assert listener.errors == [(17, 201, "Order failed")]


async def test_informational_error_is_never_dispatched_as_order_state():
    manager = make_manager()
    listener = RecordingListener()
    manager.add_order_listener(listener)

    manager._on_error(req_id=-1, code=2104, message="Hardware connection", reject="")
    await settle()

    assert listener.errors == []
    assert manager.last_error is None


async def test_dispatch_without_listener_is_harmless():
    manager = make_manager()

    manager._on_open_order(17, contract(), order(), order_state())
    await settle()

    assert len(manager._orders) == 1


async def test_dispatch_before_loop_capture_is_skipped():
    """Callbacks that arrive before connect() has a loop must not crash the reader."""
    manager = IBKRConnectionManager(
        IBKRConnectionConfig(host="127.0.0.1", port=7497, client_id=99)
    )
    listener = RecordingListener()
    manager.add_order_listener(listener)

    manager._on_open_order(17, contract(), order(), order_state())

    assert listener.updates == []


async def test_listener_exception_does_not_escape_into_the_reader_thread():
    class Exploding:
        async def apply_order_update(self, **kwargs):
            raise RuntimeError("database is down")

    manager = make_manager()
    manager.add_order_listener(Exploding())

    manager._on_open_order(17, contract(), order(), order_state())
    await settle()

    # The reader thread survives; the failure is logged by the done-callback.
    assert len(manager._orders) == 1
