from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis

from core.market.models import Quote

KEY_PREFIX = "quant:md:"
TICK_CHANNEL = "quant:md.tick"
DEFAULT_TTL_SECONDS = 60


class RedisQuoteStore:
    """Latest quotes in Redis, so the API can read them from another process.

    Every key carries a TTL: a quote nobody has refreshed is exactly the kind of
    number that must not survive its publisher as apparently current data.
    """

    def __init__(
        self,
        client: Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        key_prefix: str = KEY_PREFIX,
        channel: str = TICK_CHANNEL,
    ) -> None:
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self.channel = channel

    @classmethod
    def from_url(cls, url: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> RedisQuoteStore:
        return cls(Redis.from_url(url, decode_responses=True), ttl_seconds=ttl_seconds)

    def _key(self, instrument_key: str) -> str:
        return f"{self.key_prefix}{instrument_key}"

    async def save(self, quote: Quote) -> None:
        payload = json.dumps(quote.as_dict(), separators=(",", ":"))
        pipe = self.client.pipeline()
        pipe.set(self._key(quote.instrument.key), payload, ex=self.ttl_seconds)
        pipe.publish(self.channel, payload)
        await pipe.execute()

    async def load(self, instrument_key: str) -> dict[str, Any] | None:
        raw = await self.client.get(self._key(instrument_key))
        if raw is None:
            return None
        return json.loads(raw)

    async def keys(self) -> list[str]:
        width = len(self.key_prefix)
        found = []
        async for key in self.client.scan_iter(match=f"{self.key_prefix}*", count=100):
            found.append(key[width:])
        return sorted(found)

    async def stream(self, instrument_key: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield published ticks, optionally filtered to one instrument."""
        pubsub = self.client.pubsub()
        await pubsub.subscribe(self.channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                except (TypeError, ValueError):
                    continue
                if instrument_key is None or payload.get("key") == instrument_key:
                    yield payload
        finally:
            await pubsub.unsubscribe(self.channel)
            await pubsub.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()
