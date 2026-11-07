from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from core.reconciliation.models import (
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Finding,
    FindingCode,
    FindingSeverity,
    InstrumentKey,
    LocalOrder,
    LocalPosition,
    LocalSnapshot,
    ReconciliationResult,
)

QUANTITY_TOLERANCE = 1e-6
PRICE_TOLERANCE = 1e-6

# IBKR reports BOT/SLD while the local schema stores BUY/SELL.
_SIDE_ALIASES = {"BOT": "BUY", "SLD": "SELL", "BUY": "BUY", "SELL": "SELL"}

# Never sent to the broker, so their absence from broker open orders is expected.
_PRE_SUBMIT = {"CREATED"}
# The broker must acknowledge these, so absence is a real mismatch.
_EXPECTED_AT_BROKER = {"PENDING_SUBMIT", "SUBMITTED", "PARTIALLY_FILLED", "CANCEL_PENDING"}
# We no longer expect these at the broker, but IBKR may still report them.
_TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}


def normalize_side(side: str | None) -> str:
    return _SIDE_ALIASES.get((side or "").strip().upper(), (side or "").strip().upper())


def _close(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance


def _index_by_perm(items: Iterable, key: str) -> dict[int, object]:
    out: dict[int, object] = {}
    for item in items:
        perm = getattr(item, key)
        if perm is not None and perm not in out:
            out[perm] = item
    return out


def reconcile(
    broker: BrokerSnapshot,
    local: LocalSnapshot,
    *,
    quantity_tolerance: float = QUANTITY_TOLERANCE,
    price_tolerance: float = PRICE_TOLERANCE,
) -> ReconciliationResult:
    """Compare authoritative broker state against local records.

    Fail-closed: an unexplained difference in either direction is blocking, and a
    broker-only order is reported rather than adopted, because the order manager
    must not mint local orders it never submitted.
    """
    findings: list[Finding] = []

    if not broker.is_complete:
        findings.append(
            Finding(
                code=FindingCode.RECONCILIATION_INCOMPLETE,
                message="Broker did not deliver every end-of-snapshot barrier.",
                severity=FindingSeverity.BLOCKING,
                details={"missing_barriers": list(broker.missing_barriers)},
            )
        )

    for row in (*broker.orders, *broker.positions, *broker.executions):
        if row.account is not None and row.account != broker.account:
            findings.append(
                Finding(
                    FindingCode.ACCOUNT_MISMATCH,
                    "Snapshot contains another account's records.",
                    details={"expected": broker.account, "observed": row.account},
                )
            )

    local_by_perm = _index_by_perm(local.orders, "perm_id")
    local_by_order_id = _index_by_perm(local.orders, "broker_order_id")
    matched_local_refs: set[str] = set()
    orders_matched = 0

    for order in _dedupe(broker.orders, "perm_id", "broker_order_id"):
        local_order = _match_local_order(order, local_by_perm, local_by_order_id)

        if local_order is None:
            if order.perm_id is None and order.broker_order_id is None:
                findings.append(
                    Finding(
                        code=FindingCode.UNMATCHABLE_BROKER_ORDER,
                        message="Broker order carries neither permId nor orderId.",
                        details={"instrument": order.instrument.symbol},
                    )
                )
            else:
                findings.append(
                    Finding(
                        code=FindingCode.BROKER_ONLY_ORDER,
                        message=(
                            "Broker reports an order with no local record; refusing to create one."
                        ),
                        details={
                            "perm_id": order.perm_id,
                            "broker_order_id": order.broker_order_id,
                            "instrument": order.instrument.symbol,
                            "side": normalize_side(order.side),
                            "quantity": order.quantity,
                            "filled": order.filled,
                        },
                    )
                )
            continue

        matched_local_refs.add(local_order.ref)
        orders_matched += 1
        findings.extend(_compare_order(order, local_order, quantity_tolerance=quantity_tolerance))

    for local_order in local.orders:
        status = (local_order.status or "").strip().upper()
        if local_order.ref in matched_local_refs and status != "UNKNOWN":
            continue

        if (
            status in _PRE_SUBMIT
            and local_order.perm_id is None
            and local_order.broker_order_id is None
        ):
            continue

        if status == "UNKNOWN":
            findings.append(
                Finding(
                    code=FindingCode.LOCAL_ORDER_UNRESOLVED,
                    message="Local order is UNKNOWN and the broker did not resolve it.",
                    details={"ref": local_order.ref, "perm_id": local_order.perm_id},
                )
            )
        elif status in _EXPECTED_AT_BROKER or status == "CREATED":
            findings.append(
                Finding(
                    code=FindingCode.LOCAL_ONLY_OPEN_ORDER,
                    message="Local order is live but the broker does not report it.",
                    details={
                        "ref": local_order.ref,
                        "status": status,
                        "perm_id": local_order.perm_id,
                        "broker_order_id": local_order.broker_order_id,
                        "instrument": local_order.instrument.symbol,
                    },
                )
            )

    broker_exec_ids = {e.exec_id for e in broker.executions if e.exec_id}
    local_exec_ids = {e.exec_id for e in local.executions if e.exec_id}
    local_execs = {e.exec_id: e for e in local.executions if e.exec_id}
    executions_matched = 0

    # IBKR can redeliver the same execDetails; counting one fill twice would make
    # every later quantity comparison wrong.
    for execution in _dedupe(broker.executions, "exec_id", "perm_id"):
        if not execution.exec_id:
            findings.append(
                Finding(
                    code=FindingCode.EXECUTION_NOT_PERSISTED,
                    message="Broker reported an execution without an execId.",
                    details={"perm_id": execution.perm_id},
                )
            )
            continue
        if execution.exec_id not in local_exec_ids:
            findings.append(
                Finding(
                    code=FindingCode.EXECUTION_NOT_PERSISTED,
                    message="Broker execution is missing locally.",
                    details={
                        "exec_id": execution.exec_id,
                        "perm_id": execution.perm_id,
                        "quantity": execution.quantity,
                    },
                )
            )
            continue
        recorded = local_execs[execution.exec_id]
        if (
            recorded.perm_id != execution.perm_id
            or (
                recorded.quantity is not None
                and not _close(recorded.quantity, execution.quantity, quantity_tolerance)
            )
            or (
                recorded.price is not None
                and not _close(recorded.price, execution.price, price_tolerance)
            )
        ):
            findings.append(
                Finding(
                    FindingCode.EXECUTION_MISMATCH,
                    "Execution ownership or economics differ.",
                    details={"exec_id": execution.exec_id},
                )
            )
            continue
        executions_matched += 1

    for local_execution in local.executions:
        if local_execution.perm_id is None:
            findings.append(
                Finding(
                    code=FindingCode.ORPHAN_EXECUTION,
                    message="Local execution has no permId to reconcile against.",
                    severity=FindingSeverity.ADVISORY,
                    details={"ref": local_execution.ref},
                )
            )
        elif local_execution.perm_id not in local_by_perm:
            findings.append(
                Finding(
                    code=FindingCode.ORPHAN_EXECUTION,
                    message="Local execution references an order that does not exist.",
                    details={"ref": local_execution.ref, "perm_id": local_execution.perm_id},
                )
            )
        elif local_execution.exec_id not in broker_exec_ids:
            findings.append(
                Finding(
                    code=FindingCode.ORPHAN_EXECUTION,
                    message="Local execution is not reported by the broker window.",
                    severity=FindingSeverity.ADVISORY,
                    details={"ref": local_execution.ref, "exec_id": local_execution.exec_id},
                )
            )

    positions_matched = 0
    broker_positions = _aggregate_positions(broker.positions)
    local_positions = _aggregate_positions(local.positions)

    for key in sorted(set(broker_positions) | set(local_positions), key=lambda k: k.symbol):
        broker_position = broker_positions.get(key)
        local_position = local_positions.get(key)
        broker_qty = broker_position.quantity if broker_position else 0.0
        local_qty = local_position.quantity if local_position else 0.0

        if _close(broker_qty, local_qty, quantity_tolerance):
            if broker_position and local_position:
                positions_matched += 1
            if (
                broker_position
                and local_position
                and abs(broker_qty) > quantity_tolerance
                and not _close(
                    broker_position.average_cost, local_position.average_cost, price_tolerance
                )
            ):
                findings.append(
                    Finding(
                        code=FindingCode.POSITION_COST_MISMATCH,
                        message="Average cost basis differs from the broker.",
                        severity=FindingSeverity.ADVISORY,
                        details={
                            "instrument": key.symbol,
                            "broker": broker_position.average_cost,
                            "local": local_position.average_cost,
                        },
                    )
                )
            continue

        findings.append(
            Finding(
                code=FindingCode.POSITION_QUANTITY_MISMATCH,
                message="Position quantity differs from the broker.",
                details={
                    "instrument": key.symbol,
                    "broker": broker_qty,
                    "local": local_qty,
                },
            )
        )

    return ReconciliationResult(
        findings=tuple(findings),
        orders_matched=orders_matched,
        positions_matched=positions_matched,
        executions_matched=executions_matched,
    )


def _match_local_order(
    order: BrokerOrder,
    local_by_perm: dict[int, object],
    local_by_order_id: dict[int, object],
) -> LocalOrder | None:
    if order.perm_id is not None:
        candidate = local_by_perm.get(order.perm_id)
        if candidate is not None:
            return candidate  # type: ignore[return-value]
    if order.broker_order_id is not None:
        candidate = local_by_order_id.get(order.broker_order_id)
        if candidate is not None:
            return candidate  # type: ignore[return-value]
    return None


def _compare_order(
    order: BrokerOrder,
    local_order: LocalOrder,
    *,
    quantity_tolerance: float,
) -> list[Finding]:
    findings: list[Finding] = []
    broker_side = normalize_side(order.side)
    local_side = normalize_side(local_order.side)
    status = (local_order.status or "").strip().upper()

    if order.instrument != local_order.instrument or (
        order.perm_id is not None
        and local_order.perm_id is not None
        and order.perm_id != local_order.perm_id
    ):
        findings.append(
            Finding(
                FindingCode.ORDER_IDENTITY_MISMATCH,
                "Order instrument or permanent ID differs.",
                details={
                    "ref": local_order.ref,
                    "broker_perm_id": order.perm_id,
                    "local_perm_id": local_order.perm_id,
                },
            )
        )

    if broker_side != local_side:
        findings.append(
            Finding(
                code=FindingCode.ORDER_SIDE_MISMATCH,
                message="Order side differs from the broker.",
                details={
                    "ref": local_order.ref,
                    "perm_id": order.perm_id,
                    "broker": order.side,
                    "local": local_order.side,
                },
            )
        )

    if not _close(order.quantity, local_order.quantity, quantity_tolerance):
        findings.append(
            Finding(
                code=FindingCode.ORDER_QUANTITY_MISMATCH,
                message="Order quantity differs from the broker.",
                details={
                    "ref": local_order.ref,
                    "perm_id": order.perm_id,
                    "broker": order.quantity,
                    "local": local_order.quantity,
                },
            )
        )

    if not _close(order.filled, local_order.filled_quantity, quantity_tolerance):
        findings.append(
            Finding(
                code=FindingCode.FILL_QUANTITY_MISMATCH,
                message="Filled quantity differs from the broker.",
                details={
                    "ref": local_order.ref,
                    "perm_id": order.perm_id,
                    "broker": order.filled,
                    "local": local_order.filled_quantity,
                },
            )
        )

    remaining = order.quantity - order.filled
    if status in _TERMINAL and remaining > quantity_tolerance:
        findings.append(
            Finding(
                code=FindingCode.ORDER_STATUS_CONFLICT,
                message="Local order is terminal but the broker still reports it working.",
                details={"ref": local_order.ref, "perm_id": order.perm_id, "status": status},
            )
        )
    elif status in _TERMINAL:
        findings.append(
            Finding(
                code=FindingCode.ORDER_STATUS_CONFLICT,
                message="Broker still reports a locally terminal order as open.",
                severity=FindingSeverity.ADVISORY,
                details={"ref": local_order.ref, "perm_id": order.perm_id, "status": status},
            )
        )

    return findings


def _dedupe(items, primary: str, secondary: str):
    """Keep one entry per broker identity, preferring the stable permId."""
    seen: set[tuple] = set()
    out = []
    for item in items:
        identity = (getattr(item, primary), getattr(item, secondary))
        if identity in seen:
            continue
        seen.add(identity)
        out.append(item)
    return out


@dataclass(frozen=True, slots=True)
class _NetPosition:
    instrument: InstrumentKey
    quantity: float
    average_cost: float


def _aggregate_positions(
    entries: Iterable[BrokerPosition | LocalPosition],
) -> dict[InstrumentKey, _NetPosition]:
    """Fold per-account rows into one net quantity per instrument.

    IBKR can report the same contract as several rows, so summing is the only way
    to compare against a single local position row.
    """
    totals: dict[InstrumentKey, list[float]] = {}
    for entry in entries:
        bucket = totals.setdefault(entry.instrument, [0.0, 0.0])
        bucket[0] += entry.quantity
        bucket[1] += entry.average_cost * abs(entry.quantity)

    aggregated: dict[InstrumentKey, _NetPosition] = {}
    for instrument, (quantity, cost_sum) in totals.items():
        weight = abs(quantity)
        average_cost = cost_sum / weight if weight > QUANTITY_TOLERANCE else 0.0
        aggregated[instrument] = _NetPosition(instrument, quantity, average_cost)
    return aggregated
