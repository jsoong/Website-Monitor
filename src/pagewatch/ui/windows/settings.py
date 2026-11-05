"""The Settings window (spec: *Desktop UI → Other screens*: "Settings (defaults, network, email,
notifications, retention, backup, data folder)"), plus the pages the other sections ask for: power
and resources (*Sleep, resume and connectivity*, *Continuous operation*), and capacity (*Scheduler →
Capacity*: "Settings shows current demand per pool and per host").

It is a client like the rest of the UI: it reads ``GET /settings`` once, and ``Save`` sends **only the
fields that changed** as ``PUT /settings`` (the engine merges and validates). Every control remembers
the value it started with, read back from the widget itself, so an untouched window can never send
anything, even when a stored value does not fit the control exactly. Settings the engine reads only at
start-up are labelled; everything else applies at once.

Settings that exist but are not offered here (they are test knobs or safety margins, still reachable
through ``PUT /settings``): the probe, battery-poll, memory-check and metric intervals, the memory
guard's grace period, ``blob_grace_h`` and ``browser_args``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTime, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from pagewatch.engine.paths import DataDir
from pagewatch.models import CapacityOut, HealthOut, Settings
from pagewatch.ui.client import ApiClient
from pagewatch.ui.windows.bookmark_editor import UNITS, SettingError, best_unit, diff_dict
from pagewatch.ui.windows.notification_settings import NotificationsEditor, write_secrets
from pagewatch.ui.windows.problems import when_text
from pagewatch.ui.workers import run_async

# Read at start-up only: a change is saved but takes effect when the engine next starts.
RESTART_KEYS = {"worker_processes", "startup_delay_s", "os_power_events"}
PAGES = [
    "General", "Network", "Notifications", "Email", "Power and resources", "Retention",
    "Backup", "Data folder", "Capacity",
]  # fmt: skip
P_CAPACITY, P_DATA = PAGES.index("Capacity"), PAGES.index("Data folder")
ROLE_PATH = Qt.ItemDataRole.UserRole


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} bytes"  # unreachable; keeps the type checker content


def rate_text(per_s: float) -> str:
    """Checks per second, without false precision (1,667/s, 12/s, 0.07/s)."""
    if per_s >= 100:
        return f"{per_s:,.0f}/s"
    return f"{per_s:.0f}/s" if per_s >= 10 else f"{per_s:.2f}/s"


def duration_text(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


def tree_size(path: Path) -> int:
    """Bytes in a file, or under a folder (0 when it does not exist); unreadable entries count 0."""
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:  # missing, not a folder, or no permission
            continue
    return total


def folder_sizes(data_dir: DataDir) -> dict[str, int]:
    """What is using the disk: the database (with its write-ahead files), the page store,
    backups and logs. Walks the folder, so call it off the GUI thread."""
    db = sum(tree_size(Path(f"{data_dir.db_path}{suffix}")) for suffix in ("", "-wal", "-shm"))
    return {
        "Database": db,
        "Page store": tree_size(data_dir.blobs_dir),
        "Backups": tree_size(data_dir.backups_dir),
        "Logs": tree_size(data_dir.logs_dir),
    }


def list_backups(data_dir: DataDir) -> list[Path]:
    """Backup zips in the data folder's ``backups`` folder, newest first."""
    try:
        zips = [p for p in data_dir.backups_dir.glob("*.zip") if p.is_file()]
    except OSError:
        return []
    return sorted(zips, key=lambda p: p.stat().st_mtime, reverse=True)


@dataclass
class _Field:
    key: str
    read: Callable[[], Any]
    baseline: Any
    restart: bool = False


class SettingsDialog(QDialog):
    saved = Signal()  # settings were written
    restore_staged = Signal(str)  # the engine is restarting to apply this backup

    def __init__(
        self,
        client: ApiClient,
        settings: Settings,
        data_dir: DataDir | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._orig = settings
        self._data_dir = data_dir
        self._fields: list[_Field] = []
        self._saving = False
        self.w: dict[str, QWidget] = {}  # setting key -> its control (tests and callers use it)
        self.setWindowTitle("Settings")
        self.resize(860, 620)

        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        self.nav.setObjectName("settingsPages")
        self.stack = QStackedWidget()
        self._loaded: set[int] = set()
        # Email (SMTP) and ntfy: two group boxes placed on the Email and Notifications pages. They
        # are one tracked setting ("notifications"); their passwords go to the secret store.
        self.notifications = NotificationsEditor(client, settings.notifications, self)
        self.notifications.saved_by_test.connect(self._rebase_notifications)
        builders = [
            self._build_general, self._build_network, self._build_notifications,
            self._build_email, self._build_power_and_resources, self._build_retention,
            self._build_backup, self._build_data_folder, self._build_capacity,
        ]  # fmt: skip
        assert len(builders) == len(PAGES)
        for name, build in zip(PAGES, builders, strict=True):  # same order as PAGES
            self.nav.addItem(name)
            build()
        self.nav.currentRowChanged.connect(self._page_changed)
        self.nav.setCurrentRow(0)

        self.error = QLabel("")
        self.error.setStyleSheet("color: #c62828")
        self.error.setWordWrap(True)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(lambda: self.save(close=True))
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            lambda: self.save(close=False)
        )
        self.buttons.rejected.connect(self.reject)

        body = QHBoxLayout()
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(self.error)
        layout.addWidget(self.note)
        layout.addWidget(self.buttons)

    # -- scaffolding --------------------------------------------------------------------

    def _page(self, intro: str = "") -> QFormLayout:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        if intro:
            label = QLabel(intro)
            label.setWordWrap(True)
            form.addRow(label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(page)
        self.stack.addWidget(scroll)
        return form

    def _track(
        self, key: str, widget: QWidget, read: Callable[[], Any], tip: str = "",
    ) -> None:  # fmt: skip
        self._fields.append(_Field(key, read, read(), restart=key in RESTART_KEYS))
        self.w[key] = widget
        if tip:
            widget.setToolTip(tip)

    @staticmethod
    def _label(text: str, key: str) -> str:
        return f"{text} (needs restart)" if key in RESTART_KEYS else text

    def _int(
        self, form: QFormLayout, key: str, label: str, lo: int, hi: int, *, suffix: str = "",
        tip: str = "",
    ) -> QSpinBox:  # fmt: skip
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSuffix(suffix)
        spin.setValue(int(getattr(self._orig, key)))
        spin.setKeyboardTracking(False)
        self._track(key, spin, spin.value, tip)
        form.addRow(self._label(label, key), spin)
        return spin

    def _float(
        self, form: QFormLayout, key: str, label: str, lo: float, hi: float, *, suffix: str = "",
        decimals: int = 1, tip: str = "",
    ) -> QDoubleSpinBox:  # fmt: skip
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(lo, hi)
        spin.setSuffix(suffix)
        spin.setValue(float(getattr(self._orig, key)))
        spin.setKeyboardTracking(False)
        self._track(key, spin, spin.value, tip)
        form.addRow(self._label(label, key), spin)
        return spin

    def _bool(self, form: QFormLayout, key: str, label: str, tip: str = "") -> QCheckBox:
        box = QCheckBox(self._label(label, key))
        box.setChecked(bool(getattr(self._orig, key)))
        self._track(key, box, box.isChecked, tip)
        form.addRow(box)
        return box

    def _text(
        self, form: QFormLayout, key: str, label: str, *, placeholder: str = "",
        nullable: bool = False, tip: str = "",
    ) -> QLineEdit:  # fmt: skip
        edit = QLineEdit(getattr(self._orig, key) or "")
        edit.setPlaceholderText(placeholder)
        edit.setCursorPosition(0)

        def read() -> str | None:
            text = edit.text().strip()
            return (text or None) if nullable else text

        self._track(key, edit, read, tip)
        form.addRow(self._label(label, key), edit)
        return edit

    def _clock(self, form: QFormLayout, key: str, label: str, tip: str = "") -> QTimeEdit:
        edit = QTimeEdit(QTime.fromString(str(getattr(self._orig, key)), "HH:mm"))
        edit.setDisplayFormat("HH:mm")
        self._track(key, edit, lambda: edit.time().toString("HH:mm"), tip)
        form.addRow(self._label(label, key), edit)
        return edit

    # -- pages --------------------------------------------------------------------------

    def _build_general(self) -> None:
        s = self._orig.default_schedule
        form = self._page("Defaults every bookmark and folder starts from. Anything set on a "
                          "folder or a bookmark overrides them.")  # fmt: skip
        self.sched_mode = QComboBox()
        for m in ("interval", "times", "adaptive", "manual"):
            self.sched_mode.addItem(m, m)
        self.sched_mode.setCurrentIndex(self.sched_mode.findData(s.mode.value))
        value, size = best_unit(s.interval_s)
        self.sched_every = QSpinBox()
        self.sched_every.setRange(1, 100000)
        self.sched_every.setValue(value)
        self.sched_unit = QComboBox()
        for text, secs in UNITS:
            self.sched_unit.addItem(text, secs)
        self.sched_unit.setCurrentIndex(self.sched_unit.findData(size))
        self.sched_times = QLineEdit(", ".join(s.times))
        self.sched_times.setPlaceholderText("09:00, 17:30")
        self.sched_jitter = QSpinBox()
        self.sched_jitter.setRange(0, 50)
        self.sched_jitter.setSuffix(" %")
        self.sched_jitter.setValue(s.jitter_pct)
        self.sched_battery = QComboBox()
        for b in ("normal", "slow", "pause"):
            self.sched_battery.addItem(b, b)
        self.sched_battery.setCurrentIndex(self.sched_battery.findData(s.on_battery.value))
        self.sched_ad_min, self.sched_ad_max = QSpinBox(), QSpinBox()
        for spin, secs in (
            (self.sched_ad_min, s.adaptive.min_s),
            (self.sched_ad_max, s.adaptive.max_s),
        ):
            spin.setRange(1, 100000)
            spin.setValue(max(1, secs // 60))
            spin.setSuffix(" min")
        self.sched_ad_factor = QDoubleSpinBox()
        self.sched_ad_factor.setRange(1.01, 10.0)
        self.sched_ad_factor.setSingleStep(0.1)
        self.sched_ad_factor.setValue(s.adaptive.factor)
        every = QHBoxLayout()
        every.addWidget(self.sched_every)
        every.addWidget(self.sched_unit)
        form.addRow("Check mode", self.sched_mode)
        form.addRow("Check every", every)
        form.addRow("At these times", self.sched_times)
        form.addRow("Adaptive: shortest", self.sched_ad_min)
        form.addRow("Adaptive: longest", self.sched_ad_max)
        form.addRow("Adaptive: factor", self.sched_ad_factor)
        form.addRow("Jitter", self.sched_jitter)
        form.addRow("On battery", self.sched_battery)
        self._sched_baseline = self.schedule_dict()
        self._text(form, "default_user_agent", "User agent")
        self._text(form, "timezone", "Time zone", nullable=True,
                   placeholder="the system's time zone", tip="An IANA name such as Europe/Berlin")  # fmt: skip
        self._float(form, "startup_delay_s", "Wait after start", 0, 3600, suffix=" s",
                    tip="Checks wait this long after the engine starts")  # fmt: skip
        self._bool(form, "notify_on_first_check", "Alert on a bookmark's very first check")
        self._bool(form, "debug_logging", "Write DEBUG-level detail to the log",
                   "Applies at once, no restart")  # fmt: skip

    def schedule_dict(self) -> dict[str, Any]:
        return {
            "mode": self.sched_mode.currentData(),
            "interval_s": max(60, self.sched_every.value() * int(self.sched_unit.currentData())),
            "times": sorted({t.strip() for t in self.sched_times.text().split(",") if t.strip()}),
            "adaptive": {
                "min_s": self.sched_ad_min.value() * 60,
                "max_s": max(self.sched_ad_max.value(), self.sched_ad_min.value()) * 60,
                "factor": round(self.sched_ad_factor.value(), 2),
            },
            "jitter_pct": self.sched_jitter.value(),
            "on_battery": self.sched_battery.currentData(),
        }

    def _build_network(self) -> None:
        o = self._orig
        form = self._page("How PageWatch reaches the web, and how politely.")
        self._text(form, "global_proxy", "Proxy", nullable=True,
                   placeholder="none  (http://host:port)")  # fmt: skip
        self._text(form, "browser_channel", "Browser", nullable=True,
                   placeholder="bundled Chromium only",
                   tip="The installed browser tried first for JavaScript pages: msedge or chrome")  # fmt: skip
        self._text(form, "browser_executable", "Browser program", nullable=True,
                   placeholder="use the browser above")  # fmt: skip
        self._text(form, "connectivity_url", "Connectivity check address",
                   tip="Asked with HEAD after a failure and after waking; any answer means online")  # fmt: skip
        self._float(
            form, "connectivity_timeout_s", "Connectivity check timeout", 0.5, 60, suffix=" s"
        )
        self._float(form, "offline_probe_s", "While offline, look again every", 0.05, 3600,
                    suffix=" s", decimals=2)  # fmt: skip
        self._float(form, "resume_probe_window_s", "After waking, wait for the network up to", 0, 3600,
                    suffix=" s")  # fmt: skip
        self._int(form, "static_pool", "Pages fetched at once", 1, 256)
        self._int(form, "browser_pool", "Browser pages at once", 1, 16)
        self._int(form, "worker_processes", "Processing processes", 1, 32)
        self._int(form, "per_host_concurrency", "At once per website", 1, 32)
        self._float(form, "per_host_min_gap_s", "Minimum gap per website", 0, 3600, suffix=" s")

        note = QLabel("Per-website exceptions. An empty cell uses the default above.")
        note.setWordWrap(True)
        self.overrides = QTableWidget(0, 3)
        self.overrides.setObjectName("hostOverrides")
        self.overrides.setHorizontalHeaderLabels(["Website", "At once", "Minimum gap (s)"])
        self.overrides.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.overrides.verticalHeader().hide()
        self.overrides.setMinimumHeight(130)
        self.overrides.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for host, ov in sorted(o.host_overrides.items()):
            self.add_override(host, ov.concurrency, ov.min_gap_s)
        add, remove = QPushButton("Add website"), QPushButton("Remove selected")
        add.clicked.connect(lambda: self.add_override())
        remove.clicked.connect(self.remove_selected_overrides)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        form.addRow(note)
        form.addRow(self.overrides)
        form.addRow(row)
        self._overrides_baseline = self.read_overrides()

    def add_override(self, host: str = "", concurrency: int | None = None,
                     min_gap_s: float | None = None) -> None:  # fmt: skip
        r = self.overrides.rowCount()
        self.overrides.insertRow(r)
        for c, text in enumerate((host, "" if concurrency is None else str(concurrency),
                                  "" if min_gap_s is None else f"{min_gap_s:g}")):  # fmt: skip
            self.overrides.setItem(r, c, QTableWidgetItem(text))

    def remove_selected_overrides(self) -> None:
        for r in sorted({i.row() for i in self.overrides.selectedIndexes()}, reverse=True):
            self.overrides.removeRow(r)

    def read_overrides(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in range(self.overrides.rowCount()):

            def text(c: int, r: int = r) -> str:
                item = self.overrides.item(r, c)
                return item.text().strip() if item else ""

            host = text(0).lower()
            if not host and not text(1) and not text(2):
                continue  # a blank row the user never filled in
            if not host:
                raise SettingError(f"Website exceptions, row {r + 1}: the website name is empty")
            try:
                conc = int(text(1)) if text(1) else None
                gap = float(text(2)) if text(2) else None
            except ValueError as exc:
                raise SettingError(
                    f"Website exceptions, {host}: 'At once' is a whole number and "
                    "'Minimum gap' is a number of seconds"
                ) from exc
            out[host] = {"concurrency": conc, "min_gap_s": gap}
        return out

    def _build_notifications(self) -> None:
        form = self._page("Alerts and error handling.")
        self._float(form, "toast_coalesce_s", "Combine alerts that arrive within", 0, 3600, suffix=" s",
                    tip="Several changes inside this window become one notification")  # fmt: skip
        self._int(form, "backlog_warning", "Warn when this many checks are waiting", 1, 1_000_000)
        self._float(form, "transient_retry_s", "Retry a temporary error after", 1, 3600, suffix=" s",
                    tip="A timeout, DNS failure or 5xx is tried once more before it counts")  # fmt: skip
        form.addRow(self.notifications.ntfy_box)

    def _build_email(self) -> None:
        form = self._page(
            "The mail server the Email action sends through, and who receives alerts when a "
            "bookmark does not name its own recipients."
        )
        form.addRow(self.notifications.smtp_box)
        # one setting for both boxes: the whole `notifications` section is sent when either changed
        self._track("notifications", self.notifications.smtp_box, self.notifications.read)

    def _build_power_and_resources(self) -> None:
        form = self._page("What the engine does when the computer sleeps, runs on battery or "
                          "runs short of memory.")  # fmt: skip
        self._bool(form, "pause_on_battery_saver", "Pause checks while Windows battery saver is on")
        self._bool(form, "keep_awake", "Keep the computer awake while AutoWatch is running",
                   "Stops the machine sleeping on its own while checks are running")  # fmt: skip
        self._bool(form, "os_power_events", "Listen to Windows for sleep and wake-up",
                   "Off: only the engine's own clock checks notice a sleep")  # fmt: skip
        self._float(form, "resume_drift_s", "Treat a clock jump over this as a sleep", 1, 3600,
                    suffix=" s")  # fmt: skip
        self._float(form, "catchup_spread_s", "Spread catch-up checks over up to", 0, 3600,
                    suffix=" s", tip="After a sleep, overdue bookmarks are checked once, spread out")  # fmt: skip
        self._int(form, "rss_limit_mb", "Memory limit (engine, workers and browser)", 100, 1_000_000,
                  suffix=" MB", tip="Over this the browser is restarted, then the engine itself")  # fmt: skip
        self._float(form, "worker_job_timeout_s", "Stop processing that takes longer than", 1, 3600,
                    suffix=" s")  # fmt: skip
        self._float(form, "loop_lag_limit_s", "Log a stack dump if the engine stalls for", 0.5, 3600,
                    suffix=" s")  # fmt: skip

    def _build_retention(self) -> None:
        form = self._page("What is kept, and when the nightly clean-up runs.")
        self._int(form, "keep_changed_versions", "Versions kept per bookmark", 1, 100000,
                  tip="Versions in use, pinned ones and this many of the newest are kept")  # fmt: skip
        self._float(form, "disk_cap_gb", "Page store limit", 0.1, 1_000_000, suffix=" GB",
                    tip="Over this, the oldest versions are removed first")  # fmt: skip
        self._int(
            form, "check_run_retention_days", "Keep the check log for", 1, 3650, suffix=" days"
        )
        self._int(
            form, "metric_retention_days", "Keep resource samples for", 1, 3650, suffix=" days"
        )
        self._clock(form, "maintenance_time", "Clean up at",
                    "Local time; at the next wake if the computer was asleep")  # fmt: skip

    def _build_backup(self) -> None:
        form = self._page("Backups hold your bookmarks, settings, macros and plugins.")
        self._bool(form, "backup_enabled", "Back up automatically every day")
        self._clock(form, "backup_time", "Back up at", "Local time; at the next wake if asleep")
        self._int(form, "backup_keep", "Automatic backups kept", 1, 365)
        self.chk_blobs = self._bool(form, "backup_include_blobs",
                                    "Include stored page contents (much larger)",
                                    "Used for automatic backups and for 'Back up now'")  # fmt: skip
        self.last_backup = QLabel("Last automatic backup: checking...")
        self.btn_backup = QPushButton("Back up now")
        self.btn_backup.clicked.connect(self.backup_now)
        self.backups = QListWidget()
        self.backups.setObjectName("backupList")
        self.backups.setMinimumHeight(120)
        self.btn_restore = QPushButton("Restore selected...")
        self.btn_restore_file = QPushButton("Restore from a file...")
        self.btn_restore.clicked.connect(self.restore_selected)
        self.btn_restore_file.clicked.connect(self.restore_from_file)
        self.backups.itemSelectionChanged.connect(self._sync_backup_buttons)
        self.backup_msg = QLabel("")
        self.backup_msg.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.btn_backup)
        row.addStretch(1)
        row.addWidget(self.btn_restore)
        row.addWidget(self.btn_restore_file)
        form.addRow(self.last_backup)
        form.addRow(QLabel("Backups in the data folder:"))
        form.addRow(self.backups)
        form.addRow(row)
        form.addRow(self.backup_msg)
        self.refresh_backup_list()
        self._sync_backup_buttons()
        run_async(self._client.health, self._show_last_backup,
                  lambda _e: self.last_backup.setText("Last automatic backup: unknown"))  # fmt: skip

    def _show_last_backup(self, health: HealthOut) -> None:
        when = when_text(health.last_backup_at)
        self.last_backup.setText(
            f"Last automatic backup: {when}" if when else "Last automatic backup: never"
        )

    def refresh_backup_list(self) -> None:
        self.backups.clear()
        if self._data_dir is None:
            return
        for path in list_backups(self._data_dir):
            try:
                size = fmt_size(path.stat().st_size)
            except OSError:
                continue
            item = QListWidgetItem(f"{path.name}    ({size})")
            item.setData(ROLE_PATH, str(path))
            self.backups.addItem(item)

    def _sync_backup_buttons(self) -> None:
        self.btn_restore.setEnabled(bool(self.backups.selectedItems()))

    def backup_now(self) -> None:
        blobs = self.chk_blobs.isChecked()
        self.btn_backup.setEnabled(False)
        self.backup_msg.setText("Backing up...")

        def done(out: Any) -> None:
            self.btn_backup.setEnabled(True)
            self.backup_msg.setText(f"Saved {Path(out.path).name} ({fmt_size(out.size_bytes)})")
            self.refresh_backup_list()

        def failed(exc: Exception) -> None:
            self.btn_backup.setEnabled(True)
            self.backup_msg.setText(f"The backup failed: {str(exc).split(': ', 1)[-1]}")

        run_async(lambda: self._client.backup(include_blobs=blobs), done, failed)

    def _confirm(self, title: str, text: str) -> bool:  # a seam so tests do not show a box
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _choose_backup_file(self) -> str:  # a seam so tests do not show a file dialog
        start = str(self._data_dir.backups_dir) if self._data_dir else ""
        path, _ = QFileDialog.getOpenFileName(self, "Restore a backup", start, "Backups (*.zip)")
        return path

    def restore_selected(self) -> None:
        items = self.backups.selectedItems()
        if items:
            self._restore(str(items[0].data(ROLE_PATH)))

    def restore_from_file(self) -> None:
        path = self._choose_backup_file()
        if path:
            self._restore(path)

    def _restore(self, path: str) -> None:
        if not self._confirm(
            "Restore a backup",
            f"Replace everything PageWatch has stored with the contents of\n{Path(path).name}?\n\n"
            "The engine restarts to apply it. Your current data is first copied to the backups "
            "folder (pre-restore). Unsaved changes in this window are discarded.",
        ):
            return
        self.backup_msg.setText("Checking the backup...")
        for b in (self.btn_backup, self.btn_restore, self.btn_restore_file):
            b.setEnabled(False)

        def done(out: Any) -> None:
            self.backup_msg.setText(out.message)
            self.restore_staged.emit(path)
            self.accept()  # the engine is restarting: nothing here is current any more

        def failed(exc: Exception) -> None:
            self.btn_backup.setEnabled(True)
            self.btn_restore_file.setEnabled(True)
            self._sync_backup_buttons()
            self.backup_msg.setText(f"Not restored: {str(exc).split(': ', 1)[-1]}")

        run_async(lambda: self._client.restore(path), done, failed)

    def _build_data_folder(self) -> None:
        form = self._page("Everything PageWatch knows lives in one folder.")
        self.folder_path = QLabel(str(self._data_dir.root) if self._data_dir else "unknown")
        self.folder_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_open_folder = QPushButton("Open folder")
        self.btn_open_folder.setEnabled(self._data_dir is not None)
        self.btn_open_folder.clicked.connect(self.open_folder)
        self.sizes = QLabel("Sizes are measured when this page opens.")
        self.sizes.setObjectName("folderSizes")
        self.sizes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout()
        row.addWidget(self.btn_open_folder)
        row.addStretch(1)
        form.addRow("Folder", self.folder_path)
        form.addRow(row)
        form.addRow("Disk use", self.sizes)

    def open_folder(self) -> None:
        if self._data_dir is not None:
            self._open_folder(self._data_dir.root)

    def _open_folder(self, path: Path) -> None:  # a seam so tests do not open a file manager
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def load_sizes(self) -> None:
        if self._data_dir is None:
            self.sizes.setText("The data folder is not known to this window.")
            return
        data_dir = self._data_dir
        self.sizes.setText("Measuring...")

        def done(sizes: dict[str, int]) -> None:
            cap = self._orig.disk_cap_gb * 1024**3
            lines = [f"{name}: {fmt_size(n)}" for name, n in sizes.items()]
            lines.append(
                f"Page store limit: {fmt_size(int(cap))} ({sizes['Page store'] / cap:.0%} used)"
            )
            self.sizes.setText("\n".join(lines))

        run_async(lambda: folder_sizes(data_dir), done,
                  lambda e: self.sizes.setText(f"Could not measure: {e}"))  # fmt: skip

    def _build_capacity(self) -> None:
        form = self._page(
            "What the schedules ask of PageWatch, against what it can do. Windows and day limits "
            "are ignored, so these numbers are an upper bound."
        )
        self.capacity_summary = QLabel("Measured when this page opens.")
        self.capacity_summary.setWordWrap(True)
        self.pools = QTableWidget(0, 5)
        self.pools.setObjectName("poolDemand")
        self.pools.setHorizontalHeaderLabels(["Pool", "Bookmarks", "Asked for", "Can do", "Load"])
        self.hosts = QTableWidget(0, 5)
        self.hosts.setObjectName("hostDemand")
        self.hosts.setHorizontalHeaderLabels(["Website", "Bookmarks", "Asked for", "Limit", "Load"])
        for table, wide in ((self.pools, 3), (self.hosts, 0)):  # the column that gets the room
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().hide()
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            header = table.horizontalHeader()
            for col in range(table.columnCount()):
                header.setSectionResizeMode(
                    col,
                    QHeaderView.ResizeMode.Stretch if col == wide
                    else QHeaderView.ResizeMode.ResizeToContents,
                )  # fmt: skip
        self.pools.setMaximumHeight(96)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_capacity)
        form.addRow(self.capacity_summary)
        form.addRow(self.pools)
        form.addRow(self.hosts)
        form.addRow(refresh)

    def refresh_capacity(self) -> None:
        self.capacity_summary.setText("Measuring...")
        run_async(self._client.capacity, self._show_capacity,
                  lambda e: self.capacity_summary.setText(f"Could not measure: {e}"))  # fmt: skip

    def _show_capacity(self, out: CapacityOut) -> None:
        limit = f"{out.warn_utilisation:.0%}"
        warnings: list[str] = []
        self.pools.setRowCount(len(out.pools))
        for r, p in enumerate(out.pools):
            can_do = (
                f"{rate_text(p.capacity_per_s)}  ({p.size} at once, "
                f"{duration_text(p.mean_check_s or 0.0)} per check)"
                if p.capacity_per_s is not None
                else f"not measured yet ({p.samples} timed checks)"
            )
            load = "" if p.utilisation is None else f"{p.utilisation:.0%}"
            self._row(self.pools, r, [p.name.capitalize(), str(p.bookmarks),
                                      rate_text(p.demand_per_s), can_do, load], p.warn)  # fmt: skip
            if p.warn:
                warnings.append(
                    f"The {p.name} pool is asked for more than {limit} of what it can do."
                )
        self.hosts.setRowCount(len(out.hosts))
        for r, h in enumerate(out.hosts):
            ceiling = (
                "no minimum gap" if h.ceiling_per_min is None else f"{h.ceiling_per_min:.1f}/min"
            )
            load = "" if h.utilisation is None else f"{h.utilisation:.0%}"
            self._row(self.hosts, r, [h.host, str(h.bookmarks), f"{h.demand_per_min:.1f}/min",
                                      ceiling, load + (" over limit" if h.over_limit else "")],
                      h.over_limit)  # fmt: skip
        if out.hosts_over_limit:
            warnings.append(
                f"{out.hosts_over_limit} website(s) are asked for more checks than their minimum gap "
                "allows, so those checks will fall behind. Lengthen their intervals or shorten the gap."
            )
        if out.hosts_total > len(out.hosts):
            warnings.append(f"Showing the {len(out.hosts)} busiest of {out.hosts_total} websites.")
        self.capacity_summary.setText("\n".join(warnings) or "Nothing is over its limit.")
        self.capacity_summary.setStyleSheet("color: #c62828" if out.hosts_over_limit or any(
            p.warn for p in out.pools) else "")  # fmt: skip

    @staticmethod
    def _row(table: QTableWidget, r: int, cells: list[str], warn: bool) -> None:
        for c, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if warn:
                item.setForeground(Qt.GlobalColor.red)
            table.setItem(r, c, item)

    def _page_changed(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        if row in (P_CAPACITY, P_DATA) and row not in self._loaded:
            self._loaded.add(row)
            (self.refresh_capacity if row == P_CAPACITY else self.load_sizes)()

    # -- save ---------------------------------------------------------------------------

    def build_patch(self) -> dict[str, Any]:
        """The settings that differ from what the window opened with. Raises ``SettingError``
        for a table cell that is not a number."""
        patch: dict[str, Any] = {}
        for f in self._fields:
            now = f.read()
            if now != f.baseline:
                patch[f.key] = now
        sched = diff_dict(self._sched_baseline, self.schedule_dict())
        if sched:
            patch["default_schedule"] = sched
        now_ov = self.read_overrides()
        changes: dict[str, Any] = {
            host: ov for host, ov in now_ov.items() if self._overrides_baseline.get(host) != ov
        }
        changes.update({host: None for host in self._overrides_baseline if host not in now_ov})
        if changes:
            patch["host_overrides"] = changes
        return patch

    def _snapshot(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
        """What every control says right now."""
        return (
            {f.key: f.read() for f in self._fields},
            self.schedule_dict(),
            self.read_overrides(),
        )

    def _rebase(
        self,
        saved: Settings,
        patch: dict[str, Any],
        sent: tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]],
    ) -> None:
        """What was sent is the new starting point, so a second Apply sends nothing more. It is
        the values *as sent*, not as the controls say now: an edit made while the save was on
        its way is still a change."""
        values, sched, overrides = sent
        self._orig = saved
        for f in self._fields:
            if f.key in patch:
                f.baseline = values[f.key]
        if "default_schedule" in patch:
            self._sched_baseline = sched
        if "host_overrides" in patch:
            self._overrides_baseline = overrides

    def _set_saving(self, saving: bool) -> None:
        self._saving = saving
        for kind in (QDialogButtonBox.StandardButton.Ok, QDialogButtonBox.StandardButton.Apply):
            self.buttons.button(kind).setEnabled(not saving)

    def save(self, *, close: bool = True) -> None:
        if self._saving:  # a save is already on its way
            return
        try:
            patch = self.build_patch()
            sent = self._snapshot()
        except SettingError as exc:
            self.error.setText(str(exc))
            return
        self.error.setText("")
        secrets = self.notifications.pending_secrets()  # typed passwords and tokens
        if not patch and not secrets:
            if close:
                self.accept()
            return
        restart = [f.key for f in self._fields if f.restart and f.key in patch]
        self._set_saving(True)

        def work() -> Settings:
            write_secrets(self._client, secrets)  # first: settings must not name a missing secret
            return self._client.put_settings(patch) if patch else self._client.settings()

        def done(saved: Settings) -> None:
            self._set_saving(False)
            self._rebase(saved, patch, sent)
            self.notifications.saved(saved, secrets)
            self.saved.emit()
            self.note.setText(
                "Saved. "
                + (
                    "Restart the engine for " + ", ".join(restart) + " to take effect."
                    if restart
                    else "The changes apply now."
                )
            )
            if close:
                self.accept()

        def failed(exc: Exception) -> None:
            self._set_saving(False)
            self.error.setText(str(exc).split(": ", 1)[-1])

        run_async(work, done, failed)

    def _rebase_notifications(self, saved: Settings, sent: dict[str, Any]) -> None:
        """The Send test buttons saved the notification settings on their own: they are the
        starting point now, so a later Apply does not send them again. The rest of the window
        is untouched."""
        self._orig = saved
        for f in self._fields:
            if f.key == "notifications":
                f.baseline = sent
