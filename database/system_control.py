from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.controls.kill_switch import SystemState, TradingState
from database.models import SystemControl

CONTROL_ROW_ID = 1


def _as_utc(stamp: datetime) -> datetime:
    """SQLite hands back naive datetimes; age maths must not compare naive to aware."""
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


class SystemControlStore:
    """Durable single-row trading gate readable by the API and the worker."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def read(self) -> SystemState | None:
        async with self.session_factory() as session:
            row = await session.get(SystemControl, CONTROL_ROW_ID)
            if row is None:
                return None
            return SystemState(
                state=TradingState(row.state),
                reason=row.reason,
                actor=row.actor,
                updated_at=_as_utc(row.updated_at),
            )

    async def write(self, state: TradingState, *, reason: str, actor: str) -> SystemState:
        updated_at = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            row = await session.get(SystemControl, CONTROL_ROW_ID)
            if row is None:
                row = SystemControl(id=CONTROL_ROW_ID)
                session.add(row)
            row.state = state.value
            row.reason = reason
            row.actor = actor
            row.updated_at = updated_at
            await session.commit()

        return SystemState(state=state, reason=reason, actor=actor, updated_at=updated_at)
