"""Settings, Email and Notifications: the SMTP server for email actions and the ntfy server.

``NotificationsEditor`` builds the two group boxes and the logic behind them; the Settings window
places ``smtp_box`` on its Email page and ``ntfy_box`` on its Notifications page, and owns saving
(one ``PUT /settings`` for everything that changed, as for every other setting).

Passwords and tokens never go into the database: they are written to the engine's secret store
(Windows Credential Manager) under the key names the settings hold, and an empty password field
means "keep what is stored". The window writes them *before* the settings, so a failure there does
not leave settings that point at a secret that is not there. "Send test" saves the notification
settings first, then asks the engine to send a sample through the saved settings, so a wrong server
or password shows up now and not at the first real alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
)

from pagewatch.models import NotificationSettings, Settings
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

SECURITY = [("STARTTLS (port 587)", "starttls"), ("SSL/TLS (port 465)", "ssl"),
            ("None (only for a relay on this PC)", "none")]  # fmt: skip
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
NO_STORE = (
    "This computer has no secure credential store, so passwords cannot be saved. "
    "Email with a login and ntfy tokens will not work here."
)
KEPT_IN_STORE = (
    "Passwords and tokens are kept in the Windows Credential Manager, never in the "
    "database, exports or backups."
)
NTFY_TOKEN_KEY = (
    "ntfy-token"  # the secret-store name used when a token is entered for the first time
)


def _csv(text: str) -> list[str]:
    return [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]


@dataclass(frozen=True)
class PendingSecret:
    """A password or token the user typed and that has not been written to the secret store."""

    name: str
    value: str
    field: QLineEdit


def write_secrets(client: ApiClient, secrets: list[PendingSecret]) -> None:
    """Blocking (call it off the GUI thread). Raises on the first failure."""
    for secret in secrets:
        client.set_secret(secret.name, secret.value)


class NotificationsEditor(QObject):
    tested = Signal(str, bool, str)  # channel, ok, detail
    saved_by_test = Signal(object, object)  # the Settings the engine now has, what was sent

    def __init__(
        self, client: ApiClient, current: NotificationSettings, parent: Any = None
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._n = current
        self._secrets_ok = True

        smtp = current.smtp
        self.smtp_host = QLineEdit(smtp.host)
        self.smtp_host.setPlaceholderText("smtp.example.com")
        self.smtp_security = QComboBox()
        for label, value in SECURITY:
            self.smtp_security.addItem(label, value)
        self.smtp_security.setCurrentIndex(max(0, self.smtp_security.findData(smtp.security)))
        self.smtp_port = QSpinBox()
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(smtp.port)
        self.smtp_user = QLineEdit(smtp.username or "")
        self.smtp_password = QLineEdit()
        self.smtp_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.smtp_password.setPlaceholderText("blank: keep the stored password")
        self.smtp_from = QLineEdit(smtp.from_addr)
        self.smtp_from.setPlaceholderText("PageWatch <you@example.com>")
        self.smtp_to = QLineEdit(", ".join(smtp.to))
        self.smtp_to.setPlaceholderText("default recipients, comma separated")
        self.smtp_verify = QCheckBox("Verify the server's certificate")
        self.smtp_verify.setChecked(smtp.verify_tls)
        self.smtp_test = QPushButton("Send test email")
        self.smtp_test.setToolTip("Saves the email and ntfy settings, then sends a sample message")
        self.smtp_result = QLabel("")
        self.smtp_result.setWordWrap(True)
        self.secret_note = QLabel(KEPT_IN_STORE)
        self.secret_note.setWordWrap(True)

        ntfy = current.ntfy
        self.ntfy_server = QLineEdit(ntfy.server)
        self.ntfy_topic = QLineEdit(ntfy.topic)
        self.ntfy_topic.setPlaceholderText("your-private-topic-name")
        self.ntfy_token = QLineEdit()
        self.ntfy_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.ntfy_token.setPlaceholderText("blank: keep the stored token (optional)")
        self.ntfy_test = QPushButton("Send test notification")
        self.ntfy_test.setToolTip("Saves the email and ntfy settings, then sends a sample")
        self.ntfy_result = QLabel("")
        self.ntfy_result.setWordWrap(True)
        self.ntfy_warning = QLabel("")
        self.ntfy_warning.setWordWrap(True)
        self.ntfy_warning.setStyleSheet("color: #8a5a00")
        self.ntfy_secret_note = QLabel(KEPT_IN_STORE)
        self.ntfy_secret_note.setWordWrap(True)

        self.smtp_box = QGroupBox("Email (SMTP)")
        mform = QFormLayout(self.smtp_box)
        mform.addRow("Server", self.smtp_host)
        mform.addRow("Security", self.smtp_security)
        mform.addRow("Port", self.smtp_port)
        mform.addRow("User name", self.smtp_user)
        mform.addRow("Password", self.smtp_password)
        mform.addRow("From", self.smtp_from)
        mform.addRow("To", self.smtp_to)
        mform.addRow(self.smtp_verify)
        row = QHBoxLayout()
        row.addWidget(self.smtp_test)
        row.addWidget(self.smtp_result, 1)
        mform.addRow(row)
        mform.addRow(self.secret_note)

        self.ntfy_box = QGroupBox("ntfy (push notifications)")
        nform = QFormLayout(self.ntfy_box)
        nform.addRow("Server", self.ntfy_server)
        nform.addRow("Topic", self.ntfy_topic)
        nform.addRow("Access token", self.ntfy_token)
        nform.addRow(self.ntfy_warning)
        nrow = QHBoxLayout()
        nrow.addWidget(self.ntfy_test)
        nrow.addWidget(self.ntfy_result, 1)
        nform.addRow(nrow)
        nform.addRow(self.ntfy_secret_note)

        self.smtp_security.currentIndexChanged.connect(self._security_changed)
        self.ntfy_server.textChanged.connect(self._ntfy_warning)
        self.smtp_test.clicked.connect(lambda: self.send_test("email"))
        self.ntfy_test.clicked.connect(lambda: self.send_test("ntfy"))
        self.tested.connect(self._show_test)
        self._ntfy_warning()
        run_async(
            lambda: client.secret_status(current.smtp.password_key),
            lambda s: self._set_secrets_available(s.available),
            lambda _e: None,
        )

    # -- behaviour ----------------------------------------------------------------------

    def _security_changed(self) -> None:
        # follow the usual port while the user has not typed another one
        if self.smtp_port.value() in DEFAULT_PORTS.values():
            self.smtp_port.setValue(DEFAULT_PORTS[self.smtp_security.currentData()])

    def _ntfy_warning(self) -> None:
        public = "ntfy.sh" in self.ntfy_server.text().lower()
        self.ntfy_warning.setText(
            "ntfy.sh is a public server: anyone who learns the topic name can read these "
            "messages. Choose a long random topic, or use a self-hosted server. Mark "
            "bookmarks 'private' so alerts carry no page content."
            if public
            else ""
        )

    def _set_secrets_available(self, ok: bool) -> None:
        self._secrets_ok = ok
        if not ok:
            self.secret_note.setText(NO_STORE)
            self.ntfy_secret_note.setText(NO_STORE)
            self.smtp_password.setEnabled(False)
            self.ntfy_token.setEnabled(False)

    # -- what the Settings window needs ---------------------------------------------------

    def read(self) -> dict[str, Any]:
        """The values in the controls, shaped like ``Settings.notifications``. The window compares
        this with what it opened with; passwords are never part of it (see ``pending_secrets``)."""
        smtp, ntfy = self._n.smtp, self._n.ntfy
        return {
            "smtp": {
                "host": self.smtp_host.text().strip(),
                "port": self.smtp_port.value(),
                "security": self.smtp_security.currentData(),
                "username": self.smtp_user.text().strip() or None,
                "password_key": smtp.password_key,
                "from_addr": self.smtp_from.text().strip(),
                "to": _csv(self.smtp_to.text()),
                "verify_tls": self.smtp_verify.isChecked(),
            },
            "ntfy": {
                "server": self.ntfy_server.text().strip() or ntfy.server,
                "topic": self.ntfy_topic.text().strip(),
                "token_key": ntfy.token_key or (NTFY_TOKEN_KEY if self.ntfy_token.text() else None),
            },
        }

    def pending_secrets(self) -> list[PendingSecret]:
        """Passwords and tokens that were typed since they were last saved."""
        out: list[PendingSecret] = []
        if self.smtp_password.text():
            out.append(
                PendingSecret(
                    self._n.smtp.password_key, self.smtp_password.text(), self.smtp_password
                )
            )
        token_key = self.read()["ntfy"]["token_key"]
        if self.ntfy_token.text() and token_key:
            out.append(PendingSecret(token_key, self.ntfy_token.text(), self.ntfy_token))
        return out

    def saved(self, settings: Settings, sent: list[PendingSecret]) -> None:
        """The engine has the settings (and the secrets in ``sent``): forget the typed secrets,
        unless the user has typed something else since, and pick up the stored key names."""
        self._n = settings.notifications
        for secret in sent:
            if secret.field.text() == secret.value:
                secret.field.clear()

    # -- send test ----------------------------------------------------------------------

    def send_test(self, channel: str) -> None:
        """Save the notification settings, then send a sample through the saved settings."""
        label = self.smtp_result if channel == "email" else self.ntfy_result
        label.setText("Sending...")
        secrets = self.pending_secrets()
        sent = self.read()

        def work() -> tuple[Settings, bool, str]:
            write_secrets(self._client, secrets)
            saved = self._client.put_settings({"notifications": sent})
            try:
                out = self._client.test_notification(channel)
            except Exception as exc:  # the settings were saved; only the sample failed
                return saved, False, str(exc)
            return saved, out.ok, out.detail

        def done(result: tuple[Settings, bool, str]) -> None:
            saved, ok, detail = result
            self.saved(saved, secrets)
            self.saved_by_test.emit(saved, sent)
            self.tested.emit(channel, ok, detail)

        run_async(work, done, lambda e: self.tested.emit(channel, False, str(e)))

    def _show_test(self, channel: str, ok: bool, detail: str) -> None:
        label = self.smtp_result if channel == "email" else self.ntfy_result
        label.setStyleSheet("color: #1b6e2d" if ok else "color: #b00020")
        label.setText(detail)
