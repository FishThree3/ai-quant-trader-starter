from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Protocol

import structlog

from core.events.models import DomainEvent, EventType
from core.market.models import MarketInstrument, Quote, utcnow
from core.market.store import QuoteStore

log = structlog.get_logger()

MAX_TICKERS_ERROR = 101
# Codes that mean "this account cannot receive this data", not "try again".
ENTITLEMENT_ERROR_CODES = frozenset({354, 10167, 10168, 10089})

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
    74: ("volume", True),
}

PRICE_FIELDS = frozenset({"bid", "ask", "last"})


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
            return False

        line = _Line(req_id=self._allocate_req_id(), instrument=instrument, consumers={consumer})
        self._lines[line.req_id] = line
        self._line_by_key[instrument.key] = line

        try:
            self.gateway.subscribe(instrument, line.req_id)
        except Exception as exc:
            self._drop_line(line)
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

        self._drop_line(line)
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
        if line is None:
            return None

        mapping = FIELDS.get(tick_type)
        if mapping is None:
            return None

        field_name, delayed = mapping
        number = _as_price(value) if field_name in PRICE_FIELDS else _as_number(value)
        if number is None:
            return None

        stamp = received_at or self.clock()
        current = self._quotes.get(line.instrument.key) or Quote(
            instrument=line.instrument, received_at=stamp
        )
        quote = current.with_tick(field_name, number, stamp)
        if delayed:
            quote = replace(quote, delayed=True)

        self._quotes[line.instrument.key] = quote
        await self._publish(quote)
        return quote

    async def apply_error(self, req_id: int, code: int, message: str) -> None:
        if code == MAX_TICKERS_ERROR:
            self.ticker_limit_reached = True
            log.error("market_data_ticker_limit", message=message)
            return

        if req_id < 0:
            return

        line = self._lines.get(req_id)
        if line is None or code not in ENTITLEMENT_ERROR_CODES:
            return

        # Keep the line as a memo so nothing re-requests data this account cannot get.
        line.blocked_reason = f"IBKR {code}"
        line.consumers.clear()
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
        if quote is None:
            raise NoMarketData(f"no market data for {instrument.symbol}")
        if not quote.is_fresh(self.stale_seconds, self.clock()):
            age = quote.age_seconds(self.clock())
            raise StaleMarketData(
                f"market data for {instrument.symbol} is {age:.1f}s old "
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

    def _drop_line(self, line: _Line) -> None:
        self._lines.pop(line.req_id, None)
        self._line_by_key.pop(line.instrument.key, None)
        try:
            self.gateway.unsubscribe(line.req_id)
        except Exception as exc:  # the socket may already be gone
            log.warning("market_data_unsubscribe_failed", req_id=line.req_id, error=str(exc))

    async def _publish(self, quote: Quote) -> None:
        # The in-process cache is what risk consults, so a dead store or bus must
        # not cost us the quote itself.
        try:
            if self._store is not None:
                await self._store.save(quote)
            if self.event_bus is not None:
                await self.event_bus.publish(
                    DomainEvent(type=EventType.MARKET_TICK, payload=quote.as_dict())
                )
        except Exception as exc:
            log.error("market_data_publish_failed", symbol=quote.instrument.symbol, error=str(exc))


def _as_price(value: float | None) -> float | None:
    """IBKR reports 0.0 for an absent price, which must not become a mid of 0."""
    number = _as_number(value)
    if number is None or number <= 0:
        return None
    return number


def _as_number(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
