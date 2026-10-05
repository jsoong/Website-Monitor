"""Pydantic models shared by the engine, the UI and the CLI.

This module is the single source of truth for every JSON column in the database and for
every body the local API accepts or returns. It must stay importable without any engine,
UI or network dependency.

Storage convention: the per-bookmark JSON columns hold *sparse overrides* (only what the
user set). The effective configuration is ``defaults <- folder chain defaults <- bookmark``,
merged by ``deep_merge`` and then validated by the models below.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class SourceType(StrEnum):
    AUTO = "auto"
    HTML = "html"
    FEED = "feed"
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    FTP = "ftp"
    FILE = "file"
    FOLDER = "folder"
    IMAGE = "image"
    BINARY = "binary"
    RECORDS = "records"


class CheckMethod(StrEnum):
    AUTO = "auto"
    STATIC = "static"
    BROWSER = "browser"
    SCREENSHOT = "screenshot"


class BookmarkStatus(StrEnum):
    NEW = "new"
    OK = "ok"
    CHANGED = "changed"
    ERROR = "error"
    NEEDS_LOGIN = "needs_login"
    DISABLED = "disabled"


class Outcome(StrEnum):
    FIRST = "first"
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    SUPPRESSED = "suppressed"
    ERROR = "error"
    SKIPPED = "skipped"


class Trigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    CATCHUP = "catchup"
    RETRY = "retry"
    FOLLOW = "follow"


class CheckKind(StrEnum):
    """What ``check_run.method`` records: how the content was actually obtained."""

    STATIC = "static"
    BROWSER = "browser"
    SCREENSHOT = "screenshot"
    DOCUMENT = "document"
    FEED = "feed"
    FTP = "ftp"
    FILE = "file"
    RECORDS = "records"


class HighlightMode(StrEnum):
    STANDARD = "standard"
    EXACT = "exact"
    TABLE = "table"


class ScheduleMode(StrEnum):
    INTERVAL = "interval"
    TIMES = "times"
    ADAPTIVE = "adaptive"
    MANUAL = "manual"


class OnBattery(StrEnum):
    NORMAL = "normal"
    SLOW = "slow"
    PAUSE = "pause"


class ThresholdMode(StrEnum):
    CUMULATIVE = "cumulative"
    PER_CHECK = "per_check"


class AlertPrivacy(StrEnum):
    CONTENT = "content"
    PRIVATE = "private"


class ActionType(StrEnum):
    TOAST = "toast"
    SOUND = "sound"
    OPEN = "open"
    EMAIL = "email"
    EXPORT = "export"
    RUN_PROGRAM = "run_program"
    WEBHOOK = "webhook"
    PUSHOVER = "pushover"
    NTFY = "ntfy"
    SCRAPE = "scrape"
    SCRIPT = "script"
    MARK_READ = "mark_read"


class FetchErrorKind(StrEnum):
    TIMEOUT = "timeout"
    DNS = "dns"
    TLS = "tls"
    HTTP = "http"
    TOO_LARGE = "too_large"
    BROWSER = "browser"
    PARSE = "parse"
    CONNECTION = "connection"


Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS: tuple[Day, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

MIN_INTERVAL_S = 60
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


class PWModel(BaseModel):
    """Base class: unknown keys are rejected so typos in stored JSON surface early."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False, validate_assignment=True)


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Merge ``over`` onto ``base``: dicts merge recursively, everything else replaces."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def apply_patch(stored: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Like ``deep_merge`` but a ``None`` value removes the key (back to inherited)."""
    out = dict(stored)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = apply_patch(out[key], value)
        else:
            out[key] = value
    return out


def dumps(data: Any) -> str:
    """Compact, deterministic JSON for database columns."""
    return json.dumps(data, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


# --------------------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------------------


class Window(PWModel):
    start: str = "00:00"
    end: str = "23:59"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("expected HH:MM")
        return v


class AdaptiveConfig(PWModel):
    min_s: int = Field(900, ge=MIN_INTERVAL_S)
    max_s: int = Field(86400, ge=MIN_INTERVAL_S)
    factor: float = Field(1.5, gt=1.0, le=10.0)

    @model_validator(mode="after")
    def _order(self) -> AdaptiveConfig:
        if self.max_s < self.min_s:
            raise ValueError("max_s must be >= min_s")
        return self


class ScheduleConfig(PWModel):
    mode: ScheduleMode = ScheduleMode.INTERVAL
    interval_s: int = Field(3600, ge=MIN_INTERVAL_S)
    times: list[str] = Field(default_factory=list)
    days: list[Day] = Field(default_factory=list)  # empty = every day
    window: Window | None = None
    adaptive: AdaptiveConfig = Field(default_factory=AdaptiveConfig)
    jitter_pct: int = Field(10, ge=0, le=50)
    on_battery: OnBattery = OnBattery.NORMAL

    @field_validator("times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        for t in v:
            if not _HHMM.match(t):
                raise ValueError(f"invalid time {t!r}, expected HH:MM")
        return sorted(set(v))

    @model_validator(mode="after")
    def _mode_needs(self) -> ScheduleConfig:
        if self.mode is ScheduleMode.TIMES and not self.times:
            raise ValueError("mode 'times' needs at least one entry in 'times'")
        return self


# --------------------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------------------


class AuthConfig(PWModel):
    kind: Literal["basic", "digest"] = "basic"
    username: str
    secret_key: str  # keyring entry name; the password itself is never stored here


class Rect(PWModel):
    """A pixel rectangle on a screenshot (origin top-left)."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)


class BrowserOptions(PWModel):
    """How the browser and screenshot methods drive the page (spec: Fetch layer, browser row)."""

    delay_after_load_s: float = Field(0.0, ge=0.0, le=60.0)  # extra wait after ``load``
    scroll_count: int = Field(0, ge=0, le=50)  # 800 px, 500 ms apart
    mouse_moves: int = Field(0, ge=0, le=20)  # synthetic pointer movement
    keys: list[str] = Field(default_factory=list, max_length=10)  # pressed after the load
    full_page: bool = True  # screenshot method: whole page, or only the viewport
    clip: Rect | None = None  # screenshot method: only this rectangle


class FeedOptions(PWModel):
    summary: bool = True  # include each entry's summary text, not just its title
    max_entries: int = Field(200, ge=1, le=2000)
    download_enclosures: bool = False
    enclosures_dir: str | None = None  # required when downloading
    enclosure_max_bytes: int = Field(20 * 1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def _dir(self) -> FeedOptions:
        if self.download_enclosures and not self.enclosures_dir:
            raise ValueError("download_enclosures needs 'enclosures_dir'")
        return self


class ListingOptions(PWModel):
    """Folder and FTP directory listings."""

    recursive: bool = False
    max_entries: int = Field(5000, ge=1, le=100000)


RecordEvent = Literal["new", "changed", "removed"]


def _all_record_events() -> list[RecordEvent]:
    return ["new", "changed", "removed"]


class RecordsConfig(PWModel):
    """A JSON or CSV feed whose rows are watched record by record (spec: Records sources)."""

    format: Literal["auto", "json", "csv"] = "auto"
    path: str = "$"  # JSONPath to the row array (JSON only)
    id_field: str = Field(min_length=1)
    filter: str | None = None  # row filter, e.g. ``borough in [MN, BK] and status = Active``
    fields: list[str] = Field(default_factory=list)  # fields to watch; empty = every field
    events: list[RecordEvent] = Field(
        default_factory=_all_record_events, min_length=1
    )  # which events alert
    delimiter: str = Field(",", min_length=1, max_length=1)  # CSV only

    @model_validator(mode="after")
    def _syntax(self) -> RecordsConfig:
        from pagewatch.engine.pipeline.records import (
            RecordsSyntaxError,
            parse_path,
            parse_row_filter,
        )

        try:
            parse_path(self.path)
            if self.filter and self.filter.strip():
                parse_row_filter(self.filter)
        except RecordsSyntaxError as exc:
            raise ValueError(str(exc)) from exc
        return self


class FetchConfig(PWModel):
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    user_agent: str | None = None
    proxy: str | None = None
    timeout_s: float = Field(30.0, ge=1.0, le=300.0)
    verify_tls: bool = True
    max_bytes: int = Field(20 * 1024 * 1024, ge=1024)
    auth: AuthConfig | None = None
    cookies_file: str | None = None
    browser: BrowserOptions = Field(default_factory=BrowserOptions)
    feed: FeedOptions = Field(default_factory=FeedOptions)
    listing: ListingOptions = Field(default_factory=ListingOptions)
    records: RecordsConfig | None = None


# --------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------


def _check_selector(kind: str, selector: str) -> None:
    """Reject selectors that cannot be compiled (so a typo is a 422, not a silent no-op)."""
    try:
        if kind == "css":
            import cssselect

            cssselect.parse(selector)
        else:
            from lxml import etree

            etree.XPath(selector)
    except Exception as exc:
        raise ValueError(f"invalid {kind} selector {selector!r}: {exc}") from exc


class FilterRule(PWModel):
    """One filter. ``type`` decides which of the other fields apply.

    selector      ``selector`` (+ ``selector_kind``): whole elements.
    between       ``start`` / ``end`` text markers; ``None`` means start / end of page.
    text          ``pattern`` (+ ``pattern_kind``): spans inside block text; optional ``scope``.
    number_mask   digits become ``#`` in blocks inside ``scope`` (or everywhere without one).
    """

    type: Literal["selector", "between", "text", "number_mask"]
    selector: str | None = None
    selector_kind: Literal["css", "xpath"] = "css"
    start: str | None = None
    end: str | None = None
    inclusive: bool = False
    pattern: str | None = None
    pattern_kind: Literal["literal", "wildcard", "regex"] = "literal"
    scope: str | None = None  # CSS selector limiting a text / number_mask rule
    note: str | None = None

    @model_validator(mode="after")
    def _required(self) -> FilterRule:
        if self.type == "selector" and not self.selector:
            raise ValueError("selector filter needs 'selector'")
        if self.selector:
            _check_selector(self.selector_kind, self.selector)
        if self.scope:
            _check_selector("css", self.scope)
        if self.type == "between":
            if self.start is None and self.end is None:
                raise ValueError("between filter needs 'start' or 'end'")
            if (self.start is not None and not self.start.strip()) or (
                self.end is not None and not self.end.strip()
            ):
                raise ValueError("between markers must not be empty")
        if self.type == "text":
            if not self.pattern:
                raise ValueError("text filter needs 'pattern'")
            if self.pattern_kind == "regex":
                try:
                    re.compile(self.pattern)
                except re.error as exc:
                    raise ValueError(f"invalid regex: {exc}") from exc
        return self


class SpecialFilters(PWModel):
    text_only: bool = True  # compare text only; off compares raw HTML source
    ignore_case: bool = True
    normalize_unicode: bool = True  # NFKC + invisible characters + whitespace
    ignore_options: bool = True  # drop <option> text
    sort_content: bool = False
    watch_links: bool = False
    watch_images: bool = False


class ScreenshotFilters(PWModel):
    """Screenshot comparison (spec: Screenshot comparison): what counts as a visual change."""

    ignore: list[Rect] = Field(default_factory=list)  # blanked out before comparing
    min_ratio: float = Field(0.002, ge=0.0, le=1.0)  # changed-pixel share that is a change
    height_change_pct: float = Field(5.0, ge=0.0, le=100.0)  # page height change that is a change


class FilterConfig(PWModel):
    cosmetic: list[FilterRule] = Field(default_factory=list)
    builtin_cosmetic: bool = True  # data/cookie_banner_selectors.txt
    watch: list[FilterRule] = Field(default_factory=list)
    ignore: list[FilterRule] = Field(default_factory=list)
    special: SpecialFilters = Field(default_factory=SpecialFilters)
    screenshot: ScreenshotFilters = Field(default_factory=ScreenshotFilters)

    @model_validator(mode="after")
    def _kinds(self) -> FilterConfig:
        if any(r.type != "selector" for r in self.cosmetic):
            raise ValueError("cosmetic filters can only be selector filters")
        if any(r.type == "number_mask" for r in self.watch):
            raise ValueError("number_mask is an ignore-side filter; it cannot be used to watch")
        return self


# --------------------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------------------


class GateConfig(PWModel):
    min_chars: int = Field(0, ge=0)
    blacklist: list[str] = Field(default_factory=list)
    whitelist: list[str] = Field(default_factory=list)
    ignore_removed: bool = False
    keywords: str = ""  # one rule per line, lines OR-ed
    highlight_keywords: str = ""  # colours terms in the view without gating
    min_changed_words: int = Field(0, ge=0)
    threshold_mode: ThresholdMode = ThresholdMode.CUMULATIVE
    error_threshold: int = Field(3, ge=1)

    @field_validator("keywords", "highlight_keywords")
    @classmethod
    def _keyword_syntax(cls, v: str) -> str:
        if v.strip():
            from pagewatch.engine.pipeline.keywords import KeywordSyntaxError, parse_rules

            try:
                parse_rules(v)
            except KeywordSyntaxError as exc:
                raise ValueError(str(exc)) from exc
        return v


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------


class ActionConfig(PWModel):
    type: ActionType
    params: dict[str, Any] = Field(default_factory=dict)


def _is_mark_read(action: Any) -> bool:
    kind = action.get("type") if isinstance(action, dict) else getattr(action, "type", None)
    return kind == ActionType.MARK_READ  # StrEnum: equal to its string value


class ActionsConfig(PWModel):
    actions: list[ActionConfig] = Field(default_factory=list)
    alert_privacy: AlertPrivacy = AlertPrivacy.CONTENT

    @model_validator(mode="before")
    @classmethod
    def _legacy_list(cls, data: Any) -> Any:
        # The column default is '[]': a bare list is accepted as "just the actions".
        if isinstance(data, list):
            data = {"actions": data}
        # Spec: `mark_read` "always runs last". The queue runs a change's jobs in action_index
        # order, so the order is fixed here, where every consumer gets it (stable otherwise).
        if isinstance(data, dict) and isinstance(data.get("actions"), list):
            data = {**data, "actions": sorted(data["actions"], key=_is_mark_read)}
        return data


# --------------------------------------------------------------------------------------
# Global settings
# --------------------------------------------------------------------------------------


class HostOverride(PWModel):
    concurrency: int | None = Field(None, ge=1, le=32)
    min_gap_s: float | None = Field(None, ge=0.0)


class Settings(PWModel):
    startup_delay_s: float = Field(30.0, ge=0.0)
    static_pool: int = Field(32, ge=1, le=256)
    browser_pool: int = Field(3, ge=1, le=16)
    per_host_concurrency: int = Field(2, ge=1, le=32)
    per_host_min_gap_s: float = Field(2.0, ge=0.0)
    host_overrides: dict[str, HostOverride] = Field(default_factory=dict)
    worker_processes: int = Field(3, ge=1, le=32)
    browser_channel: str | None = "msedge"  # Playwright channel tried first; None = bundled only
    browser_executable: str | None = None  # a browser binary to use instead of the channel
    browser_args: list[str] = Field(default_factory=list)  # extra launch arguments
    default_schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    default_user_agent: str = DEFAULT_USER_AGENT
    global_proxy: str | None = None
    notify_on_first_check: bool = False
    toast_coalesce_s: float = Field(30.0, ge=0.0)
    keep_changed_versions: int = Field(20, ge=1)
    disk_cap_gb: float = Field(10.0, gt=0.0)
    backlog_warning: int = Field(200, ge=1)
    transient_retry_s: float = Field(60.0, ge=1.0)
    timezone: str | None = None  # IANA name for times/window/days; None = the system zone
    debug_logging: bool = False
    autowatch_state: Literal["running", "paused"] = "running"
    autowatch_paused_until: str | None = None
    # -- unattended operation (spec: Sleep, resume and connectivity; Continuous operation) ----
    connectivity_url: str = (
        "https://www.msftconnecttest.com/connecttest.txt"  # HEAD; any reply = online
    )
    connectivity_timeout_s: float = Field(5.0, gt=0.0, le=60.0)
    probe_interval_s: float = Field(5.0, ge=0.05)  # between probes while waiting after a resume
    resume_probe_window_s: float = Field(
        120.0, ge=0.0
    )  # give up waiting for the network after this
    offline_probe_s: float = Field(15.0, ge=0.05)  # between probes while offline
    resume_drift_s: float = Field(30.0, gt=0.0)  # wall-vs-monotonic drift that means "slept"
    catchup_spread_s: float = Field(
        300.0, ge=0.0
    )  # catch-up checks spread over min(this, count x 1 s)
    battery_poll_s: float = Field(60.0, ge=0.05)
    pause_on_battery_saver: bool = False
    keep_awake: bool = False  # hold the machine awake while AutoWatch runs
    os_power_events: bool = True  # listen for Windows' own sleep/resume/logoff messages
    backup_enabled: bool = True
    backup_time: str = "03:00"  # local clock; at the next wake if the machine was asleep
    backup_keep: int = Field(14, ge=1)
    backup_include_blobs: bool = False
    maintenance_time: str = "03:30"  # retention and blob GC, local clock
    check_run_retention_days: int = Field(30, ge=1)
    metric_retention_days: int = Field(30, ge=1)
    blob_grace_h: float = Field(24.0, ge=1.0)  # an unreferenced blob must be unused this long to go
    rss_limit_mb: int = Field(1500, ge=100)  # engine + children; browser recycled, then exit code 3
    rss_check_s: float = Field(30.0, ge=0.05)
    rss_grace_s: float = Field(60.0, ge=0.0)  # between recycling the browser and giving up
    loop_lag_limit_s: float = Field(10.0, gt=0.0)  # a stall longer than this logs a stack dump
    worker_job_timeout_s: float = Field(120.0, gt=0.0)  # a pipeline job running longer is killed
    metric_interval_s: float = Field(3600.0, ge=0.05)

    @field_validator("backup_time", "maintenance_time")
    @classmethod
    def _clock_time(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("expected HH:MM")
        return v

    @field_validator("connectivity_url")
    @classmethod
    def _probe_url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://[^/\s]+", v, re.IGNORECASE):
            raise ValueError("the connectivity URL must start with http:// or https://")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown time zone {v!r}") from exc
        return v or None


# --------------------------------------------------------------------------------------
# API bodies
# --------------------------------------------------------------------------------------


class Page[T](PWModel):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


class FolderIn(PWModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: int | None = None
    sort_order: int = 0
    is_virtual: bool = False
    query: dict[str, Any] | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)


class FolderPatch(PWModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    parent_id: int | None = None
    sort_order: int | None = None
    query: dict[str, Any] | None = None
    defaults: dict[str, Any] | None = None


class FolderOut(PWModel):
    id: int
    parent_id: int | None
    name: str
    sort_order: int
    is_virtual: bool
    query: dict[str, Any] | None
    defaults: dict[str, Any]


class BookmarkIn(PWModel):
    name: str | None = Field(None, max_length=300)  # defaults to the URL's host
    url: str = Field(min_length=1)
    folder_id: int | None = None
    source_type: SourceType = SourceType.AUTO
    check_method: CheckMethod = CheckMethod.AUTO
    enabled: bool = True
    priority: int = Field(0, ge=0, le=1)
    schedule: dict[str, Any] | None = None
    fetch: dict[str, Any] | None = None
    filter: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    highlight_mode: HighlightMode = HighlightMode.STANDARD
    actions: dict[str, Any] | list[Any] | None = None
    macro_id: int | None = None
    plugin: str | None = None
    info1: str | None = None
    info2: str | None = None
    info3: str | None = None
    note: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^(https?|ftps?|file)://", v, re.IGNORECASE):
            raise ValueError("url must start with http://, https://, ftp://, ftps:// or file://")
        return v


class BookmarkPatch(PWModel):
    """Partial update. Config dicts are merged into the stored overrides; ``null`` inside
    one removes that override (the value falls back to the inherited default)."""

    name: str | None = Field(None, max_length=300)
    url: str | None = None
    folder_id: int | None = None
    move_to_root: bool = False  # folder_id=None is ambiguous in PATCH; this clears the folder
    source_type: SourceType | None = None
    check_method: CheckMethod | None = None
    enabled: bool | None = None
    priority: int | None = Field(None, ge=0, le=1)
    schedule: dict[str, Any] | None = None
    fetch: dict[str, Any] | None = None
    filter: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    highlight_mode: HighlightMode | None = None
    actions: dict[str, Any] | list[Any] | None = None
    macro_id: int | None = None
    plugin: str | None = None
    info1: str | None = None
    info2: str | None = None
    info3: str | None = None
    note: str | None = None


class BookmarkOut(PWModel):
    id: int
    folder_id: int | None
    name: str
    url: str
    source_type: SourceType
    check_method: CheckMethod
    enabled: bool
    priority: int
    schedule: ScheduleConfig  # effective (defaults + folder chain + overrides)
    fetch: FetchConfig
    filter: FilterConfig
    gate: GateConfig
    highlight_mode: HighlightMode
    actions: ActionsConfig
    overrides: dict[str, Any]  # the sparse JSON actually stored per section
    macro_id: int | None
    plugin: str | None
    info1: str | None
    info2: str | None
    info3: str | None
    note: str | None
    status: BookmarkStatus
    unread: bool
    consecutive_errors: int
    current_interval_s: int | None
    next_due_at: str | None
    last_checked_at: str | None
    last_changed_at: str | None
    latest_version_id: int | None
    baseline_version_id: int | None
    gate_anchor_version_id: int | None
    keyword_hits: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class BookmarkSummary(PWModel):
    """The light row the bookmark list pages through (10,000 rows must stay fast)."""

    id: int
    folder_id: int | None
    name: str
    url: str
    source_type: SourceType
    check_method: CheckMethod
    enabled: bool
    priority: int
    status: BookmarkStatus
    unread: bool
    consecutive_errors: int
    interval_s: int | None  # effective schedule interval (the adaptive value when adaptive)
    schedule_mode: ScheduleMode
    next_due_at: str | None
    last_checked_at: str | None
    last_changed_at: str | None
    keyword_hits: list[str] = Field(default_factory=list)


class FolderCounts(PWModel):
    total: int
    unread: int


class BookmarkCounts(PWModel):
    total: int
    unread: int
    errors: int
    needs_login: int
    changed_today: int
    keyword_hits: int
    by_folder: dict[int, FolderCounts]  # key 0 = bookmarks outside any folder


class BulkRequest(PWModel):
    ids: list[int] = Field(min_length=1)
    action: Literal["update", "move", "enable", "disable", "delete"]
    patch: BookmarkPatch | None = None
    folder_id: int | None = None


class BulkResult(PWModel):
    affected: int


class CheckRequest(PWModel):
    ids: list[int] | None = None
    folder_id: int | None = None
    all: bool = False
    force: bool = False


class CheckQueued(PWModel):
    queued: int


class ChangeOut(PWModel):
    id: int
    bookmark_id: int
    old_version_id: int | None
    new_version_id: int
    detected_at: str
    added_words: int | None
    removed_words: int | None
    changed_blocks: int | None
    checks_accumulated: int
    keyword_hits: list[str] = Field(default_factory=list)
    summary: str | None
    feedback: str | None
    read_at: str | None


class CheckRunOut(PWModel):
    id: int
    bookmark_id: int
    started_at: str
    finished_at: str | None
    trigger: str
    method: str
    outcome: str
    reason: str | None
    duration_ms: int | None
    bytes: int | None


class TestFilterRequest(PWModel):
    """A candidate configuration, as the patch the editor would send to ``PATCH /bookmarks``."""

    __test__ = False  # not a pytest test class

    filter: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    highlight_mode: HighlightMode | None = None
    from_version_id: int | None = None  # default: the baseline (last read)
    to_version_id: int | None = None  # default: the latest


class TestFilterOut(PWModel):
    __test__ = False

    baseline: list[str]  # filtered blocks of the old version
    latest: list[str]  # filtered blocks of the new version
    marks: list[str]  # the text diff (see pipeline/render.py)
    diff: dict[str, Any]
    alert: bool  # would the gate alert?
    reason: str | None  # why not, e.g. keyword_miss
    keyword_hits: list[str]
    identical: bool
    warnings: list[str]


class ProposalOut(PWModel):
    rule: FilterRule
    kind: Literal["volatile_pattern", "element"]
    pattern_name: str | None
    explanation: str
    example_old: str
    example_new: str
    verified: bool  # on its own, it makes the false positive disappear


class FalsePositiveOut(PWModel):
    change_id: int
    proposals: list[ProposalOut]
    resolves_all: bool  # all proposals together remove the change
    remaining_changed_blocks: int
    patch: dict[str, Any]  # a ready `PATCH /bookmarks/{id}` body adding the proposals


class RenderOut(PWModel):
    """A rendered view (``format=json``); ``GET`` without it returns the HTML itself."""

    html: str
    view: str  # the view actually produced (highlight falls back to text for non-HTML)
    identical: bool = False  # nothing unread: baseline == latest
    degraded: bool = False  # too large for a word-level diff
    stats: dict[str, int] = Field(default_factory=dict)


class PreviewRequest(PWModel):
    url: str = Field(min_length=1)
    fetch: dict[str, Any] | None = None
    source_type: SourceType = SourceType.AUTO
    check_method: CheckMethod = CheckMethod.AUTO
    samples: int = Field(1, ge=1, le=3)  # fetch this many times to find what already differs
    gap_s: float = Field(5.0, ge=0.0, le=60.0)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return BookmarkIn.model_validate({"url": v}).url


class PreviewOut(PWModel):
    final_url: str
    status: int | None
    content_type: str
    kind: str  # page | js-app | feed | pdf | docx | xlsx | json | text | image | binary
    method: str  # the method auto-detection would pick: static | browser
    js_app: bool
    readable_chars: int
    words: int
    blocks: int
    html: str  # the page as the viewer would show it (sanitised)
    unstable_blocks: int = 0  # blocks that differed between the samples
    proposals: list[ProposalOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0


class AutowatchRequest(PWModel):
    state: Literal["running", "paused"]
    until: str | None = None  # ISO-8601 UTC; only meaningful with state=paused


class AutowatchState(PWModel):
    state: Literal["running", "paused"]
    until: str | None = None


class HealthOut(PWModel):
    version: str
    uptime_s: float
    pid: int
    queue_length: int
    in_flight: int
    outcomes_24h: dict[str, int]
    rss_mb: float
    browser_state: Literal["stopped", "running", "unavailable"] = "stopped"
    autowatch: AutowatchState
    bookmarks: int
    online: bool = True
    on_battery: bool = False
    backlog_warning: bool = False
    # Unattended-operation state (M5). RSS here is the engine plus its workers and browser.
    rss_total_mb: float | None = None
    cpu_percent: float | None = None
    last_backup_at: str | None = None
    last_maintenance_at: str | None = None


class BackupRequest(PWModel):
    include_blobs: bool | None = None  # None: the `backup_include_blobs` setting
    path: str | None = None  # destination zip; default: a new file in the data folder's backups\


class BackupOut(PWModel):
    path: str
    size_bytes: int
    created_at: str
    include_blobs: bool
    schema_version: int


class RestoreRequest(PWModel):
    path: str = Field(min_length=1)  # a backup zip made by this program


class RestoreOut(PWModel):
    staged: bool
    restart: bool  # the engine is about to exit (code 3) so the restore can be applied
    message: str


class LockInfo(PWModel):
    pid: int
    port: int
    token: str
    version: str
    started_at: str


EVENT_TYPES = (
    "check_started",
    "check_finished",
    "change_detected",
    "bookmark_updated",
    "engine_state",
    "problem",
)


class EngineEvent(PWModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    ts: str
