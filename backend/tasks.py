"""Background tasks that say when they die, and lanes that keep their order.

asyncio keeps a fire-and-forget task's exception to itself until the task is
garbage-collected — for the lifespan's tasks, until shutdown gathers them. The
night scheduler or the HA bridge could stop for good with nothing in the
journal until the next restart printed it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

log = logging.getLogger(__name__)

# The event loop holds tasks only weakly: a spawned task nobody else
# references could be collected mid-flight.
_live: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    """create_task, plus a strong reference and a log line if it fails."""
    task = asyncio.create_task(coro, name=name)
    _live.add(task)
    task.add_done_callback(_finished)
    return task


def _finished(task: asyncio.Task) -> None:
    _live.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task %s failed: %r", task.get_name(), exc, exc_info=exc)


def failed(task: asyncio.Task) -> bool:
    """Ended by an exception (not a cancel, not a normal return)."""
    return task.done() and not task.cancelled() and task.exception() is not None


class Lane:
    """Runs coroutines in background tasks, each after the one before it.

    For work that must not hold up its caller but must keep its order: a
    WebRTC answer reaches the display before the ICE candidates behind it, and
    two thermostat taps reach Home Assistant in the order they were made.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        # Resolved once everything queued so far has finished or been cancelled.
        self._tail: asyncio.Future | None = None

    def run(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        previous = self._tail
        released = asyncio.get_running_loop().create_future()
        self._tail = released

        async def step():
            if previous is not None and not previous.done():
                # wait(), not await: cancelling this step must not cancel the
                # ones ahead of it.
                await asyncio.wait({previous})
            return await coro

        task = spawn(step(), self.name)

        def release(_) -> None:
            if task.cancelled():
                coro.close()  # cancelled before its turn: never awaited, and that's fine
            # A cancelled step still holds the next one until the steps ahead
            # of it are done, or the order would break around it.
            if task.done() and (previous is None or previous.done()) and not released.done():
                released.set_result(None)

        task.add_done_callback(release)
        if previous is not None:
            previous.add_done_callback(release)
        return task
