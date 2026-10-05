"""Fan-out of engine events to WebSocket clients."""

from __future__ import annotations

import asyncio
from typing import Any

from pagewatch.engine.clock import Clock, iso
from pagewatch.models import EngineEvent

QUEUE_SIZE = 1000


class Subscription:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.queue: asyncio.Queue[EngineEvent] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.dropped = 0

    def close(self) -> None:
        self._bus._subs.discard(self)


class EventBus:
    """``publish`` never blocks: a slow client loses its oldest events, not the engine."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._subs: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        self._subs.add(sub)
        return sub

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, type_: str, data: dict[str, Any] | None = None) -> None:
        if not self._subs:
            return
        event = EngineEvent(type=type_, data=data or {}, ts=iso(self._clock.now()))
        for sub in list(self._subs):
            if sub.queue.full():
                sub.queue.get_nowait()
                sub.dropped += 1
            sub.queue.put_nowait(event)
