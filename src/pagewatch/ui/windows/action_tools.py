"""Toolbar entries for the notification channels and the failed-actions list.

Kept out of ``main_window.py`` on purpose (that file only constructs this): the main window gets
a second toolbar with "Notifications" and "Problems (n)", and a failed action shows up in the
status bar the moment the engine reports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QToolBar

from pagewatch.models import ActionJobOut, Settings
from pagewatch.ui.windows.failed_actions import FailedActionsDialog
from pagewatch.ui.windows.notification_settings import NotificationSettingsDialog
from pagewatch.ui.workers import run_async

if TYPE_CHECKING:
    from pagewatch.ui.windows.main_window import MainWindow


class ActionTools(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self.act_notifications = QAction("Notifications...", window)
        self.act_notifications.setToolTip("Email and ntfy settings, with a test button")
        self.act_notifications.triggered.connect(self.open_notifications)
        self.act_problems = QAction("Problems", window)
        self.act_problems.setToolTip("Actions that failed after their retries")
        self.act_problems.triggered.connect(self.open_problems)
        bar = QToolBar("Notifications")
        bar.setObjectName("notificationsBar")
        bar.setMovable(False)
        bar.addAction(self.act_notifications)
        bar.addAction(self.act_problems)
        window.addToolBar(bar)
        self.dialog: Any = None
        if window.events is not None:
            window.events.event.connect(self._on_event)
        self.refresh_count()

    # -- the count on the Problems button ------------------------------------------------

    def refresh_count(self) -> None:
        client = self._window.client
        run_async(lambda: client.action_jobs("failed", 500), self._set_count, lambda _e: None)

    def _set_count(self, jobs: list[ActionJobOut]) -> None:
        self.act_problems.setText(f"Problems ({len(jobs)})" if jobs else "Problems")

    def _on_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind != "problem" or data.get("kind") != "action_failed":
            return
        self._window.say(f"An action failed: {data.get('error', '')}"[:200])
        self.refresh_count()
        if isinstance(self.dialog, FailedActionsDialog) and self.dialog.isVisible():
            self.dialog.refresh()

    # -- dialogs -------------------------------------------------------------------------

    def open_notifications(self) -> None:
        client = self._window.client

        def show(settings: Settings) -> None:
            dialog = NotificationSettingsDialog(client, settings, self._window)
            dialog.saved.connect(lambda: self._window.say("Notification settings saved"))
            self.dialog = dialog
            dialog.open()

        run_async(client.settings, show, lambda e: self._window.say(f"Could not load: {e}"))

    def open_problems(self) -> None:
        dialog = FailedActionsDialog(self._window.client, self._window)
        dialog.finished.connect(lambda _r: self.refresh_count())
        self.dialog = dialog
        dialog.open()
