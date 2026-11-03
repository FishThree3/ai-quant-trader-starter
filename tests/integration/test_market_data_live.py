"""Opt-in live market data verification against a real TWS session.

Skipped unless ENABLE_MARKET_DATA_TESTS=true. This is the check to re-run after
adding a market-data subscription (or selecting "Delayed Quotes" at TWS login): it
proves ticks actually flow into the quote model, which no offline test can show.
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ENABLE_MARKET_DATA_TESTS") != "true",
    reason="set ENABLE_MARKET_DATA_TESTS=true to verify live market data",
)

SYMBOLS = ("AAPL", "SPY")
SETTLE_SECONDS = 30.0
POLL_SECONDS = 1.0


async def test_live_ticks_produce_usable_quotes():
    from config.settings import settings
    from core.broker.ibkr.broker import IBKRBroker
    from core.broker.ibkr.connection import IBKRConnectionConfig
    from core.market.resolver import ContractResolver
    from core.market.service import MarketDataService

    if settings.trading_mode.value != "PAPER":
        pytest.fail("market data verification is only allowed against a Paper session")

    broker = IBKRBroker(
        IBKRConnectionConfig(
            host=settings.ibkr_host,
            port=settings.ibkr_port,
            client_id=settings.ibkr_client_id + 3,
            timeout_seconds=settings.ibkr_connect_timeout_seconds,
        )
    )
    service = MarketDataService(
        broker, stale_seconds=float(settings.ibkr_stale_data_seconds)
    )
    broker.add_order_listener(service)

    resolver = ContractResolver()
    instruments = [resolver.resolve(symbol) for symbol in SYMBOLS]

    try:
        await broker.connect()
        broker.set_market_data_type(settings.market_data_type)

        for instrument in instruments:
            assert await service.subscribe(instrument, "verification"), (
                f"subscription refused for {instrument.symbol}"
            )

        complete = None
        waited = 0.0
        while waited < SETTLE_SECONDS:
            complete = next(
                (
                    quote
                    for quote in (service.quote(i) for i in instruments)
                    if quote is not None and quote.bid and quote.ask
                ),
                None,
            )
            if complete is not None:
                break
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS

        if complete is None:
            seen = {i.symbol: service.quote(i) for i in instruments}
            pytest.fail(
                "no bid/ask arrived for "
                f"{', '.join(SYMBOLS)} within {SETTLE_SECONDS:.0f}s; partial state={seen}. "
                "This account has no market data entitlement: add a subscription (or the "
                "free delayed package) and re-login choosing 'Delayed Quotes'."
            )

        # The freshness gate is what trading depends on, so check it on real time.
        quote = service.require_fresh(complete.instrument)

        assert quote.mid and quote.mid > 0
        assert quote.spread is not None and quote.spread >= 0
        assert quote.spread_pct is not None

        print(
            {
                "symbol": quote.instrument.symbol,
                "bid": quote.bid,
                "ask": quote.ask,
                "mid": round(quote.mid, 4),
                "spread_pct": round(quote.spread_pct, 4),
                "volume": quote.volume,
                "delayed": quote.delayed,
                "lines_open": service.line_count(),
                "ticker_limit_reached": service.ticker_limit_reached,
            }
        )
    finally:
        for instrument in instruments:
            await service.unsubscribe(instrument, "verification")
        await broker.disconnect()


async def test_unsubscribing_releases_every_broker_line():
    from config.settings import settings
    from core.broker.ibkr.broker import IBKRBroker
    from core.broker.ibkr.connection import IBKRConnectionConfig
    from core.market.resolver import ContractResolver
    from core.market.service import MarketDataService

    broker = IBKRBroker(
        IBKRConnectionConfig(
            host=settings.ibkr_host,
            port=settings.ibkr_port,
            client_id=settings.ibkr_client_id + 4,
            timeout_seconds=settings.ibkr_connect_timeout_seconds,
        )
    )
    service = MarketDataService(broker)
    broker.add_order_listener(service)
    instrument = ContractResolver().resolve("AAPL")

    try:
        await broker.connect()
        await service.subscribe(instrument, "a")
        await service.subscribe(instrument, "b")
        assert service.line_count() == 1, "two consumers must share one line"

        assert await service.unsubscribe(instrument, "a") is False
        assert await service.unsubscribe(instrument, "b") is True
        assert service.line_count() == 0
    finally:
        await broker.disconnect()
