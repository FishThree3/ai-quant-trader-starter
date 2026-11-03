from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.broker.base import BrokerState, BrokerStatus
from core.risk.models import RiskDecision, RiskDecisionType, TradeIntent


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    nav: float
    daily_pnl: float
    gross_exposure: float
    open_positions: int
    market_data_at: datetime
    spread_pct: float | None = None


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_daily_loss_pct: float
    max_total_exposure_pct: float
    max_open_positions: int
    max_position_pct: float
    max_trade_risk_pct: float
    max_spread_pct: float
    stale_data_seconds: int


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.killed = False

    def evaluate(
        self,
        intent: TradeIntent,
        broker: BrokerStatus,
        snapshot: RiskSnapshot,
    ) -> RiskDecision:
        if self.killed:
            return self._reject("kill_switch", "Trading is killed")
        if broker.state != BrokerState.READY:
            return self._reject("broker_ready", f"Broker state is {broker.state}")

        age = (datetime.now(timezone.utc) - snapshot.market_data_at).total_seconds()
        if age > self.limits.stale_data_seconds:
            return self._reject("market_data_stale", f"Market data age={age:.2f}s")

        if snapshot.nav <= 0:
            return self._reject("nav", "NAV must be positive")

        daily_loss_pct = max(0.0, -snapshot.daily_pnl / snapshot.nav * 100)
        if daily_loss_pct >= self.limits.max_daily_loss_pct:
            return self._reject("daily_loss", f"Daily loss={daily_loss_pct:.3f}%")

        if snapshot.open_positions >= self.limits.max_open_positions:
            return self._reject("open_positions", "Max open positions reached")

        position_value = abs(intent.quantity * intent.reference_price)
        position_pct = position_value / snapshot.nav * 100
        if position_pct > self.limits.max_position_pct:
            return self._reject("position_size", f"Position={position_pct:.3f}% NAV")

        if snapshot.spread_pct is not None and snapshot.spread_pct > self.limits.max_spread_pct:
            return self._reject("spread", f"Spread={snapshot.spread_pct:.4f}")

        return RiskDecision(RiskDecisionType.APPROVED, "All rules passed", "all")

    def kill(self) -> None:
        self.killed = True

    def reset_kill(self) -> None:
        self.killed = False

    @staticmethod
    def _reject(rule: str, reason: str) -> RiskDecision:
        return RiskDecision(RiskDecisionType.REJECTED, reason, rule)
