from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.events.models import utcnow
from database.models import BrokerEvent


async def record_state_event(
    session: AsyncSession,
    *,
    event_type: str,
    state: str,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append an immutable broker state transition.

    Only a presence flag is stored for the account, never the identifier: these
    rows are readable through the API.
    """
    session.add(
        BrokerEvent(
            event_type=event_type,
            state=state,
            message=message,
            metadata_json=metadata or {},
            # Explicit timestamps: SQLite's CURRENT_TIMESTAMP only has second
            # precision, which would make same-second transitions unordered.
            created_at=utcnow(),
        )
    )
    await session.commit()


async def latest_state_event(session: AsyncSession) -> dict[str, Any] | None:
    row = (
        await session.execute(select(BrokerEvent).order_by(BrokerEvent.created_at.desc()).limit(1))
    ).scalar_one_or_none()

    if row is None:
        return None

    return {
        "state": row.state,
        "event_type": row.event_type,
        "message": row.message,
        "recorded_at": row.created_at.isoformat() if row.created_at else None,
        "metadata": row.metadata_json or {},
    }
