from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apps.api.deps import get_kill_switch, get_sessionmaker
from apps.api.main import app
from core.controls.kill_switch import AlwaysActive
from database.broker_state import latest_state_event, record_state_event
from database.models import Base, BrokerEvent

EVENT_META = {
    "next_order_id": 42,
    "account_present": True,
    "trading_mode": "PAPER",
    "reconciliation": {"ok": True, "blocking": [], "orders_matched": 1},
}


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@asynccontextmanager
async def client_for(factory, gate=None):
    app.dependency_overrides[get_sessionmaker] = lambda: factory
    # Without this the endpoint would dial a real Redis that does not exist in tests.
    app.dependency_overrides[get_kill_switch] = lambda: gate or AlwaysActive()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def _get(client, path="/api/system/status"):
    response = await client.get(path)
    assert response.status_code == 200
    return response.json()


class UnreachableSession:
    """Stands in for a database that refuses connections, without needing one down."""

    async def __aenter__(self):
        raise OperationalError("select 1", {}, ConnectionRefusedError("connection refused"))

    async def __aexit__(self, *exc_info):
        return False


async def test_status_is_unknown_without_recorded_state(session_factory):
    async with client_for(session_factory) as client:
        body = await _get(client)

    assert body["broker"] == "UNKNOWN"
    assert body["database_visible"] is True
    assert body["trading_mode"] == "PAPER"
    assert body["live_trading_enabled"] is False


async def test_status_reports_recorded_broker_state(session_factory):
    async with session_factory() as session:
        await record_state_event(
            session,
            event_type="reconciliation.completed",
            state="READY",
            metadata=EVENT_META,
        )

    async with client_for(session_factory) as client:
        body = await _get(client)

    assert body["broker"] == "READY"
    assert body["next_order_id"] == 42
    assert body["reconciliation"]["ok"] is True
    assert body["broker_event"] == "reconciliation.completed"


async def test_status_reports_degraded_with_reason(session_factory):
    async with session_factory() as session:
        await record_state_event(
            session,
            event_type="reconciliation.completed",
            state="DEGRADED",
            message="Reconciliation blocked: BROKER_ONLY_ORDER",
            metadata=EVENT_META,
        )

    async with client_for(session_factory) as client:
        body = await _get(client)

    assert body["broker"] == "DEGRADED"
    assert body["last_error"] == "Reconciliation blocked: BROKER_ONLY_ORDER"


async def test_status_never_claims_ready_when_database_unreachable():
    async with client_for(lambda: UnreachableSession()) as client:
        body = await _get(client)

    assert body["broker"] == "UNKNOWN"
    assert body["database_visible"] is False


async def test_latest_event_wins_across_same_second_transitions(session_factory):
    """SQLite timestamps are second-granular, so ordering must still be correct."""
    async with session_factory() as session:
        await record_state_event(session, event_type="broker.identified", state="SYNCING")
        await record_state_event(session, event_type="reconciliation.completed", state="READY")

    async with session_factory() as session:
        latest = await latest_state_event(session)

    assert latest["state"] == "READY"


async def test_account_identifier_is_never_exposed(session_factory):
    async with session_factory() as session:
        await record_state_event(
            session,
            event_type="reconciliation.completed",
            state="READY",
            metadata=EVENT_META,
        )

    async with client_for(session_factory) as client:
        body = await _get(client)

    assert "account" not in body
    assert "DUR" not in str(body)


async def test_health_endpoint_still_reports_configuration(session_factory):
    async with client_for(session_factory) as client:
        body = await _get(client, "/health")

    assert body["status"] == "ok"
    assert body["trading_mode"] == "PAPER"
    assert body["allow_live_trading"] is False


async def test_old_ready_heartbeat_is_unknown_and_never_tradeable(session_factory):
    async with session_factory() as session:
        session.add(
            BrokerEvent(
                event_type="broker.heartbeat",
                state="READY",
                created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                metadata_json={**EVENT_META, "read_only": False, "submission_enabled": True},
            )
        )
        await session.commit()
    async with client_for(session_factory) as client:
        body = await _get(client)
    assert body["broker"] == "UNKNOWN"
    assert body["broker_stale"] is True
    assert body["new_orders_allowed"] is False


async def test_read_only_ready_broker_does_not_advertise_order_access(session_factory):
    async with session_factory() as session:
        await record_state_event(
            session,
            event_type="broker.heartbeat",
            state="READY",
            metadata={**EVENT_META, "read_only": True},
        )
    async with client_for(session_factory) as client:
        body = await _get(client)
    assert body["broker"] == "READY"
    assert body["gate_allows_new_orders"] is True
    assert body["new_orders_allowed"] is False
