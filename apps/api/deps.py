from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.settings import settings
from core.controls.kill_switch import KillSwitch
from core.controls.redis_plane import RedisControlPlane
from core.market.redis_store import RedisQuoteStore
from database.session import SessionLocal
from database.system_control import SystemControlStore

_quote_store: RedisQuoteStore | None = None
_kill_switch: KillSwitch | None = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Session factory dependency, overridable so the API is testable without Postgres."""
    return SessionLocal


def get_kill_switch() -> KillSwitch:
    """The trading gate, backed by the durable record plus the Redis control plane.

    The API is allowed to move the gate but never to talk to the broker: the worker
    re-reads this state and refuses to submit on its own.
    """
    global _kill_switch
    if _kill_switch is None:
        _kill_switch = KillSwitch(
            SystemControlStore(SessionLocal),
            control_plane=RedisControlPlane.from_url(settings.redis_url),
            max_age_seconds=float(settings.ibkr_stale_data_seconds) * 3,
        )
    return _kill_switch


def get_quote_store() -> RedisQuoteStore:
    """Latest quotes as published by the worker; the API never asks the broker."""
    global _quote_store
    if _quote_store is None:
        _quote_store = RedisQuoteStore.from_url(
            settings.redis_url,
            # Outlive the freshness window so "stale" is decided by age, not by an
            # arbitrary eviction racing the reader.
            ttl_seconds=max(60, settings.ibkr_stale_data_seconds * 3),
        )
    return _quote_store


async def close_quote_store() -> None:
    global _quote_store
    if _quote_store is not None:
        await _quote_store.aclose()
        _quote_store = None


async def close_kill_switch() -> None:
    global _kill_switch
    if _kill_switch is not None and isinstance(_kill_switch.control_plane, RedisControlPlane):
        await _kill_switch.control_plane.aclose()
    _kill_switch = None
