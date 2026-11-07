from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from apps.api.deps import get_quote_store
from apps.api.main import app
from core.market.models import MarketInstrument, Quote

AAPL = MarketInstrument("AAPL", "STK", "SMART", "USD")


def quote_payload(minutes_ago: float = 0.0) -> dict:
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    quote = Quote(instrument=AAPL, received_at=stamp, bid=100.0, ask=100.2, volume=1200.0)
    return quote.as_dict()


class FakeStore:
    def __init__(self, items=None, *, fail=False):
        self.items = items if items is not None else {}
        self.fail = fail
        self.loaded_keys: list[str] = []

    async def save(self, quote: Quote) -> None:
        self.items[quote.instrument.key] = quote.as_dict()

    async def load(self, instrument_key: str):
        self.loaded_keys.append(instrument_key)
        if self.fail:
            raise RedisError("connection refused")
        return self.items.get(instrument_key)

    async def keys(self):
        return sorted(self.items)

    async def stream(self, instrument_key=None):
        for payload in list(self.items.values()):
            if instrument_key is None or payload["key"] == instrument_key:
                yield payload


@pytest.fixture
def client_factory():
    @contextmanager
    def make(store):
        app.dependency_overrides[get_quote_store] = lambda: store
        try:
            with TestClient(app) as client:
                yield client
        finally:
            app.dependency_overrides.clear()

    return make


def test_quote_is_returned_with_freshness(client_factory):
    store = FakeStore({AAPL.key: quote_payload()})

    with client_factory(store) as client:
        response = client.get("/api/market/AAPL")

    assert response.status_code == 200
    body = response.json()
    assert body["bid"] == 100.0
    assert body["mid"] == pytest.approx(100.1)
    assert body["stale"] is False
    assert body["age_seconds"] < 5


def test_lookup_is_normalised_and_pinned_to_smart_usd(client_factory):
    store = FakeStore({AAPL.key: quote_payload()})

    with client_factory(store) as client:
        response = client.get("/api/market/aapl")

    assert response.status_code == 200
    assert store.loaded_keys == [AAPL.key]


def test_stale_quote_is_flagged_not_hidden(client_factory):
    store = FakeStore({AAPL.key: quote_payload(minutes_ago=5)})

    with client_factory(store) as client:
        body = client.get("/api/market/AAPL").json()

    assert body["stale"] is True
    assert body["max_age_seconds"] == 10.0


def test_recent_transport_activity_does_not_hide_stale_bbo(client_factory):
    stamp = datetime.now(timezone.utc)
    q = Quote(AAPL, stamp - timedelta(seconds=30), bid=100.0, ask=101.0)
    q = q.with_tick("volume", 100.0, stamp)
    store = FakeStore({AAPL.key: q.as_dict()})
    with client_factory(store) as client:
        body = client.get("/api/market/AAPL").json()
    assert body["stale"] is True
    assert body["age_seconds"] >= 30
    assert body["received_at"] == stamp.isoformat()
    assert body["price_at"] == q.price_at.isoformat()


@pytest.mark.parametrize("mode", [2, 3, 4])
def test_frozen_and_delayed_modes_are_stale_for_rest_and_stream(client_factory, mode):
    q = Quote(AAPL, datetime.now(timezone.utc), bid=100.0, ask=101.0, market_data_type=mode)
    store = FakeStore({AAPL.key: q.as_dict()})
    with client_factory(store) as client:
        assert client.get("/api/market/AAPL").json()["stale"] is True
        with client.websocket_connect("/ws/market?symbol=AAPL") as ws:
            assert ws.receive_json()["stale"] is True


@pytest.mark.parametrize(
    "updates",
    [
        {"bid": None},
        {"ask": None},
        {"bid": 102.0},  # crossed
        {"bid": 0.0},
        {"bid": -1.0},
        {"bid": float("nan")},
        {"ask": float("inf")},
        {"ask": -float("inf")},
        {"delayed": True},
        {"market_data_type": 2},
        {"market_data_type": 3},
        {"market_data_type": 4},
        {"market_data_type": None},
        {"market_data_type": True},
        {"bid_at": None},
        {"ask_at": None},
        {"bid_at": "not a timestamp"},
        {"ask_at": "2026-11-03T15:00:00"},  # naive is ambiguous
        {"received_at": None},
        {"received_at": "garbled"},
        {"received_at": "2026-11-03T15:00:00"},
    ],
)
def test_freshness_rejects_nonexecutable_payloads(monkeypatch, updates):
    from importlib import import_module

    module = import_module("apps.api.main")
    now = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "utcnow", lambda: now)
    payload = Quote(AAPL, now, bid=100.0, ask=101.0).as_dict()
    payload.update(updates)
    assert module._freshness(payload)["stale"] is True


@pytest.mark.parametrize("field", ["bid_at", "ask_at", "received_at"])
def test_freshness_rejects_future_timestamps_even_when_other_side_is_current(monkeypatch, field):
    from importlib import import_module

    module = import_module("apps.api.main")
    now = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "utcnow", lambda: now)
    payload = Quote(AAPL, now, bid=100.0, ask=101.0).as_dict()
    payload[field] = (now + timedelta(seconds=1)).isoformat()
    assert module._freshness(payload)["stale"] is True


def test_freshness_ignores_claimed_derived_price_at_and_requires_per_side_stamps(monkeypatch):
    from importlib import import_module

    module = import_module("apps.api.main")
    now = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "utcnow", lambda: now)
    payload = Quote(AAPL, now - timedelta(seconds=11), bid=100.0, ask=101.0).as_dict()
    payload["received_at"] = payload["price_at"] = now.isoformat()
    assert module._freshness(payload)["stale"] is True
    assert module._freshness(payload)["age_seconds"] == 11
    del payload["bid_at"]
    del payload["ask_at"]
    assert module._freshness(payload)["stale"] is True  # old worker cache fails closed
    assert module._freshness(payload)["age_seconds"] is None


@pytest.mark.parametrize("seconds", [0, 10, 10.000001, 11])
def test_api_and_quote_gate_agree_at_exact_age_boundary(monkeypatch, seconds):
    from importlib import import_module

    module = import_module("apps.api.main")
    now = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "utcnow", lambda: now)
    q = Quote(AAPL, now - timedelta(seconds=seconds), bid=100.0, ask=101.0)
    payload = q.with_tick("volume", 10.0, now).as_dict()
    freshness = module._freshness(payload)
    assert freshness["stale"] is (not q.is_fresh(freshness["max_age_seconds"], now))


async def test_published_subscription_tombstone_is_stale_to_api(monkeypatch):
    from importlib import import_module

    from core.market.service import MarketDataService

    class Gateway:
        def subscribe(self, instrument, req_id):
            pass

        def unsubscribe(self, req_id):
            pass

    module = import_module("apps.api.main")
    now = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "utcnow", lambda: now)
    store = FakeStore()
    svc = MarketDataService(Gateway(), store=store, clock=lambda: now)
    await svc.subscribe(AAPL, "a")
    await svc.apply_tick(90_000, 1, 100.0)
    await svc.apply_tick(90_000, 2, 101.0)
    assert module._freshness(await store.load(AAPL.key))["stale"] is False
    await svc.unsubscribe(AAPL, "a")
    assert module._freshness(await store.load(AAPL.key))["stale"] is True


def test_missing_quote_is_a_404(client_factory):
    with client_factory(FakeStore()) as client:
        response = client.get("/api/market/AAPL")

    assert response.status_code == 404
    assert "no market data" in response.json()["detail"]


def test_unusable_symbol_is_rejected_before_any_lookup(client_factory):
    store = FakeStore()

    with client_factory(store) as client:
        response = client.get("/api/market/1BAD")

    assert response.status_code == 400
    assert store.loaded_keys == []


def test_cache_outage_is_reported_as_unavailable(client_factory):
    with client_factory(FakeStore(fail=True)) as client:
        response = client.get("/api/market/AAPL")

    assert response.status_code == 503


def test_market_endpoint_never_touches_the_database(client_factory):
    """Reading quotes must not require a database, let alone a broker call."""
    store = FakeStore({AAPL.key: quote_payload()})

    with client_factory(store) as client:
        response = client.get("/api/market/AAPL")

    assert response.status_code == 200


def test_websocket_publishes_ticks(client_factory):
    store = FakeStore({AAPL.key: quote_payload()})

    with client_factory(store) as client:
        with client.websocket_connect("/ws/market?symbol=AAPL") as ws:
            message = ws.receive_json()

    assert message["symbol"] == "AAPL"
    assert message["mid"] == pytest.approx(100.1)


def test_websocket_filters_to_the_requested_symbol(client_factory):
    other = MarketInstrument("MSFT", "STK", "SMART", "USD")
    store = FakeStore(
        {
            AAPL.key: quote_payload(),
            other.key: Quote(
                instrument=other,
                received_at=datetime.now(timezone.utc),
                bid=1.0,
                ask=2.0,
            ).as_dict(),
        }
    )

    with client_factory(store) as client:
        with client.websocket_connect("/ws/market?symbol=MSFT") as ws:
            message = ws.receive_json()

    assert message["symbol"] == "MSFT"


def test_websocket_rejects_an_unsupported_symbol(client_factory):
    with client_factory(FakeStore()) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/market?symbol=1BAD") as ws:
                ws.receive_json()
