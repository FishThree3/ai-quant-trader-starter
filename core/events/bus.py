from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable

from core.events.models import DomainEvent, EventType

Handler = Callable[[DomainEvent], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: DomainEvent) -> None:
        for handler in self._handlers.get(event.type, []):
            await handler(event)
