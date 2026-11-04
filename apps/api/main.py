from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.control_auth import require_operator
from apps.api.deps import (
    close_kill_switch,
    close_quote_store,
    get_kill_switch,
    get_quote_store,
    get_sessionmaker,
)
from config.settings import settings
from core.controls.kill_switch import KillSwitch, TradingState
from core.market.models import utcnow
from core.market.redis_store import RedisQuoteStore
from core.market.resolver import ContractResolutionError, ContractResolver
from database.broker_state import latest_state_event
from database.audit import record_audit
from database.session import engine

resolver = ContractResolver()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The API never owns a broker connection; it only reads what the worker recorded.
    yield
    await close_kill_switch()
    await close_quote_store()
    await engine.dispose()


class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=512)
    actor: str = Field(default="api", min_length=1, max_length=128)


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.app_env,
        "trading_mode": settings.trading_mode,
        "allow_live_trading": settings.allow_live_trading,
    }


@app.get("/api/system/status")
async def system_status(
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    gate: KillSwitch = Depends(get_kill_switch),
) -> dict[str, Any]:
    """Report broker state and the trading gate as recorded in the durable store.

    Absence of evidence is reported as UNKNOWN, never as READY: a status endpoint
    that guesses healthy when it cannot see the database is worse than one that
    admits it does not know.
    """
    payload: dict[str, Any] = {
        "trading_mode": settings.trading_mode.value,
        "live_trading_enabled": settings.allow_live_trading,
        "trading_state": "UNKNOWN",
        "new_orders_allowed": False,
        "broker": "UNKNOWN",
        "database_visible": False,
    }

    # Re-read rather than serve a cached verdict: this endpoint is what an operator
    # checks right after pulling the kill switch.
    gate_state = await gate.refresh()
    payload["trading_state"] = gate_state.state.value
    payload["new_orders_allowed"] = gate.allows_new_orders()
    payload["gate_reason"] = gate_state.reason

    try:
        async with session_factory() as session:
            latest = await latest_state_event(session)
    except Exception:
        # A status route that crashes during an outage is worse than useless: an operator
        # needs the gate verdict even when the broker history cannot be read.
        return payload

    payload["database_visible"] = True

    if latest is None:
        payload["detail"] = "no broker state recorded yet"
        return payload

    metadata = latest["metadata"]
    payload.update(
        {
            "broker": latest["state"],
            "broker_event": latest["event_type"],
            "recorded_at": latest["recorded_at"],
            "last_error": latest["message"],
            "next_order_id": metadata.get("next_order_id"),
            "reconciliation": metadata.get("reconciliation"),
        }
    )
    return payload


async def _move_gate(
    state: TradingState,
    body: HaltRequest,
    gate: KillSwitch,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Record a gate change durably before admitting it happened.

    A kill that only lives in this process's memory would be undone by a restart, so a
    failed write must surface as an error rather than a confident-looking response.
    """
    previous = gate.state
    try:
        written = await gate.engage(state, reason=body.reason, actor=body.actor)
    except Exception as exc:
        # Deliberately broad: whatever the failure, the API must never report a gate
        # change it could not record durably.
        raise HTTPException(
            status_code=503, detail=f"trading state not changed: {type(exc).__name__}"
        ) from exc

    # Best-effort trail of who moved the gate: an unwritable audit store must never block
    # a kill, so failures here are swallowed.
    try:
        await record_audit(
            session_factory,
            actor=body.actor,
            action=f"control.{state.value.lower()}",
            object_type="system_control",
            object_id="trading_gate",
            before=previous.as_dict() if previous else None,
            after=written.as_dict(),
        )
    except Exception:
        pass

    poked = True
    poke = getattr(gate.control_plane, "poke", None)
    if poke is not None:
        try:
            await poke(state.value)
        except Exception:
            # The decision is already durable; a failed notification must not undo it.
            poked = False

    return {
        "trading_state": written.state.value,
        "reason": written.reason,
        "actor": written.actor,
        "updated_at": written.updated_at.isoformat(),
        # The worker re-reads on its own timer, so a lost poke costs latency only.
        "control_plane_notified": poked,
    }


@app.post("/api/system/kill")
async def kill_trading(
    body: HaltRequest,
    gate: KillSwitch = Depends(get_kill_switch),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    _authorized: None = Depends(require_operator),
) -> dict:
    return await _move_gate(TradingState.KILLED, body, gate, session_factory)


@app.post("/api/system/pause")
async def pause_trading(
    body: HaltRequest,
    gate: KillSwitch = Depends(get_kill_switch),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    _authorized: None = Depends(require_operator),
) -> dict:
    return await _move_gate(TradingState.PAUSED, body, gate, session_factory)


@app.post("/api/system/resume")
async def resume_trading(
    body: HaltRequest,
    gate: KillSwitch = Depends(get_kill_switch),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    _authorized: None = Depends(require_operator),
) -> dict:
    return await _move_gate(TradingState.ACTIVE, body, gate, session_factory)


def _freshness(payload: dict[str, Any]) -> dict[str, Any]:
    """Age a quote against the same limit risk uses, so readers see what traders get."""
    limit = float(settings.ibkr_stale_data_seconds)
    raw = payload.get("received_at")

    if not raw:
        return {"age_seconds": None, "stale": True, "max_age_seconds": limit}

    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return {"age_seconds": None, "stale": True, "max_age_seconds": limit}

    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    age = (utcnow() - stamp).total_seconds()
    return {"age_seconds": round(age, 3), "stale": age > limit, "max_age_seconds": limit}


@app.get("/api/market/{symbol}")
async def market_quote(
    symbol: str, store: RedisQuoteStore = Depends(get_quote_store)
) -> dict[str, Any]:
    try:
        instrument = resolver.resolve(symbol)
    except ContractResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        quote = await store.load(instrument.key)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="market data cache unavailable") from exc

    if quote is None:
        raise HTTPException(status_code=404, detail=f"no market data for {instrument.symbol}")

    return {**quote, **_freshness(quote)}


@app.websocket("/ws/market")
async def market_stream(
    websocket: WebSocket,
    symbol: str | None = Query(default=None),
    store: RedisQuoteStore = Depends(get_quote_store),
) -> None:
    """Publish ticks to browsers; reads Redis, never the broker."""
    await websocket.accept()

    instrument_key: str | None = None
    if symbol is not None:
        try:
            instrument_key = resolver.resolve(symbol).key
        except ContractResolutionError:
            await websocket.close(code=1008, reason="unsupported symbol")
            return

    try:
        async for tick in store.stream(instrument_key):
            await websocket.send_json({**tick, **_freshness(tick)})
    except WebSocketDisconnect:
        return
    except RedisError:
        await websocket.close(code=1011, reason="market data cache unavailable")
