from core.reconciliation.models import (
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    FindingCode,
    InstrumentKey,
    LocalExecution,
    LocalOrder,
    LocalPosition,
    LocalSnapshot,
)
from core.reconciliation.engine import reconcile

AAPL = InstrumentKey.of("AAPL", "STK", "USD")


def broker_snapshot(**kwargs) -> BrokerSnapshot:
    base = {
        "account": "DUR123456",
        "orders": (),
        "positions": (),
        "executions": (),
        "orders_complete": True,
        "positions_complete": True,
        "executions_complete": True,
    }
    base.update(kwargs)
    return BrokerSnapshot(**base)


def local_snapshot(**kwargs) -> LocalSnapshot:
    base = {"orders": (), "positions": (), "executions": ()}
    base.update(kwargs)
    return LocalSnapshot(**base)


def broker_order(perm=1001, order_id=17, side="BOT", quantity=10.0, filled=0.0, status="Submitted"):
    return BrokerOrder(
        perm_id=perm,
        broker_order_id=order_id,
        instrument=AAPL,
        side=side,
        quantity=quantity,
        filled=filled,
        status=status,
    )


def local_order(
    ref="local-1", perm=1001, order_id=17, side="BUY", quantity=10.0, filled=0.0, status="SUBMITTED"
):
    return LocalOrder(
        ref=ref,
        perm_id=perm,
        broker_order_id=order_id,
        instrument=AAPL,
        side=side,
        quantity=quantity,
        filled_quantity=filled,
        status=status,
    )


def test_consistent_state_reconciles_clean():
    result = reconcile(
        broker_snapshot(
            orders=(broker_order(quantity=10.0, filled=4.0),),
            positions=(BrokerPosition(AAPL, 4.0, 250.0),),
            executions=(BrokerExecution("EX-1", 1001, AAPL, "BOT", 4.0, 250.0),),
        ),
        local_snapshot(
            orders=(local_order(quantity=10.0, filled=4.0, status="PARTIALLY_FILLED"),),
            positions=(LocalPosition("pos-1", AAPL, 4.0, 250.0),),
            executions=(LocalExecution("exec-1", "EX-1", 1001),),
        ),
    )
    assert result.ok
    assert result.findings == ()


def test_filled_order_still_listed_by_broker_is_advisory_only():
    """IBKR lags in clearing filled orders from openOrders; that is not a mismatch."""
    result = reconcile(
        broker_snapshot(orders=(broker_order(filled=10.0, status="Filled"),)),
        local_snapshot(orders=(local_order(filled=10.0, status="FILLED"),)),
    )

    assert result.ok
    assert [f.code for f in result.advisory] == [FindingCode.ORDER_STATUS_CONFLICT]


def test_broker_only_order_is_blocking_and_never_adopted():
    """A restart must not mint a local order it never submitted."""
    result = reconcile(broker_snapshot(orders=(broker_order(perm=555),)), local_snapshot())

    assert not result.ok
    codes = [f.code for f in result.blocking]
    assert FindingCode.BROKER_ONLY_ORDER in codes
    assert result.orders_matched == 0


def test_side_aliases_bot_and_buy_are_equivalent():
    result = reconcile(
        broker_snapshot(orders=(broker_order(side="BOT"),)),
        local_snapshot(orders=(local_order(side="BUY"),)),
    )
    assert result.ok


def test_partial_fill_mismatch_is_detected():
    broker = broker_snapshot(
        orders=(broker_order(quantity=10.0, filled=4.0),),
        executions=(BrokerExecution("EX-1", 1001, AAPL, "BOT", 4.0, 250.0),),
    )
    local = local_snapshot(
        orders=(local_order(quantity=10.0, filled=2.0, status="PARTIALLY_FILLED"),),
        executions=(LocalExecution("exec-1", "EX-1", 1001),),
    )

    result = reconcile(broker, local)

    codes = [f.code for f in result.blocking]
    assert FindingCode.FILL_QUANTITY_MISMATCH in codes


def test_matching_partial_fill_and_persisted_execution_is_clean():
    broker = broker_snapshot(
        orders=(broker_order(quantity=10.0, filled=4.0),),
        executions=(BrokerExecution("EX-1", 1001, AAPL, "BOT", 4.0, 250.0),),
        positions=(BrokerPosition(AAPL, 4.0, 250.0),),
    )
    local = local_snapshot(
        orders=(local_order(quantity=10.0, filled=4.0, status="PARTIALLY_FILLED"),),
        executions=(LocalExecution("exec-1", "EX-1", 1001),),
        positions=(LocalPosition("pos-1", AAPL, 4.0, 250.0),),
    )

    result = reconcile(broker, local)

    assert result.ok
    assert result.executions_matched == 1


def test_duplicate_exec_details_are_counted_once():
    broker = broker_snapshot(
        orders=(broker_order(quantity=10.0, filled=4.0),),
        executions=(
            BrokerExecution("EX-1", 1001, AAPL, "BOT", 4.0, 250.0),
            BrokerExecution("EX-1", 1001, AAPL, "BOT", 4.0, 250.0),
        ),
    )
    local = local_snapshot(
        orders=(local_order(quantity=10.0, filled=4.0, status="PARTIALLY_FILLED"),),
        executions=(LocalExecution("exec-1", "EX-1", 1001),),
    )

    assert reconcile(broker, local).executions_matched == 1


def test_broker_execution_missing_locally_is_blocking():
    broker = broker_snapshot(
        orders=(broker_order(quantity=10.0, filled=4.0),),
        executions=(BrokerExecution("EX-9", 1001, AAPL, "BOT", 4.0, 250.0),),
    )
    local = local_snapshot(orders=(local_order(quantity=10.0, filled=4.0),))

    codes = [f.code for f in reconcile(broker, local).blocking]
    assert FindingCode.EXECUTION_NOT_PERSISTED in codes


def test_order_never_sent_is_not_reported_as_mismatch():
    """CREATED orders exist only locally until the order manager submits them."""
    result = reconcile(
        broker_snapshot(),
        local_snapshot(orders=(local_order(perm=None, order_id=None, status="CREATED"),)),
    )
    assert result.ok


def test_local_live_order_absent_from_broker_is_blocking():
    result = reconcile(
        broker_snapshot(),
        local_snapshot(orders=(local_order(status="SUBMITTED"),)),
    )
    codes = [f.code for f in result.blocking]
    assert FindingCode.LOCAL_ONLY_OPEN_ORDER in codes


def test_terminal_local_order_absent_from_broker_is_expected():
    for status in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
        result = reconcile(broker_snapshot(), local_snapshot(orders=(local_order(status=status),)))
        assert result.ok, status


def test_unresolved_local_order_blocks():
    result = reconcile(broker_snapshot(), local_snapshot(orders=(local_order(status="UNKNOWN"),)))
    codes = [f.code for f in result.blocking]
    assert FindingCode.LOCAL_ORDER_UNRESOLVED in codes


def test_position_quantity_mismatch_blocks():
    broker = broker_snapshot(positions=(BrokerPosition(AAPL, 25.0, 250.0),))
    local = local_snapshot(positions=(LocalPosition("pos-1", AAPL, 10.0, 250.0),))

    codes = [f.code for f in reconcile(broker, local).blocking]
    assert FindingCode.POSITION_QUANTITY_MISMATCH in codes


def test_broker_position_absent_locally_blocks():
    result = reconcile(
        broker_snapshot(positions=(BrokerPosition(AAPL, 10.0, 250.0),)), local_snapshot()
    )
    codes = [f.code for f in result.blocking]
    assert FindingCode.POSITION_QUANTITY_MISMATCH in codes


def test_average_cost_difference_is_advisory_only():
    broker = broker_snapshot(positions=(BrokerPosition(AAPL, 10.0, 251.5),))
    local = local_snapshot(positions=(LocalPosition("pos-1", AAPL, 10.0, 250.0),))

    result = reconcile(broker, local)

    assert result.ok
    assert [f.code for f in result.advisory] == [FindingCode.POSITION_COST_MISMATCH]


def test_positions_from_different_accounts_must_not_be_netted_clean():
    broker = broker_snapshot(
        positions=(
            BrokerPosition(AAPL, 6.0, 250.0, account="DUR123456"),
            BrokerPosition(AAPL, 4.0, 250.0, account="DU111"),
        )
    )
    local = local_snapshot(positions=(LocalPosition("pos-1", AAPL, 10.0, 250.0),))

    assert not reconcile(broker, local).ok
    assert FindingCode.ACCOUNT_MISMATCH in {f.code for f in reconcile(broker, local).blocking}


def test_missing_barrier_is_blocking_not_clean():
    """An unanswered request must not look like an empty, healthy account."""
    result = reconcile(
        broker_snapshot(orders_complete=False, positions_complete=False),
        local_snapshot(),
    )
    blocking = result.blocking
    assert len(blocking) == 1
    assert blocking[0].code is FindingCode.RECONCILIATION_INCOMPLETE
    assert blocking[0].details["missing_barriers"] == ["open_orders", "positions"]


def test_orders_match_on_broker_order_id_when_perm_id_unavailable():
    broker = broker_snapshot(orders=(broker_order(perm=None, order_id=17),))
    local = local_snapshot(orders=(local_order(perm=None, order_id=17),))

    result = reconcile(broker, local)

    assert result.orders_matched == 1
    assert not [f for f in result.blocking if f.code is FindingCode.BROKER_ONLY_ORDER]


def test_orphan_local_execution_is_blocking():
    broker = broker_snapshot(orders=(broker_order(),))
    local = local_snapshot(
        orders=(local_order(),),
        executions=(LocalExecution("exec-1", "EX-1", 999999),),
    )

    codes = [f.code for f in reconcile(broker, local).blocking]
    assert FindingCode.ORPHAN_EXECUTION in codes


def test_summary_reports_counts_for_invariant_reporting():
    result = reconcile(
        broker_snapshot(orders=(broker_order(filled=10.0),)),
        local_snapshot(orders=(local_order(filled=10.0, status="FILLED"),)),
    )
    summary = result.summary()

    assert summary["ok"] is True
    assert summary["orders_matched"] == 1
    assert summary["blocking"] == []


def test_sent_pending_order_absent_at_broker_blocks():
    result = reconcile(
        broker_snapshot(),
        local_snapshot(orders=(local_order(perm=None, order_id=17, status="PENDING_SUBMIT"),)),
    )
    assert FindingCode.LOCAL_ONLY_OPEN_ORDER in {f.code for f in result.blocking}


def test_matched_unknown_order_still_blocks_until_durably_resolved():
    result = reconcile(
        broker_snapshot(orders=(broker_order(),)),
        local_snapshot(orders=(local_order(status="UNKNOWN"),)),
    )
    assert FindingCode.LOCAL_ORDER_UNRESOLVED in {f.code for f in result.blocking}


def test_identity_conflicts_block_even_when_numeric_order_id_matches():
    from dataclasses import replace

    for broker_row in (
        broker_order(perm=9999),
        replace(broker_order(), instrument=InstrumentKey.of("MSFT", "STK", "USD")),
    ):
        result = reconcile(
            broker_snapshot(orders=(broker_row,)), local_snapshot(orders=(local_order(),))
        )
        assert FindingCode.ORDER_IDENTITY_MISMATCH in {f.code for f in result.blocking}


def test_same_exec_id_with_different_owner_or_economics_blocks():
    for row in (
        LocalExecution("e", "EX-1", 9999, 4, 250),
        LocalExecution("e", "EX-1", 1001, 5, 250),
        LocalExecution("e", "EX-1", 1001, 4, 999),
    ):
        result = reconcile(
            broker_snapshot(executions=(BrokerExecution("EX-1", 1001, AAPL, "BOT", 4, 250),)),
            local_snapshot(orders=(local_order(status="FILLED", filled=10),), executions=(row,)),
        )
        assert FindingCode.EXECUTION_MISMATCH in {f.code for f in result.blocking}
