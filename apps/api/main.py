from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import SQLAlchemyError

from apps.api.deps import get_sessionmaker
from config.settings import settings
from database.broker_state import latest_state_event
from database.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The API never owns a broker connection; it only reads what the worker recorded.
    yield
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
