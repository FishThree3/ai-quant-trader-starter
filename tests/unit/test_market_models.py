from datetime import datetime, timedelta, timezone

import pytest

from core.market.models import MarketInstrument, Quote
from core.market.resolver import (
    ContractResolutionError,
    ContractResolver,
    resolve_us_equity,
)

T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
AAPL = MarketInstrument("AAPL", "STK", "SMART", "USD")


def quote(**kwargs) -> Quote:
    base = {"instrument": AAPL, "received_at": T0}
    base.update(kwargs)
    return Quote(**base)


# --- quote maths --------------------------------------------------------------


def test_mid_is_the_touch():
    assert quote(bid=100.0, ask=100.10).mid == pytest.approx(100.05)


def test_mid_falls_back_to_last_when_the_book_is_one_sided():
    assert quote(bid=100.0).mid == 100.0
    assert quote(ask=101.0).mid == 101.0
    assert quote(last=99.5).mid == 99.5


def test_zero_prices_are_absent_quotes_not_a_mid_of_zero():
    """IBKR uses 0.0 for 'no quote', which must never look like a real price."""
    assert quote(bid=0.0, ask=0.0, last=0.0).mid is None


def test_spread_percentage():
    q = quote(bid=100.0, ask=101.0)
    assert q.spread == pytest.approx(1.0)
    assert q.spread_pct == pytest.approx(0.995025)  # 1.0 / 100.5


def test_crossed_or_single_sided_book_has_no_spread():
    assert quote(bid=101.0, ask=100.0).spread is None
    assert quote(bid=100.0).spread is None
    assert quote(bid=100.0).spread_pct is None


def test_volume_zero_is_a_real_value_not_absence():
    assert quote(volume=0.0).volume == 0.0


def test_freshness_uses_the_supplied_clock():
    q = quote(bid=100.0, ask=101.0)

    assert q.is_fresh(10.0, T0 + timedelta(seconds=9))
    assert not q.is_fresh(10.0, T0 + timedelta(seconds=11))
    assert q.age_seconds(T0 + timedelta(seconds=5)) == 5.0


def test_quote_without_a_price_is_never_fresh():
    assert not quote().is_fresh(3600.0, T0)


def test_with_tick_replaces_only_the_touched_field():
    updated = quote(bid=100.0).with_tick("ask", 101.0, T0 + timedelta(seconds=1))

    assert updated.bid == 100.0
    assert updated.ask == 101.0
    assert updated.received_at == T0 + timedelta(seconds=1)


def test_serialisation_carries_derived_fields_for_readers():
    payload = quote(bid=100.0, ask=101.0, delayed=True).as_dict()

    assert payload["symbol"] == "AAPL"
    assert payload["mid"] == pytest.approx(100.5)
    assert payload["delayed"] is True
    assert payload["received_at"].startswith("2026-11-03")


def test_instrument_key_and_con_id_update():
    assert AAPL.key == "AAPL|STK|SMART|USD"
    assert AAPL.with_con_id(1234).con_id == 1234
    assert AAPL.with_con_id(None) is AAPL
    assert AAPL.with_con_id(AAPL.con_id) is AAPL


# --- executable freshness -----------------------------------------------------


@pytest.mark.parametrize("fields", [{"bid": 100.0}, {"ask": 101.0}, {"last": 100.5}])
def test_single_sided_and_last_only_quotes_are_display_only(fields):
    q = quote(**fields)
    assert q.mid is not None
    assert q.price_at is None
    assert not q.is_fresh(10, T0)


@pytest.mark.parametrize("field", ["bid_size", "ask_size", "last_size", "volume", "last"])
def test_other_ticks_preserve_bbo_timestamps(field):
    q = quote(bid=100.0, ask=101.0)
    updated = q.with_tick(field, 102.0, T0 + timedelta(seconds=11))
    assert updated.received_at == T0 + timedelta(seconds=11)
    assert updated.bid_at == updated.ask_at == updated.price_at == T0
    assert updated.age_seconds(updated.received_at) == 0
    assert updated.price_age_seconds(updated.received_at) == 11
    assert not updated.is_fresh(10, updated.received_at)


def test_old_constructor_infers_snapshot_stamps_once():
    q = Quote(AAPL, T0, 100.0, 1.0, 101.0, 2.0, 100.5, 3.0, 10.0, False)
    assert q.bid_at == q.ask_at == q.last_at == T0
    updated = q.with_tick("ask", 101.1, T0 + timedelta(seconds=11))
    assert updated.bid_at == updated.price_at == T0
    assert updated.ask_at == updated.received_at
    assert not updated.is_fresh(10, updated.received_at)


def test_explicit_unknown_stamp_never_falls_back_to_transport():
    q = quote(bid=100.0, ask=101.0, bid_at=None)
    updated = q.with_tick("volume", 10.0, T0)
    assert updated.bid_at is None
    assert not updated.is_fresh(10, T0)


@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["bid", "ask", "last"])
def test_invalid_prices_are_absent_even_in_direct_fixtures(field, value):
    q = quote(**{field: value})
    assert getattr(q, field) is None
    assert getattr(q, f"{field}_at") is None
    assert q.mid is None
    assert not q.is_fresh(10, T0)


@pytest.mark.parametrize("field", ["bid_size", "ask_size", "last_size", "volume"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_sizes_and_volume_are_absent(field, value):
    assert getattr(quote(**{field: value}), field) is None


def test_invalid_price_tick_withdraws_a_previously_fresh_side():
    q = quote(bid=100.0, ask=101.0).with_tick("bid", float("nan"), T0)
    assert q.bid is None
    assert q.bid_at is None
    assert not q.is_fresh(10, T0)


@pytest.mark.parametrize("field", ["bid_at", "ask_at", "received_at"])
def test_future_timestamps_fail_closed(field):
    future = T0 + timedelta(seconds=1)
    fields = {"received_at": T0, "bid_at": T0, "ask_at": T0, field: future}
    q = quote(bid=100.0, ask=101.0, **fields)
    assert not q.is_fresh(10, T0)


@pytest.mark.parametrize("field", ["bid_at", "ask_at", "received_at"])
def test_naive_timestamps_are_not_executable(field):
    q = quote(bid=100.0, ask=101.0, **{field: T0.replace(tzinfo=None)})
    assert not q.is_fresh(10, T0)


@pytest.mark.parametrize("limit", [-1, float("nan"), float("inf")])
def test_invalid_freshness_limits_fail_closed(limit):
    assert not quote(bid=100.0, ask=101.0).is_fresh(limit, T0)


def test_boundary_age_and_locked_book_are_accepted_but_crossed_is_not():
    q = quote(bid=100.0, ask=100.0)
    assert q.is_fresh(10, T0 + timedelta(seconds=10))
    assert not q.is_fresh(10, T0 + timedelta(seconds=10, microseconds=1))
    assert not quote(bid=101.0, ask=100.0).is_fresh(10, T0)


@pytest.mark.parametrize("mode", [0, 2, 3, 4, 99, None, True])
def test_non_realtime_and_unknown_modes_fail_closed(mode):
    assert not quote(bid=100.0, ask=101.0, market_data_type=mode).is_fresh(10, T0)


def test_delayed_quotes_with_recent_prices_are_not_executable():
    assert not quote(bid=100.0, ask=101.0, delayed=True).is_fresh(10, T0)


def test_serialisation_preserves_independent_price_and_transport_stamps():
    q = quote(bid=100.0, ask=101.0).with_tick("last", 100.5, T0 + timedelta(seconds=5))
    payload = q.as_dict()
    assert payload["bid_at"] == payload["ask_at"] == payload["price_at"] == T0.isoformat()
    assert payload["last_at"] == payload["received_at"] == q.received_at.isoformat()
    assert payload["market_data_type"] == 1
    assert quote().as_dict()["price_at"] is None


def test_extreme_finite_prices_do_not_overflow_mid():
    q = quote(bid=1.7e308, ask=1.7e308)
    assert q.mid == 1.7e308
    assert q.spread_pct == 0


# --- contract resolver --------------------------------------------------------


def test_resolver_builds_us_equity_on_smart_usd():
    instrument = resolve_us_equity("aapl")

    assert instrument == MarketInstrument("AAPL", "STK", "SMART", "USD")


@pytest.mark.parametrize("symbol", ["", "  ", "1AAPL", "AAPL&CO", "A" * 20, "AC/DC"])
def test_resolver_rejects_unusable_symbols(symbol):
    with pytest.raises(ContractResolutionError):
        resolve_us_equity(symbol)


def test_resolver_accepts_brk_style_symbols_with_dots_and_dashes():
    assert resolve_us_equity("BRK.B").symbol == "BRK.B"
    assert resolve_us_equity("GOOG-OLD").symbol == "GOOG-OLD"


def test_resolver_refuses_options_and_non_usd():
    resolver = ContractResolver()

    with pytest.raises(ContractResolutionError, match="not supported yet"):
        resolver.remember(MarketInstrument("AAPL", "OPT", "SMART", "USD"))

    with pytest.raises(ContractResolutionError, match="USD"):
        resolver.remember(MarketInstrument("SAP", "STK", "SMART", "EUR"))


def test_resolver_caches_and_exposes_known_instruments():
    resolver = ContractResolver()

    first = resolver.resolve("AAPL")
    second = resolver.resolve("aapl")

    assert first is second
    assert resolver.known("aapl") is first
    assert resolver.known("MSFT") is None


def test_resolver_stores_broker_con_id():
    resolver = ContractResolver()

    resolver.remember(MarketInstrument("AAPL", "STK", "SMART", "USD", con_id=265598))

    assert resolver.resolve("AAPL").con_id == 265598
