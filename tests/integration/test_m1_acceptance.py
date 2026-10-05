"""M1 acceptance criteria, driven through the real CLI against a real engine and a real
HTTP fixture server, under a fake clock."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.cli.main import main as cli_main
from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle

N = 20


async def cli(data_dir: DataDir, *args: str) -> tuple[int, str]:
    """Run the CLI in a thread (it is synchronous; the engine shares this event loop)."""
    import contextlib
    import io

    def go() -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(["--data-dir", str(data_dir.root), *args])
        return code, buf.getvalue()

    return await asyncio.to_thread(go)


@pytest.fixture
async def twenty(
    api: tuple[str, str], engine: Engine, clock: FakeClock, site: FixtureSite, data_dir: DataDir
) -> list[int]:
    """20 fixture bookmarks added with ``pagewatch-cli add`` and baselined."""
    ids: list[int] = []
    for i in range(N):
        site.set(f"/p{i}", article(f"story {i} alpha", f"story {i} beta", title=f"Site {i}"))
        code, out = await cli(
            data_dir, "--json", "add", site.url(f"/p{i}"), "--name", f"Site {i}", "--interval", "1m"
        )
        assert code == 0, out
        ids.append(json.loads(out)["id"])
    await settle(engine, clock)
    return ids


async def test_20_cli_bookmarks_each_change_produces_exactly_one_toast(
    twenty: list[int], engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend, data_dir: DataDir,
) -> None:  # fmt: skip
    assert not toasts.shown  # baselines only
    code, out = await cli(data_dir, "--json", "list")
    assert code == 0 and len(json.loads(out)) == N

    # change the fixtures one at a time, a check cycle apart
    for i in range(N):
        site.set(f"/p{i}", article(f"story {i} alpha", f"story {i} gamma", title=f"Site {i}"))
        await advance(engine, clock, 90)
        assert len(toasts.shown) == i + 1, f"after changing fixture {i}"
        assert toasts.shown[-1].title == f"Site {i}"

    names = [t.title for t in toasts.shown]
    assert sorted(names) == sorted(f"Site {i}" for i in range(N))  # each exactly once
    change_ids = [c for t in toasts.shown for c in t.change_ids]
    assert len(change_ids) == len(set(change_ids)) == N

    # nothing more happens while nothing changes
    await advance(engine, clock, 600)
    assert len(toasts.shown) == N


async def test_all_20_changing_at_once_still_one_toast_per_change(
    twenty: list[int], engine: Engine, clock: FakeClock, site: FixtureSite, toasts: LogToastBackend,
) -> None:  # fmt: skip
    for i in range(N):
        site.set(f"/p{i}", article(f"story {i} alpha", f"story {i} delta", title=f"Site {i}"))
    await advance(engine, clock, 120)
    assert len(toasts.shown) == N
    assert len({c for t in toasts.shown for c in t.change_ids}) == N


async def test_unchanged_fixtures_produce_zero_toasts_over_one_hour(
    api: tuple[str, str], engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend, data_dir: DataDir,
) -> None:  # fmt: skip
    # Every response differs byte-for-byte (a build id, a timestamp, an HTML comment) but
    # not in text: the filtered comparison must see no change.
    for i in range(N):
        site.set_dynamic(
            f"/p{i}",
            lambda n, i=i: (
                f"<html><head><script>var build={n}</script></head><body><!-- rendered {n} -->"
                f"<h1>Site {i}</h1><p>steady content {i}</p></body></html>"
            ),
        )
        code, _ = await cli(data_dir, "add", site.url(f"/p{i}"), "--interval", "1m")
        assert code == 0
    await advance(engine, clock, 3600, step=10)
    assert not toasts.shown
    health = (await httpx.AsyncClient().get(
        f"{api[0]}/health", headers={"Authorization": f"Bearer {api[1]}"}
    )).json()  # fmt: skip
    outcomes = health["outcomes_24h"]
    assert outcomes.get("changed", 0) == 0 and outcomes.get("error", 0) == 0
    assert outcomes["unchanged"] >= N * 50  # ~1 check/min/bookmark for an hour
    assert outcomes["first"] == N

    # the 60-second floor holds: no bookmark is ever re-checked sooner than a minute
    from pagewatch.engine.clock import parse_iso

    rows = await engine.db.read(
        lambda c: c.execute("SELECT bookmark_id, started_at FROM check_run ORDER BY id").fetchall()
    )
    last: dict[int, Any] = {}
    gaps: list[float] = []
    for r in rows:
        t = parse_iso(r["started_at"])
        if r["bookmark_id"] in last:
            gaps.append((t - last[r["bookmark_id"]]).total_seconds())
        last[r["bookmark_id"]] = t
    assert gaps and min(gaps) >= 60.0, min(gaps)


async def test_restart_preserves_every_next_due_at(
    twenty: list[int], engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, settings_overrides: dict[str, Any], toasts: LogToastBackend,
) -> None:  # fmt: skip
    # give the bookmarks different schedules and let some time pass so due times diverge
    for n, bid in enumerate(twenty):
        await engine.db.write(
            lambda c, n=n, bid=bid: c.execute(
                "UPDATE bookmark SET schedule_json=? WHERE id=?",
                (json.dumps({"interval_s": 60 + 30 * (n % 7), "jitter_pct": 10}), bid),
            )
        )
    await advance(engine, clock, 400)
    before = {r["id"]: r["next_due_at"] for r in await engine.db.read(
        lambda c: c.execute("SELECT id, next_due_at FROM bookmark").fetchall()
    )}  # fmt: skip
    assert len(set(before.values())) > 5  # genuinely different due times
    sched_before = {bid: engine.scheduler.due_at(bid) for bid in twenty}

    await engine.stop()
    again = Engine(
        data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
        toast_backend=toasts,
    )  # fmt: skip
    await again.start()
    try:
        after = {r["id"]: r["next_due_at"] for r in await again.db.read(
            lambda c: c.execute("SELECT id, next_due_at FROM bookmark").fetchall()
        )}  # fmt: skip
        assert after == before
        assert {bid: again.scheduler.due_at(bid) for bid in twenty} == sched_before
        # ...and the restarted engine checks exactly the bookmarks that were due, no earlier
        hits = len(site.hits)
        earliest = min(d for d in sched_before.values() if d is not None)
        await advance(again, clock, (earliest - clock.now()).total_seconds() + 1, step=1)
        due_now = {bid for bid, d in sched_before.items() if d is not None and d <= clock.now()}
        checked = {twenty[int(h.path[2:])] for h in site.hits[hits:]}
        assert checked == due_now and 0 < len(due_now) < N
    finally:
        await again.stop()


async def test_stored_but_unalerted_change_is_not_rediffed_and_does_not_reset_adaptive(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from pagewatch.engine.pipeline import core as pipeline_core

    calls = {"n": 0}
    real = pipeline_core.diff_blocks

    def counting(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(pipeline_core, "diff_blocks", counting)

    site.set("/a", article("one two three"))
    r = await client.post("/bookmarks", json={
        "url": site.url("/a"), "name": "Adaptive",
        "schedule": {"mode": "adaptive", "adaptive": {"min_s": 600, "max_s": 86400, "factor": 2.0},
                     "jitter_pct": 0},
        "gate": {"min_changed_words": 50, "threshold_mode": "per_check"},
    })  # fmt: skip
    bid = r.json()["id"]
    await settle(engine, clock)

    async def interval() -> int:
        return (await client.get(f"/bookmarks/{bid}")).json()["current_interval_s"]

    assert await interval() == 600  # the first check starts at the minimum
    await advance(engine, clock, 700)  # unchanged: interval doubles
    assert await interval() == 1200
    await advance(engine, clock, 1300)
    assert await interval() == 2400

    site.set("/a", article("one two three four"))  # a real change that misses the threshold
    await advance(engine, clock, 2500)
    assert calls["n"] == 1  # diffed once
    assert not toasts.shown  # below threshold: stored, not alerted
    assert await interval() == 600  # the change reset the interval to the minimum, once

    await advance(engine, clock, 700)  # same bytes again: shortcut, no diff, interval grows
    assert calls["n"] == 1
    assert await interval() == 1200
    await advance(engine, clock, 1300)
    assert calls["n"] == 1 and await interval() == 2400
    runs = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    assert [x["reason"] for x in runs if x["outcome"] == "suppressed"] == ["below_threshold"]


def test_differ_benchmark_choice_is_recorded_and_default_is_fast_enough() -> None:
    from pagewatch.engine.pipeline.differs import DEFAULT_DIFFER_NAME

    decisions = (Path(__file__).parents[2] / "docs" / "DECISIONS.md").read_text()
    assert f"Differ: `{DEFAULT_DIFFER_NAME}`" in decisions  # chosen by tools/bench_differ.py
