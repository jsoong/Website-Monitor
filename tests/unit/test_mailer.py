"""The SMTP transport arguments and the parts of a message that do not need an engine."""

from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any

import pytest

from pagewatch.engine.actions import mailer
from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.secrets import MemorySecrets
from pagewatch.models import SmtpSettings


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send(self, message: EmailMessage, **kwargs: Any) -> None:
        self.calls.append({"message": message, **kwargs})


@pytest.fixture
def smtp_send(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(mailer.aiosmtplib, "send", rec.send)
    return rec


def engine_with(**secrets: str) -> Any:
    return SimpleNamespace(secrets=MemorySecrets(secrets))


def message() -> EmailMessage:
    m = EmailMessage()
    m["From"] = "PageWatch <pw@example.com>"
    m["To"] = "a@example.com, B <b@example.com>"
    m["Subject"] = "x"
    m.set_content("hi")
    return m


@pytest.mark.parametrize(
    ("security", "use_tls", "start_tls"),
    [("ssl", True, False), ("starttls", False, True), ("none", False, False)],
)
async def test_tls_mode_is_never_left_to_opportunistic_negotiation(
    security: str, use_tls: bool, start_tls: bool, smtp_send: Recorder
) -> None:
    cfg = SmtpSettings(host="mail.example.com", port=587, security=security)  # type: ignore[arg-type]
    await mailer.deliver(engine_with(), message(), cfg)
    [call] = smtp_send.calls
    # start_tls=False (not None): aiosmtplib must not "upgrade if offered" behind our back
    assert (call["use_tls"], call["start_tls"]) == (use_tls, start_tls)
    assert call["hostname"] == "mail.example.com" and call["port"] == 587
    assert call["sender"] == "pw@example.com"
    assert call["recipients"] == ["a@example.com", "b@example.com"]
    assert call["validate_certs"] is True and call["timeout"] == 30.0


async def test_certificate_checks_can_be_turned_off_per_server(smtp_send: Recorder) -> None:
    cfg = SmtpSettings(host="mail.example.com", verify_tls=False)
    await mailer.deliver(engine_with(), message(), cfg)
    assert smtp_send.calls[0]["validate_certs"] is False


async def test_the_password_comes_from_the_secret_store(smtp_send: Recorder) -> None:
    cfg = SmtpSettings(host="mail.example.com", username="me", password_key="my-smtp")
    await mailer.deliver(engine_with(**{"my-smtp": "hunter2"}), message(), cfg)
    call = smtp_send.calls[0]
    assert (call["username"], call["password"]) == ("me", "hunter2")


async def test_no_username_means_no_login(smtp_send: Recorder) -> None:
    await mailer.deliver(engine_with(), message(), SmtpSettings(host="localhost", security="none"))
    assert smtp_send.calls[0]["username"] is None and smtp_send.calls[0]["password"] is None


async def test_a_missing_password_is_a_configuration_error_not_an_empty_login(
    smtp_send: Recorder,
) -> None:
    cfg = SmtpSettings(host="mail.example.com", username="me")
    with pytest.raises(ActionConfigError, match="no stored password named 'smtp-password'"):
        await mailer.deliver(engine_with(), message(), cfg)
    assert smtp_send.calls == []


@pytest.mark.parametrize("host", ["mail.example.com", "10.1.2.3", "192.168.1.5"])
async def test_a_password_is_never_sent_without_tls_to_another_machine(
    host: str, smtp_send: Recorder
) -> None:
    cfg = SmtpSettings(host=host, security="none", username="me")
    with pytest.raises(ActionConfigError, match="refusing to send a password without TLS"):
        await mailer.deliver(engine_with(**{"smtp-password": "x"}), message(), cfg)
    assert smtp_send.calls == []


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
async def test_a_local_relay_may_skip_tls_even_with_a_login(host: str, smtp_send: Recorder) -> None:
    cfg = SmtpSettings(host=host, security="none", username="me")
    await mailer.deliver(engine_with(**{"smtp-password": "x"}), message(), cfg)
    assert len(smtp_send.calls) == 1


async def test_no_server_is_the_first_thing_reported(smtp_send: Recorder) -> None:
    with pytest.raises(ActionConfigError, match="no SMTP server"):
        await mailer.deliver(engine_with(), message(), SmtpSettings())


# -- small pieces ---------------------------------------------------------------------------


def alert(**over: Any) -> AlertContext:
    fields: dict[str, Any] = {
        "bookmark_id": 3, "name": "Rent", "url": "https://x/", "info": (None, None, None),
        "change_id": 9, "detected_at": "2026-11-05T14:30:07.000000Z", "summary": "s",
        "added_words": 3, "removed_words": 1, "changed_blocks": 2, "checks_accumulated": 1,
        "keyword_hits": [], "private": False,
    }  # fmt: skip
    return AlertContext(**{**fields, **over})


def test_message_id_is_stable_per_change_and_recipients_and_differs_otherwise() -> None:
    a = mailer._message_id(alert(), ["a@x.com", "b@x.com"], "pw@example.com", "simple")
    assert a == mailer._message_id(alert(), ["b@x.com", "a@x.com"], "pw@example.com", "simple")
    assert a.startswith("<pagewatch.9.3.") and a.endswith("@example.com>")
    assert a != mailer._message_id(
        alert(change_id=10), ["a@x.com", "b@x.com"], "pw@x.com", "simple"
    )
    assert a != mailer._message_id(alert(), ["a@x.com"], "pw@example.com", "simple")


def test_stats_line() -> None:
    assert mailer._stats_line(alert(checks_accumulated=4)) == (
        "3 words added, 1 removed, 2 blocks changed, over 4 checks"
    )
    assert mailer._stats_line(alert(added_words=None, removed_words=0, changed_blocks=None)) == ""


@pytest.mark.parametrize(
    ("host", "local"),
    [("localhost", True), ("LOCALHOST", True), ("127.0.0.1", True), ("::1", True),
     ("mail.example.com", False), ("10.0.0.1", False), ("", False)],
)  # fmt: skip
def test_is_local_host(host: str, local: bool) -> None:
    assert mailer._is_local_host(host) is local
