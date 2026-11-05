"""The M6 UI: per-action settings, the Notifications dialog, the failed-actions (Problems) list,
and their place in the bookmark editor and the main window."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPlainTextEdit

from pagewatch.engine.secrets import NullSecrets
from pagewatch.ui.client import ApiClient
from pagewatch.ui.events import EventStream
from pagewatch.ui.windows.action_params import CONFIGURABLE, ActionParamsDialog, RowTable
from pagewatch.ui.windows.bookmark_editor import IMPLEMENTED_ACTIONS, BookmarkEditor
from pagewatch.ui.windows.failed_actions import FailedActionsDialog
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.notification_settings import NotificationSettingsDialog
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


# -- Notifications ------------------------------------------------------------------------------


def start_servers(eng: EngineThread) -> tuple[SmtpServer, HttpSink]:
    smtp = eng.call(SmtpServer().start())
    sink = eng.call(HttpSink().start())
    return smtp, sink


def test_notifications_dialog_saves_settings_and_keeps_the_password_in_the_secret_store(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    smtp, sink = start_servers(eng)
    dialog = NotificationSettingsDialog(api_client, api_client.settings())
    pump_until(lambda: dialog._secrets_ok, what="secret store check")
    dialog.smtp_host.setText("127.0.0.1")
    dialog.smtp_security.setCurrentIndex(dialog.smtp_security.findData("none"))
    dialog.smtp_port.setValue(smtp.port)
    dialog.smtp_user.setText("me")
    dialog.smtp_password.setText("hunter2")
    dialog.smtp_from.setText("pw@example.com")
    dialog.smtp_to.setText("a@example.com, b@example.com")
    dialog.ntfy_server.setText(sink.base)
    dialog.ntfy_topic.setText("alerts-1")
    dialog.ntfy_token.setText("tk_secret")
    saved: list[bool] = []
    dialog.saved.connect(lambda: saved.append(True))
    dialog.save()
    pump_until(lambda: bool(saved), what="save")

    n = api_client.settings().notifications
    assert (n.smtp.host, n.smtp.port, n.smtp.username) == ("127.0.0.1", smtp.port, "me")
    assert n.smtp.to == ["a@example.com", "b@example.com"] and n.ntfy.topic == "alerts-1"
    assert n.ntfy.token_key == "ntfy-token"
    assert eng.engine.secrets.get("smtp-password") == "hunter2"  # in the secret store...
    assert eng.engine.secrets.get("ntfy-token") == "tk_secret"
    dumped = api_client.settings().model_dump_json()
    assert "hunter2" not in dumped and "tk_secret" not in dumped  # ...and nowhere else
    reopened = NotificationSettingsDialog(api_client, api_client.settings())
    assert reopened.smtp_password.text() == "" and reopened.smtp_host.text() == "127.0.0.1"


def test_send_test_buttons_save_first_then_report_the_outcome(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    smtp, sink = start_servers(eng)
    dialog = NotificationSettingsDialog(api_client, api_client.settings())
    dialog.smtp_host.setText("127.0.0.1")
    dialog.smtp_security.setCurrentIndex(dialog.smtp_security.findData("none"))
    dialog.smtp_port.setValue(smtp.port)
    dialog.smtp_from.setText("pw@example.com")
    dialog.smtp_to.setText("me@example.com")
    dialog.ntfy_server.setText(sink.base)
    dialog.ntfy_topic.setText("alerts-1")
    dialog.smtp_test.click()
    pump_until(lambda: dialog.smtp_result.text().startswith("Sent"), what="email test")
    assert len(smtp.messages) == 1 and smtp.messages[0].rcpt_to == ["me@example.com"]
    dialog.ntfy_test.click()
    pump_until(lambda: dialog.ntfy_result.text().startswith("Sent"), what="ntfy test")
    assert sink.requests[0].json()["topic"] == "alerts-1"

    smtp.reject = "554 no thanks"
    dialog.smtp_test.click()
    pump_until(lambda: "554" in dialog.smtp_result.text(), what="a failing test")
    assert dialog.smtp_result.styleSheet().endswith("#b00020")  # shown as a failure


def test_notifications_dialog_warns_about_the_public_ntfy_server(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication
) -> None:
    dialog = NotificationSettingsDialog(api_client, api_client.settings())
    assert "public server" in dialog.ntfy_warning.text()  # ntfy.sh is the default
    dialog.ntfy_server.setText("https://ntfy.example.org")
    assert dialog.ntfy_warning.text() == ""


def test_without_a_credential_store_the_dialog_says_so_and_locks_the_password_fields(
    tmp_path: Path, qapp: QApplication, pump_until: Callable[..., None]
) -> None:
    et = EngineThread(tmp_path / "null", secret_store=NullSecrets()).start()
    client = ApiClient(et.base, et.engine.token)
    try:
        dialog = NotificationSettingsDialog(client, client.settings())
        pump_until(lambda: not dialog.smtp_password.isEnabled(), what="secret store check")
        assert "no secure credential store" in dialog.secret_note.text()
        assert not dialog.ntfy_token.isEnabled()
    finally:
        wait_idle(2000)
        client.close()
        et.stop()


# -- Problems -----------------------------------------------------------------------------------


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


def test_failed_actions_dialog_lists_retries_and_dismisses(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    fail_an_action(eng, api_client)
    dialog = FailedActionsDialog(api_client)
    pump_until(lambda: dialog.table.rowCount() == 1, what="the list")
    row = [dialog.table.item(0, c).text() for c in range(5)]
    assert row[1] == "Tea shop" and row[2] == "pushover" and row[3] == "1"
    assert "pushover action is not available" in row[4]

    dialog.retry.click()  # nothing selected yet
    assert "Select" in dialog.status.text()
    dialog.table.selectRow(0)
    dialog.retry.click()
    pump_until(lambda: dialog.status.text().startswith("Queued again"), what="retry")
    eng.settle()  # it fails again, immediately, for the same reason
    dialog.refresh_button.click()
    pump_until(lambda: dialog.table.rowCount() == 1, what="the list again")

    dialog.table.selectRow(0)
    dialog.dismiss.click()
    pump_until(lambda: dialog.table.rowCount() == 0, what="an empty list")
    assert dialog.status.text() == "No failed actions."
    assert not dialog.retry.isEnabled() and not dialog.dismiss.isEnabled()


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


def test_main_window_has_notifications_and_a_live_problems_count(
    eng: EngineThread, api_client: ApiClient, window: MainWindow, pump_until: Callable[..., None]
) -> None:
    assert window.tools.act_notifications.text() == "Notifications..."
    assert window.tools.act_problems.text() == "Problems"
    fail_an_action(eng, api_client)
    pump_until(lambda: window.tools.act_problems.text() == "Problems (1)", what="the count")
    pump_until(lambda: "An action failed" in window.lbl_message.text(), what="a status message")
    assert "pushover action is not available" in window.lbl_message.text()


def test_main_window_opens_the_problems_and_notifications_dialogs(
    eng: EngineThread, api_client: ApiClient, window: MainWindow, pump_until: Callable[..., None]
) -> None:
    window.tools.act_problems.trigger()
    pump_until(lambda: isinstance(window.tools.dialog, FailedActionsDialog), what="Problems")
    assert window.tools.dialog.isVisible()
    window.tools.dialog.close()
    window.tools.act_notifications.trigger()
    pump_until(
        lambda: isinstance(window.tools.dialog, NotificationSettingsDialog), what="Notifications"
    )
    assert window.tools.dialog.isVisible()
    window.tools.dialog.close()
