"""Dark mode follows the operating system (Qt 6.5+ tracks the Windows app theme itself)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPalette


def follow_system() -> None:
    app = QGuiApplication.instance()
    if isinstance(app, QGuiApplication):
        app.styleHints().setColorScheme(Qt.ColorScheme.Unknown)  # Unknown = follow the system


def is_dark() -> bool:
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        return False
    window = app.palette().color(QPalette.ColorRole.Window)
    return bool(window.lightness() < 128)
