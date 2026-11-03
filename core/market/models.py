from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class MarketInstrument:
    """What the market data service subscribes to.

    Options are deliberately out of scope for this phase, so only simple
    exchange-routed instruments appear here.
    """

    symbol: str
    sec_type: str
    exchange: str
    currency: str
    con_id: int | None = None

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.sec_type}|{self.exchange}|{self.currency}"

    def with_con_id(self, con_id: int | None) -> MarketInstrument:
        if con_id is None or con_id == self.con_id:
            return self
        return replace(self, con_id=con_id)


@dataclass(frozen=True, slots=True)
class Quote:
    """Latest known book snapshot for one instrument.

    Every field is optional because IBKR delivers ticks one at a time; a quote is
    assembled as they arrive rather than received whole.
    """

    instrument: MarketInstrument
    received_at: datetime
    bid: float | None = None
    bid_size: float | None = None
    ask: float | None = None
    ask_size: float | None = None
    last: float | None = None
    last_size: float | None = None
    volume: float | None = None
    delayed: bool = False

    @property
    def mid(self) -> float | None:
        """Mid of the touch, falling back to last.

        A zero price is IBKR's way of saying "no quote here", so it is treated as
        absent instead of producing a nonsense mid.
        """
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        if self.bid or self.ask:
            return self.bid or self.ask
        if self.last:
            return self.last
        return None

    @property
    def spread(self) -> float | None:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return self.ask - self.bid
        return None

    @property
    def spread_pct(self) -> float | None:
        spread = self.spread
        mid = self.mid
        if spread is None or not mid:
            return None
        return spread / mid * 100

    def age_seconds(self, now: datetime) -> float:
        return (now - self.received_at).total_seconds()

    def is_fresh(self, max_age_seconds: float, now: datetime) -> bool:
        """Freshness needs a usable price; a stale-but-present quote is not usable."""
        if self.mid is None:
            return False
        return self.age_seconds(now) <= max_age_seconds

    def with_tick(self, field_name: str, value: float, received_at: datetime) -> Quote:
        return replace(self, **{field_name: value}, received_at=received_at)

    def as_dict(self) -> dict:
        return {
            "key": self.instrument.key,
            "symbol": self.instrument.symbol,
            "sec_type": self.instrument.sec_type,
            "exchange": self.instrument.exchange,
            "currency": self.instrument.currency,
            "bid": self.bid,
            "bid_size": self.bid_size,
            "ask": self.ask,
            "ask_size": self.ask_size,
            "last": self.last,
            "last_size": self.last_size,
            "volume": self.volume,
            "mid": self.mid,
            "spread_pct": self.spread_pct,
            "delayed": self.delayed,
            "received_at": self.received_at.isoformat(),
        }
