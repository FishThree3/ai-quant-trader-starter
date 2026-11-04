from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import AuditLog


async def record_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    actor: str,
    action: str,
    object_type: str,
    object_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    """Append one immutable audit entry describing who changed what."""
    async with session_factory() as session:
        session.add(
            AuditLog(
                actor=actor,
                action=action,
                object_type=object_type,
                object_id=object_id,
                before_json=before,
                after_json=after,
            )
        )
        await session.commit()
