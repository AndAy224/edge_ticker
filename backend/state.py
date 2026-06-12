"""Central state store: latest payload per module + WebSocket broadcast fan-out."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class TapeItem(BaseModel):
    text: str
    accent: Literal["neutral", "up", "down", "alert"] = "neutral"
    priority: int = 0
    # Optional icon key rendered by the display before the text (e.g. a sport
    # name mapped to an inline SVG — the appliance has no emoji fonts).
    icon: str | None = None


class ModulePayload(BaseModel):
    module: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stale: bool = False
    stage: dict[str, Any] = Field(default_factory=dict)
    tape: list[TapeItem] = Field(default_factory=list)


class Bus:
    """Holds the latest payload per module and pushes diffs to subscribers.

    Each WebSocket connection subscribes with its own queue; a slow client
    drops messages rather than backing up collectors.
    """

    def __init__(self) -> None:
        self.payloads: dict[str, ModulePayload] = {}
        # Last state report from the display (current module, pinned, blanked),
        # kept so late-joining admin clients get it in their snapshot.
        self.display_state: dict = {}
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def publish(self, payload: ModulePayload) -> None:
        self.payloads[payload.module] = payload
        await self.broadcast({"type": "module", "payload": payload.model_dump(mode="json")})

    async def broadcast(self, message: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                log.warning("dropping %s message for slow client", message.get("type"))

    def snapshot(self) -> dict[str, dict]:
        return {name: p.model_dump(mode="json") for name, p in self.payloads.items()}
