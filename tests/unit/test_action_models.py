"""Parameter models for the M6 actions and the notification settings."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from pagewatch.models import (
    ACTION_PARAMS,
    ActionConfig,
    ActionsConfig,
    ActionType,
    EmailParams,
    ExportParams,
    NtfyParams,
    RunProgramParams,
    ScrapeParams,
    ScriptParams,
    Settings,
    SmtpSettings,
    WebhookParams,
)


def action(kind: str, **params: Any) -> ActionConfig:
    return ActionConfig(type=ActionType(kind), params=params)


# -- which actions are validated ------------------------------------------------------------


def test_every_m6_action_has_a_parameter_model_and_the_old_ones_stay_free_form() -> None:
    assert {t.value for t in ACTION_PARAMS} == {
        "email", "ntfy", "webhook", "export", "run_program", "scrape", "script",
    }  # fmt: skip
    # toast / sound / open / mark_read / pushover keep their free-form dicts
    assert action("toast").params == {}
    assert action("sound", path="x.wav").params == {"path": "x.wav"}
    assert action("open", target="external").params == {"target": "external"}


def test_a_bare_email_action_is_valid_because_recipients_may_come_from_settings() -> None:
    assert EmailParams.model_validate({}).format == "changes_only"
    action("email")


@pytest.mark.parametrize(
    ("kind", "params", "message"),
    [
        ("export", {}, "path"),
        ("export", {"path": "{nope}.html"}, "unknown variable {nope}"),
        ("export", {"path": "{date}.html"}, "needs a format"),
        ("export", {"path": "{name:%Y}.html"}, "only {date:<format>} takes a format"),
        ("export", {"path": "a{b"}, "unmatched"),
        ("webhook", {}, "url"),
        ("webhook", {"url": "ftp://x/y"}, "http"),
        ("webhook", {"url": "https://x/y", "body": "{{ unclosed"}, "body"),
        ("webhook", {"url": "https://x/y", "headers": {"Host": "evil"}}, "not allowed"),
        ("webhook", {"url": "https://x/y", "headers": {"X-A": "a\r\nB: c"}}, "line break"),
        ("email", {"to": ["not an address"]}, "not an email address"),
        ("email", {"subject": "{% if %}"}, "subject"),
        ("ntfy", {"topic": "has space"}, "topic"),
        ("ntfy", {"server": "ntfy.sh"}, "http"),
        ("ntfy", {"token_key": "bad key!"}, "secret name"),
        ("run_program", {"executable": "x"}, "explicit confirmation"),
        ("run_program", {"executable": "x", "confirmed": True, "args": ["{nope}"]}, "unknown"),
        ("scrape", {"path": "a.csv", "fields": []}, "fields"),
        ("scrape", {"path": "a.csv", "fields": [{"name": "p", "kind": "css", "expr": "div["}]}, "css"),
        ("scrape", {"path": "a.csv", "fields": [{"name": "p", "kind": "regex", "expr": "("}]}, "regex"),
        ("scrape", {"path": "a.csv", "fields": [{"name": "url", "kind": "css", "expr": "p"}]}, "adds itself"),
        ("scrape", {"path": "a.csv", "fields": [{"name": "1x", "kind": "css", "expr": "p"}]}, "identifier"),
        ("script", {"steps": []}, "steps"),
        ("script", {"steps": [{"op": "check_folder"}]}, "needs a folder"),
        ("script", {"steps": [{"op": "pause_bookmark"}]}, "needs 'minutes'"),
    ],
)  # fmt: skip
def test_bad_parameters_are_rejected_with_a_message_naming_the_action(
    kind: str, params: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        action(kind, **params)
    text = str(exc.value)
    assert f"{kind} action" in text
    assert message in text


def test_duplicate_scrape_field_names_are_rejected() -> None:
    f = {"name": "price", "kind": "css", "expr": "p"}
    with pytest.raises(ValidationError, match="unique"):
        ScrapeParams.model_validate({"path": "a.csv", "fields": [f, f]})


def test_valid_parameters_round_trip_through_the_actions_config() -> None:
    cfg = ActionsConfig.model_validate(
        {
            "actions": [
                {"type": "email", "params": {"to": ["me@example.com"], "format": "simple"}},
                {"type": "ntfy", "params": {"topic": "abc-123", "priority": "high"}},
                {"type": "webhook", "params": {"url": "https://h/x", "body": '{"a": {{ 1|tojson }}}'}},
                {"type": "export", "params": {"path": "{name}/{date:%Y-%m-%d}.html"}},
                {"type": "run_program", "params": {"executable": "p", "confirmed": True}},
                {"type": "scrape", "params": {"path": "p.csv",
                                              "fields": [{"name": "a", "kind": "xpath", "expr": "//p"}]}},
                {"type": "script", "params": {"steps": [{"op": "pause_bookmark", "minutes": 5}]}},
                {"type": "mark_read"},
            ]
        }
    )  # fmt: skip
    assert [a.type.value for a in cfg.actions][-1] == "mark_read"
    assert cfg.actions[1].params == {"topic": "abc-123", "priority": "high"}  # stored sparse


def test_defaults_are_filled_when_the_action_runs() -> None:
    p = NtfyParams.model_validate({"topic": "t"})
    assert (p.priority, p.tags, p.server) == ("default", [], None)
    e = ExportParams.model_validate({"path": "x.html"})
    assert (e.format, e.highlight) == ("html", True)
    r = RunProgramParams.model_validate({"executable": "p", "confirmed": True})
    assert (r.timeout_s, r.args) == (60.0, [])
    w = WebhookParams.model_validate({"url": "https://h/"})
    assert (w.method, w.body, w.verify_tls) == ("POST", None, True)
    s = ScriptParams.model_validate({"steps": [{"op": "check_folder", "name": " News "}]})
    assert s.steps[0].name == " News "


def test_unknown_parameter_names_are_rejected() -> None:
    with pytest.raises(ValidationError):
        action("email", colour="red")


# -- notification settings ------------------------------------------------------------------


def test_notification_settings_default_to_nothing_configured() -> None:
    n = Settings().notifications
    assert n.smtp.host == "" and n.smtp.security == "starttls" and n.smtp.port == 587
    assert n.smtp.password_key == "smtp-password"
    assert n.ntfy.server == "https://ntfy.sh" and n.ntfy.topic == ""


def test_notification_settings_validate_and_normalise() -> None:
    s = Settings.model_validate(
        {
            "notifications": {
                "smtp": {"host": " smtp.example.com ", "from_addr": "PageWatch <me@example.com>",
                         "to": ["a@example.com"]},
                "ntfy": {"server": "https://ntfy.example.org/", "topic": "my-topic"},
            }
        }
    )  # fmt: skip
    assert s.notifications.smtp.host == "smtp.example.com"
    assert s.notifications.ntfy.server == "https://ntfy.example.org"  # no trailing slash


@pytest.mark.parametrize(
    "patch",
    [
        {"smtp": {"security": "plain"}},
        {"smtp": {"port": 0}},
        {"smtp": {"host": "smtp.example.com/x"}},
        {"smtp": {"from_addr": "nobody"}},
        {"smtp": {"to": ["bad"]}},
        {"smtp": {"password_key": "has space"}},
        {"ntfy": {"server": "ftp://x"}},
        {"ntfy": {"topic": "a b"}},
        {"ntfy": {"token_key": "a b"}},
        {"smtp": {"password": "hunter2"}},  # passwords are never settings
    ],
)
def test_bad_notification_settings_are_rejected(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"notifications": patch})


def test_no_setting_can_hold_a_password() -> None:
    names = set(SmtpSettings.model_fields)
    assert "password" not in names and "password_key" in names
