"""Settings, Notifications: the SMTP server for email actions and the ntfy server and topic.

Passwords and tokens never go into the database: they are written to the engine's secret store
(Windows Credential Manager) under the key names shown here, and an empty password field means
"keep what is stored". "Send test" saves first, then asks the engine to send a sample through the
saved settings, so a wrong server or password shows up now and not at the first real alert.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from pagewatch.models import Settings
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

SECURITY = [("STARTTLS (port 587)", "starttls"), ("SSL/TLS (port 465)", "ssl"),
            ("None (only for a relay on this PC)", "none")]  # fmt: skip
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}


def _csv(text: str) -> list[str]:
    return [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]


class NotificationSettingsDialog(QDialog):
    saved = Signal()
    failed = Signal(str)
    test_done = Signal(str, bool, str)  # channel, ok, detail

    def __init__(self, client: ApiClient, current: Settings, parent: Any = None) -> None:
        super().__init__(parent)
        self._client = client
        self._n = current.notifications
        self.setWindowTitle("Notifications")
        self.resize(560, 560)
        self._secrets_ok = True

        smtp = self._n.smtp
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
        self.smtp_result = QLabel("")
        self.smtp_result.setWordWrap(True)

        ntfy = self._n.ntfy
        self.ntfy_server = QLineEdit(ntfy.server)
        self.ntfy_topic = QLineEdit(ntfy.topic)
        self.ntfy_topic.setPlaceholderText("your-private-topic-name")
        self.ntfy_token = QLineEdit()
        self.ntfy_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.ntfy_token.setPlaceholderText("blank: keep the stored token (optional)")
        self.ntfy_test = QPushButton("Send test notification")
        self.ntfy_result = QLabel("")
        self.ntfy_result.setWordWrap(True)
        self.ntfy_warning = QLabel("")
        self.ntfy_warning.setWordWrap(True)
        self.ntfy_warning.setStyleSheet("color: #8a5a00")

        self.secret_note = QLabel(
            "Passwords and tokens are kept in the Windows Credential Manager, never in the "
            "database, exports or backups."
        )
        self.secret_note.setWordWrap(True)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #b00020")
        self.error.setWordWrap(True)

        mail = QGroupBox("Email (SMTP)")
        mform = QFormLayout(mail)
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

        push = QGroupBox("ntfy")
        nform = QFormLayout(push)
        nform.addRow("Server", self.ntfy_server)
        nform.addRow("Topic", self.ntfy_topic)
        nform.addRow("Access token", self.ntfy_token)
        nform.addRow(self.ntfy_warning)
        nrow = QHBoxLayout()
        nrow.addWidget(self.ntfy_test)
        nrow.addWidget(self.ntfy_result, 1)
        nform.addRow(nrow)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(mail)
        layout.addWidget(push)
        layout.addWidget(self.secret_note)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

        self.smtp_security.currentIndexChanged.connect(self._security_changed)
        self.ntfy_server.textChanged.connect(self._ntfy_warning)
        self.smtp_test.clicked.connect(lambda: self.send_test("email"))
        self.ntfy_test.clicked.connect(lambda: self.send_test("ntfy"))
        self.test_done.connect(self._show_test)
        self.failed.connect(self.error.setText)
        self._ntfy_warning()
        run_async(
            lambda: client.secret_status(self._n.smtp.password_key),
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
            self.secret_note.setText(
                "This computer has no secure credential store, so passwords cannot be saved. "
                "Email with a login and ntfy tokens will not work here."
            )
            self.smtp_password.setEnabled(False)
            self.ntfy_token.setEnabled(False)

    def settings_patch(self) -> dict[str, Any]:
        smtp = self._n.smtp
        ntfy = self._n.ntfy
        return {
            "notifications": {
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
                    "token_key": ntfy.token_key
                    or ("ntfy-token" if self.ntfy_token.text() else None),
                },
            }
        }

    def _save_blocking(self) -> Settings:
        """Worker thread: secrets first (a failure there must not leave half the settings)."""
        patch = self.settings_patch()
        if self.smtp_password.text():
            self._client.set_secret(patch["notifications"]["smtp"]["password_key"],
                                    self.smtp_password.text())  # fmt: skip
        if self.ntfy_token.text():
            self._client.set_secret(patch["notifications"]["ntfy"]["token_key"],
                                    self.ntfy_token.text())  # fmt: skip
        return self._client.put_settings(patch)

    def save(self) -> None:
        self.error.setText("")
        run_async(self._save_blocking, self._saved, lambda e: self.failed.emit(str(e)))

    def _saved(self, settings: Settings) -> None:
        self._n = settings.notifications
        self.smtp_password.clear()
        self.ntfy_token.clear()
        self.saved.emit()
        self.accept()

    def send_test(self, channel: str) -> None:
        """Save, then send a sample through the saved settings."""
        self.error.setText("")
        label = self.smtp_result if channel == "email" else self.ntfy_result
        label.setText("Sending...")

        def work() -> tuple[bool, str]:
            self._save_blocking()
            out = self._client.test_notification(channel)
            return out.ok, out.detail

        run_async(
            work,
            lambda r: self.test_done.emit(channel, r[0], r[1]),
            lambda e: self.test_done.emit(channel, False, str(e)),
        )

    def _show_test(self, channel: str, ok: bool, detail: str) -> None:
        label = self.smtp_result if channel == "email" else self.ntfy_result
        label.setStyleSheet("color: #1b6e2d" if ok else "color: #b00020")
        label.setText(detail)
