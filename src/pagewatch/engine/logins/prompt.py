"""The daily offer to sign in again (spec: Portal strategy -> Semi-attended sign-in).

Once a day, at ``signin_prompt_time`` local (or at the next wake if the machine slept through it),
the engine lists the semi-attended bookmarks whose session has not been refreshed lately and
offers to open their sign-in windows: one toast, and one ``engine_state`` event the UI turns into a
prompt. It only *offers*. It never signs in by itself, and it never pings a site to keep a session
open (spec: No keep-alive).

A bookmark already waiting for a login is left out: it has had its one alert.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from pagewatch.engine.clock import iso, parse_iso
from pagewatch.engine.config import effective_section
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.signin_prompt")

POLL_S = 60.0
FIRST_DELAY_S = 180.0
FRESH_FOR = timedelta(hours=20)  # a session refreshed this recently needs no reminder
STATE_KEY = "signin_prompt_last"


class SignInPrompter:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._task: asyncio.Task[None] | None = None
        self.last: datetime | None = None

    async def start(self) -> None:
        text = await self.e.settings_store.get_state(STATE_KEY)
        self.last = parse_iso(text) if text else None
        self._task = asyncio.create_task(self._loop(), name="signin-prompt")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def slot(self, now: datetime) -> datetime:
        """The most recent occurrence of the prompt time at or before ``now`` (aware UTC)."""
        zone = self.e.zone
        h, m = (int(x) for x in self.e.settings.signin_prompt_time.split(":"))
        local = zone.to_local(now)
        today = zone.localize(datetime.combine(local.date(), time(h, m)))
        if today <= now:
            return today
        return zone.localize(datetime.combine(local.date() - timedelta(days=1), time(h, m)))

    def due(self, now: datetime | None = None) -> bool:
        now = now or self.e.clock.now()
        if not self.e.settings.signin_prompt_enabled:
            return False
        return self.last is None or self.last < self.slot(now)

    async def _loop(self) -> None:
        await self.e.clock.sleep(FIRST_DELAY_S)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("signin_prompt_failed")
            await self.e.clock.sleep(POLL_S)

    async def candidates(self, now: datetime) -> list[tuple[int, str]]:
        """Semi-attended bookmarks that are enabled, not waiting for a login, not mid-sign-in and
        not signed in within the last 20 hours: ``(id, name)``."""

        def read(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], dict[int, sqlite3.Row]]:
            rows = conn.execute(
                "SELECT id, name, fetch_json, folder_id FROM bookmark "
                "WHERE enabled=1 AND status NOT IN ('needs_login','disabled') ORDER BY id"
            ).fetchall()
            return rows, repo.login_state_all(conn)

        rows, states = await self.e.db.read(read)
        out: list[tuple[int, str]] = []
        for r in rows:
            fetch = effective_section("fetch", r, self.e.folders, self.e.settings)
            login = fetch.get("login") or {}
            if not login.get("semi_attended") or self.e.logins.signin.is_open(r["id"]):
                continue
            state = states.get(r["id"])
            signed = state["last_signin_at"] if state is not None else None
            if signed and now - parse_iso(signed) < FRESH_FOR:
                continue
            out.append((r["id"], r["name"]))
        return out

    async def tick(self) -> list[int]:
        """Offer sign-ins if today's slot has come and not been used. Returns the bookmark ids."""
        now = self.e.clock.now()
        if not self.due(now):
            return []
        self.last = now
        await self.e.settings_store.set_state(STATE_KEY, iso(now))
        found = await self.candidates(now)
        if not found:
            return []
        ids = [i for i, _ in found]
        names = ", ".join(n for _, n in found[:3]) + (
            f" and {len(found) - 3} more" if len(found) > 3 else ""
        )
        self.e.events.publish("engine_state", {"reason": "signin_prompt", "bookmark_ids": ids})
        self.e.spawn(
            self.e.toasts.notify(
                ids[0], "Sign in again?", f"A fresh sign-in keeps checks going: {names}"
            ),
            "signin-prompt-toast",
        )
        log.info("signin_prompt", bookmarks=len(ids))
        return ids
