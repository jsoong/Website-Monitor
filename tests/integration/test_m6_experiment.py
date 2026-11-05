"""The portal experiment: the instrument, run against the fixture portal on a fake clock.

This proves the *tool* does what the spec's protocol asks. It is not the experiment: the real
one needs the real portal, the owner's account and 48 hours (see docs/DECISIONS_M6_LOGINS.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.fetch.browser import BrowserFetcher
from pagewatch.engine.logins.experiment import detect_challenge
from pagewatch.engine.secrets import MemorySecrets
from tests.integration.test_m6_logins import SEMI, FakeOpener, add, get, macro, sign_in_by_hand
from tests.support.http_browser import HttpBrowser
from tests.support.login_site import LoginSite
from tests.support.sim import settle

HOUR = 3600.0


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


@pytest.fixture
def secret_store() -> MemorySecrets:
    return MemorySecrets({"portal-pw": "s3cret-pw"})


@pytest.fixture(autouse=True)
def _http(engine: Engine, http_browser: HttpBrowser) -> None:
    engine.fetchers.replace("browser", BrowserFetcher(http_browser, engine.secrets))  # type: ignore[arg-type]


async def run_for(engine: Engine, clock: FakeClock, seconds: float, step: float = 300.0) -> None:
    left = seconds
    while left > 0:
        dt = min(step, left)
        clock.advance(dt)
        left -= dt
        await engine.logins.experiments.tick()
        await settle(engine, clock)


async def setup(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    http_browser: HttpBrowser, **kw: Any,
) -> tuple[int, str]:  # fmt: skip
    opener = FakeOpener()
    engine.logins.signin.set_opener(opener)
    bid = await add(
        client, portal.url("/account"), macro_id=await macro(client, portal), fetch=SEMI,
        schedule={"mode": "manual"},
    )  # fmt: skip
    await settle(engine, clock)
    r = await client.post("/experiments", json={"bookmark_id": bid, **kw})
    assert r.status_code == 201, r.text
    exp_id: str = r.json()["id"]
    assert r.json()["state"] == "waiting_for_signin"
    await client.post(f"/bookmarks/{bid}/signin")
    await sign_in_by_hand(portal, http_browser, opener.opened[-1][0])
    await client.post(f"/bookmarks/{bid}/signin/finish")  # time zero
    await settle(engine, clock)
    return bid, exp_id


async def test_the_spec_protocol_probes_at_the_set_minutes_then_logs_in_spaced_apart(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    http_browser: HttpBrowser,
) -> None:  # fmt: skip
    bid, exp_id = await setup(client, engine, clock, portal, http_browser, duration_h=48)
    exp = (await client.get(f"/experiments/{exp_id}")).json()
    assert (
        exp["state"] == "running"
        and exp["signin_at"]
        and exp["probe_minutes"] == [10, 20, 30, 60, 120]
    )
    assert [o["kind"] for o in exp["observations"]] == ["signin"]
    portal.idle_timeout_s = 45 * 60  # the portal ends a session idle for 45 minutes

    await run_for(engine, clock, 10 * 60 + 60)
    probes = lambda: [  # noqa: E731
        o
        for o in (engine.logins.experiments.get(exp_id)).observations
        if o.kind == "probe"  # type: ignore[union-attr]
    ]
    assert [p.minutes_since_signin for p in probes()] == [10.0]
    await run_for(engine, clock, 2 * HOUR)
    got = [(p.minutes_since_signin, p.outcome) for p in probes()]
    # Each probe is activity. The gaps are 10, 10, 10, 30 and 60 minutes: a 45-minute idle
    # timeout survives all but the last, which is exactly what the report must be able to say.
    assert got == [
        (10.0, "session_alive"), (20.0, "session_alive"), (30.0, "session_alive"),
        (60.0, "session_alive"), (120.0, "session_expired"),
    ]  # fmt: skip
    assert portal.logins == ["alice"]  # probes never logged in

    await run_for(engine, clock, 48 * HOUR)
    exp = (await client.get(f"/experiments/{exp_id}")).json()
    logins = [o for o in exp["observations"] if o["kind"] == "login"]
    assert exp["state"] == "done" and exp["stopped_because"] is None
    assert len(logins) == 6 and all(o["outcome"] == "logged_in" for o in logins)  # 3 a day, 2 days
    gaps = [
        b["minutes_since_signin"] - a["minutes_since_signin"]
        for a, b in zip(logins, logins[1:], strict=False)
    ]
    assert all(abs(g - 8 * 60) < 10 for g in gaps)  # evenly spaced, every 8 hours
    assert portal.logins == ["alice"] * 7  # the hand sign-in plus six password logins
    assert (await get(client, bid))["status"] in ("ok", "needs_login")  # never paged or errored


async def test_a_challenge_stops_the_attempts_and_the_page_is_saved(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    http_browser: HttpBrowser,
) -> None:  # fmt: skip
    _bid, exp_id = await setup(client, engine, clock, portal, http_browser, duration_h=24)
    await run_for(engine, clock, 2 * HOUR + 300)  # the probes are done; no login attempt yet
    portal.challenge = "otp"  # from now on the portal asks for a one-time code
    await run_for(engine, clock, 24 * HOUR)
    exp = (await client.get(f"/experiments/{exp_id}")).json()
    logins = [o for o in exp["observations"] if o["kind"] == "login"]
    assert exp["state"] == "stopped" and len(logins) == 1  # none after a failure
    assert logins[0]["outcome"] == "challenge" and logins[0]["challenge"] == "otp"
    assert "challenge" in exp["stopped_because"]
    saved = engine.logins.experiments.folder / logins[0]["snapshot"]
    assert saved.exists() and b"One-time code" in saved.read_bytes()
    assert portal.failed_logins == 0 and len(portal.logins) == 1  # nothing hammered the portal


async def test_the_report_reads_like_findings_and_gates_nothing(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    http_browser: HttpBrowser,
) -> None:  # fmt: skip
    _bid, exp_id = await setup(client, engine, clock, portal, http_browser, duration_h=48)
    early = (await client.get(f"/experiments/{exp_id}/report")).json()
    assert early["stored_password_verdict"] == "not_enough_data"
    assert "No probe has run yet" in early["idle_timeout"]
    await run_for(engine, clock, 50 * HOUR)
    rep = (await client.get(f"/experiments/{exp_id}/report")).json()
    assert rep["stored_password_verdict"] == "looks_safe" and rep["logins_succeeded"] == 6
    assert "survived every probe" in rep["idle_timeout"]
    assert "| probe |" in rep["markdown"] and "terms of use" in rep["markdown"]
    # the experiment only reports: the global switch is still the owner's to turn on
    assert (await client.get("/settings")).json()["stored_password_logins"] is False


async def test_the_experiment_has_rules_about_what_it_will_start_on(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite
) -> None:
    mid = await macro(client, portal)
    plain = await add(client, portal.url("/account"), enabled=False)
    assert (await client.post("/experiments", json={"bookmark_id": plain})).status_code == 422
    nomacro = await add(client, portal.url("/account"), fetch=SEMI, schedule={"mode": "manual"})
    r = await client.post("/experiments", json={"bookmark_id": nomacro})
    assert r.status_code == 422 and "macro" in r.text
    busy = await add(client, portal.url("/account"), fetch=SEMI, macro_id=mid)  # scheduled checks
    r = await client.post("/experiments", json={"bookmark_id": busy})
    assert r.status_code == 422 and "manual" in r.text
    assert (await client.post("/experiments", json={"bookmark_id": 9999})).status_code == 404
    ok = await add(
        client, portal.url("/account"), fetch=SEMI, macro_id=mid, schedule={"mode": "manual"}
    )
    assert (await client.post("/experiments", json={"bookmark_id": ok})).status_code == 201
    r = await client.post("/experiments", json={"bookmark_id": ok})
    assert r.status_code == 422 and "already running" in r.text
    bad = {"bookmark_id": ok, "logins_per_day": 9}
    assert (await client.post("/experiments", json=bad)).status_code == 422
    bad = {"bookmark_id": ok, "probe_minutes": [30, 10]}
    assert (await client.post("/experiments", json=bad)).status_code == 422


async def test_an_experiment_can_be_cancelled_and_survives_a_restart(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    http_browser: HttpBrowser,
) -> None:  # fmt: skip
    _bid, exp_id = await setup(client, engine, clock, portal, http_browser)
    assert (await client.get("/experiments")).json()[0]["id"] == exp_id
    await engine.logins.experiments.stop()
    engine.logins.experiments._exps.clear()  # a fresh process knows nothing in memory
    await engine.logins.experiments.start()
    assert engine.logins.experiments.get(exp_id) is not None  # read back from its file
    gone = (await client.delete(f"/experiments/{exp_id}")).json()
    assert gone["state"] == "cancelled"
    await run_for(engine, clock, 3 * HOUR)
    assert [
        o["kind"] for o in (await client.get(f"/experiments/{exp_id}")).json()["observations"]
    ] == ["signin"]
    assert (await client.get("/experiments/nope")).status_code == 404
    assert (await client.delete("/experiments/nope")).status_code == 404
    assert (await client.get("/experiments/nope/report")).status_code == 404


# -- recognising a challenge --------------------------------------------------------------------


@pytest.mark.parametrize(
    "page,kind",
    [
        ('<form><input name="otp" id="otp"></form>', "otp"),
        ('<input autocomplete="one-time-code">', "otp"),
        ("<p>We sent a verification code to your phone. Enter the code below.</p>", "otp"),
        ("<p>Two-factor authentication is required</p>", "otp"),
        ('<div class="g-recaptcha" data-sitekey="x"></div>', "captcha"),
        ('<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>', "captcha"),
        ("<p>Please verify you are human</p>", "captcha"),
        ("<p>Unusual sign-in activity detected</p>", "captcha"),
        ('<div class="g-recaptcha"></div><input name="otp">', "captcha"),
        ("<h1>Your account</h1><p>Balance: 100</p>", None),
        ("<script>var captcha = 'otp';</script><p>Welcome back</p>", None),
        ("", None),
    ],
)
def test_challenge_pages_are_recognised(page: str, kind: str | None) -> None:
    assert detect_challenge(f"<html><body>{page}</body></html>" if page else page) == kind
