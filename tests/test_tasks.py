"""backend/tasks.py: supervised background tasks and ordered lanes."""
from __future__ import annotations

import asyncio
import logging

from backend.tasks import Lane, failed, spawn


async def test_a_failing_background_task_is_logged_when_it_dies(caplog):
    # Fire-and-forget tasks kept their exception until shutdown gathered them.
    async def crash():
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="backend.tasks"):
        task = spawn(crash(), "night-scheduler")
        await asyncio.wait({task})
        await asyncio.sleep(0)  # done callbacks run on the next loop pass
    assert failed(task)
    assert "night-scheduler" in caplog.text and "boom" in caplog.text


async def test_a_cancelled_task_is_not_a_failure(caplog):
    task = spawn(asyncio.Event().wait(), "night-boost")
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait({task})
    await asyncio.sleep(0)
    assert not failed(task)
    assert caplog.text == ""


async def test_a_lane_runs_in_order_and_cancelling_one_step_spares_the_rest():
    lane, done = Lane("test"), []

    async def step(name, delay):
        await asyncio.sleep(delay)
        done.append(name)

    first = lane.run(step("first", 0.05))
    second = lane.run(step("second", 0))
    third = lane.run(step("third", 0))
    second.cancel()
    await asyncio.wait({first, second, third})
    assert done == ["first", "third"]
    assert second.cancelled() and not first.cancelled()
