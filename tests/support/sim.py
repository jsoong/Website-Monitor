"""Drive an engine under a FakeClock."""

from __future__ import annotations

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine


async def settle(engine: Engine, clock: FakeClock) -> None:
    """Let everything that can run right now finish (checks, then action jobs)."""
    await clock.settle()
    await engine.scheduler.wait_idle()
    await engine.actions.wait_idle()


async def advance(engine: Engine, clock: FakeClock, seconds: float, step: float = 30.0) -> None:
    """Move time forward in ``step`` increments, settling the engine after each."""
    remaining = seconds
    while remaining > 0:
        dt = min(step, remaining)
        clock.advance(dt)
        remaining -= dt
        await settle(engine, clock)
