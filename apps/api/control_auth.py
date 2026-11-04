from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.deps import get_sessionmaker
from config.settings import settings
from database.audit import record_audit


def presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def _audit(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession],
    allowed: bool,
    reason: str,
) -> None:
    """Record control-plane access attempts without ever storing the token itself.

    An unwritable audit trail must not block a legitimate kill, so write failures here
    are swallowed rather than raised.
    """
    try:
        await record_audit(
            session_factory,
            actor="unknown",
            action="control.authorized" if allowed else "control.denied",
            object_type="system_control",
            object_id=request.url.path,
            before=None,
            after={"allowed": allowed, "reason": reason},
        )
    except Exception:
        pass


async def require_operator(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> None:
    """Gate every trading-control mutation behind the configured operator token.

    An unset token refuses all mutations instead of allowing them: a deployment that
    forgot the credential must not be able to flip the trading gate. Comparison is
    constant-time so this endpoint cannot be used to probe the token byte by byte.
    """
    configured = settings.control_api_token

    if not configured:
        await _audit(request, session_factory, False, "no operator token configured")
        raise HTTPException(
            status_code=503,
            detail="control api token not configured; refusing to change the trading gate",
        )

    presented = presented_token(request)
    if presented is None:
        await _audit(request, session_factory, False, "missing bearer token")
        raise HTTPException(status_code=401, detail="operator bearer token required")

    if not hmac.compare_digest(presented, configured):
        await _audit(request, session_factory, False, "token mismatch")
        raise HTTPException(status_code=401, detail="operator token rejected")

    await _audit(request, session_factory, True, "token accepted")
