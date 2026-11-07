from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
from typing import cast


# Omitted timestamps in old Quote constructors inherit received_at once. Explicit
# None means unknown; it must not silently become fresh on a later replace().
_UNSET_TIMESTAMP = object()
PRICE_FIELDS = frozenset({"bid", "ask", "last"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_number(value: float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _as_price(value: float | None) -> float | None:
    number = _as_number(value)
    return number if number is not None and number > 0 else None


def _aware(stamp: datetime | None) -> bool:
    return isinstance(stamp, datetime) and stamp.utcoffset() is not None


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
    """Latest book snapshot; transport activity is not executable price freshness.

    received_at tracks the latest tick, while each price keeps its own timestamp.
    Omitted price timestamps in directly constructed fixtures inherit received_at.
    delayed also latches non-real-time evidence until both BBO sides recover.
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
    bid_at: datetime | None = cast(datetime, _UNSET_TIMESTAMP)
    ask_at: datetime | None = cast(datetime, _UNSET_TIMESTAMP)
    last_at: datetime | None = cast(datetime, _UNSET_TIMESTAMP)
    market_data_type: int = 1

    def __post_init__(self) -> None:
        for name in ("bid", "ask", "last", "bid_size", "ask_size", "last_size", "volume"):
            value = getattr(self, name)
            number = _as_price(value) if name in PRICE_FIELDS else _as_number(value)
            object.__setattr__(self, name, number)
            if name in PRICE_FIELDS:
                stamp = getattr(self, f"{name}_at")
                if stamp is _UNSET_TIMESTAMP:
                    stamp = self.received_at
                if number is None or not _aware(stamp):
                    stamp = None
                object.__setattr__(self, f"{name}_at", stamp)
        if type(self.market_data_type) is not int or self.market_data_type not in (1, 2, 3, 4):
            object.__setattr__(self, "market_data_type", 0)  # unknown is not real-time

    @property
    def mid(self) -> float | None:
        """Display price only; a one-sided/last fallback is never executable BBO."""
        if self.bid is not None and self.ask is not None:
            return self.bid / 2 + self.ask / 2  # avoid overflowing two finite prices
        if self.bid is not None or self.ask is not None:
            return self.bid if self.bid is not None else self.ask
        return self.last

    @property
    def spread(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask >= self.bid:
            return self.ask - self.bid
        return None

    @property
    def spread_pct(self) -> float | None:
        spread = self.spread
        mid = self.mid
        if spread is None or not mid:
            return None
        return spread / mid * 100

    @property
    def price_at(self) -> datetime | None:
        """Oldest side of a usable two-sided BBO, not the last trade or tick."""
        if self.spread is None or self.bid_at is None or self.ask_at is None:
            return None
        return min(self.bid_at, self.ask_at)

    def age_seconds(self, now: datetime) -> float:
        """Transport age, retained for callers that monitor tick delivery."""
        return (now - self.received_at).total_seconds()

    def price_age_seconds(self, now: datetime) -> float | None:
        stamp = self.price_at
        if stamp is None or not _aware(now):
            return None
        return (now - stamp).total_seconds()

    def is_fresh(self, max_age_seconds: float, now: datetime) -> bool:
        """Require current, finite, uncrossed, real-time prices on BOTH sides."""
        limit = _as_number(max_age_seconds)
        if limit is None or limit < 0 or self.delayed or self.market_data_type != 1:
            return False
        if self.price_at is None or not _aware(now) or not _aware(self.received_at):
            return False
        if self.received_at > now:
            return False
        # Check both sides: the older side alone cannot reveal a future-dated side.
        return all(
            stamp is not None and 0 <= (now - stamp).total_seconds() <= limit
            for stamp in (self.bid_at, self.ask_at)
        )

    def with_tick(self, field_name: str, value: float | None, received_at: datetime) -> Quote:
        changes = {field_name: value, "received_at": received_at}
        if field_name in PRICE_FIELDS:
            changes[f"{field_name}_at"] = received_at if _as_price(value) is not None else None
        return replace(self, **changes)

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
            "market_data_type": self.market_data_type,
            "received_at": self.received_at.isoformat(),
            "bid_at": self.bid_at.isoformat() if self.bid_at is not None else None,
            "ask_at": self.ask_at.isoformat() if self.ask_at is not None else None,
            "last_at": self.last_at.isoformat() if self.last_at is not None else None,
            "price_at": self.price_at.isoformat() if self.price_at is not None else None,
        }
