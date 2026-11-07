from __future__ import annotations

from asyncio import Lock
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Protocol

import structlog

from core.events.models import DomainEvent, EventType
from core.market.models import (
    PRICE_FIELDS,
    MarketInstrument,
    Quote,
    _as_number,
    _as_price,
    _aware,
    utcnow,
)
from core.market.store import QuoteStore

log = structlog.get_logger()

MAX_TICKERS_ERROR = 101
# Codes that mean "this account cannot receive this data", not "try again".
ENTITLEMENT_ERROR_CODES = frozenset({354, 10168, 10089})
DELAYED_DATA_AVAILABLE = 10167

# IBKR tick field ids -> (Quote attribute, is_delayed_variant)
FIELDS: dict[int, tuple[str, bool]] = {
    0: ("bid_size", False),
    1: ("bid", False),
    2: ("ask", False),
    3: ("ask_size", False),
    4: ("last", False),
    5: ("last_size", False),
    8: ("volume", False),
    66: ("bid", True),
    67: ("ask", True),
    68: ("last", True),
    69: ("bid_size", True),
    70: ("ask_size", True),
    71: ("last_size", True),
    74: ("volume", True),
}


class NoMarketData(RuntimeError):
    pass


class StaleMarketData(RuntimeError):
    pass


class MarketDataGateway(Protocol):
    """The broker calls the market data service is allowed to make."""

    def subscribe(self, instrument: MarketInstrument, req_id: int) -> None: ...

    def unsubscribe(self, req_id: int) -> None: ...


@dataclass(slots=True)
class _Line:
    req_id: int
    instrument: MarketInstrument
    consumers: set[str] = field(default_factory=set)
    blocked_reason: str | None = None
    market_data_type: int = 1
    non_realtime: bool = False
    realtime_sides: set[str] = field(default_factory=set)


class MarketDataService:
    """Reference-counted stock market data with a hard freshness gate.

    Two rules shape this class. One IBKR line per instrument, no matter how many
    consumers want it, because lines are a paid, capped resource. And a refusal to
    retry a subscription the broker rejected, because retry loops are how a
    market-data outage turns into a wedged worker.
    """

    def __init__(
        self,
        gateway: MarketDataGateway,
        *,
        stale_seconds: float = 10.0,
        req_id_start: int = 90_000,
        store: QuoteStore | None = None,
        event_bus: Any = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.gateway = gateway
        self.stale_seconds = stale_seconds
        self._next_req_id = req_id_start
        self._store = store
        self.event_bus = event_bus
        self.clock = clock

        self._lines: dict[int, _Line] = {}
        self._line_by_key: dict[str, _Line] = {}
        self._quotes: dict[str, Quote] = {}
        self._publish_lock = Lock()
        self.ticker_limit_reached = False

    # --- subscription lifecycle -------------------------------------------------

    async def subscribe(self, instrument: MarketInstrument, consumer: str) -> bool:
        """Attach a consumer, opening one broker line for everyone."""
        existing = self._line_by_key.get(instrument.key)
        if existing is not None:
            existing.consumers.add(consumer)
            if existing.blocked_reason is not None:
                log.info(
                    "market_data_reuse_blocked_line",
                    symbol=instrument.symbol,
                    reason=existing.blocked_reason,
                )
            return existing.blocked_reason is None

        if self.ticker_limit_reached:
            # No retries: the limit is an entitlement/usage condition, not a blip.
            log.warning(
                "market_data_subscribe_refused",
                symbol=instrument.symbol,
                reason="ticker limit reached",
            )
            await self._invalidate_quote(instrument)
            return False

        line = _Line(req_id=self._allocate_req_id(), instrument=instrument, consumers={consumer})
        self._lines[line.req_id] = line
        self._line_by_key[instrument.key] = line

        try:
            self.gateway.subscribe(instrument, line.req_id)
        except Exception as exc:
            await self._drop_line(line)
            log.error("market_data_subscribe_failed", symbol=instrument.symbol, error=str(exc))
            return False

        log.info("market_data_subscribed", symbol=instrument.symbol, req_id=line.req_id)
        return True

    async def unsubscribe(self, instrument: MarketInstrument, consumer: str) -> bool:
        """Release a consumer; the broker line closes only when the last one leaves."""
        line = self._line_by_key.get(instrument.key)
        if line is None:
            return False

        line.consumers.discard(consumer)
        if line.consumers:
            return False

        await self._drop_line(line)
        log.info("market_data_released", symbol=instrument.symbol, req_id=line.req_id)
        return True

    def subscribers(self, instrument: MarketInstrument) -> frozenset[str]:
        line = self._line_by_key.get(instrument.key)
        return frozenset(line.consumers) if line else frozenset()

    # --- inbound data -----------------------------------------------------------

    async def apply_tick(
        self,
        req_id: int,
        tick_type: int,
        value: float | None,
        *,
        received_at: datetime | None = None,
    ) -> Quote | None:
        line = self._lines.get(req_id)
        if line is None or line.blocked_reason is not None:
            return None

        mapping = FIELDS.get(tick_type)
        if mapping is None:
            return None

        field_name, delayed = mapping
        is_price = field_name in PRICE_FIELDS
        number = _as_price(value) if is_price else _as_number(value)
        now = self.clock()
        stamp = received_at if received_at is not None else now
        valid_stamp = _aware(stamp) and _aware(now) and stamp <= now
        if not valid_stamp:
            # Invalid price ticks withdraw the old side rather than preserving an
            # executable value (including a future tick that could later age in).
            number = None
            stamp = now

        if delayed or line.market_data_type != 1:
            line.non_realtime = True
            line.realtime_sides.clear()
        elif is_price and field_name in {"bid", "ask"}:
            if number is None or not 0 <= (now - stamp).total_seconds() <= self.stale_seconds:
                line.realtime_sides.discard(field_name)
            else:
                line.realtime_sides.add(field_name)
            if line.realtime_sides == {"bid", "ask"}:
                line.non_realtime = False

        current = self._quotes.get(line.instrument.key) or Quote(
            instrument=line.instrument, received_at=stamp
        )
        # Clearing malformed numeric fields prevents NaN/inf from reaching JSON.
        # with_tick preserves all untouched price timestamps.
        quote = current.with_tick(field_name, number, stamp)
        quote = replace(quote, delayed=line.non_realtime, market_data_type=line.market_data_type)
        self._quotes[line.instrument.key] = quote
        await self._publish(quote)
        return quote

    async def apply_market_data_type(self, req_id: int, mode: int) -> None:
        """Consume IBKR's actual per-line mode, not the requested global preference.

        1 = real-time, 2 = frozen, 3 = delayed, 4 = delayed-frozen. Unknown
        modes fail closed. A mode notification never refreshes a price or clears
        the non-real-time latch: that requires genuine bid AND ask ticks.
        """
        line = self._lines.get(req_id)
        if line is None or line.blocked_reason is not None:
            return
        line.market_data_type = mode if type(mode) is int and mode in (1, 2, 3, 4) else 0
        if line.market_data_type != 1:
            line.non_realtime = True
            line.realtime_sides.clear()
        current = self._quotes.get(line.instrument.key)
        if current is not None:
            quote = replace(
                current, delayed=line.non_realtime, market_data_type=line.market_data_type
            )
            self._quotes[line.instrument.key] = quote
            await self._publish(quote)

    async def apply_error(self, req_id: int, code: int, message: str) -> None:
        if code == DELAYED_DATA_AVAILABLE:
            # A warning that delayed data follows, not a rejected subscription.
            # Keep displaying it, but never authorize execution from these ticks.
            await self.apply_market_data_type(req_id, 3)
            log.warning("market_data_delayed", req_id=req_id, code=code)
            return
        if code == MAX_TICKERS_ERROR:
            self.ticker_limit_reached = True
            log.error("market_data_ticker_limit", message=message)

        if req_id < 0:
            return

        line = self._lines.get(req_id)
        if line is None or code not in ENTITLEMENT_ERROR_CODES | {MAX_TICKERS_ERROR}:
            return

        # Keep the line as a memo so nothing re-requests data this account cannot get.
        line.blocked_reason = f"IBKR {code}"
        line.consumers.clear()
        await self._invalidate_quote(line.instrument)
        log.error(
            "market_data_unavailable",
            symbol=line.instrument.symbol,
            code=code,
            message=message,
        )

    # --- reads ------------------------------------------------------------------

    def quote(self, instrument: MarketInstrument) -> Quote | None:
        return self._quotes.get(instrument.key)

    def require_fresh(self, instrument: MarketInstrument) -> Quote:
        """The gate risk uses: no usable quote, or a stale one, is a hard refusal."""
        quote = self._quotes.get(instrument.key)
        line = self._line_by_key.get(instrument.key)
        if quote is None or line is None or line.blocked_reason is not None:
            raise NoMarketData(f"no market data for {instrument.symbol}")
        now = self.clock()
        if not quote.is_fresh(self.stale_seconds, now):
            age = quote.price_age_seconds(now)
            detail = f"BBO is {age:.1f}s old" if age is not None else "no usable two-sided BBO"
            raise StaleMarketData(
                f"market data for {instrument.symbol} is not executable real-time data: {detail} "
                f"(limit {self.stale_seconds}s)"
            )
        return quote

    def line_count(self) -> int:
        return len(self._lines)

    # --- internals --------------------------------------------------------------

    def _allocate_req_id(self) -> int:
        req_id = self._next_req_id
        self._next_req_id += 1
        return req_id

    async def _drop_line(self, line: _Line) -> None:
        self._lines.pop(line.req_id, None)
        self._line_by_key.pop(line.instrument.key, None)
        try:
            self.gateway.unsubscribe(line.req_id)
        except Exception as exc:  # the socket may already be gone
            log.warning("market_data_unsubscribe_failed", req_id=line.req_id, error=str(exc))
        await self._invalidate_quote(line.instrument)

    async def _invalidate_quote(self, instrument: MarketInstrument) -> None:
        current = self._quotes.pop(instrument.key, None)
        # Stores have no deletion API; a price-less tombstone also informs
        # streaming readers immediately. Preserve the last transport stamp.
        # Publish even without a local snapshot to withdraw any older store entry.
        await self._publish(
            Quote(
                instrument=instrument,
                received_at=current.received_at if current is not None else self.clock(),
            )
        )

    async def _publish(self, quote: Quote) -> None:
        # The in-process cache is what risk consults, so a dead store or bus must
        # not cost us the quote itself.
        # Broker callbacks run as separate asyncio tasks. Serialize their writes
        # so an older in-flight tick cannot overwrite a mode change or tombstone.
        async with self._publish_lock:
            try:
                if self._store is not None:
                    await self._store.save(quote)
            except Exception as exc:
                log.error(
                    "market_data_publish_failed", symbol=quote.instrument.symbol, error=str(exc)
                )
        # Do not hold the store lock across subscriber code: a handler may release
        # its subscription and publish a tombstone in response to this event.
        try:
            if self.event_bus is not None:
                await self.event_bus.publish(
                    DomainEvent(type=EventType.MARKET_TICK, payload=quote.as_dict())
                )
        except Exception as exc:
            log.error("market_data_publish_failed", symbol=quote.instrument.symbol, error=str(exc))
