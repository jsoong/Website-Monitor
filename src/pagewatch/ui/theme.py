"""The UI's colour scheme: Dark (the default), Light, or follow the operating system.

Qt's default Windows style (``windowsvista``) is drawn by the OS and ignores the application's
colour scheme, so choosing Dark did nothing there. The Fusion style is drawn by Qt from the
palette, which follows ``QStyleHints.setColorScheme``; so ``apply`` sets both. On a platform that
cannot switch the scheme itself (a bare X11 or offscreen session) the request is ignored by Qt, so
the matching palette is painted by hand and the choice still holds.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

THEMES = ("dark", "light", "system")
DEFAULT_THEME = "dark"
#: What the web view paints before (and behind) a page, so an empty view does not flash white.
DARK_BACKGROUND = "#1e1e1e"
LIGHT_BACKGROUND = "#ffffff"

_SCHEMES = {
    "dark": Qt.ColorScheme.Dark,
    "light": Qt.ColorScheme.Light,
    "system": Qt.ColorScheme.Unknown,  # Unknown = follow the system
}


def dark_palette() -> QPalette:
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    p = QPalette()
    text, dim = QColor("#e6e6e6"), QColor("#808080")
    for role, colour in (
        (R.Window, "#2b2b2b"), (R.WindowText, text), (R.Base, DARK_BACKGROUND),
        (R.AlternateBase, "#262626"), (R.ToolTipBase, "#3a3a3a"), (R.ToolTipText, text),
        (R.Text, text), (R.Button, "#353535"), (R.ButtonText, text), (R.BrightText, "#ff5555"),
        (R.Link, "#4a9eff"), (R.LinkVisited, "#b48ef0"), (R.Highlight, "#2a82da"),
        (R.HighlightedText, "#ffffff"), (R.PlaceholderText, dim),
    ):  # fmt: skip
        p.setColor(role, QColor(colour))
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(G.Disabled, role, dim)
    return p


def apply(theme: str = DEFAULT_THEME) -> str:
    """Use Fusion and the colour scheme for ``theme`` (``dark``, ``light`` or ``system``; anything
    else is the default). Safe to call again when the setting changes. Returns the theme applied."""
    chosen = theme if theme in _SCHEMES else DEFAULT_THEME
    app = QGuiApplication.instance()
    if isinstance(app, QApplication) and app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")
    if not isinstance(app, QGuiApplication):
        return chosen
    wanted = _SCHEMES[chosen]
    app.styleHints().setColorScheme(wanted)
    if wanted != Qt.ColorScheme.Unknown and app.styleHints().colorScheme() != wanted:
        # the platform did not take the request: paint the scheme ourselves
        app.setPalette(dark_palette() if chosen == "dark" else QPalette())
    else:
        app.setPalette(QPalette())  # the platform / style palette (also undoes an earlier fallback)
    return chosen


def follow_system() -> None:
    apply("system")


def is_dark() -> bool:
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        return False
    scheme = app.styleHints().colorScheme()
    if scheme != Qt.ColorScheme.Unknown:
        return bool(scheme == Qt.ColorScheme.Dark)
    window = app.palette().color(QPalette.ColorRole.Window)
    return bool(window.lightness() < 128)


def background() -> QColor:
    """The colour the web view should paint behind a page."""
    return QColor(DARK_BACKGROUND if is_dark() else LIGHT_BACKGROUND)
