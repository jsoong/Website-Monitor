"""Small pure helpers shared by the logins modules (kept apart to avoid import cycles)."""

from __future__ import annotations

import sqlite3

from pagewatch.models import FetchConfig, MacroDef, MacroOut, loads


def is_signed_in(fetch: FetchConfig, macro_id: int | None) -> bool:
    """Credentials, a cookie file, a macro or a sign-in session: the spec's "signed-in
    bookmarks" (their alerts default to private)."""
    return bool(
        fetch.auth
        or fetch.cookies_file
        or macro_id is not None
        or (fetch.login is not None and fetch.login.semi_attended)
    )


def needs_browser(fetch: FetchConfig, macro_id: int | None) -> bool:
    """A macro and a hand sign-in both live in a browser profile."""
    return macro_id is not None or (fetch.login is not None and fetch.login.semi_attended)


def macro_def(row: sqlite3.Row) -> MacroDef:
    return MacroDef.model_validate(
        {
            "name": row["name"],
            "steps": loads(row["steps_json"], []),
            "login_signal": loads(row["login_signal_json"], None),
        }
    )


def macro_out(row: sqlite3.Row) -> MacroOut:
    d = macro_def(row)
    return MacroOut(
        id=row["id"],
        name=d.name,
        steps=d.steps,
        login_signal=d.login_signal,
        secret_keys_used=d.secret_keys,
        stored_password=d.uses_stored_password,
        bookmarks=row["bookmarks"],
    )
