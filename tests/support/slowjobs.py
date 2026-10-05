"""Jobs for the worker-timeout tests. A module of its own so the spawned worker imports little."""

from __future__ import annotations

import time


def spin(seconds: float) -> int:
    """Hold a worker busy (in a loop, as a catastrophic regex would) for ``seconds``."""
    end = time.monotonic() + seconds
    n = 0
    while time.monotonic() < end:
        n += 1
    return n


def quick(value: int) -> int:
    return value * 2
