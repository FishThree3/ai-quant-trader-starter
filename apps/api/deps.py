from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.settings import settings
from core.market.redis_store import RedisQuoteStore
from database.session import SessionLocal

_quote_store: RedisQuoteStore | None = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Session factory dependency, overridable so the API is testable without Postgres."""
    return SessionLocal


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
