"""Real IBKR wire behaviour for market data, without a socket."""

import asyncio
from datetime import datetime, timezone

import pytest

from core.broker.base import BrokerState
from core.broker.ibkr.connection import IBKRConnectionConfig, IBKRConnectionManager
from core.market.models import MarketInstrument
from core.market.service import MarketDataService

pytest.importorskip("ibapi", reason="these tests exercise the real IBKR wire objects")

AAPL = MarketInstrument("AAPL", "STK", "SMART", "USD")
T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


class RecordingApp:
    def __init__(self):
        self.md_requests = []
        self.md_cancelled = []
        self.data_types = []

    def isConnected(self):  # noqa: N802
        return True

    def reqMktData(  # noqa: N802
        self, req_id, contract, generic_tick_list, snapshot, regulatory, options
    ):
        self.md_requests.append((req_id, contract, generic_tick_list, snapshot))

    def cancelMktData(self, req_id):  # noqa: N802
        self.md_cancelled.append(req_id)

    def reqMarketDataType(self, mode):  # noqa: N802
        self.data_types.append(mode)


def make_manager() -> IBKRConnectionManager:
    manager = IBKRConnectionManager(IBKRConnectionConfig(host="127.0.0.1", port=7497, client_id=99))
    manager._app = RecordingApp()
    manager.next_order_id = 1
    manager.state = BrokerState.READY
    return manager


def with_loop(manager: IBKRConnectionManager) -> IBKRConnectionManager:
    """connect() captures the running loop; dispatch needs it to hand work back."""
    manager._loop = asyncio.get_running_loop()
    return manager


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_subscribe_sends_a_streaming_request_with_the_right_contract():
    manager = make_manager()

    manager.subscribe(AAPL, 90_000)

    req_id, contract, generic, snapshot = manager._app.md_requests[0]
    assert (req_id, generic, snapshot) == (90_000, "", False)
    assert (contract.symbol, contract.secType) == ("AAPL", "STK")
    assert (contract.exchange, contract.currency) == ("SMART", "USD")


def test_unsubscribe_cancels_only_that_request_id():
    manager = make_manager()
    manager.subscribe(AAPL, 90_000)
    manager.subscribe(MarketInstrument("MSFT", "STK", "SMART", "USD"), 90_001)

    manager.unsubscribe(90_000)

    assert manager._app.md_cancelled == [90_000]


def test_set_market_data_type_reaches_the_socket():
    manager = make_manager()

    manager.set_market_data_type(3)

    assert manager._app.data_types == [3]


def test_market_data_calls_need_ibapi_and_a_live_app():
    manager = IBKRConnectionManager(IBKRConnectionConfig(host="h", port=1, client_id=1))

    with pytest.raises(Exception):
        manager.subscribe(AAPL, 90_000)

    # unsubscribe is safe to call with no socket at all
    manager.unsubscribe(90_000)


async def test_tick_price_callback_feeds_the_market_data_service():
    manager = with_loop(make_manager())
    service = MarketDataService(manager, stale_seconds=10.0, clock=lambda: T0)
    manager.add_order_listener(service)

    await service.subscribe(AAPL, "worker")
    assert manager._app.md_requests[0][0] == 90_000

    manager._on_tick(90_000, 1, 100.0)  # BID
    manager._on_tick(90_000, 2, 100.2)  # ASK
    await settle()

    quote = service.quote(AAPL)
    assert quote is not None
    assert (quote.bid, quote.ask) == (100.0, 100.2)
    assert quote.mid == pytest.approx(100.1)


async def test_tick_size_callback_updates_sizes():
    manager = with_loop(make_manager())
    service = MarketDataService(manager)
    manager.add_order_listener(service)
    await service.subscribe(AAPL, "worker")

    manager._on_tick(90_000, 0, 300)  # BID_SIZE
    await settle()

    assert service.quote(AAPL).bid_size == 300.0


async def test_error_101_from_the_socket_trips_the_service_breaker():
    manager = with_loop(make_manager())
    service = MarketDataService(manager)
    manager.add_order_listener(service)
    await service.subscribe(AAPL, "worker")

    manager._on_error(
        req_id=-1, code=101, message="Max number of tickers has been reached.", reject=""
    )
    await settle()

    assert service.ticker_limit_reached is True
    msft = MarketInstrument("MSFT", "STK", "SMART", "USD")
    assert await service.subscribe(msft, "worker") is False
    assert len(manager._app.md_requests) == 1


async def test_entitlement_error_is_memoised_instead_of_re_requested():
    manager = with_loop(make_manager())
    service = MarketDataService(manager)
    manager.add_order_listener(service)
    await service.subscribe(AAPL, "worker")

    manager._on_error(
        req_id=90_000,
        code=354,
        message="No market data permissions for ISLAND STK",
        reject="",
    )
    await settle()

    assert await service.subscribe(AAPL, "risk_gate") is False
    assert len(manager._app.md_requests) == 1, "must not re-request refused data"


async def test_ticks_for_other_request_ids_are_ignored():
    manager = with_loop(make_manager())
    service = MarketDataService(manager)
    manager.add_order_listener(service)
    await service.subscribe(AAPL, "worker")

    manager._on_tick(42, 1, 999.0)
    await settle()

    assert service.quote(AAPL) is None, "only ticks for our own line may create a quote"
