"""Live engine events over the ``/events`` WebSocket, as Qt signals."""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

RECONNECT_MS = 2000


class EventStream(QObject):
    event = Signal(str, dict)  # type, data
    connection_changed = Signal(bool)

    def __init__(self, ws_url: str, token: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url, self._token = ws_url, token
        self._ws = QWebSocket()
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_message)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._open)
        self._wanted = False
        self.connected = False

    def start(self) -> None:
        self._wanted = True
        self._open()

    def stop(self) -> None:
        self._wanted = False
        self._timer.stop()
        self._ws.close()

    def _open(self) -> None:
        if self._wanted:
            self._ws.open(QUrl(self._url))

    def _on_connected(self) -> None:
        self._ws.sendTextMessage(json.dumps({"token": self._token}))  # auth is the first message

    def _on_message(self, text: str) -> None:
        try:
            msg: dict[str, Any] = json.loads(text)
        except ValueError:
            return
        kind = str(msg.get("type", ""))
        if kind == "hello":
            self.connected = True
            self.connection_changed.emit(True)
            return
        self.event.emit(kind, dict(msg.get("data") or {}))

    def _on_disconnected(self) -> None:
        if self.connected:
            self.connected = False
            self.connection_changed.emit(False)
        if self._wanted:
            self._timer.start(RECONNECT_MS)
