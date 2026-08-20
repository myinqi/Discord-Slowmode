"""In-process event broker for the private TrYa DCS player."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class TryaDcsEventBroker:
    """Fan out versioned state/chat events without carrying media bytes."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._sequence = 0

    async def publish(self, event_type: str, data: dict | None = None) -> None:
        self._sequence += 1
        event = {
            "version": 1,
            "sequence": self._sequence,
            "type": event_type,
            "timestamp": time.time(),
            "data": data or {},
        }
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        await self.publish("radio.listener_count", {"count": self.listener_count})
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            await self.publish("radio.listener_count", {"count": self.listener_count})

    @property
    def listener_count(self) -> int:
        return len(self._subscribers)


trya_dcs_events = TryaDcsEventBroker()
