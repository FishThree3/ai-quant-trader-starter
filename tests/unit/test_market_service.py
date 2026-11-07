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


async def test_zero_price_ticks_clear_the_affected_side():
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
    await svc.apply_tick(90_000, 2, 100.2)

    now["at"] = T0 + timedelta(seconds=11)

    with pytest.raises(StaleMarketData, match="11.0s"):
        svc.require_fresh(AAPL)


async def test_require_fresh_returns_the_quote_when_current():
    svc = service()
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    quote = await svc.apply_tick(90_000, 2, 100.2)

    assert svc.require_fresh(AAPL) is quote


@pytest.mark.parametrize("tick_type", [0, 3, 4, 5, 8])
async def test_non_bbo_activity_does_not_refresh_stale_executable_prices(tick_type):
    now = {"at": T0}
    svc = service(clock=lambda: now["at"])
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    now["at"] += timedelta(seconds=11)

    q = await svc.apply_tick(90_000, tick_type, 100.5)

    assert q.received_at == now["at"]
    assert q.bid_at == q.ask_at == q.price_at == T0
    with pytest.raises(StaleMarketData, match="11.0s"):
        svc.require_fresh(AAPL)


@pytest.mark.parametrize("tick_type", [1, 2, 4])
async def test_single_sided_or_last_only_ticks_are_never_executable(tick_type):
    svc = service()
    await svc.subscribe(AAPL, "a")
    q = await svc.apply_tick(90_000, tick_type, 100.0)
    assert q.mid == 100.0
    with pytest.raises(StaleMarketData, match="two-sided"):
        svc.require_fresh(AAPL)


async def test_refreshing_one_side_does_not_refresh_the_other():
    now = {"at": T0}
    svc = service(clock=lambda: now["at"])
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    now["at"] += timedelta(seconds=11)
    await svc.apply_tick(90_000, 1, 100.0)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    q = await svc.apply_tick(90_000, 2, 101.0)
    assert svc.require_fresh(AAPL) is q


@pytest.mark.parametrize("value", [None, 0.0, -1.0, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("tick_type, field", [(1, "bid"), (2, "ask")])
async def test_invalid_bbo_ticks_withdraw_cached_prices_and_publish(value, tick_type, field):
    store = InMemoryQuoteStore()
    svc = service(store=store)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    assert svc.require_fresh(AAPL)

    q = await svc.apply_tick(90_000, tick_type, value)

    assert getattr(q, field) is None
    assert getattr(q, f"{field}_at") is None
    assert (await store.load(AAPL.key))[field] is None
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)


@pytest.mark.parametrize("tick_type, field", [(0, "bid_size"), (4, "last"), (8, "volume")])
async def test_nonfinite_non_bbo_values_are_not_published(tick_type, field):
    import json

    store = InMemoryQuoteStore()
    svc = service(store=store)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    await svc.apply_tick(90_000, tick_type, float("nan"))
    payload = await store.load(AAPL.key)
    assert payload[field] is None
    json.dumps(payload, allow_nan=False)
    assert svc.require_fresh(AAPL)


@pytest.mark.parametrize("stamp", [T0 + timedelta(seconds=1), T0.replace(tzinfo=None)])
@pytest.mark.parametrize("tick_type, field", [(1, "bid"), (2, "ask")])
async def test_invalid_tick_timestamps_withdraw_old_price_without_aging_in(stamp, tick_type, field):
    now = {"at": T0}
    svc = service(clock=lambda: now["at"])
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    q = await svc.apply_tick(90_000, tick_type, 100.5, received_at=stamp)
    assert q.received_at == T0  # local receipt time, not the invalid supplied stamp
    assert getattr(q, field) is None
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    now["at"] += timedelta(seconds=2)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)


async def test_out_of_order_price_tick_remains_stale_even_after_other_side_updates():
    svc = service()
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    await svc.apply_tick(90_000, 1, 100.0, received_at=T0 - timedelta(seconds=11))
    await svc.apply_tick(90_000, 2, 101.0)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)


@pytest.mark.parametrize("delayed_type", [66, 67, 68, 69, 70, 71, 74])
async def test_every_delayed_variant_latches_until_both_realtime_bbo_sides(delayed_type):
    svc = service()
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    await svc.apply_tick(90_000, delayed_type, 100.5)
    await svc.apply_tick(90_000, 4, 100.5)  # real-time last is insufficient
    await svc.apply_tick(90_000, 0, 1.0)  # as are sizes/volume
    await svc.apply_tick(90_000, 8, 1.0)
    await svc.apply_market_data_type(90_000, 1)  # a mode switch is not a price tick
    assert svc.quote(AAPL).delayed
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    await svc.apply_tick(90_000, 1, 100.0)
    assert svc.quote(AAPL).delayed
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    q = await svc.apply_tick(90_000, 2, 101.0)
    assert not q.delayed
    assert svc.require_fresh(AAPL) is q


@pytest.mark.parametrize("mode", [2, 3, 4, 0, 99])
async def test_actual_non_realtime_modes_block_normal_tick_ids_until_recovery(mode):
    store = InMemoryQuoteStore()
    svc = service(store=store)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    await svc.apply_market_data_type(90_000, mode)
    assert (await store.load(AAPL.key))["delayed"]
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    await svc.apply_market_data_type(90_000, 1)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    await svc.apply_tick(90_000, 1, 100.0)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    q = await svc.apply_tick(90_000, 2, 101.0)
    assert q.market_data_type == 1
    assert svc.require_fresh(AAPL) is q


async def test_mode_before_first_tick_and_late_delayed_ticks_fail_closed():
    svc = service()
    await svc.subscribe(AAPL, "a")
    await svc.apply_market_data_type(90_000, 2)
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    await svc.apply_market_data_type(90_000, 1)
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 74, 1.0)  # discards the recovery bid
    await svc.apply_tick(90_000, 2, 101.0)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    await svc.apply_tick(90_000, 1, 100.0)
    assert svc.require_fresh(AAPL)


async def test_mode_notifications_do_not_refresh_any_timestamp():
    now = {"at": T0}
    svc = service(clock=lambda: now["at"])
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    now["at"] += timedelta(seconds=11)
    await svc.apply_market_data_type(90_000, 1)
    q = svc.quote(AAPL)
    assert q.received_at == q.bid_at == q.ask_at == T0
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    await svc.apply_market_data_type(12345, 3)  # another service's request
    assert svc.quote(AAPL) is q


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


async def test_last_unsubscribe_clears_local_and_published_quote_and_ignores_late_ticks():
    store = InMemoryQuoteStore()
    svc = service(store=store)
    await svc.subscribe(AAPL, "a")
    await svc.subscribe(AAPL, "b")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    assert not await svc.unsubscribe(AAPL, "a")
    assert svc.require_fresh(AAPL)
    assert await svc.unsubscribe(AAPL, "b")
    assert svc.quote(AAPL) is None
    with pytest.raises(NoMarketData):
        svc.require_fresh(AAPL)
    payload = await store.load(AAPL.key)
    assert payload["bid"] is payload["ask"] is payload["price_at"] is None
    assert payload["received_at"] == T0.isoformat()
    assert await svc.apply_tick(90_000, 1, 102.0) is None
    await svc.apply_market_data_type(90_000, 1)
    assert await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_001, 1, 100.0)
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)
    await svc.apply_tick(90_001, 2, 101.0)
    assert svc.require_fresh(AAPL)


@pytest.mark.parametrize("code", [354, 10168, 10089, MAX_TICKERS_ERROR])
async def test_rejected_line_withdraws_cached_quote_and_rejects_late_callbacks(code):
    store = InMemoryQuoteStore()
    svc = service(store=store)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    await svc.apply_error(90_000, code, "subscription rejected")
    assert svc.quote(AAPL) is None
    assert (await store.load(AAPL.key))["bid"] is None
    with pytest.raises(NoMarketData):
        svc.require_fresh(AAPL)
    assert await svc.subscribe(AAPL, "b") is False
    assert await svc.apply_tick(90_000, 1, 100.0) is None
    assert await svc.apply_tick(90_000, 2, 101.0) is None
    await svc.apply_market_data_type(90_000, 1)
    assert svc.quote(AAPL) is None


async def test_delayed_data_warning_keeps_display_subscription_but_blocks_execution():
    svc = service()
    await svc.subscribe(AAPL, "a")
    await svc.apply_error(90_000, 10167, "Displaying delayed market data")
    assert await svc.subscribe(AAPL, "b") is True
    await svc.apply_tick(90_000, 66, 100.0)
    await svc.apply_tick(90_000, 67, 101.0)
    quote = svc.quote(AAPL)
    assert quote.bid == 100.0 and quote.ask == 101.0
    assert quote.delayed and quote.market_data_type == 3
    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)


async def test_rejecting_one_line_does_not_invalidate_other_subscriptions():
    svc = service()
    await svc.subscribe(AAPL, "a")
    await svc.subscribe(MSFT, "a")
    for req_id in (90_000, 90_001):
        await svc.apply_tick(req_id, 1, 100.0)
        await svc.apply_tick(req_id, 2, 101.0)
    await svc.apply_error(90_000, MAX_TICKERS_ERROR, "max tickers")
    assert svc.require_fresh(MSFT)
    with pytest.raises(NoMarketData):
        svc.require_fresh(AAPL)


async def test_subscribe_failure_withdraws_an_older_store_entry():
    from core.market.models import Quote

    store = InMemoryQuoteStore()
    await store.save(Quote(AAPL, T0, bid=100.0, ask=101.0))
    svc = service(FakeGateway(fail_subscribe=True), store=store)
    assert not await svc.subscribe(AAPL, "a")
    assert (await store.load(AAPL.key))["price_at"] is None
    assert svc.quote(AAPL) is None


async def test_ticker_limit_refusal_also_withdraws_an_older_store_entry():
    from core.market.models import Quote

    store = InMemoryQuoteStore()
    await store.save(Quote(AAPL, T0, bid=100.0, ask=101.0))
    svc = service(store=store)
    await svc.apply_error(-1, MAX_TICKERS_ERROR, "max tickers")
    assert not await svc.subscribe(AAPL, "a")
    assert (await store.load(AAPL.key))["price_at"] is None
    assert svc.quote(AAPL) is None


async def test_unsubscribe_invalidates_even_when_gateway_cannot_cancel():
    class DeadGateway(FakeGateway):
        def unsubscribe(self, req_id):
            raise OSError("socket gone")

    store = InMemoryQuoteStore()
    svc = service(DeadGateway(), store=store)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    assert await svc.unsubscribe(AAPL, "a")
    with pytest.raises(NoMarketData):
        svc.require_fresh(AAPL)
    assert (await store.load(AAPL.key))["bid"] is None


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


@pytest.mark.parametrize("change", ["unsubscribe", "reject", "frozen"])
async def test_inflight_old_tick_cannot_overwrite_published_invalidation(change):
    import asyncio

    class PausingStore(InMemoryQuoteStore):
        def __init__(self):
            super().__init__()
            self.pause_next = False
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def save(self, quote):
            if self.pause_next:
                self.pause_next = False
                self.started.set()
                await self.release.wait()
            await super().save(quote)

    store = PausingStore()
    svc = service(store=store)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    store.pause_next = True
    tick = asyncio.create_task(svc.apply_tick(90_000, 1, 100.1))
    await asyncio.wait_for(store.started.wait(), timeout=1)
    if change == "unsubscribe":
        update = svc.unsubscribe(AAPL, "a")
    elif change == "reject":
        update = svc.apply_error(90_000, 354, "not entitled")
    else:
        update = svc.apply_market_data_type(90_000, 2)
    invalidation = asyncio.create_task(update)
    await asyncio.sleep(0)
    try:
        with pytest.raises((NoMarketData, StaleMarketData)):
            svc.require_fresh(AAPL)
    finally:
        store.release.set()
        await asyncio.wait_for(asyncio.gather(tick, invalidation), timeout=1)
    payload = await store.load(AAPL.key)
    if change == "frozen":
        assert payload["market_data_type"] == 2
        assert payload["delayed"]
    else:
        assert payload["bid"] is payload["ask"] is None


async def test_failed_store_does_not_preserve_local_quote_or_skip_invalidation_event():
    class FailingStore(InMemoryQuoteStore):
        async def save(self, quote):
            raise OSError("store offline")

    bus = EventBus()
    seen = []
    bus.subscribe(EventType.MARKET_TICK, lambda event: _record(seen, event))
    svc = service(store=FailingStore(), event_bus=bus)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    assert svc.require_fresh(AAPL)
    await svc.unsubscribe(AAPL, "a")
    with pytest.raises(NoMarketData):
        svc.require_fresh(AAPL)
    assert seen[-1].payload["bid"] is None
    assert seen[-1].payload["price_at"] is None


async def test_event_handler_can_release_subscription_without_deadlock():
    import asyncio

    bus = EventBus()
    store = InMemoryQuoteStore()
    svc = service(store=store, event_bus=bus)

    async def release_on_price(event):
        if event.payload["bid"] is not None:
            await svc.unsubscribe(AAPL, "a")

    bus.subscribe(EventType.MARKET_TICK, release_on_price)
    await svc.subscribe(AAPL, "a")
    await asyncio.wait_for(svc.apply_tick(90_000, 1, 100.0), timeout=1)
    assert svc.quote(AAPL) is None
    assert (await store.load(AAPL.key))["bid"] is None


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
        intent = TradeIntent(
            symbol="AAPL", side="BUY", quantity=1.0, reference_price=quote.mid, stop_price=99.0
        )
        snapshot = RiskSnapshot(
            nav=100_000.0,
            daily_pnl=0.0,
            gross_exposure=0.0,
            open_positions=0,
            market_data_at=quote.price_at,
            spread_pct=quote.spread_pct,
        )
        status = BrokerStatus(state=BrokerState.READY, connected=True, account="DUR1")
        return engine.evaluate(intent, status, snapshot)

    assert evaluate(svc.require_fresh(AAPL)).decision is RiskDecisionType.APPROVED

    await svc.apply_tick(
        90_000, 1, 100.0, received_at=datetime.now(timezone.utc) - timedelta(seconds=11)
    )
    aged = await svc.apply_tick(90_000, 8, 1000.0, received_at=fresh_at)
    assert aged.received_at == fresh_at  # transport must not substitute for price_at

    with pytest.raises(StaleMarketData):
        svc.require_fresh(AAPL)

    # Even if a caller ignored the gate, risk's own staleness rule still refuses.
    decision = evaluate(aged)
    assert decision.decision is RiskDecisionType.REJECTED
    assert decision.rule == "market_data_stale"
