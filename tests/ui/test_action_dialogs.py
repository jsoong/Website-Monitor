"""The M6 UI: per-action settings in the bookmark editor, and the two M6 features that live in M5's
screens: the Email and Notifications pages of the Settings window, and Retry / Dismiss for failed
actions in the Problems window."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPlainTextEdit, QToolBar

from pagewatch.engine.secrets import NullSecrets
from pagewatch.ui.client import ApiClient
from pagewatch.ui.events import EventStream
from pagewatch.ui.windows.action_params import CONFIGURABLE, ActionParamsDialog, RowTable
from pagewatch.ui.windows.bookmark_editor import IMPLEMENTED_ACTIONS, BookmarkEditor
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.problems import ProblemsWindow
from pagewatch.ui.windows.settings import PAGES, SettingsDialog
from pagewatch.ui.workers import wait_idle
from tests.support.engine_thread import EngineThread
from tests.support.fixture_site import article
from tests.support.http_sink import HttpSink
from tests.support.smtp_server import SmtpServer

SAMPLES: dict[str, dict[str, Any]] = {
    "email": {"to": ["me@example.com"], "format": "simple", "priority": "high",
              "subject": "{{ bookmark.name }} moved", "attach_screenshot": True},
    "ntfy": {"topic": "my-topic", "priority": "high", "tags": ["warning", "tada"]},
    "webhook": {"url": "https://example.com/hook", "method": "PUT",
                "body": '{"t": {{ change.summary|tojson }}}',
                "headers": {"X-A": "b"}, "token_key": "hook-token"},
    "export": {"path": "C:/out/{name}/{date:%Y-%m-%d}.html", "format": "html_assets",
               "highlight": False},
    "run_program": {"executable": "tool.exe", "args": ["--url", "{url}", "{file_changes}"],
                    "timeout_s": 90.0, "cwd": "C:/work", "confirmed": True},
    "scrape": {"path": "out.jsonl", "format": "jsonl", "fields": [
        {"name": "price", "kind": "css", "expr": "p.price", "attr": "data-x"},
        {"name": "stock", "kind": "regex", "expr": "Stock: (\\d+)"}]},
    "script": {"steps": [{"op": "check_folder", "name": "News"},
                         {"op": "pause_bookmark", "minutes": 30}]},
}  # fmt: skip


def test_every_configurable_action_has_a_form_and_a_sample() -> None:
    assert set(SAMPLES) == set(CONFIGURABLE)


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_an_untouched_action_settings_dialog_returns_what_it_was_given(
    kind: str, qapp: QApplication
) -> None:
    dialog = ActionParamsDialog(kind, SAMPLES[kind])
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    assert dialog.params() == SAMPLES[kind]  # nothing invented, nothing lost


def test_defaults_are_left_out_so_the_stored_json_stays_small(qapp: QApplication) -> None:
    dialog = ActionParamsDialog("email", {})
    dialog.accept()
    assert dialog.params() == {}
    dialog = ActionParamsDialog("export", {"path": "x.html"})
    dialog.accept()
    assert dialog.params() == {"path": "x.html"}


@pytest.mark.parametrize(
    ("kind", "params", "message"),
    [
        ("export", {}, "path"),
        ("export", {"path": "{nope}.html"}, "unknown variable"),
        ("webhook", {"url": "ftp://x"}, "http"),
        ("run_program", {"executable": "tool.exe"}, "confirmation"),
        ("scrape", {"path": "a.csv"}, "fields"),
        ("script", {"steps": []}, "steps"),
    ],
)
def test_the_dialog_refuses_what_the_engine_would_refuse_and_says_why(
    kind: str, params: dict[str, Any], message: str, qapp: QApplication
) -> None:
    dialog = ActionParamsDialog(kind, params)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert message in dialog.error.text()


def test_free_form_fields_that_cannot_be_read_are_explained(qapp: QApplication) -> None:
    webhook = ActionParamsDialog("webhook", {"url": "https://x/y"})
    headers = [
        w for w in webhook.findChildren(QPlainTextEdit) if "Name: value" in w.placeholderText()
    ][0]
    headers.setPlainText("no colon here")
    webhook.accept()
    assert "must be 'Name: value'" in webhook.error.text()

    script = ActionParamsDialog("script", {"steps": [{"op": "pause_bookmark", "minutes": 5}]})
    table = script.findChildren(RowTable)[0]
    table.table.item(0, 1).setText("soon")
    script.accept()
    assert "whole number of minutes" in script.error.text()
    assert script.result() != QDialog.DialogCode.Accepted


# -- the editor's Actions tab -------------------------------------------------------------------


def make_bookmark(eng: EngineThread, client: ApiClient, **kw: Any) -> int:
    eng.site.set("/shop", article("Tea $4", "Coffee $5", title="Shop"))
    out = client.create_bookmark(
        {
            "url": eng.site.url("/shop"),
            "name": "Shop",
            "schedule": {"interval_s": 3600, "jitter_pct": 0},
            **kw,
        }  # fmt: skip
    )
    eng.settle()
    return out.id


def test_all_actions_but_pushover_are_available_in_the_editor(qapp: QApplication) -> None:
    assert "pushover" not in IMPLEMENTED_ACTIONS
    assert {"email", "ntfy", "webhook", "export", "run_program", "scrape", "script"} <= (
        IMPLEMENTED_ACTIONS
    )


def test_ticking_an_action_that_needs_settings_asks_for_them_and_cancel_unticks_it(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    bid = make_bookmark(eng, api_client)
    editor = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    asked: list[str] = []

    def cancel(self: ActionParamsDialog) -> QDialog.DialogCode:
        asked.append(self.action_type)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ActionParamsDialog, "exec", cancel)
    editor.action_boxes["export"].setChecked(True)
    assert asked == ["export"] and not editor.action_boxes["export"].isChecked()
    assert not editor.action_buttons["export"].isEnabled()
    editor.action_boxes["email"].setChecked(True)  # valid with no settings: no question asked
    assert asked == ["export"] and editor.action_buttons["email"].isEnabled()


def test_actions_configured_in_the_editor_are_saved_and_reloaded(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication,
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bid = make_bookmark(eng, api_client)
    editor = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())

    def accept(self: ActionParamsDialog) -> QDialog.DialogCode:
        self._params = dict(SAMPLES[self.action_type])
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ActionParamsDialog, "exec", accept)
    for kind in ("export", "ntfy", "run_program"):
        editor.action_boxes[kind].setChecked(True)  # export and run_program ask at once
    assert (
        editor._action_params["export"] == SAMPLES["export"] and "ntfy" not in editor._action_params
    )
    editor.action_buttons["ntfy"].click()  # ntfy works with defaults: its button is optional
    patch = editor.build_patch()
    assert [a["type"] for a in patch["actions"]["actions"]] == [
        "toast",
        "export",
        "run_program",
        "ntfy",
    ]
    saved: list[bool] = []
    editor.saved.connect(lambda: saved.append(True))
    editor.save()
    pump_until(lambda: bool(saved), what="save")
    stored = api_client.bookmark(bid).actions.actions
    by_type = {a.type.value: a.params for a in stored}
    assert by_type["export"] == SAMPLES["export"] and by_type["run_program"]["confirmed"] is True

    again = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    assert again.build_patch() == {}  # reopening changes nothing
    assert again._action_params["ntfy"] == SAMPLES["ntfy"]


def test_switching_a_signed_in_bookmark_to_content_alerts_needs_confirmation(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication,
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bid = make_bookmark(
        eng, api_client,
        fetch={"auth": {"kind": "basic", "username": "me", "secret_key": "site-pw"}},
        actions={"actions": [{"type": "toast"}], "alert_privacy": "private"},
    )  # fmt: skip
    editor = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    editor.privacy.setCurrentIndex(editor.privacy.findData("content"))
    answers = iter([QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
    asked: list[str] = []

    def question(*args: Any) -> QMessageBox.StandardButton:
        asked.append(args[1])
        return next(answers)

    monkeypatch.setattr(QMessageBox, "question", question)
    editor.save()  # "No": nothing is sent
    wait_idle(1000)
    assert asked == ["Show page content in alerts?"]
    assert api_client.bookmark(bid).actions.alert_privacy.value == "private"
    saved: list[bool] = []
    editor.saved.connect(lambda: saved.append(True))
    editor.save()  # "Yes"
    pump_until(lambda: bool(saved), what="save")
    assert api_client.bookmark(bid).actions.alert_privacy.value == "content"


def test_a_plain_bookmark_switches_privacy_without_a_question(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication,
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bid = make_bookmark(eng, api_client, actions={"actions": [{"type": "toast"}],
                                                  "alert_privacy": "private"})  # fmt: skip
    editor = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    monkeypatch.setattr(QMessageBox, "question", lambda *a: pytest.fail("no question expected"))
    editor.privacy.setCurrentIndex(editor.privacy.findData("content"))
    saved: list[bool] = []
    editor.saved.connect(lambda: saved.append(True))
    editor.save()
    pump_until(lambda: bool(saved), what="save")


# -- Notifications: the Email and Notifications pages of Settings ------------------------------


def start_servers(eng: EngineThread) -> tuple[SmtpServer, HttpSink]:
    smtp = eng.call(SmtpServer().start())
    sink = eng.call(HttpSink().start())
    return smtp, sink


@pytest.fixture
def settings_window(
    api_client: ApiClient, qapp: QApplication
) -> Iterator[Callable[[], SettingsDialog]]:
    dialogs: list[SettingsDialog] = []

    def factory() -> SettingsDialog:
        d = SettingsDialog(api_client, api_client.settings())
        dialogs.append(d)
        return d

    yield factory
    wait_idle(2000)
    for d in dialogs:
        d.close()


def fill_mail_and_ntfy(d: SettingsDialog, smtp: SmtpServer, sink: HttpSink) -> None:
    ed = d.notifications
    ed.smtp_host.setText("127.0.0.1")
    ed.smtp_security.setCurrentIndex(ed.smtp_security.findData("none"))
    ed.smtp_port.setValue(smtp.port)
    ed.smtp_from.setText("pw@example.com")
    ed.smtp_to.setText("a@example.com, b@example.com")
    ed.ntfy_server.setText(sink.base)
    ed.ntfy_topic.setText("alerts-1")


def test_the_mail_settings_are_on_the_email_page_and_ntfy_on_the_notifications_page(
    settings_window: Callable[[], SettingsDialog],
) -> None:
    d = settings_window()

    def holds(page: str, widget: Any) -> bool:
        return bool(d.stack.widget(PAGES.index(page)).widget().isAncestorOf(widget))  # type: ignore[attr-defined]

    assert holds("Email", d.notifications.smtp_host) and holds("Email", d.notifications.smtp_test)
    assert holds("Notifications", d.notifications.ntfy_topic)
    assert not holds("Email", d.notifications.ntfy_topic)
    assert d.build_patch() == {}  # looking at them changes nothing


def test_settings_save_the_mail_and_ntfy_settings_and_keep_passwords_in_the_secret_store(
    eng: EngineThread, api_client: ApiClient, settings_window: Callable[[], SettingsDialog],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    smtp, sink = start_servers(eng)
    d = settings_window()
    ed = d.notifications
    fill_mail_and_ntfy(d, smtp, sink)
    ed.smtp_user.setText("me")
    ed.smtp_password.setText("hunter2")
    ed.ntfy_token.setText("tk_secret")
    saved: list[bool] = []
    d.saved.connect(lambda: saved.append(True))
    d.save(close=False)
    pump_until(lambda: bool(saved), what="save")

    n = api_client.settings().notifications
    assert (n.smtp.host, n.smtp.port, n.smtp.username) == ("127.0.0.1", smtp.port, "me")
    assert n.smtp.to == ["a@example.com", "b@example.com"] and n.ntfy.topic == "alerts-1"
    assert n.ntfy.token_key == "ntfy-token"
    assert eng.engine.secrets.get("smtp-password") == "hunter2"  # in the secret store...
    assert eng.engine.secrets.get("ntfy-token") == "tk_secret"
    dumped = api_client.settings().model_dump_json()
    assert "hunter2" not in dumped and "tk_secret" not in dumped  # ...and nowhere else
    assert ed.smtp_password.text() == "" and ed.ntfy_token.text() == ""  # forgotten once stored
    assert d.build_patch() == {} and d.notifications.pending_secrets() == []  # nothing left to send
    reopened = settings_window()
    assert reopened.notifications.smtp_password.text() == ""
    assert reopened.notifications.smtp_host.text() == "127.0.0.1"


def test_a_password_typed_on_its_own_is_saved_though_no_setting_changed(
    eng: EngineThread, settings_window: Callable[[], SettingsDialog],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    d = settings_window()
    d.notifications.smtp_password.setText("only-this")
    assert d.build_patch() == {}  # a password is not a setting...
    saved: list[bool] = []
    d.saved.connect(lambda: saved.append(True))
    d.save(close=False)  # ...but it must not be swallowed by "nothing changed"
    pump_until(lambda: bool(saved), what="save")
    assert eng.engine.secrets.get("smtp-password") == "only-this"
    assert d.notifications.smtp_password.text() == ""


def test_send_test_buttons_save_first_then_report_the_outcome(
    eng: EngineThread, settings_window: Callable[[], SettingsDialog],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    smtp, sink = start_servers(eng)
    d = settings_window()
    ed = d.notifications
    fill_mail_and_ntfy(d, smtp, sink)
    ed.smtp_to.setText("me@example.com")
    assert d.build_patch() != {}
    ed.smtp_test.click()
    pump_until(lambda: ed.smtp_result.text().startswith("Sent"), what="email test")
    assert len(smtp.messages) == 1 and smtp.messages[0].rcpt_to == ["me@example.com"]
    assert d.build_patch() == {}  # the test saved them: Apply has nothing more to send
    ed.ntfy_test.click()
    pump_until(lambda: ed.ntfy_result.text().startswith("Sent"), what="ntfy test")
    assert sink.requests[0].json()["topic"] == "alerts-1"

    smtp.reject = "554 no thanks"
    ed.smtp_test.click()
    pump_until(lambda: "554" in ed.smtp_result.text(), what="a failing test")
    assert ed.smtp_result.styleSheet().endswith("#b00020")  # shown as a failure


def test_a_test_does_not_save_the_rest_of_the_window(
    eng: EngineThread, settings_window: Callable[[], SettingsDialog],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    smtp, sink = start_servers(eng)
    d = settings_window()
    fill_mail_and_ntfy(d, smtp, sink)
    d.w["keep_changed_versions"].setValue(9)  # type: ignore[attr-defined]
    d.notifications.smtp_test.click()
    pump_until(lambda: d.notifications.smtp_result.text().startswith("Sent"), what="email test")
    assert d.build_patch() == {"keep_changed_versions": 9}  # still pending, still the user's call


def test_the_public_ntfy_server_is_flagged(
    settings_window: Callable[[], SettingsDialog],
) -> None:
    ed = settings_window().notifications
    assert "public server" in ed.ntfy_warning.text()  # ntfy.sh is the default
    ed.ntfy_server.setText("https://ntfy.example.org")
    assert ed.ntfy_warning.text() == ""


def test_without_a_credential_store_the_page_says_so_and_locks_the_password_fields(
    tmp_path: Path, qapp: QApplication, pump_until: Callable[..., None]
) -> None:
    et = EngineThread(tmp_path / "null", secret_store=NullSecrets()).start()
    client = ApiClient(et.base, et.engine.token)
    try:
        d = SettingsDialog(client, client.settings())
        ed = d.notifications
        pump_until(lambda: not ed.smtp_password.isEnabled(), what="secret store check")
        assert "no secure credential store" in ed.secret_note.text()
        assert "no secure credential store" in ed.ntfy_secret_note.text()
        assert not ed.ntfy_token.isEnabled()
    finally:
        wait_idle(2000)
        client.close()
        et.stop()


# -- Problems: retry and dismiss in the Problems window ------------------------------------------


def fail_an_action(eng: EngineThread, client: ApiClient) -> int:
    """Pushover is not available, so its job fails at once and lands in Problems."""
    eng.site.set("/shop", article("Tea $4"))
    bid = client.create_bookmark(
        {
            "url": eng.site.url("/shop"),
            "name": "Tea shop",
            "schedule": {"interval_s": 60, "jitter_pct": 0},
            "actions": {"actions": [{"type": "pushover"}]},
        }  # fmt: skip
    ).id
    eng.settle()
    eng.site.set("/shop", article("Tea $4", "Coffee $5"))
    eng.advance(70)
    return bid


def test_problems_window_retries_and_dismisses_a_failed_action(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    fail_an_action(eng, api_client)
    w = ProblemsWindow(api_client)
    pump_until(lambda: w.table.rowCount() == 1, what="the list")
    assert w.table.item(0, 1).text() == "Tea shop" and w.table.item(0, 4).text() == "1"
    assert "pushover action is not available" in w.table.item(0, 2).text()
    assert not w.btn_retry.isEnabled() and not w.btn_dismiss.isEnabled()  # nothing selected

    w.table.selectRow(0)
    assert w.btn_retry.isEnabled() and w.btn_dismiss.isEnabled()
    w.btn_retry.click()
    pump_until(lambda: w.message.text().startswith("Queued again"), what="retry")
    assert "Tea shop" in w.message.text() and "pushover" in w.message.text()
    eng.settle()  # it fails again, immediately, for the same reason
    w.btn_refresh.click()
    pump_until(lambda: w.table.rowCount() == 1, what="the list again")

    w.table.selectRow(0)
    w.btn_dismiss.click()
    pump_until(lambda: w.table.rowCount() == 0, what="an empty list")
    assert w.total == 0 and w.message.text().startswith("Dismissed")
    assert not w.btn_retry.isEnabled() and not w.btn_dismiss.isEnabled()
    assert api_client.action_jobs("failed") == []  # really gone from the engine, not just hidden


def test_retrying_a_job_that_is_already_gone_says_so_and_refreshes(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    fail_an_action(eng, api_client)
    w = ProblemsWindow(api_client)
    pump_until(lambda: w.table.rowCount() == 1, what="the list")
    w.table.selectRow(0)
    job = api_client.action_jobs("failed")[0]
    api_client.dismiss_action_job(job.id)  # someone else (the CLI, another window) removed it
    w.btn_retry.click()
    pump_until(lambda: "no failed action job" in w.message.text(), what="the error")
    pump_until(lambda: w.table.rowCount() == 0, what="the refreshed list")


# -- the main window ----------------------------------------------------------------------------


@pytest.fixture
def window(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication, pump_until: Callable[..., None]
) -> Iterator[MainWindow]:
    events = EventStream(api_client.ws_url, api_client.token)
    w = MainWindow(api_client, events)
    events.start()
    w.show()
    w.activateWindow()
    QTest.qWaitForWindowActive(w)
    pump_until(lambda: events.connected, what="event stream")
    yield w
    w.close()


def test_a_failed_action_is_announced_and_counted_on_the_problems_button(
    eng: EngineThread, api_client: ApiClient, window: MainWindow, pump_until: Callable[..., None]
) -> None:
    pump_until(lambda: window.btn_problems.text() == "Problems: 0", what="the first count")
    fail_an_action(eng, api_client)
    pump_until(lambda: window.btn_problems.text() == "Problems: 1", what="the count")
    pump_until(lambda: "An action failed" in window.lbl_message.text(), what="a status message")
    assert "pushover action is not available" in window.lbl_message.text()


def test_notifications_and_problems_have_one_home_each_and_no_second_toolbar(
    window: MainWindow,
) -> None:
    assert len(window.findChildren(QToolBar)) == 1  # M6 once added its own beside the main one
    window.open_problems()  # the same window the status-bar button opens, with Retry and Dismiss
    assert window.problems_window is not None and window.problems_window.btn_retry.text() == "Retry"
