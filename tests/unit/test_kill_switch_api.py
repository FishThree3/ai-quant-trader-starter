import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apps.api.deps import get_kill_switch, get_sessionmaker
from apps.api.main import app
from core.controls.kill_switch import KillSwitch, SystemState, TradingState

T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
OPERATOR_TOKEN = "test-operator-token"
AUTH = {"authorization": f"Bearer {OPERATOR_TOKEN}"}


@pytest.fixture
def session_factory():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from database.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create())
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    asyncio.run(engine.dispose())


class SpyStore:
    def __init__(self, state=None, *, write_fails=False):
        self.state = state
        self.write_fails = write_fails
        self.write_calls = []

    async def read(self):
        return self.state

    async def write(self, state, *, reason, actor):
        self.write_calls.append((state, reason, actor))
        if self.write_fails:
            raise RuntimeError("database write refused")
        self.state = SystemState(state, reason, actor, T0)
        return self.state


class SpyPlane:
    def __init__(self, *, healthy=True, poke_fails=False):
        self.healthy_flag = healthy
        self.poke_fails = poke_fails
        self.pokes = []

    async def healthy(self):
        return self.healthy_flag

    async def poke(self, reason):
        if self.poke_fails:
            raise ConnectionError("redis down")
        self.pokes.append(reason)


def build_gate(store=None, plane=None):
    store = store or SpyStore(SystemState(TradingState.ACTIVE, "go", "operator", T0))
    plane = plane or SpyPlane()
    gate = KillSwitch(store, control_plane=plane, max_age_seconds=30.0, clock=lambda: T0)
    return gate, store, plane


@pytest.fixture(autouse=True)
def configured_token(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "control_api_token", OPERATOR_TOKEN)


@pytest.fixture
def gate_client(session_factory):
    gate, store, plane = build_gate()
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_kill_switch] = lambda: gate
    with TestClient(app) as client:
        yield client, gate, store, plane
    app.dependency_overrides.clear()


def fresh_gate(store=None, plane=None, age_seconds=0.0):
    state = SystemState(TradingState.ACTIVE, "go", "operator", T0 - timedelta(seconds=age_seconds))
    return build_gate(store or SpyStore(state), plane)


# --- reading the gate ------------------------------------------------------------


def test_status_reports_the_real_gate_not_a_hardcoded_guess(gate_client):
    client, gate, store, plane = gate_client

    body = client.get("/api/system/status").json()

    assert body["trading_state"] == "ACTIVE"
    assert body["gate_allows_new_orders"] is True
    assert body["new_orders_allowed"] is False, "an ACTIVE gate alone is not trading readiness"
    assert body["gate_reason"] == "go"


def test_status_reports_killed_when_the_record_says_killed(session_factory):
    store = SpyStore(SystemState(TradingState.KILLED, "incident", "operator", T0))
    gate, store, plane = fresh_gate(store)
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_kill_switch] = lambda: gate
    try:
        with TestClient(app) as client:
            body = client.get("/api/system/status").json()
    finally:
        app.dependency_overrides.clear()

    assert body["trading_state"] == "KILLED"
    assert body["new_orders_allowed"] is False


def test_status_reports_blocked_when_redis_is_gone(session_factory):
    gate, store, plane = fresh_gate(SpyStore(), SpyPlane(healthy=False))
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_kill_switch] = lambda: gate
    try:
        with TestClient(app) as client:
            body = client.get("/api/system/status").json()
    finally:
        app.dependency_overrides.clear()

    assert body["trading_state"] == "PAUSED"
    assert body["new_orders_allowed"] is False
    assert "control plane" in body["gate_reason"]


# --- moving the gate --------------------------------------------------------------


def test_kill_is_written_durably_and_signalled(gate_client):
    client, gate, store, plane = gate_client

    response = client.post(
        "/api/system/kill",
        headers=AUTH,
        json={"reason": "bad fills observed", "actor": "kvn"},
    )

    assert response.status_code == 200
    assert response.json()["trading_state"] == "KILLED"
    assert store.write_calls[-1][0] is TradingState.KILLED
    assert plane.pokes == ["KILLED"]


def test_resume_reopens_trading(gate_client):
    client, gate, store, plane = gate_client
    client.post(
        "/api/system/kill", headers=AUTH, json={"reason": "something looks wrong", "actor": "kvn"}
    )

    response = client.post(
        "/api/system/resume", headers=AUTH, json={"reason": "incident closed", "actor": "kvn"}
    )

    assert response.json()["trading_state"] == "ACTIVE"
    assert gate.allows_new_orders() is True


def test_pause_is_distinct_from_kill(gate_client):
    client, gate, store, plane = gate_client

    response = client.post(
        "/api/system/pause",
        headers=AUTH,
        json={"reason": "waiting for reopen", "actor": "kvn"},
    )

    assert response.json()["trading_state"] == "PAUSED"
    assert gate.allows_new_orders() is False


def test_kill_is_reflected_by_a_following_status_call(gate_client):
    client, gate, store, plane = gate_client

    client.post(
        "/api/system/kill", headers=AUTH, json={"reason": "risk breach suspected", "actor": "kvn"}
    )
    body = client.get("/api/system/status").json()

    assert body["trading_state"] == "KILLED"
    assert body["new_orders_allowed"] is False


def test_a_failed_write_is_reported_as_a_failure_not_a_confirmed_kill(session_factory):
    gate, store, plane = build_gate(SpyStore(write_fails=True))
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_kill_switch] = lambda: gate
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/system/kill",
                headers=AUTH,
                json={"reason": "try to kill", "actor": "kvn"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "not changed" in response.json()["detail"]
    assert gate.allows_new_orders() is False


def test_a_lost_poke_does_not_undo_the_durable_decision(session_factory):
    gate, store, plane = build_gate(plane=SpyPlane(poke_fails=True))
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_kill_switch] = lambda: gate
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/system/kill",
                headers=AUTH,
                json={"reason": "kill me anyway", "actor": "kvn"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["trading_state"] == "KILLED"
    assert response.json()["control_plane_notified"] is False


def test_an_empty_reason_is_rejected_before_touching_the_store(session_factory):
    gate, store, plane = build_gate()
    app.dependency_overrides[get_sessionmaker] = lambda: session_factory
    app.dependency_overrides[get_kill_switch] = lambda: gate
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/system/kill", headers=AUTH, json={"reason": "no", "actor": "kvn"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert store.write_calls == []
