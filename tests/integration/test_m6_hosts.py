"""Per-host overrides (spec: Politeness and network; Scheduler -> Capacity).

Concurrency and spacing overrides were already honoured by the host gate; this milestone adds
key normalisation (``Example.COM:8080`` and ``https://example.com/`` are one host) and the
scheduler stretching an over-limit host's intervals evenly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
import pytest

from pagewatch.engine.clock import FakeClock, parse_iso
from pagewatch.engine.core import Engine
from pagewatch.models import HostOverride, Settings, normalize_host
from tests.support.fakes import ScriptedFetcher, ok
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def get(client: httpx.AsyncClient, bid: int) -> dict[str, Any]:
    out: dict[str, Any] = (await client.get(f"/bookmarks/{bid}")).json()
    return out


def gap_seconds(b: dict[str, Any]) -> float:
    due: datetime = parse_iso(b["next_due_at"])
    checked: datetime = parse_iso(b["last_checked_at"])
    return (due - checked).total_seconds()


# -- keys ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,host",
    [
        ("Example.COM", "example.com"),
        ("https://Example.com:8443/x?y=1", "example.com"),
        ("example.com:80", "example.com"),
        ("user@Host.org.", "host.org"),
        ("[::1]:8080", "::1"),
        ("127.0.0.1", "127.0.0.1"),
        ("  spaced.example  ", "spaced.example"),
    ],
)
def test_host_keys_are_normalised(text: str, host: str) -> None:
    assert normalize_host(text) == host


@pytest.mark.parametrize("bad", ["", "   ", "a b.example", "back\\slash", "http://"])
def test_nonsense_host_keys_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_host(bad)
    with pytest.raises(ValueError):
        Settings(host_overrides={bad: HostOverride(concurrency=1)})


async def test_settings_accept_any_spelling_and_remove_by_any_spelling(
    client: httpx.AsyncClient,
) -> None:
    r = await client.put(
        "/settings", json={"host_overrides": {"Example.COM:8080": {"min_gap_s": 5}}}
    )
    assert r.status_code == 200 and list(r.json()["host_overrides"]) == ["example.com"]
    r = await client.put(
        "/settings", json={"host_overrides": {"https://example.com/": {"concurrency": 1}}}
    )
    assert r.json()["host_overrides"] == {"example.com": {"concurrency": 1, "min_gap_s": 5.0}}
    r = await client.put("/settings", json={"host_overrides": {"EXAMPLE.com.": None}})
    assert r.json()["host_overrides"] == {}
    bad = await client.put("/settings", json={"host_overrides": {"no way": {"concurrency": 1}}})
    assert bad.status_code == 422 and "host_overrides" in bad.text


# -- overrides drive the scheduler's dispatch -----------------------------------------------


async def test_a_spacing_override_delays_the_second_check_on_that_host_only(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    starts: list[tuple[str, float]] = []

    def respond(req: Any, n: int) -> Any:
        starts.append((req.url, clock.monotonic()))
        return ok(req, article("x"))

    engine.fetchers.replace("static", ScriptedFetcher(respond))  # type: ignore[arg-type]
    port = site.port
    r = await client.put(
        "/settings", json={"host_overrides": {f"HTTP://127.0.0.1:{port}/": {"min_gap_s": 30}}}
    )
    assert r.status_code == 200
    for path in ("/a", "/b"):
        site.set(path, article(path))
    a = await add(client, site.url("/a"), schedule={"mode": "manual"})
    b = await add(client, site.url("/b"), schedule={"mode": "manual"})
    elsewhere = await add(client, "http://other.test/c", schedule={"mode": "manual"})
    await settle(engine, clock)
    await advance(engine, clock, 61, step=1)  # let the spacing after their first checks pass
    starts.clear()
    t0 = clock.monotonic()
    await client.post("/check", json={"ids": [a, b, elsewhere]})
    await settle(engine, clock)
    started = {u: t for u, t in starts}
    assert started["http://other.test/c"] == t0  # another host is not held back
    first_two = sorted(t for u, t in starts if "127.0.0.1" in u)
    assert first_two == [t0]  # one went at once; the other waits out the 30 s spacing
    await advance(engine, clock, 29, step=1)
    assert len([1 for u, _ in starts if "127.0.0.1" in u]) == 1
    await advance(engine, clock, 2, step=1)
    both = sorted(t for u, t in starts if "127.0.0.1" in u)
    assert len(both) == 2 and both[1] - both[0] >= 30


async def test_a_concurrency_override_limits_parallel_checks_to_that_host(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    running = {"now": 0, "max": 0}
    release = asyncio.Event()

    class Slow:
        async def fetch(self, req: Any) -> Any:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            await release.wait()
            running["now"] -= 1
            return ok(req, article("x"))

        async def aclose(self) -> None:
            return None

    engine.fetchers.replace("static", Slow())  # type: ignore[arg-type]
    await client.put(
        "/settings", json={"host_overrides": {f"127.0.0.1:{site.port}": {"concurrency": 1}}}
    )
    ids = [await add(client, site.url(f"/p{i}"), schedule={"mode": "manual"}) for i in range(4)]
    await client.post("/check", json={"ids": ids})
    for _ in range(20):
        await clock.settle()
        await asyncio.sleep(0.01)
    assert running["max"] == 1  # the default of 16 would have started all four
    release.set()
    await settle(engine, clock)
    assert running["now"] == 0


# -- stretching ---------------------------------------------------------------------------


async def five_bookmarks(client: httpx.AsyncClient, site: FixtureSite, **kw: Any) -> list[int]:
    ids = []
    for i in range(5):
        site.set(f"/p{i}", article(f"page {i}"))
        ids.append(await add(client, site.url(f"/p{i}"), **kw))
    return ids


async def test_an_over_limit_host_has_all_its_intervals_stretched_by_the_same_factor(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    ids = await five_bookmarks(client, site)  # five bookmarks, each once a minute: 5 a minute
    await settle(engine, clock)
    await client.put(
        "/settings", json={"host_overrides": {"127.0.0.1": {"min_gap_s": 30}}}
    )  # ceiling: 2 a minute
    await engine.drain_background()
    assert engine.host_load.factor("127.0.0.1") == pytest.approx(2.5)
    assert engine.host_load.factor("other.example") == 1.0

    await advance(engine, clock, 400, step=10)  # every bookmark checks again with the new factor
    waits = [gap_seconds(await get(client, b)) for b in ids]
    assert waits == [pytest.approx(150.0)] * 5  # 60 s asked, 2.5 x 60 s given; all alike
    cap = (await client.get("/capacity")).json()
    host = next(h for h in cap["hosts"] if h["host"] == "127.0.0.1")
    assert host["over_limit"] is True and host["stretch"] == pytest.approx(2.5)
    # the stored adaptive state is not stretched, so the factor cannot feed back into itself
    assert (await get(client, ids[0]))["current_interval_s"] in (None, 60)


async def test_stretching_stops_when_the_limit_is_raised_or_the_option_is_off(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    ids = await five_bookmarks(client, site)
    await settle(engine, clock)
    await client.put("/settings", json={"host_overrides": {"127.0.0.1": {"min_gap_s": 30}}})
    await engine.drain_background()
    assert engine.host_load.factor("127.0.0.1") > 1.0
    await client.put("/settings", json={"stretch_hosts": False})
    assert engine.host_load.factor("127.0.0.1") == 1.0 and engine.host_load.stretched == {}
    await client.put("/settings", json={"stretch_hosts": True})
    await client.put("/settings", json={"host_overrides": {"127.0.0.1": {"min_gap_s": 5}}})
    await engine.drain_background()
    assert engine.host_load.factor("127.0.0.1") == 1.0  # 12 a minute of room for 5
    await advance(engine, clock, 200, step=10)
    assert [gap_seconds(await get(client, b)) for b in ids] == [pytest.approx(60.0)] * 5


async def test_fixed_times_and_manual_bookmarks_are_never_stretched(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    await five_bookmarks(client, site)
    await client.put("/settings", json={"host_overrides": {"127.0.0.1": {"min_gap_s": 30}}})
    await engine.drain_background()
    assert engine.host_load.factor("127.0.0.1") > 1.0
    site.set("/t", article("t"))
    times = await add(
        client, site.url("/t"), schedule={"mode": "times", "times": ["06:00"], "jitter_pct": 0}
    )
    manual = await add(client, site.url("/t"), schedule={"mode": "manual"})
    await advance(engine, clock, 200, step=10)  # past the host's spacing: their first checks ran
    t = await get(client, times)
    assert parse_iso(t["next_due_at"]).strftime("%H:%M") == "06:00"
    assert (await get(client, manual))["next_due_at"] is None


async def test_demand_ignores_bookmarks_that_are_waiting_for_a_login(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    """A paused bookmark asks for nothing, so it must not push its host over the limit."""
    ids = await five_bookmarks(client, site)
    await settle(engine, clock)
    await client.put("/settings", json={"host_overrides": {"127.0.0.1": {"min_gap_s": 30}}})
    await engine.drain_background()
    assert engine.host_load.factor("127.0.0.1") == pytest.approx(2.5)
    await engine.db.write(
        lambda c: c.execute("UPDATE bookmark SET status='needs_login' WHERE id IN (?,?,?)", ids[:3])
    )
    await engine.host_load.refresh()
    assert (
        engine.host_load.factor("127.0.0.1") == 1.0
    )  # two left: 2 a minute is exactly the ceiling
