from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite

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

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isfinite(value) or value < 0 or (value == 0 and name != "max_spread_pct"):
                raise ValueError(f"{name} must be finite and positive (spread may be zero)")
        if not isinstance(self.max_open_positions, int):
            raise ValueError("max_open_positions must be an integer")


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
        if broker.state != BrokerState.READY or not broker.connected:
            return self._reject("broker_ready", f"Broker state is {broker.state}")

        numbers = (
            intent.quantity,
            intent.reference_price,
            snapshot.nav,
            snapshot.daily_pnl,
            snapshot.gross_exposure,
            snapshot.open_positions,
        )
        if any(not isinstance(n, (int, float)) or not isfinite(n) for n in numbers):
            return self._reject("invalid_input", "Risk inputs must be finite numbers")
        if not intent.symbol or intent.side not in {"BUY", "SELL"}:
            return self._reject("intent", "A symbol and BUY/SELL side are required")
        if intent.quantity <= 0 or intent.reference_price <= 0:
            return self._reject("intent", "Quantity and reference price must be positive")
        if snapshot.nav <= 0:
            return self._reject("nav", "NAV must be positive")
        if (
            snapshot.gross_exposure < 0
            or snapshot.open_positions < 0
            or not isinstance(snapshot.open_positions, int)
        ):
            return self._reject("invalid_input", "Exposure and position count must be nonnegative")
        if (
            snapshot.spread_pct is None
            or not isfinite(snapshot.spread_pct)
            or snapshot.spread_pct < 0
        ):
            return self._reject("spread", "A finite nonnegative spread is required")
        if (
            not isinstance(snapshot.market_data_at, datetime)
            or snapshot.market_data_at.utcoffset() is None
        ):
            return self._reject("market_data_stale", "A timezone-aware quote timestamp is required")
        age = (datetime.now(timezone.utc) - snapshot.market_data_at).total_seconds()
        if age < 0 or age > self.limits.stale_data_seconds:
            return self._reject("market_data_stale", f"Market data age={age:.2f}s")

        daily_loss_pct = max(0.0, -snapshot.daily_pnl / snapshot.nav * 100)
        if daily_loss_pct >= self.limits.max_daily_loss_pct:
            return self._reject("daily_loss", f"Daily loss={daily_loss_pct:.3f}%")
        if snapshot.open_positions >= self.limits.max_open_positions:
            return self._reject("open_positions", "Max open positions reached")

        position_value = intent.quantity * intent.reference_price
        position_pct = position_value / snapshot.nav * 100
        if not isfinite(position_pct) or position_pct > self.limits.max_position_pct:
            return self._reject("position_size", f"Position={position_pct:.3f}% NAV")
        # Until position-aware reductions exist, every intent conservatively consumes
        # capacity. The caller must include outstanding reservations in gross exposure.
        exposure_pct = (snapshot.gross_exposure + position_value) / snapshot.nav * 100
        if not isfinite(exposure_pct) or exposure_pct > self.limits.max_total_exposure_pct:
            return self._reject("total_exposure", f"Projected exposure={exposure_pct:.3f}% NAV")
        stop = intent.stop_price
        if stop is None or not isfinite(stop) or stop <= 0:
            return self._reject("trade_risk", "A finite positive stop price is required")
        distance = (
            intent.reference_price - stop if intent.side == "BUY" else stop - intent.reference_price
        )
        if distance <= 0:
            return self._reject("trade_risk", "Stop price is on the wrong side of the entry")
        trade_risk_pct = distance * intent.quantity / snapshot.nav * 100
        if not isfinite(trade_risk_pct) or trade_risk_pct > self.limits.max_trade_risk_pct:
            return self._reject("trade_risk", f"Stop-distance risk={trade_risk_pct:.3f}% NAV")
        if snapshot.spread_pct > self.limits.max_spread_pct:
            return self._reject("spread", f"Spread={snapshot.spread_pct:.4f}")

        return RiskDecision(RiskDecisionType.APPROVED, "All rules passed", "all")

    def kill(self) -> None:
        self.killed = True

    def reset_kill(self) -> None:
        self.killed = False

    @staticmethod
    def _reject(rule: str, reason: str) -> RiskDecision:
        return RiskDecision(RiskDecisionType.REJECTED, reason, rule)
