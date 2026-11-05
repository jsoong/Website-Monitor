"""``exceeds``: the over-limit test the Capacity page and the scheduler's stretch share."""

from __future__ import annotations

from pagewatch.engine.capacity import exceeds


def test_a_host_asked_for_exactly_what_it_can_serve_is_not_over_its_limit() -> None:
    per_s = 0.0
    for _ in range(60):  # as capacity.tally adds them up (sum() would compensate and hide it)
        per_s += 1 / 60
    demand = per_s * 60  # 60 bookmarks asking once a minute each
    assert demand != 60.0  # the rounding noise this guards against...
    assert not exceeds(demand, 60.0)  # ...must not read as "over the limit"


def test_a_host_asked_for_more_is_over_its_limit() -> None:
    assert exceeds(60.1, 60.0) and exceeds(31.0, 30.0) and exceeds(120.0, 60.0)
