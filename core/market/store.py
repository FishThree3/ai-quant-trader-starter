from __future__ import annotations

from typing import Any, Protocol

from core.market.models import Quote


class QuoteStore(Protocol):
    """Where the latest quote is published for readers outside the worker.

    Kept async because every implementation touches a socket: an in-memory dict for
    one-process runs, Redis when the API is a separate service.
    """

    async def save(self, quote: Quote) -> None: ...

    async def load(self, instrument_key: str) -> dict[str, Any] | None: ...

    async def keys(self) -> list[str]: ...


class InMemoryQuoteStore:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    async def save(self, quote: Quote) -> None:
        self._items[quote.instrument.key] = quote.as_dict()

    async def load(self, instrument_key: str) -> dict[str, Any] | None:
        return self._items.get(instrument_key)

    async def keys(self) -> list[str]:
        return sorted(self._items)
