from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

import structlog

from core.events.models import utcnow

log = structlog.get_logger()


class TradingState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    KILLED = "KILLED"


class TradingHalted(RuntimeError):
    """Raised when the gate cannot affirmatively show it is safe to trade."""

    def __init__(self, state: TradingState, reasons: tuple[str, ...]) -> None:
        super().__init__(f"new orders refused while {state.value}: {', '.join(reasons)}")
        self.state = state
        self.reasons = reasons


@dataclass(frozen=True, slots=True)
class SystemState:
    state: TradingState
    reason: str
    actor: str
    updated_at: datetime

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "actor": self.actor,
            "updated_at": self.updated_at.isoformat(),
        }


class SystemStateStore(Protocol):
    """Authoritative record of the trading gate, durable across worker restarts."""

    async def read(self) -> SystemState | None: ...

    async def write(self, state: TradingState, *, reason: str, actor: str) -> SystemState: ...


class ControlPlaneProbe(Protocol):
    """Health of the channel that delivers kill/resume signals to a running worker."""

    async def healthy(self) -> bool: ...


class TradingGate(Protocol):
    """What the order manager needs: an answer about new orders, and nothing else.

    Cancellation is deliberately outside this gate: reducing exposure must stay possible
    even while new business is blocked.
    """

    def allows_new_orders(self) -> bool: ...

    def require_new_orders_allowed(self) -> None: ...


class AlwaysActive:
    """Fixed gate for unit tests and offline development; production must inject a real one.

    Exposes the same surface as KillSwitch, so a caller cannot tell which one it was handed.
    """

    def __init__(
        self,
        state: TradingState = TradingState.ACTIVE,
        *,
        reason: str = "test gate",
        actor: str = "tests",
    ) -> None:
        self._state = state
        self.reason = reason
        self.actor = actor

    @property
    def state(self) -> SystemState:
        return SystemState(self._state, self.reason, self.actor, utcnow())

    async def refresh(self) -> SystemState:
        return self.state

    def allows_new_orders(self) -> bool:
        return self._state is TradingState.ACTIVE

    def require_new_orders_allowed(self) -> None:
        if not self.allows_new_orders():
            raise TradingHalted(self._state, (self.reason,))

    def block(self, reason: str, state: TradingState = TradingState.PAUSED) -> None:
        self._state = state
        self.reason = reason


class KillSwitch:
    """The trading gate, which fails closed whenever evidence of safety is missing.

    An ACTIVE verdict needs three things at once: the durable record really says ACTIVE,
    that record is recent enough to still be true, and the control plane that would
    deliver a kill is reachable. Losing the database, losing Redis, or simply failing to
    refresh are each enough on their own to stop new orders.
    """

    def __init__(
        self,
        store: SystemStateStore | None,
        *,
        control_plane: ControlPlaneProbe | None = None,
        fallback_state: TradingState = TradingState.PAUSED,
        max_age_seconds: float = 30.0,
        clock=utcnow,
    ) -> None:
        self.store = store
        self.control_plane = control_plane
        self.fallback_state = fallback_state
        self.max_age_seconds = max_age_seconds
        self.clock = clock
        self._current: SystemState | None = None
        self.reasons: tuple[str, ...] = ("not refreshed yet",)

    @property
    def state(self) -> SystemState | None:
        return self._current

    async def refresh(self) -> SystemState:
        """Re-read the authoritative state and the control plane health."""
        reasons: list[str] = []
        recorded: SystemState | None = None

        if self.store is None:
            reasons.append("no state store configured")
        else:
            try:
                recorded = await self.store.read()
            except Exception as exc:
                reasons.append(f"state store unavailable: {type(exc).__name__}")

        if recorded is None:
            if self.store is not None:
                reasons.append("no durable trading state recorded")

        if self.control_plane is not None:
            try:
                if not await self.control_plane.healthy():
                    reasons.append("control plane unreachable")
            except Exception as exc:
                reasons.append(f"control plane probe failed: {type(exc).__name__}")

        if recorded is not None:
            age = (self.clock() - recorded.updated_at).total_seconds()
            if age > self.max_age_seconds:
                reasons.append(f"trading state is {age:.0f}s old")

        if reasons:
            # Never reuse a previously ACTIVE verdict once the evidence is gone.
            self._current = SystemState(
                self._held_state(recorded), "; ".join(reasons), "kill_switch", self.clock()
            )
        else:
            self._current = recorded

        self.reasons = tuple(reasons)
        return self._current

    def _held_state(self, recorded: SystemState | None) -> TradingState:
        if recorded is not None and recorded.state is not TradingState.ACTIVE:
            return recorded.state
        return self.fallback_state

    def allows_new_orders(self) -> bool:
        return self._current is not None and not self.reasons and self._current.state is (
            TradingState.ACTIVE
        )

    def require_new_orders_allowed(self) -> None:
        if self.allows_new_orders():
            return
        state = self._current.state if self._current else TradingState.PAUSED
        raise TradingHalted(state, self.reasons or (f"state is {state.value}",))

    async def engage(
        self, state: TradingState, *, reason: str, actor: str
    ) -> SystemState:
        """Record a gate change, then adopt it locally without waiting for a refresh."""
        if self.store is None:
            raise TradingHalted(
                TradingState.PAUSED, ("cannot change the gate without a durable store",)
            )

        written = await self.store.write(state, reason=reason, actor=actor)
        self._current = written
        self.reasons = ()
        log.warning("trading_state_changed", state=state.value, reason=reason, actor=actor)
        return written
