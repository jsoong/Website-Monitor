"""The portal experiment (spec: Portal strategy -> Portal experiment).

    "Over 48 hours on the owner's machine, sign in once by hand, then check at 10, 20, 30, 60 and
    120 minutes to measure the idle timeout. Try two to four spaced password logins a day to see
    whether a code or CAPTCHA appears, and record any challenge pages."

This module is the *instrument*. It runs that protocol against a bookmark and writes down what it
sees; it does not decide anything for the owner, and **running it requires the portal, the
owner's account and 48 hours, none of which a test environment has**. The findings are recorded
in DECISIONS.md by the person who ran it. Until they are, ``stored_password_logins`` stays off.

Protocol:

1. ``create`` starts an experiment on a semi-attended bookmark whose schedule is *manual* (any
   other check would refresh the session and spoil the measurement).
2. The owner signs in by hand (the normal "Sign in..." window). That moment is time zero.
3. **Probes** at the chosen minutes after time zero open the bookmark's page on the sign-in
   profile *without logging in* and record whether the session is still there.
4. **Login attempts** follow, spaced evenly (``logins_per_day`` a day) until the experiment ends.
   Each runs the bookmark's macro on a fresh, empty profile, so a real password login happens, and
   is checked for a one-time-code or CAPTCHA page. The first failure or challenge stops the
   attempts (spec: none after a failure) and the page is saved for the record.
5. ``report`` turns the log into the text to paste into DECISIONS.md.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from lxml import html as lxml_html

from pagewatch.engine.clock import iso, parse_iso
from pagewatch.engine.config import resolve
from pagewatch.engine.fetch.base import FetchRequest
from pagewatch.engine.fetch.login import LoginPlan, MacroPermit
from pagewatch.engine.fetch.select import BROWSER
from pagewatch.engine.logins.common import macro_def
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo
from pagewatch.models import (
    ChallengeKind,
    ExperimentIn,
    ExperimentOut,
    ExperimentReport,
    Observation,
    ScheduleMode,
)

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.experiment")

POLL_S = 30.0
LOGIN_START_MARGIN = timedelta(minutes=30)  # after the last probe, so a login cannot spoil it

_CAPTCHA_TEXT = re.compile(
    r"captcha|are you (a )?(human|robot)|verify (that )?you are (a )?human|i'm not a robot|"
    r"security check|unusual (sign.?in )?activity",
    re.IGNORECASE,
)
_OTP_TEXT = re.compile(
    r"one.?time (pass)?code|verification code|security code|authentication code|"
    r"enter the code|two.?factor|2fa|text message with a code|code (was )?sent to",
    re.IGNORECASE,
)
_CAPTCHA_CSS = (
    ".g-recaptcha, iframe[src*=recaptcha], iframe[src*=hcaptcha], .h-captcha, "
    "[data-sitekey], #captcha, .cf-turnstile, iframe[src*=turnstile]"
)
_LOWER = "translate({0},'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
_OTP_XPATH = (
    "//input[@autocomplete='one-time-code' or "
    + " or ".join(
        f"contains({_LOWER.format('@' + attr)},'{word}')"
        for attr in ("name", "id")
        for word in ("otp", "verification", "mfa")
    )
    + "]"
)


def detect_challenge(page_html: str | bytes) -> ChallengeKind | None:
    """Does this page ask for a one-time code or a CAPTCHA instead of letting the login through?
    ``captcha`` wins over ``otp`` (a CAPTCHA in front of a code form is the stronger signal)."""
    if not page_html:
        return None
    try:
        tree = lxml_html.fromstring(page_html)
    except Exception:
        return None
    try:
        if tree.cssselect(_CAPTCHA_CSS):
            return "captcha"
        if tree.xpath(_OTP_XPATH):
            return "otp"
    except Exception:
        pass
    for bad in tree.xpath("//script|//style|//noscript"):
        bad.drop_tree()
    text = re.sub(r"\s+", " ", tree.text_content())
    if _CAPTCHA_TEXT.search(text):
        return "captcha"
    if _OTP_TEXT.search(text):
        return "otp"
    return None


ObsOutcome = Literal["session_alive", "session_expired", "logged_in", "challenge", "failed"]
Verdict = Literal["looks_safe", "not_safe", "not_enough_data"]


class ExperimentError(ValueError):
    pass


class ExperimentService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._exps: dict[str, ExperimentOut] = {}
        self._task: asyncio.Task[None] | None = None
        self._busy = asyncio.Lock()

    @property
    def folder(self) -> Path:
        return self.e.data_dir.experiments_dir

    # -- persistence --------------------------------------------------------------------

    async def start(self) -> None:
        def load() -> list[ExperimentOut]:
            out: list[ExperimentOut] = []
            for path in sorted(self.folder.glob("*.json")):
                try:
                    out.append(ExperimentOut.model_validate_json(path.read_text("utf-8")))
                except (OSError, ValueError):
                    log.warning("experiment_unreadable", file=path.name)
            return out

        for exp in await asyncio.to_thread(load):
            self._exps[exp.id] = exp
        self._task = asyncio.create_task(self._loop(), name="experiments")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _save(self, exp: ExperimentOut) -> None:
        def write() -> None:
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self.folder / f"{exp.id}.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(exp.model_dump_json(indent=1), encoding="utf-8")
            os.replace(tmp, target)

        await asyncio.to_thread(write)

    # -- the API ------------------------------------------------------------------------

    def all(self) -> list[ExperimentOut]:
        return sorted(self._exps.values(), key=lambda x: x.started_at, reverse=True)

    def get(self, exp_id: str) -> ExperimentOut | None:
        return self._exps.get(exp_id)

    async def create(self, body: ExperimentIn) -> ExperimentOut:
        row = await self.e.db.read(lambda c: repo.bookmark_get(c, body.bookmark_id))
        if row is None:
            raise ExperimentError(f"bookmark {body.bookmark_id} not found")
        fetch = resolve(row, self.e.folders, self.e.settings)
        if not (fetch.fetch.login and fetch.fetch.login.semi_attended):
            raise ExperimentError(
                "the experiment starts from a hand sign-in: turn on semi-attended sign-in first"
            )
        if row["macro_id"] is None:
            raise ExperimentError("the login attempts need a macro on the bookmark")
        if row["enabled"] and fetch.schedule.mode is not ScheduleMode.MANUAL:
            raise ExperimentError(
                "set the bookmark's schedule to manual (or disable it) for the experiment: any "
                "other check would refresh the session and spoil the measurement"
            )
        if any(
            x.bookmark_id == body.bookmark_id and x.state in ("waiting_for_signin", "running")
            for x in self._exps.values()
        ):
            raise ExperimentError("an experiment is already running on this bookmark")
        exp = ExperimentOut(
            id=uuid.uuid4().hex[:10],
            bookmark_id=body.bookmark_id,
            state="waiting_for_signin",
            started_at=iso(self.e.clock.now()),
            probe_minutes=body.probe_minutes,
            logins_per_day=body.logins_per_day,
            duration_h=body.duration_h,
        )
        self._exps[exp.id] = exp
        await self._save(exp)
        return exp

    async def cancel(self, exp_id: str) -> ExperimentOut | None:
        exp = self._exps.get(exp_id)
        if exp is None:
            return None
        if exp.state in ("waiting_for_signin", "running"):
            exp.state = "cancelled"
            await self._save(exp)
        return exp

    async def note_signin(self, bookmark_id: int) -> None:
        """The owner has just signed in by hand: that is time zero of any waiting experiment."""
        now = self.e.clock.now()
        for exp in self._exps.values():
            if exp.bookmark_id == bookmark_id and exp.state == "waiting_for_signin":
                exp.state = "running"
                exp.signin_at = iso(now)
                exp.ends_at = iso(now + timedelta(hours=exp.duration_h))
                exp.observations.append(
                    Observation(at=iso(now), kind="signin", outcome=None, note="signed in by hand")
                )
                await self._save(exp)

    # -- the schedule -------------------------------------------------------------------

    def _login_times(self, exp: ExperimentOut) -> list[datetime]:
        assert exp.signin_at and exp.ends_at
        start = (
            parse_iso(exp.signin_at)
            + timedelta(minutes=max(exp.probe_minutes))
            + LOGIN_START_MARGIN
        )
        spacing = timedelta(hours=24 / exp.logins_per_day)
        end = parse_iso(exp.ends_at)
        out: list[datetime] = []
        t = start
        while t <= end:
            out.append(t)
            t += spacing
        return out

    async def _loop(self) -> None:
        while True:
            await self.e.clock.sleep(POLL_S)
            try:
                await self.tick()
            except Exception:
                log.exception("experiment_tick_failed")

    async def tick(self) -> int:
        """Do whatever is due, once. Returns how many observations were made."""
        done = 0
        async with self._busy:
            for exp in list(self._exps.values()):
                if exp.state != "running" or not exp.signin_at:
                    continue
                now = self.e.clock.now()
                zero = parse_iso(exp.signin_at)
                seen_probes = {
                    o.minutes_since_signin for o in exp.observations if o.kind == "probe"
                }
                for offset in exp.probe_minutes:
                    if offset not in seen_probes and now >= zero + timedelta(minutes=offset):
                        await self._probe(exp, offset)
                        done += 1
                        break  # one at a time: a slow browser must not stack observations
                else:
                    attempts = sum(1 for o in exp.observations if o.kind == "login")
                    times = self._login_times(exp)
                    if exp.state == "running" and attempts < len(times) and now >= times[attempts]:
                        await self._login_attempt(exp, attempts + 1)
                        done += 1
                    elif now >= parse_iso(exp.ends_at or exp.signin_at) and (
                        len(seen_probes) == len(exp.probe_minutes)
                    ):
                        exp.state = "done"
                        await self._save(exp)
        return done

    # -- observations -------------------------------------------------------------------

    async def _request(self, exp: ExperimentOut, plan_edit: str) -> tuple[FetchRequest, str]:
        row = await self.e.db.read(lambda c: repo.bookmark_get(c, exp.bookmark_id))
        if row is None:
            raise ExperimentError("the bookmark was deleted")
        resolved = resolve(row, self.e.folders, self.e.settings)
        plan = await self.e.logins.plan_for(row, resolved.fetch)
        if plan is None:
            raise ExperimentError("the bookmark has no sign-in to measure")
        if plan_edit == "probe":
            plan.macro, plan.permit_macro, plan.macro_finished = None, None, None  # never log in
        else:
            mrow = await self.e.db.read(lambda c: repo.macro_get(c, row["macro_id"]))
            if mrow is None:
                raise ExperimentError("the bookmark's macro was deleted")

            async def allow() -> MacroPermit:
                return MacroPermit(True)  # the owner started this: the usual gates do not apply

            async def nothing(ok: bool) -> None:
                return None

            fresh = self.e.data_dir.profiles_dir / f"experiment-{exp.bookmark_id}"
            await asyncio.to_thread(shutil.rmtree, fresh, True)  # a real login needs no session
            plan = LoginPlan(
                profile_dir=fresh, signal=plan.signal, macro=macro_def(mrow), permit_macro=allow,
                macro_finished=nothing, resolve_secret=plan.resolve_secret,
            )  # fmt: skip
        req = FetchRequest(
            url=row["url"], resolved=resolved, settings=self.e.settings, force=True,
            bookmark_id=exp.bookmark_id, login=plan,
        )  # fmt: skip
        return req, row["name"]

    async def _probe(self, exp: ExperimentOut, offset: int) -> None:
        assert exp.signin_at
        minutes = (self.e.clock.now() - parse_iso(exp.signin_at)).total_seconds() / 60.0
        outcome: ObsOutcome
        note: str | None
        try:
            req, _ = await self._request(exp, "probe")
            res = await self.e.fetchers.get(BROWSER).fetch(req)
            if res.login_required:
                outcome, note = "session_expired", res.login_reason
            elif res.error is not None:
                outcome, note = "failed", f"{res.error.reason}: {res.error.message}"[:200]
            else:
                outcome, note = "session_alive", None
        except ExperimentError as exc:
            outcome, note = "failed", str(exc)
        exp.observations.append(
            Observation(
                at=iso(self.e.clock.now()),
                kind="probe",
                minutes_since_signin=float(offset),
                outcome=outcome,
                note=note or f"{minutes:.0f} min after sign-in",
            )  # fmt: skip
        )
        await self._save(exp)

    async def _login_attempt(self, exp: ExperimentOut, number: int) -> None:
        assert exp.signin_at
        minutes = (self.e.clock.now() - parse_iso(exp.signin_at)).total_seconds() / 60.0
        snapshot: str | None = None
        challenge: ChallengeKind | None = None
        outcome: ObsOutcome
        note: str | None
        try:
            req, _ = await self._request(exp, "login")
            res = await self.e.fetchers.get(BROWSER).fetch(req)
            if res.error is not None:
                outcome, note = "failed", f"{res.error.reason}: {res.error.message}"[:200]
            elif res.login_required:
                challenge = detect_challenge(res.body)
                outcome = "challenge" if challenge else "failed"
                note = res.macro_error or res.login_reason
                snapshot = await self._snapshot(exp, number, res.body)
            else:
                outcome, note = "logged_in", None
        except ExperimentError as exc:
            outcome, note = "failed", str(exc)
        exp.observations.append(
            Observation(
                at=iso(self.e.clock.now()),
                kind="login",
                minutes_since_signin=round(minutes, 1),
                outcome=outcome,
                challenge=challenge,
                snapshot=snapshot,
                note=note,
            )  # fmt: skip
        )
        if outcome != "logged_in":  # spec: none after a failure
            exp.state = "stopped"
            exp.stopped_because = (
                f"login attempt {number} met a {challenge} challenge"
                if challenge
                else f"login attempt {number} failed"
            )
        await self._save(exp)

    async def _snapshot(self, exp: ExperimentOut, number: int, body: bytes) -> str | None:
        if not body:
            return None
        name = f"{exp.id}-login-{number}.html"

        def write() -> None:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / name).write_bytes(body)

        await asyncio.to_thread(write)
        return name

    # -- findings -----------------------------------------------------------------------

    def report(self, exp: ExperimentOut) -> ExperimentReport:
        probes = [o for o in exp.observations if o.kind == "probe"]
        logins = [o for o in exp.observations if o.kind == "login"]
        challenges = [o for o in logins if o.outcome == "challenge"]
        ok = [o for o in logins if o.outcome == "logged_in"]

        # idle timeout
        gone = next((o for o in probes if o.outcome == "session_expired"), None)
        previous = 0.0
        longest_gap = 0.0
        for o in probes:
            offset = o.minutes_since_signin or 0.0
            if o is gone:
                break
            longest_gap = max(longest_gap, offset - previous)
            previous = offset
        if not probes:
            idle = "No probe has run yet, so nothing is known about the idle timeout."
        elif gone is not None:
            at = gone.minutes_since_signin or 0.0
            idle = (
                f"The session was still there {previous:.0f} min after sign-in and gone at "
                f"{at:.0f} min. Either an idle timeout shorter than {at - previous:.0f} min or an "
                f"absolute timeout between {previous:.0f} and {at:.0f} min ended it (each probe "
                "may itself restart an idle timer, so the two cannot be told apart from this run)."
            )
        else:
            idle = (
                f"The session survived every probe up to {previous:.0f} min after sign-in; the "
                f"longest gap between probes was {longest_gap:.0f} min, so the idle timeout is "
                f"longer than that and the absolute timeout is longer than {previous:.0f} min."
            )

        verdict: Verdict
        if challenges or (logins and len(ok) < len(logins)):
            verdict = "not_safe"
        elif len(ok) >= 4:
            verdict = "looks_safe"
        else:
            verdict = "not_enough_data"
        words = {
            "looks_safe": f"{len(ok)} spaced password logins, none met a code or a CAPTCHA. "
            "Stored-password login looks usable for this portal.",
            "not_safe": "A login failed or met a challenge. Do not enable stored-password login "
            "for this portal.",
            "not_enough_data": f"Only {len(logins)} login attempt(s) so far; the spec asks for two "
            "to four a day over 48 hours.",
        }[verdict]

        lines = [
            f"### Portal experiment `{exp.id}` (bookmark {exp.bookmark_id})",
            "",
            f"* Started {exp.started_at}; signed in by hand at {exp.signin_at or 'not yet'}; "
            f"state: {exp.state}"
            + (f" ({exp.stopped_because})" if exp.stopped_because else "")
            + ".",
            f"* **Idle timeout:** {idle}",
            f"* **Password logins:** {len(ok)} of {len(logins)} succeeded; "
            f"{len(challenges)} met a challenge"
            + (
                f" ({', '.join(sorted({c.challenge or '?' for c in challenges}))})"
                if challenges
                else ""
            )
            + ".",
            f"* **Verdict:** {words}",
            "",
            "| When (UTC) | Kind | Min. after sign-in | Outcome | Challenge | Page saved | Note |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for o in exp.observations:
            lines.append(
                f"| {o.at} | {o.kind} | "
                f"{'' if o.minutes_since_signin is None else f'{o.minutes_since_signin:g}'} | "
                f"{o.outcome or ''} | {o.challenge or ''} | {o.snapshot or ''} | {o.note or ''} |"
            )
        lines += [
            "",
            "Review each portal's terms of use before turning on automated checks; this record "
            "does not do that for you.",
        ]
        return ExperimentReport(
            id=exp.id,
            markdown="\n".join(lines),
            idle_timeout=idle,
            challenges=len(challenges),
            login_attempts=len(logins),
            logins_succeeded=len(ok),
            stored_password_verdict=verdict,
        )
