"""The bookmark editor's Login tab (spec: Bookmark editor -> Login; Logins and Check-Macros;
Portal strategy).

Four independent ways to be signed in, any of which a bookmark can use:

* **Server login** (HTTP Basic or Digest): a user name and a stored password.
* **Cookies**: a ``cookies.txt`` exported from a browser where you are signed in.
* **Macro**: recorded steps that log in when the site shows its login page.
* **Sign in by hand** (semi-attended): "Sign in..." opens a visible browser window on the
  bookmark's own profile; you log in, including any one-time code, and PageWatch checks at once.

Passwords go to the engine's credential store and are never read back: the field is emptied after
saving, and the page only ever learns whether a password is stored. The tab edits two parts of the
configuration (``fetch.auth`` / ``fetch.login``, and ``macro_id``) and reports them to the editor,
which sends them with the rest of its patch.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pagewatch.models import BookmarkOut, CookieStatusOut, LoginStatusOut, MacroOut, Settings
from pagewatch.ui.client import ApiClient
from pagewatch.ui.windows.macros import MacrosDialog
from pagewatch.ui.workers import run_async

REASON_TEXT = {
    "login_signal": "the site showed its login page",
    "auth_rejected": "the site rejected the saved login or cookies",
    "macro_failed": "the macro did not get past the login page",
    "stored_password_off": "signing in with a stored password is switched off",
    "login_rate_limited": "an automatic sign-in already ran within the last hours",
    "login_blocked": "automatic sign-in stopped after a failure",
}


def reason_sentence(reason: str | None) -> str:
    if not reason:
        return ""
    code, _, detail = reason.partition(":")
    text = REASON_TEXT.get(code, code)
    return f"{text} ({detail.strip()})" if detail.strip() else text


def status_text(st: LoginStatusOut) -> str:
    """One paragraph: how this bookmark signs in, and whether it needs you."""
    modes = {
        "none": "No sign-in is set up.",
        "server_auth": "Signs in with a stored user name and password (HTTP).",
        "cookies": "Uses cookies you imported.",
        "macro": "Signs in with a macro when the site shows its login page.",
        "semi_attended": "Signs in by hand: you log in in a browser window.",
    }
    parts = [modes[st.signed_in_mode]]
    if st.needs_login:
        why = reason_sentence(st.reason)
        parts.append("NEEDS YOU: checks are paused" + (f" because {why}." if why else "."))
    if st.last_signin_at:
        parts.append(f"Last signed in by hand: {st.last_signin_at}.")
    if st.stored_password:
        if st.stored_password_allowed:
            parts.append("Automatic sign-in with a stored password is on for this bookmark.")
        else:
            parts.append("Automatic sign-in with a stored password is off (see below).")
        if st.auto_login_blocked:
            parts.append("Automatic sign-in stopped after a failure; fix it, then Resume checks.")
        elif st.next_auto_login_at:
            parts.append(f"The next automatic sign-in is allowed after {st.next_auto_login_at}.")
    if st.signing_in:
        parts.append("A sign-in window is open.")
    return " ".join(parts)


def cookie_text(c: CookieStatusOut) -> str:
    if not c.present:
        return "No cookies imported."
    bits = [f"{c.count} cookie(s) for {', '.join(c.domains[:3]) or 'no site'}"]
    if c.expired:
        bits.append(f"{c.expired} expired")
    if c.earliest_expiry:
        bits.append(f"the first expires {c.earliest_expiry}")
    if c.skipped_lines:
        bits.append(f"{c.skipped_lines} line(s) could not be read")
    return "; ".join(bits) + "."


class LoginPanel(QWidget):
    def __init__(
        self, client: ApiClient, bookmark: BookmarkOut, settings: Settings | None = None,
        parent: Any = None,
    ) -> None:  # fmt: skip
        super().__init__(parent)
        self._client = client
        self._b = bookmark
        self._settings = settings
        self._macros: list[MacroOut] = []
        f = bookmark.fetch

        # -- status and the session buttons ------------------------------------------
        self.status = QLabel("Loading...")
        self.status.setWordWrap(True)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.btn_signin = QPushButton("Sign in...")
        self.btn_finish = QPushButton("I'm signed in")
        self.btn_resume = QPushButton("Resume checks")
        self.btn_wipe = QPushButton("Wipe session")
        self.btn_signin.setToolTip("Open a browser window on this bookmark's own profile")
        self.btn_finish.setToolTip("Close the window, keep the session and check now")
        self.btn_resume.setToolTip("Leave 'needs sign-in' and check again now")
        self.btn_wipe.setToolTip("Forget the saved browser session for this bookmark")
        self.btn_signin.clicked.connect(self.sign_in)
        self.btn_finish.clicked.connect(self.finish)
        self.btn_resume.clicked.connect(self.resume)
        self.btn_wipe.clicked.connect(self.wipe)
        session = QHBoxLayout()
        for b in (self.btn_signin, self.btn_finish, self.btn_resume, self.btn_wipe):
            session.addWidget(b)
        session.addStretch(1)

        # -- server login ---------------------------------------------------------------
        a = f.auth
        self.auth_on = QCheckBox("Sign in to the server with a user name and password")
        self.auth_on.setChecked(a is not None)
        self.auth_kind = QComboBox()
        self.auth_kind.addItem("Basic", "basic")
        self.auth_kind.addItem("Digest", "digest")
        self.auth_kind.setCurrentIndex(self.auth_kind.findData(a.kind if a else "basic"))
        self.auth_user = QLineEdit(a.username if a else "")
        self.auth_key = QLineEdit(a.secret_key if a else f"bookmark-{bookmark.id}-login")
        self.auth_key.setToolTip("The name the password is kept under in the credential store")
        self.auth_password = QLineEdit()
        self.auth_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.auth_password.setPlaceholderText("type a new password to replace the stored one")
        self.auth_state = QLabel("")
        self.btn_auth_save = QPushButton("Save password")
        self.btn_auth_save.clicked.connect(self.save_auth_password)
        auth_row = QHBoxLayout()
        auth_row.addWidget(self.auth_password, 1)
        auth_row.addWidget(self.btn_auth_save)
        auth_form = QFormLayout()
        auth_form.addRow(self.auth_on)
        auth_form.addRow("Kind", self.auth_kind)
        auth_form.addRow("User name", self.auth_user)
        auth_form.addRow("Password name", self.auth_key)
        auth_form.addRow("Password", auth_row)
        auth_form.addRow("", self.auth_state)
        auth_box = QGroupBox("Server login (HTTP Basic or Digest)")
        auth_box.setLayout(auth_form)

        # -- cookies --------------------------------------------------------------------
        self.cookie_state = QLabel("")
        self.cookie_state.setWordWrap(True)
        self.btn_cookie_import = QPushButton("Import cookies.txt...")
        self.btn_cookie_remove = QPushButton("Remove")
        self.btn_cookie_import.clicked.connect(self.import_cookies)
        self.btn_cookie_remove.clicked.connect(self.remove_cookies)
        cookie_row = QHBoxLayout()
        cookie_row.addWidget(self.btn_cookie_import)
        cookie_row.addWidget(self.btn_cookie_remove)
        cookie_row.addStretch(1)
        cookie_layout = QVBoxLayout()
        cookie_layout.addWidget(self.cookie_state)
        cookie_layout.addLayout(cookie_row)
        cookie_box = QGroupBox("Cookies (a Netscape cookies.txt from your browser)")
        cookie_box.setLayout(cookie_layout)

        # -- macro and hand sign-in ---------------------------------------------------
        lg = f.login
        sig = lg.signal if lg else None
        self.macro = QComboBox()
        self.macro.addItem("None", None)
        self.btn_macros = QPushButton("Macros...")
        self.btn_macros.clicked.connect(self.open_macros)
        macro_row = QHBoxLayout()
        macro_row.addWidget(self.macro, 1)
        macro_row.addWidget(self.btn_macros)
        self.semi = QCheckBox("I sign in by hand (a browser window opens for me)")
        self.semi.setChecked(bool(lg and lg.semi_attended))
        self.sig_selector = QLineEdit(sig.selector or "" if sig else "")
        self.sig_selector.setPlaceholderText("input[type=password]")
        self.sig_url = QLineEdit(sig.url_contains or "" if sig else "")
        self.sig_url.setPlaceholderText("/login")
        self.sig_text = QLineEdit(sig.text or "" if sig else "")
        self.sig_text.setPlaceholderText("Please sign in")
        self.signin_url = QLineEdit(lg.signin_url or "" if lg else "")
        self.signin_url.setPlaceholderText("the bookmark's own address")
        self.allow_pw = QCheckBox("Let the macro sign in with a stored password on its own")
        self.allow_pw.setChecked(bool(lg and lg.allow_stored_password))
        self.allow_note = QLabel("")
        self.allow_note.setWordWrap(True)
        self.interval_h = QSpinBox()
        self.interval_h.setRange(1, 720)
        self.interval_h.setSuffix(" h")
        self.interval_h.setValue((lg.min_login_interval_s // 3600) if lg else 6)
        self.interval_h.setToolTip(
            "At most one automatic sign-in in this time; none after a failure"
        )
        macro_form = QFormLayout()
        macro_form.addRow("Macro", macro_row)
        macro_form.addRow(self.semi)
        macro_form.addRow("Login page has", self.sig_selector)
        macro_form.addRow("or the address has", self.sig_url)
        macro_form.addRow("or the page says", self.sig_text)
        macro_form.addRow("Sign-in window opens at", self.signin_url)
        macro_form.addRow(self.allow_pw)
        macro_form.addRow("", self.allow_note)
        macro_form.addRow("At most one automatic sign-in per", self.interval_h)
        macro_box = QGroupBox("Macro and signing in by hand")
        macro_box.setLayout(macro_form)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addLayout(session)
        layout.addWidget(self.message)
        layout.addWidget(macro_box)
        layout.addWidget(auth_box)
        layout.addWidget(cookie_box)
        layout.addStretch(1)

        self._update_allow_note()
        if settings is None:
            run_async(self._client.settings, self._got_settings, None)
        self.refresh_macros(select=bookmark.macro_id)
        self.refresh_status()
        self.refresh_auth_state()

    # -- reading the form -------------------------------------------------------------

    def macro_id(self) -> int | None:
        data = self.macro.currentData()
        return int(data) if data is not None else None

    def fetch_part(self) -> dict[str, Any]:
        """``auth`` and ``login`` in the shape ``FetchConfig.model_dump`` gives, so the editor can
        tell what changed. Raises ``ValueError`` for a half-filled server login."""
        auth: dict[str, Any] | None = None
        if self.auth_on.isChecked():
            user, key = self.auth_user.text().strip(), self.auth_key.text().strip()
            if not user or not key:
                raise ValueError("Server login: give a user name and a password name")
            auth = {"kind": self.auth_kind.currentData(), "username": user, "secret_key": key}
        selector = self.sig_selector.text().strip() or None
        url = self.sig_url.text().strip() or None
        text = self.sig_text.text().strip() or None
        signal = (
            {"selector": selector, "url_contains": url, "text": text}
            if (selector or url or text)
            else None
        )
        signin_url = self.signin_url.text().strip() or None
        used = (
            self.semi.isChecked() or signal is not None or signin_url is not None
            or self.allow_pw.isChecked()
        )  # fmt: skip
        login: dict[str, Any] | None = None
        if used:
            login = {
                "signal": signal,
                "semi_attended": self.semi.isChecked(),
                "signin_url": signin_url,
                "allow_stored_password": self.allow_pw.isChecked(),
                "min_login_interval_s": self.interval_h.value() * 3600,
            }
        return {"auth": auth, "login": login}

    # -- status -----------------------------------------------------------------------

    def say(self, text: str) -> None:
        self.message.setText(text)

    def _update_allow_note(self) -> None:
        on = bool(self._settings and self._settings.stored_password_logins)
        self.allow_note.setText(
            "The global switch (Settings, Network) is ON."
            if on
            else "Off until you switch on 'Allow stored-password logins' in Settings, Network. "
            "That switch is for after the portal experiment has been run and recorded."
        )

    def _got_settings(self, settings: Settings) -> None:
        self._settings = settings
        self._update_allow_note()

    def refresh_status(self) -> None:
        run_async(
            lambda: self._client.login_status(self._b.id), self._show_status,
            lambda e: self.status.setText(f"Could not load the sign-in status: {e}"),
        )  # fmt: skip

    def _show_status(self, st: LoginStatusOut) -> None:
        self.status.setText(status_text(st))
        self.cookie_state.setText(cookie_text(st.cookies))
        self.btn_cookie_remove.setEnabled(st.cookies.present)
        self.btn_signin.setEnabled(self.semi.isChecked() and not st.signing_in)
        self.btn_finish.setEnabled(st.signing_in)
        self.btn_resume.setEnabled(st.needs_login)
        self.btn_wipe.setEnabled(st.profile_exists and not st.signing_in)
        self.last_status = st

    def refresh_auth_state(self) -> None:
        key = self.auth_key.text().strip()
        if not key:
            return

        def done(out: Any) -> None:
            self.auth_state.setText(
                "A password is stored." if out.exists
                else "No password stored yet." if out.available
                else "This computer has no credential store; passwords cannot be saved."
            )  # fmt: skip

        run_async(lambda: self._client.secret(key), done, lambda e: self.auth_state.setText(str(e)))

    # -- macros -----------------------------------------------------------------------

    def refresh_macros(self, select: int | None = None) -> None:
        keep = select if select is not None else self.macro_id()

        def done(items: list[MacroOut]) -> None:
            self._macros = items
            self.macro.blockSignals(True)
            self.macro.clear()
            self.macro.addItem("None", None)
            for m in items:
                self.macro.addItem(m.name, m.id)
            self.macro.setCurrentIndex(max(0, self.macro.findData(keep)))
            self.macro.blockSignals(False)

        run_async(self._client.macros, done, lambda e: self.say(f"Could not load macros: {e}"))

    def open_macros(self) -> None:
        dialog = MacrosDialog(self._client, self)
        dialog.macros_changed.connect(lambda: self.refresh_macros())
        self._exec(dialog)
        self.refresh_macros()

    def _exec(self, dialog: Any) -> Any:  # a seam for tests
        return dialog.exec()

    # -- actions ----------------------------------------------------------------------

    def _fail(self, exc: Exception) -> None:
        self.say(str(exc).split(": ", 1)[-1])
        self.refresh_status()

    def sign_in(self) -> None:
        self.say("Opening the sign-in window...")
        run_async(
            lambda: self._client.start_signin(self._b.id),
            lambda _o: (self.say("Sign in in the window that opened, then close it (or press "
                                 "'I'm signed in'). PageWatch checks as soon as you do."),
                        self.refresh_status()),
            self._fail,
        )  # fmt: skip

    def finish(self) -> None:
        run_async(
            lambda: self._client.finish_signin(self._b.id),
            lambda _o: (self.say("Signed in. Checking now."), self.refresh_status()),
            self._fail,
        )

    def resume(self) -> None:
        run_async(
            lambda: self._client.resume_login(self._b.id),
            lambda _o: (self.say("Checking again now."), self.refresh_status()),
            self._fail,
        )

    def wipe(self) -> None:
        if not self._confirm("Wipe session", "Forget the saved browser session for this bookmark?"):
            return
        run_async(
            lambda: self._client.wipe_session(self._b.id),
            lambda _o: (self.say("The saved session was removed."), self.refresh_status()),
            self._fail,
        )

    def _confirm(self, title: str, text: str) -> bool:  # a seam for tests
        from PySide6.QtWidgets import QMessageBox

        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def save_auth_password(self) -> None:
        key, password = self.auth_key.text().strip(), self.auth_password.text()
        if not key or not password:
            self.say("Type a password and a name for it first.")
            return
        self.auth_password.clear()  # the password leaves the form the moment it is sent

        def done(_o: Any) -> None:
            self.say("Password saved to the credential store.")
            self.refresh_auth_state()

        run_async(lambda: self._client.put_secret(key, password), done, self._fail)

    def choose_cookie_file(self) -> str:  # a seam: tests do not open a file dialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Import cookies", "", "Cookie files (*.txt);;All files (*)"
        )
        return path

    def import_cookies(self) -> None:
        path = self.choose_cookie_file()
        if not path:
            return

        def done(out: CookieStatusOut) -> None:
            self.say("Cookies imported. " + cookie_text(out))
            self.refresh_status()

        run_async(lambda: self._client.import_cookies(self._b.id, path=path), done, self._fail)

    def remove_cookies(self) -> None:
        run_async(
            lambda: self._client.remove_cookies(self._b.id),
            lambda _o: (self.say("Cookies removed."), self.refresh_status()),
            self._fail,
        )
