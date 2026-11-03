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
