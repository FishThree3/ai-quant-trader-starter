from datetime import datetime, timedelta, timezone

import pytest

from core.events.bus import EventBus
from core.events.models import EventType
from core.market.models import MarketInstrument
from core.market.service import (
    MAX_TICKERS_ERROR,
    MarketDataService,
    NoMarketData,
    StaleMarketData,
)
from core.market.store import InMemoryQuoteStore

T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
AAPL = MarketInstrument("AAPL", "STK", "SMART", "USD")
MSFT = MarketInstrument("MSFT", "STK", "SMART", "USD")


class FakeGateway:
    def __init__(self, *, fail_subscribe=False):
        self.subscribed = []
        self.cancelled = []
        self.fail_subscribe = fail_subscribe

    def subscribe(self, instrument, req_id):
        if self.fail_subscribe:
            raise OSError("socket is closed")
        self.subscribed.append((instrument.key, req_id))

    def unsubscribe(self, req_id):
        self.cancelled.append(req_id)


def service(gateway=None, **overrides):
    kwargs = {
        "stale_seconds": 10.0,
        "req_id_start": 90_000,
        "clock": lambda: T0,
    }
    kwargs.update(overrides)
    return MarketDataService(gateway or FakeGateway(), **kwargs)


# --- reference counting -------------------------------------------------------


async def test_two_consumers_share_one_broker_line():
    gateway = FakeGateway()
    svc = service(gateway)

    await svc.subscribe(AAPL, "momentum_v1")
    await svc.subscribe(AAPL, "risk_gate")

    assert len(gateway.subscribed) == 1
    assert svc.line_count() == 1
    assert svc.subscribers(AAPL) == frozenset({"momentum_v1", "risk_gate"})


async def test_line_closes_only_when_the_last_consumer_leaves():
    gateway = FakeGateway()
    svc = service(gateway)
    await svc.subscribe(AAPL, "a")
    await svc.subscribe(AAPL, "b")

    closed_first = await svc.unsubscribe(AAPL, "a")
    assert closed_first is False, "line must stay open while another consumer remains"
    assert gateway.cancelled == []
    assert svc.subscribers(AAPL) == frozenset({"b"})

    closed_second = await svc.unsubscribe(AAPL, "b")
    assert closed_second is True
    assert gateway.cancelled == [90_000]
    assert svc.line_count() == 0


async def test_unsubscribing_an_unknown_instrument_is_reported():
    assert await service().unsubscribe(MSFT, "nobody") is False


async def test_each_instrument_gets_its_own_req_id_from_a_reserved_range():
    gateway = FakeGateway()
    svc = service(gateway)

    await svc.subscribe(AAPL, "a")
    await svc.subscribe(MSFT, "b")

    req_ids = [req for _, req in gateway.subscribed]
    assert req_ids == [90_000, 90_001], "md ids must not collide with broker order ids"


# --- tick application ---------------------------------------------------------


async def test_ticks_assemble_one_quote_across_field_ids():
    svc = service()
    await svc.subscribe(AAPL, "a")

    await svc.apply_tick(90_000, 1, 100.0)  # BID
    await svc.apply_tick(90_000, 2, 100.2)  # ASK
    await svc.apply_tick(90_000, 0, 300.0)  # BID_SIZE
    await svc.apply_tick(90_000, 8, 12000.0)  # VOLUME

    quote = svc.quote(AAPL)

    assert (quote.bid, quote.ask, quote.bid_size, quote.volume) == (100.0, 100.2, 300.0, 12000.0)
    assert quote.mid == pytest.approx(100.1)
    assert quote.delayed is False


async def test_delayed_field_ids_mark_the_quote_delayed():
    svc = service()
    await svc.subscribe(AAPL, "a")

    await svc.apply_tick(90_000, 66, 99.0)  # DELAYED_BID
    await svc.apply_tick(90_000, 67, 99.5)  # DELAYED_ASK

    quote = svc.quote(AAPL)

    assert quote.delayed is True
    assert quote.bid == 99.0


async def test_zero_price_ticks_are_dropped():
    svc = service()
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)

    await svc.apply_tick(90_000, 2, 0.0)

    assert svc.quote(AAPL).ask is None
    assert svc.quote(AAPL).bid == 100.0


async def test_unmapped_and_unknown_ticks_are_ignored():
    svc = service()
    await svc.subscribe(AAPL, "a")

    assert await svc.apply_tick(90_000, 43, 1.0) is None  # not a field we cache
    assert await svc.apply_tick(12345, 1, 1.0) is None  # not our line


# --- freshness gate -----------------------------------------------------------


async def test_require_fresh_raises_without_data():
    svc = service()

    with pytest.raises(NoMarketData):
        svc.require_fresh(AAPL)


async def test_require_fresh_rejects_a_stale_quote():
    now = {"at": T0}
    svc = service(clock=lambda: now["at"])
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)

    now["at"] = T0 + timedelta(seconds=11)

    with pytest.raises(StaleMarketData, match="11.0s"):
        svc.require_fresh(AAPL)


async def test_require_fresh_returns_the_quote_when_current():
    svc = service()
    await svc.subscribe(AAPL, "a")
    quote = await svc.apply_tick(90_000, 1, 100.0)

    assert svc.require_fresh(AAPL) is quote


# --- failure handling without retry loops -------------------------------------


async def test_ticker_limit_trips_a_breaker_and_stops_new_requests():
    gateway = FakeGateway()
    svc = service(gateway)
    await svc.subscribe(AAPL, "a")

    await svc.apply_error(-1, MAX_TICKERS_ERROR, "Max number of tickers has been reached.")

    assert svc.ticker_limit_reached is True
    assert await svc.subscribe(MSFT, "b") is False
    assert len(gateway.subscribed) == 1, "must not ask for another line after the limit"


async def test_entitlement_error_blocks_the_line_without_re_requesting():
    gateway = FakeGateway()
    svc = service(gateway)
    await svc.subscribe(AAPL, "a")

    await svc.apply_error(90_000, 354, "Not subscribed to market data")

    assert svc.ticker_limit_reached is False
    # A later consumer for the same instrument reuses the memo, it does not re-ask.
    assert await svc.subscribe(AAPL, "b") is False
    assert len(gateway.subscribed) == 1


async def test_blocked_line_keeps_its_req_id_from_leaking():
    gateway = FakeGateway()
    svc = service(gateway)
    await svc.subscribe(AAPL, "a")

    await svc.apply_error(90_000, 10168, "Subscribed market data not connected")

    assert await svc.subscribe(MSFT, "b") is True
    assert [req for _, req in gateway.subscribed] == [90_000, 90_001]


async def test_subscribe_failure_does_not_leave_a_line_behind():
    gateway = FakeGateway(fail_subscribe=True)
    svc = service(gateway)

    assert await svc.subscribe(AAPL, "a") is False
    assert svc.line_count() == 0
    assert await svc.subscribe(AAPL, "b") is False


async def test_unsubscribe_swallows_a_dead_socket():
    class DeadGateway(FakeGateway):
        def unsubscribe(self, req_id):
            raise OSError("socket gone")

    svc = service(DeadGateway())
    await svc.subscribe(AAPL, "a")

    assert await svc.unsubscribe(AAPL, "a") is True
    assert svc.line_count() == 0


async def test_unrelated_error_codes_do_not_block_a_line():
    svc = service()
    await svc.subscribe(AAPL, "a")

    await svc.apply_error(90_000, 2104, "Market data farm connection is OK")

    assert svc.quote(AAPL) is None  # line still healthy, no data yet
    assert await svc.subscribe(AAPL, "b") is True


# --- publishing ---------------------------------------------------------------


async def test_quote_is_published_to_store_and_event_bus():
    store = InMemoryQuoteStore()
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.MARKET_TICK, lambda event: _record(seen, event))
    svc = service(store=store, event_bus=bus)

    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)

    stored = await store.load(AAPL.key)
    assert stored["bid"] == 100.0
    assert stored["symbol"] == "AAPL"
    assert seen and seen[0].type is EventType.MARKET_TICK
    assert (await store.keys()) == [AAPL.key]


async def _record(sink, event):
    sink.append(event)


async def test_store_receives_derived_mid_for_readers():
    store = InMemoryQuoteStore()
    svc = service(store=store)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 100.4)

    assert (await store.load(AAPL.key))["mid"] == pytest.approx(100.2)


# --- risk gate integration: stale data must refuse new trades -----------------


async def test_risk_engine_approves_on_fresh_data_and_refuses_when_it_ages_out():
    from datetime import datetime, timedelta, timezone

    from core.broker.base import BrokerState, BrokerStatus
    from core.risk.engine import RiskEngine, RiskLimits, RiskSnapshot
    from core.risk.models import RiskDecisionType, TradeIntent

    # RiskEngine reads the wall clock, so this scenario uses real timestamps.
    fresh_at = datetime.now(timezone.utc)
    svc = service(clock=lambda: fresh_at, stale_seconds=10.0)
    await svc.subscribe(AAPL, "risk_gate")
    await svc.apply_tick(90_000, 1, 100.0, received_at=fresh_at)
    await svc.apply_tick(90_000, 2, 100.2, received_at=fresh_at)

    engine = RiskEngine(
        RiskLimits(
            max_daily_loss_pct=1.0,
            max_total_exposure_pct=20.0,
            max_open_positions=3,
            max_position_pct=5.0,
            max_trade_risk_pct=0.25,
            max_spread_pct=0.30,
            stale_data_seconds=10,
        )
    )

    def evaluate(quote):
        intent = TradeIntent(symbol="AAPL", side="BUY", quantity=1.0, reference_price=quote.mid)
        snapshot = RiskSnapshot(
            nav=100_000.0,
            daily_pnl=0.0,
            gross_exposure=0.0,
            open_positions=0,
            market_data_at=quote.received_at,
            spread_pct=quote.spread_pct,
        )
        status = BrokerStatus(state=BrokerState.READY, connected=True, account="DUR1")
        return engine.evaluate(intent, status, snapshot)

    assert evaluate(svc.require_fresh(AAPL)).decision is RiskDecisionType.APPROVED

    aged = await svc.apply_tick(
        90_000, 1, 100.0, received_at=datetime.now(timezone.utc) - timedelta(seconds=11)
    )

    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)

    # Even if a caller ignored the gate, risk's own staleness rule still refuses.
    decision = evaluate(aged)
    assert decision.decision is RiskDecisionType.REJECTED
    assert decision.rule == "market_data_stale"
