import asyncio
from datetime import datetime, timezone

import pytest
from fakeredis.aioredis import FakeRedis

from core.market.models import MarketInstrument, Quote
from core.market.redis_store import RedisQuoteStore

AAPL = MarketInstrument("AAPL", "STK", "SMART", "USD")
MSFT = MarketInstrument("MSFT", "STK", "SMART", "USD")
T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


def quote_for(instrument=AAPL, **kwargs) -> Quote:
    base = {"instrument": instrument, "received_at": T0, "bid": 100.0, "ask": 100.2}
    base.update(kwargs)
    return Quote(**base)


@pytest.fixture
async def store():
    client = FakeRedis(decode_responses=True)
    yield RedisQuoteStore(client, ttl_seconds=60)
    await client.aclose()


async def test_round_trip_preserves_derived_fields(store):
    await store.save(quote_for())

    loaded = await store.load(AAPL.key)

    assert loaded["symbol"] == "AAPL"
    assert loaded["bid"] == 100.0
    assert loaded["mid"] == pytest.approx(100.1)
    assert loaded["key"] == AAPL.key


async def test_absent_instrument_loads_as_none(store):
    assert await store.load(MSFT.key) is None


async def test_quotes_expire_so_a_dead_worker_cannot_look_current(store):
    await store.save(quote_for())

    ttl = await store.client.ttl(f"quant:md:{AAPL.key}")

    assert 0 < ttl <= 60


async def test_latest_write_wins_for_one_instrument(store):
    await store.save(quote_for(bid=100.0))
    await store.save(quote_for(bid=101.5, received_at=T0.replace(second=1)))

    loaded = await store.load(AAPL.key)

    assert loaded["bid"] == 101.5


async def test_keys_strip_the_prefix(store):
    await store.save(quote_for())
    await store.save(quote_for(instrument=MSFT))

    assert await store.keys() == sorted([AAPL.key, MSFT.key])


async def test_stream_publishes_ticks_to_subscribers(store):
    received: list[dict] = []

    async def collect():
        async for tick in store.stream():
            received.append(tick)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)

    await store.save(quote_for())
    await store.save(quote_for(instrument=MSFT))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [t["symbol"] for t in received] == ["AAPL", "MSFT"]


async def test_stream_can_filter_to_one_instrument(store):
    received: list[dict] = []

    async def collect():
        async for tick in store.stream(AAPL.key):
            received.append(tick)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)

    await store.save(quote_for(instrument=MSFT))
    await store.save(quote_for())
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [t["symbol"] for t in received] == ["AAPL"]


async def test_from_url_builds_a_client_without_connecting():
    store = RedisQuoteStore.from_url("redis://localhost:6379/0")

    assert store.ttl_seconds == 60
    await store.aclose()
