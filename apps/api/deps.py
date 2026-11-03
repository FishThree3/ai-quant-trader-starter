from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.session import SessionLocal


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Session factory dependency, overridable so the API is testable without Postgres."""
    return SessionLocal
