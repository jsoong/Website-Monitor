"""M6 logins in the UI, against a real engine (real HTTP and WebSocket, fake clock): the editor's
Login tab, proxy login, the Macros dialog and recorder, Sign in from the main window and the
Problems window, and the Settings additions."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtTest import QTest

from pagewatch.engine.fetch.base import FetchRequest, FetchResult
from pagewatch.models import CapacityOut, HostDemand, MacroOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.events import EventStream
from pagewatch.ui.windows.bookmark_editor import BookmarkEditor
from pagewatch.ui.windows.login_panel import cookie_text, reason_sentence, status_text
from pagewatch.ui.windows.macros import MacrosDialog, RecordDialog, steps_text
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.problems import ProblemsWindow, explain
from pagewatch.ui.windows.settings import SettingsDialog
from tests.support.engine_thread import EngineThread
from tests.support.fakes import ScriptedFetcher
from tests.support.fixture_site import article

SIGNAL = {"selector": "input[type=password]"}
MACRO = {
    "name": "portal",
    "steps": [
        {"op": "goto", "url": "http://portal.test/login"},
        {"op": "fill", "selector": "#user", "value": "alice"},
        {"op": "fill", "selector": "#password", "value": {"secret": "portal-pw"}},
        {"op": "click", "selector": "#go"},
    ],
    "login_signal": SIGNAL,
}


class Session:
    """Whether the (pretend) browser is signed in; the sign-in window flips it."""

    def __init__(self) -> None:
        self.signed_in = False

    def respond(self, req: FetchRequest, n: int) -> FetchResult:
        if not self.signed_in:
            return FetchResult(
                req.url, 200, {}, "", b"", login_required=True, login_reason="login_signal"
            )
        body = b"<html><body><h1>Account</h1><p>Balance 100</p></body></html>"
        return FetchResult(req.url, 200, {}, "text/html; charset=utf-8", body, elapsed_ms=3)


class Window:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.closed = asyncio.Event()

    async def wait_closed(self) -> None:
        await self.closed.wait()

    async def close(self) -> None:
        self.session.signed_in = True  # the owner logged in before closing
        self.closed.set()


class Opener:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.opened = 0

    async def open(self, profile_dir: Path, url: str) -> Window:
        self.opened += 1

        def write() -> None:
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "Cookies").write_text("session")

        await asyncio.to_thread(write)
        return Window(self.session)


@pytest.fixture
def portal(eng: EngineThread) -> Session:
    """The engine's browser is a scripted one: signed out until the sign-in window closes."""
    session = Session()

    async def install() -> None:
        eng.engine.fetchers.replace("browser", ScriptedFetcher(session.respond))  # type: ignore[arg-type]
        eng.engine.logins.signin.set_opener(Opener(session))

    eng.call(install())
    return session


def semi_bookmark(eng: EngineThread, client: ApiClient, name: str = "Portal") -> int:
    out = client.create_bookmark(
        {
            "url": "http://portal.test/account", "name": name,
            "schedule": {"interval_s": 3600, "jitter_pct": 0},
            "fetch": {"login": {"semi_attended": True, "signal": SIGNAL}},
        }
    )  # fmt: skip
    eng.settle()
    return out.id


def plain_bookmark(
    eng: EngineThread, client: ApiClient, name: str = "Shop", interval_s: int = 3600
) -> int:
    path = "/" + name.lower()
    eng.site.set(path, article("Tea $4", title=name))
    out = client.create_bookmark(
        {
            "url": eng.site.url(path),
            "name": name,
            "schedule": {"interval_s": interval_s, "jitter_pct": 0},
        }  # fmt: skip
    )
    eng.settle()
    return out.id


def editor(client: ApiClient, bid: int) -> BookmarkEditor:
    return BookmarkEditor(client, [client.bookmark(bid)], client.folders())


# -- the sentences ----------------------------------------------------------------------------


def test_the_status_and_cookie_sentences() -> None:
    from pagewatch.models import CookieStatusOut, LoginStatusOut

    st = LoginStatusOut(
        bookmark_id=1, signed_in_mode="macro", needs_login=True, reason="macro_failed: step 3",
        stored_password=True, stored_password_allowed=True, auto_login_blocked=True,
    )  # fmt: skip
    text = status_text(st)
    assert "Signs in with a macro" in text and "NEEDS YOU" in text and "step 3" in text
    assert "stopped after a failure" in text
    assert "No sign-in" in status_text(LoginStatusOut(bookmark_id=1, signed_in_mode="none"))
    assert reason_sentence("auth_rejected") == "the site rejected the saved login or cookies"
    assert reason_sentence(None) == "" and reason_sentence("odd_code") == "odd_code"
    assert cookie_text(CookieStatusOut()) == "No cookies imported."
    c = CookieStatusOut(present=True, count=3, expired=1, domains=["a.com"], skipped_lines=2,
                        earliest_expiry="2030-01-01T00:00:00Z")  # fmt: skip
    assert "3 cookie(s) for a.com" in cookie_text(c) and "1 expired" in cookie_text(c)
    assert "2 line(s) could not be read" in cookie_text(c)


# -- the editor's Login tab ---------------------------------------------------------------------


def test_opening_and_saving_an_untouched_bookmark_still_changes_nothing(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = plain_bookmark(eng, api_client)
    ed = editor(api_client, bid)
    pump_until(
        lambda: ed.login is not None and "No sign-in" in ed.login.status.text(), what="status"
    )
    assert ed.build_patch() == {}


def test_a_server_login_is_saved_with_its_password_in_the_credential_store(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = plain_bookmark(eng, api_client)
    ed = editor(api_client, bid)
    assert ed.login is not None
    login = ed.login
    login.auth_on.setChecked(True)
    login.auth_kind.setCurrentIndex(login.auth_kind.findData("digest"))
    login.auth_user.setText("alice")
    assert login.auth_key.text() == f"bookmark-{bid}-login"
    login.auth_password.setText("s3cret-pw")
    login.btn_auth_save.click()
    pump_until(lambda: eng.secret_store.get(f"bookmark-{bid}-login") == "s3cret-pw", what="stored")
    assert login.auth_password.text() == ""  # it left the form the moment it was sent
    pump_until(lambda: "password is stored" in login.auth_state.text(), what="state")
    patch = ed.build_patch()
    assert patch == {
        "fetch": {
            "auth": {"kind": "digest", "username": "alice", "secret_key": f"bookmark-{bid}-login"}
        }
    }
    assert "s3cret-pw" not in json.dumps(patch)  # the password is never part of a bookmark
    ed.save()
    pump_until(lambda: api_client.bookmark(bid).fetch.auth is not None, what="saved")
    after = api_client.bookmark(bid)
    assert after.fetch.auth is not None and after.fetch.auth.kind == "digest"
    assert after.actions.alert_privacy.value == "private"  # signed in: private by default
    again = editor(api_client, bid)
    assert again.login is not None and again.login.auth_on.isChecked()
    assert again.build_patch() == {}


def test_a_half_filled_server_login_says_what_is_missing(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    bid = plain_bookmark(eng, api_client)
    ed = editor(api_client, bid)
    assert ed.login is not None
    ed.login.auth_on.setChecked(True)
    ed.save()
    assert "user name and a password name" in ed.error.text()


def test_cookies_are_imported_from_a_file_and_removed(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None],
    tmp_path: Path,
) -> None:  # fmt: skip
    bid = plain_bookmark(eng, api_client)
    ed = editor(api_client, bid)
    assert ed.login is not None
    login = ed.login
    jar = tmp_path / "cookies.txt"
    jar.write_text("127.0.0.1\tFALSE\t/\tFALSE\t0\ttoken\tabc123\n")
    login.choose_cookie_file = lambda: str(jar)  # type: ignore[method-assign]
    login.btn_cookie_import.click()
    pump_until(lambda: "1 cookie(s)" in login.cookie_state.text(), what="imported")
    assert "abc123" not in login.cookie_state.text() and "abc123" not in login.message.text()
    assert api_client.bookmark(bid).fetch.cookies_file is not None
    login.choose_cookie_file = lambda: ""  # type: ignore[method-assign]
    login.btn_cookie_import.click()  # cancelling the dialog does nothing
    login.btn_cookie_remove.click()
    pump_until(lambda: api_client.bookmark(bid).fetch.cookies_file is None, what="removed")
    pump_until(lambda: login.cookie_state.text() == "No cookies imported.", what="status")

    jar.write_text("not a cookie file")
    login.choose_cookie_file = lambda: str(jar)  # type: ignore[method-assign]
    login.btn_cookie_import.click()
    pump_until(lambda: "no cookies" in login.message.text().lower(), what="refused")


def test_choosing_a_macro_and_a_signal_saves_and_defaults_the_alerts_to_private(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    mid = api_client.create_macro({**MACRO, "name": "no signal", "login_signal": None}).id
    bid = plain_bookmark(eng, api_client)
    ed = editor(api_client, bid)
    assert ed.login is not None
    login = ed.login
    pump_until(lambda: login.macro.findData(mid) >= 0, what="macros listed")
    login.macro.setCurrentIndex(login.macro.findData(mid))
    ed.save()  # a macro without a login signal: the engine says why
    pump_until(lambda: "login signal" in ed.error.text(), what="refused")
    assert api_client.bookmark(bid).macro_id is None

    login.sig_selector.setText("input[type=password]")
    assert ed.build_patch()["macro_id"] == mid
    assert ed.build_patch()["fetch"]["login"]["signal"] == {
        "selector": "input[type=password]", "url_contains": None, "text": None,
    }  # fmt: skip
    ed.save()
    pump_until(lambda: api_client.bookmark(bid).macro_id == mid, what="saved")
    after = api_client.bookmark(bid)
    assert after.actions.alert_privacy.value == "private"
    again = editor(api_client, bid)
    pump_until(lambda: again.login is not None and again.login.macro_id() == mid, what="selected")
    assert again.build_patch() == {}


def test_the_global_switch_is_explained_next_to_the_bookmarks_opt_in(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = plain_bookmark(eng, api_client)
    ed = editor(api_client, bid)
    assert ed.login is not None
    pump_until(lambda: "Off until" in ed.login.allow_note.text(), what="note")
    api_client.put_settings({"stored_password_logins": True})
    ed2 = editor(api_client, bid)
    assert ed2.login is not None
    pump_until(lambda: "is ON" in ed2.login.allow_note.text(), what="note on")
    ed2.login.allow_pw.setChecked(True)
    ed2.login.interval_h.setValue(8)
    login = ed2.build_patch()["fetch"]["login"]
    assert login["allow_stored_password"] is True and login["min_login_interval_s"] == 8 * 3600


def test_bulk_edit_leaves_sign_in_alone(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    a, b = plain_bookmark(eng, api_client, "A"), plain_bookmark(eng, api_client, "B")
    ed = BookmarkEditor(
        api_client, [api_client.bookmark(a), api_client.bookmark(b)], api_client.folders()
    )
    assert ed.login is None
    ed.apply_all = None  # nothing ticked
    assert "login" not in json.dumps(ed.build_patch())


def test_sign_in_finish_resume_and_wipe_from_the_tab(
    eng: EngineThread, api_client: ApiClient, qapp: Any, portal: Session,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    bid = semi_bookmark(eng, api_client)
    assert api_client.bookmark(bid).status.value == "needs_login"
    ed = editor(api_client, bid)
    assert ed.login is not None
    login = ed.login
    pump_until(lambda: "NEEDS YOU" in login.status.text(), what="needs login shown")
    assert login.btn_resume.isEnabled() and login.btn_signin.isEnabled()
    assert not login.btn_finish.isEnabled()

    login.btn_signin.click()
    pump_until(lambda: api_client.login_status(bid).signing_in, what="window open")
    login.refresh_status()
    pump_until(lambda: login.btn_finish.isEnabled(), what="finish enabled")
    login.btn_finish.click()
    pump_until(lambda: api_client.bookmark(bid).status.value == "ok", what="signed in and checked")
    pump_until(lambda: "NEEDS YOU" not in login.status.text(), what="status refreshed")
    st = api_client.login_status(bid)
    assert st.last_signin_at and st.profile_exists and not st.signing_in

    login._confirm = lambda title, text: True  # type: ignore[method-assign]
    pump_until(lambda: login.btn_wipe.isEnabled(), what="wipe enabled")
    login.btn_wipe.click()
    pump_until(lambda: not api_client.login_status(bid).profile_exists, what="wiped")

    portal.signed_in = False  # the session ends; a manual resume checks again
    api_client.check([bid], force=True)
    eng.settle()
    assert api_client.bookmark(bid).status.value == "needs_login"
    login.refresh_status()
    pump_until(lambda: login.btn_resume.isEnabled(), what="resume enabled")
    portal.signed_in = True
    login.btn_resume.click()
    pump_until(lambda: api_client.bookmark(bid).status.value == "ok", what="resumed")


def test_a_sign_in_that_cannot_start_says_why(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = plain_bookmark(eng, api_client)  # not semi-attended, as far as the engine knows
    ed = editor(api_client, bid)
    assert ed.login is not None
    login = ed.login
    pump_until(lambda: "No sign-in" in login.status.text(), what="status")
    assert not login.btn_signin.isEnabled()  # nothing to sign in to yet
    login.semi.setChecked(True)  # ticked but not saved: the engine still does not know
    login.btn_signin.setEnabled(True)
    login.btn_signin.click()
    pump_until(lambda: "semi-attended" in login.message.text(), what="reason")


# -- the proxy login (Advanced tab) -----------------------------------------------------------


def test_a_proxy_with_a_login_keeps_its_password_in_the_credential_store(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = plain_bookmark(eng, api_client)
    ed = editor(api_client, bid)
    ed.proxy.setText("socks5://proxy.test:1080")
    ed.proxy_user.setText("bob")
    assert ed.proxy_key.text() == f"bookmark-{bid}-proxy"
    ed.proxy_password.setText("proxy-pw")
    ed.btn_proxy_password.click()
    pump_until(lambda: eng.secret_store.get(f"bookmark-{bid}-proxy") == "proxy-pw", what="stored")
    pump_until(lambda: "saved" in ed.proxy_note.text(), what="told")
    assert ed.proxy_password.text() == ""
    patch = ed.build_patch()["fetch"]
    assert patch["proxy"] == "socks5://proxy.test:1080" and patch["proxy_username"] == "bob"
    assert patch["proxy_secret_key"] == f"bookmark-{bid}-proxy" and "proxy-pw" not in json.dumps(
        patch
    )
    ed.save()
    pump_until(lambda: api_client.bookmark(bid).fetch.proxy_username == "bob", what="saved")
    ed.proxy_user.setText("")  # no login: the password name is dropped with it
    form = ed.fetch_dict()
    assert form["proxy_username"] is None and form["proxy_secret_key"] is None


def test_a_proxy_address_with_a_password_in_it_is_refused(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = plain_bookmark(eng, api_client)
    ed = editor(api_client, bid)
    ed.proxy.setText("http://bob:hunter2@proxy.test:3128")
    ed.save()
    pump_until(lambda: "password" in ed.error.text(), what="refused")
    assert api_client.bookmark(bid).fetch.proxy is None
    ed.proxy.setText("")
    ed.btn_proxy_password.click()  # nothing typed
    assert "first" in ed.proxy_note.text()


# -- the Macros dialog and the recorder ------------------------------------------------------


def test_macros_are_created_edited_and_deleted_with_their_mistakes_caught_first(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    d = MacrosDialog(api_client)
    d.new_macro()
    assert (
        "example-password" in d.secret_keys()[0] and "input[type=password]" in d.sig_selector.text()
    )
    d.save()
    assert "name" in d.error.text().lower()  # no name yet
    d.name.setText("portal")
    d.steps.setPlainText("not json")
    d.save()
    assert "not valid JSON" in d.error.text()
    d.steps.setPlainText('{"op": "goto"}')
    d.save()
    assert "list" in d.error.text()
    d.steps.setPlainText(json.dumps([{"op": "fill", "selector": "#password", "value": "plain"}]))
    d.save()
    assert "password field" in d.error.text() and api_client.macros() == []
    d.steps.setPlainText(json.dumps(MACRO["steps"]))
    d.sig_selector.setText("")
    d.sig_url.setText("/login")
    seen: list[int] = []
    d.macros_changed.connect(lambda: seen.append(1))
    d.save()
    pump_until(lambda: len(api_client.macros()) == 1, what="saved")
    pump_until(lambda: d.listing.count() == 1, what="listed")
    saved = api_client.macros()[0]
    assert saved.login_signal is not None and saved.login_signal.url_contains == "/login"
    assert saved.secret_keys_used == ["portal-pw"] and seen

    d.steps.setPlainText(steps_text(list(saved.steps)[:2]))  # shorter
    d.save()
    pump_until(lambda: len(api_client.macros()[0].steps) == 2, what="replaced")

    d._confirm = lambda title, text: False  # type: ignore[method-assign]
    d.delete()
    assert len(api_client.macros()) == 1
    d._confirm = lambda title, text: True  # type: ignore[method-assign]
    d.delete()
    pump_until(lambda: api_client.macros() == [], what="deleted")


def test_the_password_a_macro_needs_is_set_without_ever_being_shown(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    api_client.create_macro(MACRO)
    d = MacrosDialog(api_client)
    pump_until(lambda: d.listing.count() == 1, what="listed")
    d.listing.setCurrentRow(0)
    pump_until(lambda: d.secrets.rowCount() == 1, what="secret rows")
    pump_until(lambda: d.secrets.item(0, 1).text() == "no", what="not stored yet")
    assert d.secrets.item(0, 0).text() == "portal-pw"
    d._ask_password = lambda key: "hunter2"  # type: ignore[method-assign]
    d.set_password("portal-pw")
    pump_until(lambda: eng.secret_store.get("portal-pw") == "hunter2", what="stored")
    pump_until(lambda: d.secrets.item(0, 1).text() == "yes", what="shown as stored")
    assert "hunter2" not in d.error.text() and "hunter2" not in d.steps.toPlainText()
    d._ask_password = lambda key: ""  # cancelled
    d.set_password("portal-pw")


RECORDED = [
    {"name": "navigate", "url": "http://portal.test/login"},
    {"name": "fill", "selector": "#username", "text": "alice"},
    {"name": "fill", "selector": "#password", "text": "typed-during-recording"},
    {"name": "click", "selector": "#go"},
]


def fake_codegen(
    lines: list[dict[str, Any]], *, hang: bool = False
) -> Callable[[str, Path], list[str]]:
    script = (
        "import sys, time, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        f"{'time.sleep(60)' if hang else ''}\n"
        f"out.write_text({chr(10).join(json.dumps(x) for x in lines)!r})\n"
    )
    return lambda url, out: [sys.executable, "-c", script, "-o", str(out), url]


def test_recording_a_macro_shows_the_steps_for_review(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    eng.engine.logins.recorder.set_command(fake_codegen(RECORDED))
    dlg = RecordDialog(api_client, url="http://portal.test/login")
    assert not dlg.btn_use.isEnabled()
    dlg.password_key.setText("pw")
    dlg.btn_start.click()
    pump_until(lambda: dlg.btn_use.isEnabled(), what="recording done")
    assert [s.op for s in dlg.result_steps] == ["goto", "fill", "fill", "click"]
    assert "typed-during-recording" not in dlg.preview.toPlainText()
    assert '"secret": "pw"' in dlg.preview.toPlainText() and "NOT kept" in dlg.warnings.text()

    macros = MacrosDialog(api_client)
    macros._make_recorder = lambda: dlg  # type: ignore[method-assign]
    macros._exec = lambda d: True  # type: ignore[method-assign]
    macros.record()
    assert json.loads(macros.steps.toPlainText())[2]["value"] == {"secret": "pw"}
    assert "Recorded" in macros.error.text()
    macros.refresh_secrets()
    assert macros.secret_keys() == ["pw"]


def test_a_recording_that_is_cancelled_or_fails_says_so(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    eng.engine.logins.recorder.set_command(
        fake_codegen([{"name": "openPage", "url": "about:blank"}])
    )
    dlg = RecordDialog(api_client, url="http://portal.test/login")
    dlg.btn_start.click()
    pump_until(lambda: "nothing was recorded" in dlg.status.text(), what="failure")
    assert dlg.btn_start.isEnabled() and not dlg.btn_use.isEnabled()

    eng.engine.logins.recorder.set_command(fake_codegen(RECORDED, hang=True))
    dlg2 = RecordDialog(api_client, url="http://portal.test/login")
    dlg2.btn_start.click()
    pump_until(lambda: dlg2._id is not None and "Recording" in dlg2.status.text(), what="running")
    dlg2.cancel()
    pump_until(lambda: api_client.recording(dlg2._id or "").state == "cancelled", what="cancelled")

    bad = RecordDialog(api_client, url="ftp://nope")
    bad.btn_start.click()
    pump_until(lambda: bad.btn_start.isEnabled(), what="refused")
    assert "http" in bad.status.text()


# -- main window and Problems -----------------------------------------------------------------


@pytest.fixture
def window(eng: EngineThread, qapp: Any, pump_until: Callable[..., None]) -> Any:
    client = ApiClient(eng.base, eng.engine.token)
    events = EventStream(client.ws_url, client.token)
    w = MainWindow(client, events)
    events.start()
    w.show()
    QTest.qWaitForWindowActive(w)
    pump_until(lambda: events.connected, what="event stream")
    yield w
    w.close()


def test_sign_in_from_the_main_window_opens_the_window_and_checks_when_it_closes(
    eng: EngineThread, api_client: ApiClient, window: MainWindow, portal: Session,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    bid = semi_bookmark(eng, api_client)
    pump_until(lambda: window.model.rowCount() == 1, what="listed")
    window.table.selectRow(0)
    window.act_signin.trigger()
    pump_until(lambda: api_client.login_status(bid).signing_in, what="window opened")
    pump_until(lambda: "Sign in to Portal" in window.lbl_message.text(), what="told")

    async def close_window() -> None:
        await eng.engine.logins.signin.finish(bid)

    eng.call(close_window())
    pump_until(lambda: api_client.bookmark(bid).status.value == "ok", what="checked")
    pump_until(lambda: "Signed in" in window.lbl_message.text(), what="told")


def test_sign_in_needs_exactly_one_bookmark_selected(
    eng: EngineThread, api_client: ApiClient, window: MainWindow
) -> None:
    window.sign_in_selected()
    assert "Select one bookmark" in window.lbl_message.text()


def test_the_daily_offer_changes_the_button_and_says_so_without_a_dialog(
    eng: EngineThread, api_client: ApiClient, window: MainWindow
) -> None:
    window._on_event("engine_state", {"reason": "signin_prompt", "bookmark_ids": [1, 2]})
    assert window.act_signin.text() == "Sign in... (2)" and window.signin_offer == [1, 2]
    assert "2 bookmark(s) could use a fresh sign-in" in window.lbl_message.text()
    window.offer_signin([])
    assert window.act_signin.text() == "Sign in..."


def test_macros_open_from_the_toolbar(eng: EngineThread, window: MainWindow) -> None:
    opened: list[Any] = []
    window._exec = lambda d: opened.append(d)  # type: ignore[method-assign]
    window.act_macros.trigger()
    assert isinstance(opened[0], MacrosDialog)


def test_the_problems_window_offers_sign_in_and_resume_for_a_bookmark_that_needs_one(
    eng: EngineThread, api_client: ApiClient, qapp: Any, portal: Session,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    bid = semi_bookmark(eng, api_client)
    win = ProblemsWindow(api_client)
    pump_until(lambda: win.total == 1, what="listed")
    win.table.selectRow(0)
    p = win.selected_problem()
    assert p is not None and p.kind == "needs_login" and p.bookmark_id == bid
    assert explain(p) == "The site is showing its login page: sign in again"
    assert win.btn_signin.isEnabled() and win.btn_resume.isEnabled()
    win.btn_signin.click()
    pump_until(lambda: api_client.login_status(bid).signing_in, what="window opened")
    pump_until(lambda: "Sign in to Portal" in win.message.text(), what="told")
    eng.call(eng.engine.logins.signin.finish(bid))
    pump_until(lambda: api_client.bookmark(bid).status.value == "ok", what="checked")
    pump_until(lambda: win.total == 0 or win.refresh() is None and win.total == 0, what="cleared")

    portal.signed_in = False
    api_client.check([bid], force=True)
    eng.settle()
    win.refresh()
    pump_until(lambda: win.total == 1, what="listed again")
    win.table.selectRow(0)
    portal.signed_in = True
    win.btn_resume.click()
    pump_until(lambda: api_client.bookmark(bid).status.value == "ok", what="resumed")


def test_a_bookmark_that_is_failing_gets_neither_sign_in_nor_resume(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    from tests.ui.test_problems import make as make_failing

    make_failing(eng, api_client, "Broken")
    eng.site.set("/broken", "x", status=503)
    api_client.check(None, all=True, force=True)
    eng.advance(200)
    win = ProblemsWindow(api_client)
    pump_until(lambda: win.total >= 1, what="listed")
    win.table.selectRow(0)
    assert not win.btn_signin.isEnabled() and not win.btn_resume.isEnabled()


# -- Settings -----------------------------------------------------------------------------------


def test_settings_save_the_new_switches_and_the_proxy_login(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    d = SettingsDialog(api_client, api_client.settings(), eng.data_dir)
    d.w["stored_password_logins"].setChecked(True)
    d.w["stretch_hosts"].setChecked(False)
    d.w["global_proxy"].setText("http://proxy.test:3128")
    d.w["global_proxy_username"].setText("bob")
    d.w["global_proxy_secret_key"].setText("global-proxy")
    d.proxy_password.setText("p@ss")
    d.btn_proxy_password.click()
    pump_until(lambda: eng.secret_store.get("global-proxy") == "p@ss", what="stored")
    pump_until(lambda: "saved" in d.proxy_note.text(), what="told")
    assert d.proxy_password.text() == ""
    patch = d.build_patch()
    assert patch == {
        "stored_password_logins": True, "stretch_hosts": False,
        "global_proxy": "http://proxy.test:3128", "global_proxy_username": "bob",
        "global_proxy_secret_key": "global-proxy",
    }  # fmt: skip
    assert "p@ss" not in json.dumps(patch)
    d.save()
    pump_until(lambda: api_client.settings().stored_password_logins, what="saved")
    after = api_client.settings()
    assert after.global_proxy_username == "bob" and after.stretch_hosts is False


def test_a_pasted_address_in_the_website_exceptions_becomes_the_host_name(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    d = SettingsDialog(api_client, api_client.settings(), eng.data_dir)
    d.add_override("https://Example.COM:8443/path", 1, 5.0)
    assert d.build_patch()["host_overrides"] == {
        "example.com": {"concurrency": 1, "min_gap_s": 5.0}
    }
    d.save()
    pump_until(lambda: "example.com" in api_client.settings().host_overrides, what="saved")
    again = SettingsDialog(api_client, api_client.settings(), eng.data_dir)
    assert again.build_patch() == {}  # reopened: the stored spelling is the host name
    d.add_override("no way", 1, None)
    with pytest.raises(Exception, match="not a website name"):
        d.build_patch()


def test_the_capacity_page_shows_how_much_a_website_is_slowed(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    d = SettingsDialog(api_client, api_client.settings(), eng.data_dir)
    d._show_capacity(
        CapacityOut(
            pools=[],
            hosts=[
                HostDemand(
                    host="busy.test",
                    bookmarks=5,
                    demand_per_min=5.0,
                    ceiling_per_min=2.0,
                    utilisation=2.5,
                    over_limit=True,
                    stretch=2.5,
                ),
                HostDemand(
                    host="calm.test",
                    bookmarks=1,
                    demand_per_min=1.0,
                    ceiling_per_min=30.0,
                    utilisation=0.03,
                ),
            ],
            hosts_total=2,
            hosts_over_limit=1,
            warn_utilisation=0.8,
            generated_at="2026-01-01T00:00:00Z",
        )  # fmt: skip
    )
    assert d.hosts.item(0, 5).text() == "x2.5" and d.hosts.item(1, 5).text() == ""
    assert "waits proportionally longer" in d.capacity_summary.text()


def test_the_editor_warns_when_the_website_is_asked_for_more_than_it_allows(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    ids = [plain_bookmark(eng, api_client, f"Shop{i}", 60) for i in range(4)]  # 4 a minute
    api_client.put_settings({"host_overrides": {"127.0.0.1": {"min_gap_s": 120}}})  # 0.5 a minute
    ed = editor(api_client, ids[0])
    pump_until(lambda: "minimum gap allows" in ed.host_warning.text(), what="warning")
    assert "127.0.0.1" in ed.host_warning.text()
    api_client.put_settings({"host_overrides": {"127.0.0.1": {"min_gap_s": 1}}})
    calm = editor(api_client, ids[0])
    QTest.qWait(300)
    pump_until(lambda: calm.host_warning.text() == "", what="no warning")


def test_macro_models_round_trip_through_the_dialog_text() -> None:
    m = MacroOut.model_validate({"id": 1, **MACRO, "bookmarks": 0})
    text = steps_text(list(m.steps))
    assert json.loads(text)[2] == {
        "op": "fill", "selector": "#password", "value": {"secret": "portal-pw"},
    }  # fmt: skip
