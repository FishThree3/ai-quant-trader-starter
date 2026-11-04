from __future__ import annotations

from collections.abc import AsyncIterator

from redis.asyncio import Redis

CONTROL_CHANNEL = "quant:control.changed"


class RedisControlPlane:
    """Accelerator for gate changes: the database stays authoritative.

    Publishing a poke only tells a running worker to re-read immediately instead of
    waiting for its next poll, so a lost message costs latency, never safety.
    """

    def __init__(self, client: Redis, *, channel: str = CONTROL_CHANNEL) -> None:
        self.client = client
        self.channel = channel

    @classmethod
    def from_url(cls, url: str) -> "RedisControlPlane":
        return cls(Redis.from_url(url, decode_responses=True))

    async def healthy(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def poke(self, reason: str) -> None:
        await self.client.publish(self.channel, reason[:512] or "changed")

    async def stream(self) -> AsyncIterator[str]:
        pubsub = self.client.pubsub()
        await pubsub.subscribe(self.channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    yield str(message.get("data") or "")
        finally:
            await pubsub.unsubscribe(self.channel)
            await pubsub.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()
