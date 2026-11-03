from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import SQLAlchemyError

from apps.api.deps import close_quote_store, get_quote_store, get_sessionmaker
from config.settings import settings
from core.market.models import utcnow
from core.market.redis_store import RedisQuoteStore
from core.market.resolver import ContractResolutionError, ContractResolver
from database.broker_state import latest_state_event
from database.session import engine

resolver = ContractResolver()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The API never owns a broker connection; it only reads what the worker recorded.
    yield
    await close_quote_store()
    await engine.dispose()


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
) -> dict[str, Any]:
    """Report broker state as recorded by the worker.

    Absence of evidence is reported as UNKNOWN, never as READY: a status endpoint
    that guesses healthy when it cannot see the database is worse than one that
    admits it does not know.
    """
    payload: dict[str, Any] = {
        "trading_mode": settings.trading_mode.value,
        "live_trading_enabled": settings.allow_live_trading,
        "trading_state": "PAUSED",
        "broker": "UNKNOWN",
        "database_visible": False,
    }

    try:
        async with session_factory() as session:
            latest = await latest_state_event(session)
    except SQLAlchemyError:
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
