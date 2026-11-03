from __future__ import annotations

import re

from core.market.models import MarketInstrument

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")

SUPPORTED_SEC_TYPE = "STK"
SUPPORTED_CURRENCY = "USD"
ROUTING_EXCHANGE = "SMART"


class ContractResolutionError(ValueError):
    pass


def resolve_us_equity(symbol: str) -> MarketInstrument:
    """Build the US equity contract used for both market data and orders.

    SMART routing is intentional: it lets IBKR choose the venue, so a quote's
    exchange never has to match the venue that eventually prints the fill.
    """
    cleaned = (symbol or "").strip().upper()

    if not _SYMBOL_RE.match(cleaned):
        raise ContractResolutionError(
            f"{symbol!r} is not a usable US equity symbol (letters, digits, '.' or '-')"
        )

    return MarketInstrument(
        symbol=cleaned,
        sec_type=SUPPORTED_SEC_TYPE,
        exchange=ROUTING_EXCHANGE,
        currency=SUPPORTED_CURRENCY,
    )


def require_supported(instrument: MarketInstrument) -> MarketInstrument:
    """Reject anything outside this phase's scope before it reaches the broker."""
    if instrument.sec_type != SUPPORTED_SEC_TYPE:
        raise ContractResolutionError(
            f"sec_type {instrument.sec_type!r} is not supported yet; only US STK is enabled"
        )
    if instrument.currency != SUPPORTED_CURRENCY:
        raise ContractResolutionError(
            f"currency {instrument.currency!r} is not supported; only USD equities are enabled"
        )
    return instrument


class ContractResolver:
    """Caches resolved instruments so repeated lookups do not revalidate."""

    def __init__(self) -> None:
        self._cache: dict[str, MarketInstrument] = {}

    def resolve(self, symbol: str) -> MarketInstrument:
        cached = self._cache.get((symbol or "").strip().upper())
        if cached is not None:
            return cached

        instrument = require_supported(resolve_us_equity(symbol))
        self._cache[instrument.symbol] = instrument
        return instrument

    def remember(self, instrument: MarketInstrument) -> MarketInstrument:
        """Store a broker-resolved contract (e.g. one carrying a conId)."""
        validated = require_supported(instrument)
        self._cache[validated.symbol] = validated
        return validated

    def known(self, symbol: str) -> MarketInstrument | None:
        return self._cache.get((symbol or "").strip().upper())
