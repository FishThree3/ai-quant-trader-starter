import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apps.api.deps import get_kill_switch, get_sessionmaker
from apps.api.main import app
from core.controls.kill_switch import KillSwitch, SystemState, TradingState
from database.models import AuditLog, Base

T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
SECRET = "correct-horse-battery-staple"
AUTH = {"authorization": f"Bearer {SECRET}"}
MUTATIONS = ("/api/system/kill", "/api/system/pause", "/api/system/resume")
BODY = {"reason": "operator decided so", "actor": "kvn"}


class SpyStore:
    def __init__(self):
        self.state = SystemState(TradingState.ACTIVE, "go", "operator", T0)
        self.writes = []

    async def read(self):
        return self.state

    async def write(self, state, *, reason, actor):
        self.writes.append((state, reason, actor))
        self.state = SystemState(state, reason, actor, T0)
        return self.state


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


@pytest.fixture
def store():
    return SpyStore()


@pytest.fixture
def client_for(session_factory, store, monkeypatch):
    """Builds a TestClient against a gate whose operator token is TOKEN."""

    def make(token=SECRET):
        from config.settings import settings

        monkeypatch.setattr(settings, "control_api_token", token)
        gate = KillSwitch(store, clock=lambda: T0)
        app.dependency_overrides[get_sessionmaker] = lambda: session_factory
        app.dependency_overrides[get_kill_switch] = lambda: gate
        return TestClient(app)

    return make


async def audit_rows(session_factory):
    async with session_factory() as session:
        return (await session.execute(select(AuditLog))).scalars().all()


# --- unconfigured token refuses everything -------------------------------------


@pytest.mark.parametrize("route", MUTATIONS)
def test_an_unset_operator_token_refuses_mutation_rather_than_allowing_it(client_for, store, route):
    with client_for("") as client:
        response = client.post(route, headers=AUTH, json=BODY)

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]
    assert store.writes == [], "a deployment that forgot the token must not move the gate"


# --- wrong or missing credentials ------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": ""},
        {"authorization": "Bearer"},
        {"authorization": "Bearer "},
        {"authorization": f"token {SECRET}"},
        {"authorization": "Bearer wrong-token"},
        {"authorization": f"Bearer {SECRET}x"},
    ],
    ids=["absent", "empty", "no-value", "blank-value", "wrong-scheme", "wrong-token", "extended"],
)
def test_any_imperfect_credential_is_rejected_before_the_gate_moves(client_for, store, headers):
    with client_for() as client:
        response = client.post("/api/system/kill", headers=headers, json=BODY)

    assert response.status_code == 401
    assert store.writes == []


def test_the_response_never_leaks_the_expected_token(client_for):
    with client_for() as client:
        response = client.post(
            "/api/system/kill", headers={"authorization": "Bearer nope"}, json=BODY
        )

    assert SECRET not in response.text


def test_read_only_status_stays_available_without_credentials(client_for, store):
    with client_for() as client:
        response = client.get("/api/system/status")

    assert response.status_code == 200
    assert response.json()["trading_state"] == "ACTIVE"


# --- accepted credentials ----------------------------------------------------------


@pytest.mark.parametrize(
    "route,expected",
    [
        ("/api/system/kill", TradingState.KILLED),
        ("/api/system/pause", TradingState.PAUSED),
        ("/api/system/resume", TradingState.ACTIVE),
    ],
)
def test_a_valid_operator_token_permits_the_mutation(client_for, store, route, expected):
    with client_for() as client:
        response = client.post(route, headers=AUTH, json=BODY)

    assert response.status_code == 200
    assert store.writes[-1][0] is expected


def test_the_accepted_attempt_and_the_decided_state_are_both_audited(client_for, session_factory):
    with client_for() as client:
        client.post(
            "/api/system/kill", headers=AUTH, json={"reason": "fills look wrong", "actor": "kvn"}
        )
        client.post(
            "/api/system/kill", headers={"authorization": "Bearer intruder"}, json=BODY
        )

    rows = [(row.action, row.actor, row.object_type, row.object_id) for row in asyncio.run(audit_rows(session_factory))]

    assert ("control.authorized", "unknown", "system_control", "/api/system/kill") in rows
    assert ("control.denied", "unknown", "system_control", "/api/system/kill") in rows
    assert ("control.killed", "kvn", "system_control", "trading_gate") in rows


def test_the_denied_attempt_records_why_without_recording_the_token(client_for, session_factory):
    with client_for() as client:
        client.post("/api/system/kill", headers={"authorization": f"Bearer {SECRET[:-1]}X"}, json=BODY)

    rows = asyncio.run(audit_rows(session_factory))
    denied = [row for row in rows if row.action == "control.denied"]

    assert denied and SECRET not in str(denied[0].after_json)
    assert denied[0].after_json["allowed"] is False


def test_an_unwritable_audit_trail_does_not_block_a_kill(store, monkeypatch):
    """The gate decision matters more than the paperwork about it."""
    from config.settings import settings

    monkeypatch.setattr(settings, "control_api_token", SECRET)

    class UnwritableSession:
        async def __aenter__(self):
            raise RuntimeError("audit store down")

        async def __aexit__(self, *exc_info):
            return False

    def explode():
        return lambda: UnwritableSession()
    app.dependency_overrides[get_kill_switch] = lambda: KillSwitch(store, clock=lambda: T0)
    try:
        with TestClient(app) as client:
            response = client.post("/api/system/kill", headers=AUTH, json=BODY)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert store.writes[-1][0] is TradingState.KILLED
