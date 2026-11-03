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
        TradeIntent("SPCX", "BUY", 10, 110),
        BrokerStatus(BrokerState.READY, True),
        snapshot(),
    )
    assert decision.decision == RiskDecisionType.APPROVED
