from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from core.reconciliation.models import (
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    InstrumentKey,
)


def _num(value: Any, default: float = 0.0) -> float:
    """IBKR uses sentinel values for unset numbers, which must not become NaNs."""
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number or None


def _identity(contract: Any) -> InstrumentKey:
    return InstrumentKey.of(
        symbol=getattr(contract, "symbol", "") or getattr(contract, "code", "") or "",
        sec_type=getattr(contract, "secType", "") or "",
        currency=getattr(contract, "currency", "") or "",
    )


def _parse_time(raw: Any) -> datetime | None:
    """IBKR sends execution timestamps in several shapes depending on settings."""
    text = str(raw or "").strip()
    if not text:
        return None

    for pattern in ("%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
        try:
            parsed = datetime.strptime(text.replace("  ", " "), pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def broker_order(order_id: int, contract: Any, order: Any, order_state: Any) -> BrokerOrder:
    return BrokerOrder(
        perm_id=_optional_int(getattr(order, "permId", None)),
        broker_order_id=_optional_int(order_id),
        instrument=_identity(contract),
        side=str(getattr(order, "action", "") or ""),
        quantity=_num(getattr(order, "totalQuantity", None)),
        filled=_num(getattr(order_state, "filled", None)),
        status=str(getattr(order_state, "status", "") or ""),
        limit_price=_num(getattr(order, "lmtPrice", None), default=0.0) or None,
        account=str(getattr(order, "account", "") or "") or None,
    )


def _required_number(value: Any) -> float:
    """Missing/malformed exposure is not the same as zero exposure."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid required broker numeric value") from exc
    if not isfinite(number):
        raise ValueError("Nonfinite required broker numeric value")
    return number


def broker_position(account: str, contract: Any, quantity: Any, avg_cost: Any) -> BrokerPosition:
    return BrokerPosition(
        instrument=_identity(contract),
        quantity=_required_number(quantity),
        average_cost=_required_number(avg_cost),
        account=account or None,
    )


def broker_execution(req_id: int, contract: Any, execution: Any) -> BrokerExecution:
    return BrokerExecution(
        exec_id=str(getattr(execution, "execId", "") or ""),
        perm_id=_optional_int(getattr(execution, "permId", None)),
        instrument=_identity(contract),
        side=str(getattr(execution, "side", "") or ""),
        quantity=_num(getattr(execution, "shares", None)),
        price=_num(getattr(execution, "price", None)),
        exchange=str(getattr(execution, "exchange", "") or "") or None,
        executed_at=_parse_time(getattr(execution, "time", None)),
        account=str(getattr(execution, "acctNumber", "") or "") or None,
        broker_order_id=_optional_int(getattr(execution, "orderId", None)),
        client_id=getattr(execution, "clientId", None),
    )
