"""``Engine.spawn`` and ``Engine.drain_background``: fire-and-forget work must always be drainable.

The regression these guard (docs/DECISIONS.md, *How green it was, honestly*, item 4): the full
suite once hung for 17 minutes at 90 % CPU inside ``drain_background``. The cause was not a task that
kept spawning more: ``drain_background`` looped on ``gather`` over the engine's task set, Python
3.12's ``gather`` completes eagerly when every task it is given has finished, and ``await`` on a
finished future does not suspend. A task that finished in the same turn of the event loop as the
caller was still in the set (its ``discard`` callback runs on the *next* turn), so the loop spun
forever on a loop that never got to turn.
"""

from __future__ import annotations

import asyncio
import inspect
import signal
from collections.abc import Iterator
from typing import Any

import pytest

from pagewatch.engine.core import DrainError, Engine


@pytest.fixture(autouse=True)
def event_loop_watchdog() -> Iterator[None]:
    """Fail a test, instead of hanging the run, if the event loop is starved. A starved loop cannot
    time itself out, but SIGALRM is delivered to the main thread between any two bytecodes. POSIX
    only: elsewhere a regression hangs, as it used to."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def starved(signum: int, frame: Any) -> None:
        raise AssertionError("the event loop was starved: drain_background spun without yielding")

    previous = signal.signal(signal.SIGALRM, starved)
    signal.alarm(20)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


async def test_drain_returns_when_a_task_finished_in_the_turn_the_caller_resumed_in(
    engine: Engine,
) -> None:
    """The hang itself, made deterministic: ``quick`` finishes and the caller resumes in the same
    turn of the loop, so ``quick`` is done but still in the set. Before the fix this never returned."""

    async def quick() -> None:
        return None

    engine.spawn(quick(), "quick")
    await asyncio.sleep(0)
    # the state that hung: a finished task, its discard callback still queued
    assert engine._background
    assert all(task.done() for task in engine._background)

    await engine.drain_background()
    assert not engine._background


async def test_drain_waits_for_work_and_for_the_work_that_work_spawns(engine: Engine) -> None:
    done: list[str] = []

    async def second() -> None:
        await asyncio.sleep(0.02)
        done.append("second")

    async def first() -> None:
        await asyncio.sleep(0.02)
        engine.spawn(second(), "second")
        done.append("first")

    engine.spawn(first(), "first")
    await engine.drain_background()
    assert done == ["first", "second"]
    assert not engine._background


async def test_drain_with_nothing_to_wait_for_returns_at_once(engine: Engine) -> None:
    await engine.drain_background()
    assert not engine._background


async def test_drain_gives_up_on_work_that_never_finishes_and_says_where_it_is(
    engine: Engine,
) -> None:
    async def stuck() -> None:
        await asyncio.Event().wait()

    engine.spawn(stuck(), "stuck-task")
    with pytest.raises(DrainError) as caught:
        await engine.drain_background(timeout_s=0.1)
    message = str(caught.value)
    assert "stuck-task" in message  # which task
    assert "test_engine_background.py" in message  # where it is suspended
    assert "still busy after 0.1 s" in message


async def test_drain_gives_up_on_a_task_that_keeps_yielding_without_finishing(
    engine: Engine,
) -> None:
    """The busy variant: it hands control back every turn, so it is not starving the loop, but it
    never ends either (a ``clock.sleep(0)`` loop on a FakeClock looks like this)."""

    async def busy() -> None:
        while True:  # noqa: ASYNC110  (the point: a loop that yields but never ends)
            await asyncio.sleep(0)

    engine.spawn(busy(), "busy-task")
    with pytest.raises(DrainError, match="busy-task"):
        await engine.drain_background(timeout_s=0.2)


async def test_drain_stops_a_task_that_respawns_itself_and_refuses_the_rest(
    engine: Engine,
) -> None:
    """The cycle the audit looked for (none was found in the engine): the draining guard raises,
    names the culprit and the spawns after the budget are refused, so nothing is left running."""
    created: list[Any] = []

    async def again() -> None:
        await asyncio.sleep(0)
        coro = again()
        created.append(coro)
        engine.spawn(coro, "again")

    engine.spawn(again(), "again")
    with pytest.raises(DrainError, match=r"more than 20 tasks spawned.*again x2\d"):
        await engine.drain_background(max_spawns=20, timeout_s=10)

    await asyncio.sleep(0.05)
    assert not engine._running_background()  # the chain ended: the next spawn was refused
    assert inspect.getcoroutinestate(created[-1]) == inspect.CORO_CLOSED  # and not left un-awaited


async def test_drain_gives_up_after_its_rounds(engine: Engine) -> None:
    """Each round waits for the tasks running at that moment; a chain that always has a successor
    ends on the round cap even when it stays under the spawn budget."""

    async def link() -> None:
        await asyncio.sleep(0.001)
        engine.spawn(link(), "link")

    engine.spawn(link(), "link")
    with pytest.raises(DrainError, match="still busy after 5 rounds"):
        await engine.drain_background(max_rounds=5, max_spawns=10_000, timeout_s=10)


async def test_spawn_is_not_limited_when_nothing_is_draining(engine: Engine) -> None:
    """The budget belongs to a drain: ordinary spawning is never refused or counted."""
    ran: list[int] = []

    async def job(n: int) -> None:
        ran.append(n)

    for n in range(50):
        engine.spawn(job(n), "job")
    assert not engine._drain_spawned
    await engine.drain_background(max_spawns=10)  # fewer than were spawned before it began
    assert sorted(ran) == list(range(50))


async def test_spawn_is_refused_once_stop_has_begun(engine: Engine) -> None:
    """A task cancelled by ``stop()`` must not leave a successor that outlives the database."""
    ran: list[str] = []
    refused: list[Any] = []

    async def successor() -> None:
        ran.append("successor")

    async def dying() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            coro = successor()
            refused.append(coro)
            engine.spawn(coro, "successor")
            raise

    engine.spawn(dying(), "dying")
    await asyncio.sleep(0)
    await engine.stop()

    assert not ran
    assert not engine._background
    assert inspect.getcoroutinestate(refused[0]) == inspect.CORO_CLOSED
