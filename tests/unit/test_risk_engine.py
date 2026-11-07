from datetime import datetime, timezone

from core.broker.base import BrokerState, BrokerStatus
from core.risk.engine import RiskEngine, RiskLimits, RiskSnapshot
from core.risk.models import RiskDecisionType, TradeIntent


def engine() -> RiskEngine:
    return RiskEngine(
        RiskLimits(
            max_daily_loss_pct=1,
            max_total_exposure_pct=20,
            max_open_positions=3,
            max_position_pct=5,
            max_trade_risk_pct=0.25,
            max_spread_pct=0.30,
            stale_data_seconds=10,
        )
    )


def snapshot(**kwargs) -> RiskSnapshot:
    base = dict(
        nav=100_000,
        daily_pnl=0,
        gross_exposure=0,
        open_positions=0,
        market_data_at=datetime.now(timezone.utc),
        spread_pct=0.01,
    )
    base.update(kwargs)
    return RiskSnapshot(**base)


def test_reject_when_broker_not_ready():
    decision = engine().evaluate(
        TradeIntent("SPCX", "BUY", 1, 110),
        BrokerStatus(BrokerState.SYNCING, True),
        snapshot(),
    )
    assert decision.decision == RiskDecisionType.REJECTED
    assert decision.rule == "broker_ready"


def test_approve_normal_small_trade():
    decision = engine().evaluate(
        TradeIntent("SPCX", "BUY", 10, 110, stop_price=100),
        BrokerStatus(BrokerState.READY, True),
        snapshot(),
    )
    assert decision.decision == RiskDecisionType.APPROVED


def test_total_exposure_limit_is_enforced():
    result = engine().evaluate(
        TradeIntent("AAPL", "BUY", 10, 100, 99),
        BrokerStatus(BrokerState.READY, True),
        snapshot(gross_exposure=19999),
    )
    assert result.rule == "total_exposure"


def test_stop_distance_risk_limit_is_enforced():
    result = engine().evaluate(
        TradeIntent("AAPL", "BUY", 10, 100, 1), BrokerStatus(BrokerState.READY, True), snapshot()
    )
    assert result.rule == "trade_risk"


def test_missing_or_wrong_side_stop_is_rejected():
    for stop in (None, 0, 100, 101, float("nan")):
        result = engine().evaluate(
            TradeIntent("AAPL", "BUY", 10, 100, stop),
            BrokerStatus(BrokerState.READY, True),
            snapshot(),
        )
        assert result.decision == RiskDecisionType.REJECTED


def test_nonfinite_risk_values_fail_closed():
    from dataclasses import replace

    for field in ("nav", "daily_pnl", "gross_exposure", "spread_pct"):
        result = engine().evaluate(
            TradeIntent("AAPL", "BUY", 10, 100, 99),
            BrokerStatus(BrokerState.READY, True),
            snapshot(**{field: float("nan")}),
        )
        assert result.decision == RiskDecisionType.REJECTED, field
    for field in ("quantity", "reference_price"):
        result = engine().evaluate(
            replace(TradeIntent("AAPL", "BUY", 10, 100, 99), **{field: float("nan")}),
            BrokerStatus(BrokerState.READY, True),
            snapshot(),
        )
        assert result.decision == RiskDecisionType.REJECTED, field


def test_missing_spread_or_future_quote_rejected():
    from datetime import timedelta

    for values in (
        {"spread_pct": None},
        {"market_data_at": datetime.now(timezone.utc) + timedelta(days=1)},
        {"market_data_at": datetime.now()},
    ):
        result = engine().evaluate(
            TradeIntent("AAPL", "BUY", 10, 100, 99),
            BrokerStatus(BrokerState.READY, True),
            snapshot(**values),
        )
        assert result.decision == RiskDecisionType.REJECTED


def test_ready_but_disconnected_broker_rejected():
    result = engine().evaluate(
        TradeIntent("AAPL", "BUY", 10, 100, 99), BrokerStatus(BrokerState.READY, False), snapshot()
    )
    assert result.rule == "broker_ready"


def test_zero_spread_limit_is_valid():
    from dataclasses import replace

    assert replace(engine().limits, max_spread_pct=0).max_spread_pct == 0
