"""The engine's side of logins: who is "signed in", what a check needs to stay signed in, and
what happens when the session is gone.

Rules, all from the spec's *Portal strategy* and *Logins and Check-Macros*:

* A check that lands on a login signal sets ``needs_login``, alerts **once**, and **pauses** the
  bookmark until the owner signs in again (``alert_needs_login``).
* A macro runs only when the signal appears, never as a keep-alive.
* A macro that fills a stored password is "stored-password login": opt-in twice (the global
  switch, off until the portal experiment is recorded, and the bookmark's own box), at most
  one automated login per ``min_login_interval_s`` (6 h by default), and none after a failure
  until the owner resumes the bookmark.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pagewatch.engine.clock import iso, parse_iso
from pagewatch.engine.config import resolve
from pagewatch.engine.fetch import cookies as cookie_files
from pagewatch.engine.fetch.login import LoginPlan, MacroPermit
from pagewatch.engine.logins.common import macro_def, macro_out
from pagewatch.engine.logins.experiment import ExperimentService
from pagewatch.engine.logins.prompt import SignInPrompter
from pagewatch.engine.logins.recorder import MacroRecorder
from pagewatch.engine.logins.signin import SignInService
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo
from pagewatch.models import (
    BookmarkStatus,
    CookieImportIn,
    CookieStatusOut,
    FetchConfig,
    LoginStatusOut,
    MacroDef,
    MacroOut,
)

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.logins")


class LoginError(ValueError):
    """The request cannot be done (a bad cookie file, a bookmark without that kind of login)."""


class LoginNotFound(LookupError):
    pass


class Logins:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.signin = SignInService(engine)
        self.recorder = MacroRecorder(engine)
        self.prompter = SignInPrompter(engine)
        self.experiments = ExperimentService(engine)

    # -- places -------------------------------------------------------------------------

    def profile_dir(self, bookmark_id: int) -> Path:
        return self.e.data_dir.profiles_dir / str(bookmark_id)

    def cookie_path(self, bookmark_id: int) -> Path:
        return self.e.data_dir.cookies_dir / f"{bookmark_id}.txt"

    async def aclose(self) -> None:
        await self.prompter.stop()
        await self.experiments.stop()
        await self.signin.aclose()
        await self.recorder.aclose()

    # -- what a check needs -------------------------------------------------------------

    async def plan_for(self, row: sqlite3.Row, fetch: FetchConfig) -> LoginPlan | None:
        """The login plan for one check, or ``None`` for a bookmark that has no browser session."""
        macro: MacroDef | None = None
        if row["macro_id"] is not None:
            mrow = await self.e.db.read(lambda c: repo.macro_get(c, row["macro_id"]))
            if mrow is not None:
                macro = macro_def(mrow)
        semi = fetch.login is not None and fetch.login.semi_attended
        if macro is None and not semi:
            return None
        signal = (fetch.login.signal if fetch.login else None) or (
            macro.login_signal if macro else None
        )
        bookmark_id: int = row["id"]
        return LoginPlan(
            profile_dir=self.profile_dir(bookmark_id),
            signal=signal,
            macro=macro,
            permit_macro=lambda: self._permit(bookmark_id, macro, fetch),
            macro_finished=lambda ok: self._macro_finished(bookmark_id, ok),
            resolve_secret=self._secret,
        )

    async def _secret(self, key: str) -> str | None:
        return await asyncio.to_thread(self.e.secrets.get, key)

    async def _permit(
        self, bookmark_id: int, macro: MacroDef | None, fetch: FetchConfig
    ) -> MacroPermit:
        """May the macro log in now? The attempt is written down *before* it runs, so a crash
        in the middle cannot be followed by another attempt on the next check."""
        if macro is None:
            return MacroPermit(False, "login_signal")
        now = self.e.clock.now()
        state = await self.e.db.read(lambda c: repo.login_state_get(c, bookmark_id))
        if state is not None and state["auto_login_blocked"]:
            return MacroPermit(False, "login_blocked")
        if macro.uses_stored_password:
            cfg = fetch.login
            if not (self.e.settings.stored_password_logins and cfg and cfg.allow_stored_password):
                return MacroPermit(False, "stored_password_off")
            last = state["last_auto_login_at"] if state is not None else None
            interval = cfg.min_login_interval_s
            if last and now - parse_iso(last) < timedelta(seconds=interval):
                return MacroPermit(False, "login_rate_limited")
        await self.e.db.write(
            lambda c: repo.login_state_set(c, bookmark_id, last_auto_login_at=iso(now))
        )
        return MacroPermit(True)

    async def _macro_finished(self, bookmark_id: int, ok: bool) -> None:
        if not ok:  # none after a failure: the owner has to look at it first
            await self.e.db.write(
                lambda c: repo.login_state_set(c, bookmark_id, auto_login_blocked=1)
            )
            log.warning("macro_login_failed", bookmark_id=bookmark_id)

    # -- the session is gone ------------------------------------------------------------

    async def alert_needs_login(
        self, row: sqlite3.Row, reason: str, detail: str | None, *, first: bool
    ) -> None:
        """Record why, and tell the owner **once**: only the check that moves the bookmark into
        ``needs_login`` alerts. Further checks of a paused bookmark (a manual one) stay silent.
        The alert says which bookmark, nothing about the page (signed-in bookmarks are private)."""
        text = reason if not detail else f"{reason}: {detail}"
        now = iso(self.e.clock.now())
        bookmark_id: int = row["id"]
        fields: dict[str, Any] = {"reason": text[:300]}
        if first:
            fields["needs_login_at"] = now
        await self.e.db.write(lambda c: repo.login_state_set(c, bookmark_id, **fields))
        if first:
            self.e.notify_problem(
                bookmark_id,
                f"{row['name']} needs you to sign in",
                "Checks are paused until you sign in again.",
                kind="needs_login",
            )
            log.info("needs_login", bookmark_id=bookmark_id, reason=reason)

    async def session_restored(self, bookmark_id: int) -> None:
        """A check got through again: forget the needs-login note."""
        await self.e.db.write(
            lambda c: repo.login_state_set(c, bookmark_id, needs_login_at=None, reason=None)
        )

    async def resume(self, bookmark_id: int, *, reset_macro_block: bool = True) -> None:
        """The owner has fixed the sign-in (new cookies, a hand sign-in, a corrected macro):
        leave ``needs_login``, schedule a check at once and run it."""
        now = iso(self.e.clock.now())

        def write(conn: sqlite3.Connection) -> sqlite3.Row | None:
            row = repo.bookmark_get(conn, bookmark_id)
            if row is None:
                return None
            upd: dict[str, Any] = {"next_due_at": now}
            if row["status"] == BookmarkStatus.NEEDS_LOGIN.value:
                upd["status"] = (
                    BookmarkStatus.OK.value
                    if row["latest_version_id"]
                    else BookmarkStatus.NEW.value
                )
            repo.bookmark_update(conn, bookmark_id, upd, now)
            state: dict[str, Any] = {"needs_login_at": None, "reason": None}
            if reset_macro_block:
                state["auto_login_blocked"] = 0
            repo.login_state_set(conn, bookmark_id, **state)
            return repo.bookmark_get(conn, bookmark_id)

        row = await self.e.db.write(write)
        if row is None:
            raise LoginNotFound(f"bookmark {bookmark_id} not found")
        self.e.schedule_bookmark(row)
        self.e.events.publish("bookmark_updated", {"bookmark_id": bookmark_id})
        self.e.check_now([bookmark_id], force=True)

    # -- sessions and cookies -----------------------------------------------------------

    async def wipe_session(self, bookmark_id: int) -> bool:
        """Delete the bookmark's browser profile and in-memory cookie jar ("wipe session").
        Refused while a sign-in window has the profile open."""
        if self.signin.is_open(bookmark_id):
            raise LoginError("a sign-in window is open for this bookmark; close it first")
        path = self.profile_dir(bookmark_id)
        existed = await asyncio.to_thread(path.exists)
        if existed:
            await asyncio.to_thread(shutil.rmtree, path, True)
        self.e.static_fetcher.forget_session(bookmark_id)
        await self.e.db.write(lambda c: repo.login_state_set(c, bookmark_id, last_signin_at=None))
        return existed

    async def import_cookies(self, bookmark_id: int, body: CookieImportIn) -> CookieStatusOut:
        """Validate a cookies.txt, keep a private copy in the data folder, point the bookmark
        at it, and resume the bookmark if it was waiting for a login."""
        from pagewatch.engine import bookmarks as svc
        from pagewatch.models import BookmarkPatch

        row = await self.e.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
        if row is None:
            raise LoginNotFound(f"bookmark {bookmark_id} not found")
        try:
            if body.text is not None:
                parsed = cookie_files.parse_netscape(body.text)
                text = body.text
            else:
                assert body.path is not None
                text = await asyncio.to_thread(
                    lambda: Path(body.path or "").read_text(encoding="utf-8", errors="replace")
                )
                parsed = cookie_files.parse_netscape(text)
        except (OSError, cookie_files.CookieFileError) as exc:
            raise LoginError(f"cannot read the cookie file: {exc}") from exc
        if not parsed.cookies:
            raise LoginError(
                "no cookies were found: this does not look like a Netscape cookies.txt"
            )
        target = self.cookie_path(bookmark_id)

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.replace(tmp, target)

        await asyncio.to_thread(write)
        try:
            await svc.patch(self.e, bookmark_id, BookmarkPatch(fetch={"cookies_file": str(target)}))
        except svc.InvalidError as exc:
            raise LoginError(str(exc)) from exc
        if row["status"] == BookmarkStatus.NEEDS_LOGIN.value:
            await self.resume(bookmark_id, reset_macro_block=False)
        status = cookie_files.summarize(parsed, time.time())
        status.imported_at = iso(self.e.clock.now())
        return status

    async def remove_cookies(self, bookmark_id: int) -> None:
        from pagewatch.engine import bookmarks as svc
        from pagewatch.models import BookmarkPatch

        row = await self.e.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
        if row is None:
            raise LoginNotFound(f"bookmark {bookmark_id} not found")
        await svc.patch(self.e, bookmark_id, BookmarkPatch(fetch={"cookies_file": None}))
        await asyncio.to_thread(lambda: self.cookie_path(bookmark_id).unlink(missing_ok=True))
        self.e.static_fetcher.forget_session(bookmark_id)

    async def cookie_status(self, fetch: FetchConfig) -> CookieStatusOut:
        if not fetch.cookies_file:
            return CookieStatusOut()
        path = Path(fetch.cookies_file)
        try:
            parsed = await asyncio.to_thread(cookie_files.load_cookie_file, path)
            mtime = (await asyncio.to_thread(path.stat)).st_mtime
        except (cookie_files.CookieFileError, OSError):
            return CookieStatusOut()
        status = cookie_files.summarize(parsed, time.time())
        from datetime import UTC, datetime

        status.imported_at = (
            datetime.fromtimestamp(mtime, tz=UTC).isoformat().replace("+00:00", "Z")
        )
        return status

    # -- status -------------------------------------------------------------------------

    async def status(self, bookmark_id: int) -> LoginStatusOut:
        def read(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
            return repo.bookmark_get(conn, bookmark_id), repo.login_state_get(conn, bookmark_id)

        row, state = await self.e.db.read(read)
        if row is None:
            raise LoginNotFound(f"bookmark {bookmark_id} not found")
        resolved = resolve(row, self.e.folders, self.e.settings)
        fetch = resolved.fetch
        macro: MacroDef | None = None
        if row["macro_id"] is not None:
            mrow = await self.e.db.read(lambda c: repo.macro_get(c, row["macro_id"]))
            macro = macro_def(mrow) if mrow is not None else None
        semi = bool(fetch.login and fetch.login.semi_attended)
        mode = (
            "semi_attended" if semi else "macro" if macro else "cookies" if fetch.cookies_file
            else "server_auth" if fetch.auth else "none"
        )  # fmt: skip
        stored = bool(macro and macro.uses_stored_password)
        allowed = bool(
            stored
            and self.e.settings.stored_password_logins
            and fetch.login
            and fetch.login.allow_stored_password
        )
        last_auto = state["last_auto_login_at"] if state is not None else None
        next_auto = None
        if stored and last_auto and fetch.login:
            next_auto = iso(
                parse_iso(last_auto) + timedelta(seconds=fetch.login.min_login_interval_s)
            )
        profile = self.profile_dir(bookmark_id)
        return LoginStatusOut(
            bookmark_id=bookmark_id,
            signed_in_mode=mode,
            needs_login=row["status"] == BookmarkStatus.NEEDS_LOGIN.value,
            reason=state["reason"] if state is not None else None,
            semi_attended=semi,
            macro_id=row["macro_id"],
            stored_password=stored,
            stored_password_allowed=allowed,
            last_signin_at=state["last_signin_at"] if state is not None else None,
            last_auto_login_at=last_auto,
            next_auto_login_at=next_auto,
            auto_login_blocked=bool(state["auto_login_blocked"]) if state is not None else False,
            signing_in=self.signin.is_open(bookmark_id),
            profile_exists=await asyncio.to_thread(profile.exists),
            cookies=await self.cookie_status(fetch),
        )

    # -- macros -------------------------------------------------------------------------

    async def macros(self) -> list[MacroOut]:
        rows = await self.e.db.read(repo.macro_list)
        out: list[MacroOut] = []
        for r in rows:
            try:
                out.append(macro_out(r))
            except ValueError:  # a hand-edited macro must not hide the others
                log.warning("macro_unreadable", macro_id=r["id"])
        return out
