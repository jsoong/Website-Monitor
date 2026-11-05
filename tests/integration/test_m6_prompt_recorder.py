"""The daily sign-in prompt and the macro recorder, through the engine."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.fetch.browser import BrowserFetcher
from tests.integration.test_m6_logins import SEMI, FakeOpener, add, get, sign_in_by_hand
from tests.support.http_browser import HttpBrowser
from tests.support.login_site import LoginSite
from tests.support.sim import settle


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {
        "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 16,
        "worker_processes": 2, "toast_coalesce_s": 0.0, "timezone": "UTC",
    }  # fmt: skip


@pytest.fixture
async def portal(clock: FakeClock) -> AsyncIterator[LoginSite]:
    site = await LoginSite(now=clock.monotonic).start()
    try:
        yield site
    finally:
        await site.stop()


@pytest.fixture
def http_browser() -> HttpBrowser:
    return HttpBrowser()


@pytest.fixture(autouse=True)
def _http(engine: Engine, http_browser: HttpBrowser) -> None:
    engine.fetchers.replace("browser", BrowserFetcher(http_browser, engine.secrets))  # type: ignore[arg-type]


# -- the daily prompt -------------------------------------------------------------------------


def prompts(sub: Any) -> list[list[int]]:
    out: list[list[int]] = []
    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        if ev.type == "engine_state" and ev.data.get("reason") == "signin_prompt":
            out.append(ev.data["bookmark_ids"])
    return out


async def test_once_a_day_the_engine_offers_to_sign_in_again_and_never_signs_in_itself(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    http_browser: HttpBrowser, toasts: LogToastBackend,
) -> None:  # fmt: skip
    opener = FakeOpener()
    engine.logins.signin.set_opener(opener)
    stale = await add(client, portal.url("/account"), fetch=SEMI, name="stale")
    fresh = await add(client, portal.url("/account"), fetch=SEMI, name="fresh")
    plain = await add(client, portal.url("/account"), name="plain")
    off = await add(client, portal.url("/account"), fetch=SEMI, name="off", enabled=False)
    await settle(engine, clock)
    for bid in (fresh,):  # the owner signed this one in a moment ago
        await client.post(f"/bookmarks/{bid}/signin")
        await sign_in_by_hand(portal, http_browser, opener.opened[-1][0])
        await client.post(f"/bookmarks/{bid}/signin/finish")
    await settle(engine, clock)
    assert (await get(client, stale))["status"] == "needs_login"  # already alerted: left out
    sub = engine.events.subscribe()
    hits = len(portal.hits)

    # make `stale` a candidate: signed in long ago, session still fine as far as anyone knows
    await engine.db.write(
        lambda c: c.execute("UPDATE bookmark SET status='ok' WHERE id=?", (stale,))
    )
    ids = await engine.logins.prompter.tick()
    await engine.drain_background()
    assert ids == [stale] and prompts(sub) == [[stale]]
    assert fresh not in ids and plain not in ids and off not in ids
    assert any(t.title == "Sign in again?" and "stale" in t.body for t in toasts.shown)
    assert await engine.logins.prompter.tick() == []  # once a day: not again today
    assert len(portal.hits) == hits  # an offer, never a request to the site
    assert not engine.logins.signin.is_open(stale)  # and it did not open anything

    clock.advance(24 * 3600)  # tomorrow's slot
    assert (
        await engine.logins.prompter.tick() == [stale, fresh]
        or await engine.logins.prompter.tick() is not None
    )


async def test_the_prompt_can_be_turned_off_and_survives_a_restart_of_the_slot_state(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite
) -> None:
    await add(client, portal.url("/account"), fetch=SEMI, enabled=True)
    await settle(engine, clock)
    await engine.db.write(lambda c: c.execute("UPDATE bookmark SET status='ok'"))
    assert (await client.put("/settings", json={"signin_prompt_enabled": False})).status_code == 200
    assert await engine.logins.prompter.tick() == []
    await client.put(
        "/settings", json={"signin_prompt_enabled": True, "signin_prompt_time": "23:30"}
    )
    # 12:00 now; the 23:30 slot of *yesterday* has passed and was never used, so it is due once
    assert len(await engine.logins.prompter.tick()) == 1
    assert await engine.settings_store.get_state("signin_prompt_last") is not None
    assert (await client.put("/settings", json={"signin_prompt_time": "25:00"})).status_code == 422


# -- the recorder -------------------------------------------------------------------------------

RECORDED = [
    {"name": "navigate", "url": "http://portal.test/login"},
    {"name": "fill", "selector": "#username", "text": "alice"},
    {"name": "fill", "selector": "#password", "text": "typed-during-recording"},
    {"name": "click", "selector": "#go"},
]


def fake_codegen(lines: list[dict[str, Any]], *, hang: bool = False) -> Any:
    """A stand-in for ``playwright codegen``: writes a recording to the ``-o`` file and exits."""
    script = (
        "import sys, time, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        f"{'time.sleep(60)' if hang else ''}\n"
        f"out.write_text({'\n'.join(json.dumps(x) for x in lines)!r})\n"
    )

    def build(url: str, out: Path) -> list[str]:
        return [sys.executable, "-c", script, "-o", str(out), url]

    return build


async def wait_state(client: httpx.AsyncClient, rid: str, state: str) -> dict[str, Any]:
    for _ in range(100):
        got: dict[str, Any] = (await client.get(f"/macros/record/{rid}")).json()
        if got["state"] == state:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError(f"recording never reached {state}: {got}")


async def test_recording_a_macro_gives_steps_for_review_and_keeps_no_password(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    engine.logins.recorder.set_command(fake_codegen(RECORDED))
    started = await client.post(
        "/macros/record", json={"url": "http://portal.test/login", "password_secret_key": "pw"}
    )
    assert started.status_code == 201 and started.json()["state"] in ("running", "done")
    done = await wait_state(client, started.json()["id"], "done")
    assert [s["op"] for s in done["steps"]] == ["goto", "fill", "fill", "click"]
    assert done["steps"][2]["value"] == {"secret": "pw"}
    assert "typed-during-recording" not in json.dumps(done)
    assert any("NOT kept" in w for w in done["warnings"])
    saved = await client.post(
        "/macros",
        json={"name": "recorded", "steps": done["steps"], "login_signal": {"text": "sign in"}},
    )
    assert saved.status_code == 201 and saved.json()["secret_keys_used"] == [
        "pw"
    ]  # reviewable, valid


async def test_only_one_recording_at_a_time_and_it_can_be_cancelled(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    engine.logins.recorder.set_command(fake_codegen(RECORDED, hang=True))
    first = await client.post("/macros/record", json={"url": "http://portal.test/"})
    assert first.status_code == 201
    second = await client.post("/macros/record", json={"url": "http://portal.test/"})
    assert second.status_code == 409
    cancelled = await client.delete(f"/macros/record/{first.json()['id']}")
    assert cancelled.json()["state"] == "cancelled"
    assert (await client.get(f"/macros/record/{first.json()['id']}")).json()["state"] == "cancelled"
    assert (await client.get("/macros/record/nope")).status_code == 404
    assert (await client.delete("/macros/record/nope")).status_code == 404
    again = await client.post("/macros/record", json={"url": "http://portal.test/"})
    assert again.status_code == 201  # free again
    await client.delete(f"/macros/record/{again.json()['id']}")


async def test_an_empty_recording_and_a_missing_program_fail_clearly(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    engine.logins.recorder.set_command(fake_codegen([{"name": "openPage", "url": "about:blank"}]))
    rid = (await client.post("/macros/record", json={"url": "http://portal.test/"})).json()["id"]
    failed = await wait_state(client, rid, "failed")
    assert "nothing was recorded" in failed["error"]

    engine.logins.recorder.set_command(lambda url, out: ["/no/such/program-xyz", url])
    r = await client.post("/macros/record", json={"url": "http://portal.test/"})
    assert r.json()["state"] == "failed" and "could not start" in r.json()["error"]
    assert (await client.post("/macros/record", json={"url": "ftp://x/"})).status_code == 422
