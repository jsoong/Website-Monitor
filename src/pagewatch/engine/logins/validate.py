"""Checks that only make sense once a bookmark's whole configuration is known, and the
spec's privacy default for signed-in bookmarks."""

from __future__ import annotations

from typing import Any

from pagewatch.engine.logins.common import is_signed_in
from pagewatch.models import FetchConfig, MacroDef


def default_private_when_signed_in(
    sections: dict[str, Any], inherited: dict[str, Any], fetch: FetchConfig, macro_id: int | None
) -> bool:
    """Spec (Actions, alert privacy): signed-in bookmarks default to ``private``. "Signed in"
    means credentials, a cookie file, a macro or a semi-attended session. An explicit choice on
    the bookmark or on a folder it inherits from is left alone. Returns True when it set one."""
    if not is_signed_in(fetch, macro_id):
        return False
    chosen = sections.get("actions")
    if isinstance(chosen, list):
        chosen = {"actions": chosen}
    for source in (chosen, inherited.get("actions")):
        if isinstance(source, dict) and "alert_privacy" in source:
            return False
    sections["actions"] = {**(chosen or {}), "alert_privacy": "private"}
    return True


def check_signed_in(
    *,
    url: str,
    check_method: str,
    fetch: FetchConfig,
    macro_id: int | None,
    macro_row_exists: bool,
    macro: MacroDef | None,
) -> None:
    """Raises ``ValueError`` (a 422) when a macro or a hand sign-in cannot work as configured."""
    if macro_id is not None and not macro_row_exists:
        raise ValueError(f"macro {macro_id} does not exist")
    if macro_id is None and not (fetch.login and fetch.login.semi_attended):
        return
    what = "a macro" if macro_id is not None else "semi-attended sign-in"
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"{what} works on web pages only")
    if check_method == "static":
        raise ValueError(
            f"{what} needs the browser method (the session lives in a browser profile)"
        )
    signal = (fetch.login.signal if fetch.login else None) or (
        macro.login_signal if macro else None
    )
    if signal is None:
        raise ValueError(
            f"{what} needs a login signal, so the engine can tell when the session is gone "
            "(set fetch.login.signal, or give the macro its own)"
        )
