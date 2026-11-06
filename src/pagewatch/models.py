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
from typing import Annotated, Any, Literal, overload

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
    REPORT = "report"  # M7: write a report file when a change is detected


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


SECRET_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")  # a secret-store key; never the secret


@overload
def _secret_name(value: str) -> str: ...
@overload
def _secret_name(value: None) -> None: ...
def _secret_name(value: str | None) -> str | None:
    if value is not None and not SECRET_NAME.match(value):
        raise ValueError("a secret name uses letters, digits and _ . : - (at most 64)")
    return value


class AuthConfig(PWModel):
    kind: Literal["basic", "digest"] = "basic"
    username: str = Field(min_length=1)
    secret_key: str  # keyring entry name; the password itself is never stored here

    @field_validator("secret_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _secret_name(v)


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


def _check_link_patterns(kind: str, patterns: list[str]) -> None:
    for p in patterns:
        if not p.strip():
            raise ValueError("link patterns must not be empty")
        if kind == "regex":
            try:
                re.compile(p)
            except re.error as exc:
                raise ValueError(f"invalid regex {p!r}: {exc}") from exc


class FollowLinksConfig(PWModel):
    """Follow-Links (spec: Multi-page sources): discover links on this page and watch each match
    as a child bookmark in a generated folder."""

    enabled: bool = True
    include: list[str] = Field(default_factory=list)  # empty = every link
    exclude: list[str] = Field(default_factory=list)
    pattern_kind: Literal["wildcard", "regex"] = "wildcard"  # matched against the absolute URL
    depth: int = Field(1, ge=1, le=5)  # 1 = the links on this page; 2 = and the links on those
    max_pages: int = Field(50, ge=1, le=500)  # child bookmarks over the whole crawl
    same_host: bool = True  # only links on the parent's host (a leading www. is ignored)
    folder_name: str | None = Field(None, max_length=200)  # default: "<name> (followed)"

    @model_validator(mode="after")
    def _patterns(self) -> FollowLinksConfig:
        _check_link_patterns(self.pattern_kind, self.include)
        _check_link_patterns(self.pattern_kind, self.exclude)
        return self


class MergePagesConfig(PWModel):
    """Merge pages (spec: Multi-page sources): follow a next-page link or a URL pattern up to
    ``max_pages`` pages and concatenate them before extraction."""

    enabled: bool = True
    next_selector: str | None = None  # CSS selector of the "next page" link
    url_pattern: str | None = None  # e.g. ``https://example.com/list?page={n}``
    first_n: int = Field(2, ge=0, le=1000)  # the value of {n} for the second page
    max_pages: int = Field(5, ge=2, le=50)  # pages in all, including the first

    @model_validator(mode="after")
    def _one_way(self) -> MergePagesConfig:
        if self.enabled and bool(self.next_selector) == bool(self.url_pattern):
            raise ValueError("merge pages needs exactly one of 'next_selector' or 'url_pattern'")
        if self.next_selector:
            _check_selector("css", self.next_selector)
        if self.url_pattern and "{n}" not in self.url_pattern:
            raise ValueError("'url_pattern' must contain {n}")
        return self


# --- Logins: proxies, login signals, Check-Macros (spec: Logins and Check-Macros) ---

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def check_proxy_url(value: str) -> str:
    """A proxy address the fetchers can use: http, https or socks5 with a host, and *no* password
    (the spec keeps passwords out of the database; a proxy login is a user name plus a
    secret-store key, see ``FetchConfig.proxy_username``)."""
    from urllib.parse import urlsplit

    value = value.strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"not a proxy address: {exc}") from exc
    if parts.scheme.lower() not in PROXY_SCHEMES:
        raise ValueError("a proxy address starts with http://, https://, socks5:// or socks5h://")
    if not parts.hostname:
        raise ValueError("the proxy address has no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            "do not put a user name or password in the proxy address; use the proxy user name "
            "field and keep the password in the credential store"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("a proxy address is scheme://host:port with nothing after it")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("the proxy port must be between 1 and 65535")
    return value.rstrip("/")


_PASSWORDISH = re.compile(r"password|passwd|passcode|pwd|secret", re.IGNORECASE)
MAX_MACRO_STEPS = 100


class SecretRef(PWModel):
    """A macro value kept in the credential store: ``{"secret": "<key>"}``, never inline."""

    secret: str

    @field_validator("secret")
    @classmethod
    def _key(cls, v: str) -> str:
        return _secret_name(v)


class _StepBase(PWModel):
    timeout_s: float = Field(15.0, gt=0.0, le=120.0)  # how long the step may wait for its target


class GotoStep(_StepBase):
    op: Literal["goto"]
    url: str

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://[^/\s]+", v, re.IGNORECASE):
            raise ValueError("a goto step needs an http:// or https:// address")
        return v


class FillStep(_StepBase):
    op: Literal["fill"]
    selector: str = Field(min_length=1)
    value: str | SecretRef

    @model_validator(mode="after")
    def _no_inline_passwords(self) -> FillStep:
        if isinstance(self.value, str) and _PASSWORDISH.search(self.selector):
            raise ValueError(
                'this looks like a password field; use {"secret": "<key>"} for the value, '
                "never the password itself"
            )
        return self


class ClickStep(_StepBase):
    op: Literal["click"]
    selector: str = Field(min_length=1)


class SelectStep(_StepBase):
    op: Literal["select"]
    selector: str = Field(min_length=1)
    value: str


class PressStep(_StepBase):
    op: Literal["press"]
    key: str = Field(min_length=1, max_length=40)
    selector: str | None = None  # None: the focused element


class WaitForStep(_StepBase):
    op: Literal["wait_for"]
    selector: str | None = None
    ms: int | None = Field(None, ge=1, le=60000)

    @model_validator(mode="after")
    def _one_of(self) -> WaitForStep:
        if (self.selector is None) == (self.ms is None):
            raise ValueError("a wait_for step needs either a selector or a number of milliseconds")
        return self


class AssertTextStep(_StepBase):
    op: Literal["assert_text"]
    text: str = Field(min_length=1)
    selector: str | None = None  # None: anywhere on the page


MacroStep = Annotated[
    GotoStep | FillStep | ClickStep | SelectStep | PressStep | WaitForStep | AssertTextStep,
    Field(discriminator="op"),
]


class LoginSignal(PWModel):
    """How the engine recognises "you are signed out": any one of these appearing means the
    check landed on a login page (a selector, or a redirect to the login URL)."""

    selector: str | None = None  # CSS: a login form's field, e.g. ``input[type=password]``
    url_contains: str | None = None  # the final address contains this (a redirect to /login)
    text: str | None = None  # the page contains this text, e.g. "Please sign in"

    @field_validator("selector", "url_contains", "text")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @field_validator("selector")
    @classmethod
    def _css(cls, v: str | None) -> str | None:
        if v:
            _check_selector("css", v)
        return v

    @model_validator(mode="after")
    def _any(self) -> LoginSignal:
        if not (self.selector or self.url_contains or self.text):
            raise ValueError("a login signal needs a selector, an address fragment or some text")
        return self


class MacroDef(PWModel):
    """The body of a macro (``macro.steps_json`` holds ``steps``)."""

    name: str = Field(min_length=1, max_length=120)
    steps: list[MacroStep] = Field(min_length=1, max_length=MAX_MACRO_STEPS)
    login_signal: LoginSignal | None = None

    @property
    def secret_keys(self) -> list[str]:
        return sorted({s.value.secret for s in self.steps if isinstance(s, FillStep)
                       and isinstance(s.value, SecretRef)})  # fmt: skip

    @property
    def uses_stored_password(self) -> bool:
        return bool(self.secret_keys)


class MacroOut(MacroDef):
    id: int
    secret_keys_used: list[str] = Field(default_factory=list)
    stored_password: bool = False  # a step fills a secret: this macro is a stored-password login
    bookmarks: int = 0  # how many bookmarks run it


MIN_LOGIN_INTERVAL_S = 3600
DEFAULT_LOGIN_INTERVAL_S = 6 * 3600  # spec: at most one automated login per 6 hours


class LoginConfig(PWModel):
    """A bookmark's sign-in policy (``fetch.login``)."""

    signal: LoginSignal | None = None  # overrides the macro's own signal
    semi_attended: bool = False  # the owner signs in by hand; checks reuse that profile
    signin_url: str | None = None  # where "Sign in" opens (default: the bookmark's address)
    allow_stored_password: bool = False  # opt in: the macro may fill a stored password
    min_login_interval_s: int = Field(DEFAULT_LOGIN_INTERVAL_S, ge=MIN_LOGIN_INTERVAL_S)

    @field_validator("signin_url")
    @classmethod
    def _signin_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not re.match(r"^https?://[^/\s]+", v, re.IGNORECASE):
            raise ValueError("the sign-in address must start with http:// or https://")
        return v


class FetchConfig(PWModel):
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    user_agent: str | None = None
    proxy: str | None = None
    proxy_username: str | None = None  # the proxy's login; its password is a secret-store entry
    proxy_secret_key: str | None = None
    timeout_s: float = Field(30.0, ge=1.0, le=300.0)
    verify_tls: bool = True
    max_bytes: int = Field(20 * 1024 * 1024, ge=1024)
    auth: AuthConfig | None = None
    cookies_file: str | None = None  # a Netscape cookies.txt (the import endpoint fills this in)
    login: LoginConfig | None = None
    browser: BrowserOptions = Field(default_factory=BrowserOptions)
    feed: FeedOptions = Field(default_factory=FeedOptions)
    listing: ListingOptions = Field(default_factory=ListingOptions)
    records: RecordsConfig | None = None
    follow: FollowLinksConfig | None = None
    merge: MergePagesConfig | None = None

    @field_validator("proxy")
    @classmethod
    def _proxy(cls, v: str | None) -> str | None:
        return check_proxy_url(v) if v and v.strip() else None

    @field_validator("proxy_secret_key")
    @classmethod
    def _proxy_key(cls, v: str | None) -> str | None:
        return _secret_name(v) if v else None

    @model_validator(mode="after")
    def _proxy_login(self) -> FetchConfig:
        if (self.proxy_username or self.proxy_secret_key) and not self.proxy:
            raise ValueError("a proxy user name or password needs a proxy address")
        if bool(self.proxy_username) != bool(self.proxy_secret_key):
            raise ValueError("a proxy login needs both a user name and a stored password key")
        return self


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
    @model_validator(mode="after")
    def _params_valid(self) -> ActionConfig:
        # The parameter models are defined below (ACTION_PARAMS); a bad template, an unknown
        # variable or a missing path is rejected here, at the model boundary (HTTP 422).
        model = ACTION_PARAMS.get(self.type)
        if model is not None:
            try:
                model.model_validate(self.params)
            except ValidationError as exc:
                first = exc.errors()[0]
                where = ".".join(str(p) for p in first["loc"])
                raise ValueError(
                    f"{self.type.value} action: {where + ': ' if where else ''}"
                    f"{str(first['msg']).removeprefix('Value error, ')}"
                ) from exc
        return self

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
# Action parameters (spec: Actions and notifications)
#
# ``ActionConfig.params`` stays a plain dict in the stored JSON; these models validate it (at
# create/patch time, and again when the action runs) and fill in the defaults.
# --------------------------------------------------------------------------------------


def _address(value: str) -> str:
    from email.utils import parseaddr

    value = value.strip()
    _, addr = parseaddr(value)
    if not addr or "@" not in addr or any(c in value for c in "\r\n\x00"):
        raise ValueError(f"{value!r} is not an email address")
    return value


def _http_url(value: str) -> str:
    value = value.strip()
    if not re.match(r"^https?://[^/\s]+", value, re.IGNORECASE):
        raise ValueError("expected an http:// or https:// URL")
    return value


def _jinja(value: str | None, what: str) -> str | None:
    if value:
        from pagewatch.engine.actions.templating import TemplateError, check_jinja

        try:
            check_jinja(value, what)
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc
    return value


def _brace(value: str, allowed: frozenset[str], what: str) -> str:
    from pagewatch.engine.actions.templating import TemplateError, check_brace_template

    try:
        check_brace_template(value, allowed, what)
    except TemplateError as exc:
        raise ValueError(str(exc)) from exc
    return value


class EmailParams(PWModel):
    """``email``: one message per change over SMTP (server settings: ``Settings.notifications``)."""

    to: list[str] = Field(default_factory=list)  # empty: the default recipients in Settings
    format: Literal["full", "simple", "changes_only"] = "changes_only"
    attach_screenshot: bool = False  # screenshot bookmarks: the picture (or its diff) as a PNG
    subject: str | None = Field(None, max_length=300)  # Jinja2; default "<name> changed"
    template: str | None = Field(None, max_length=20000)  # Jinja2; replaces the built-in body
    priority: Literal["low", "normal", "high"] = "normal"

    @field_validator("to")
    @classmethod
    def _to(cls, v: list[str]) -> list[str]:
        return [_address(a) for a in v]

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str | None) -> str | None:
        return _jinja(v, "subject")

    @field_validator("template")
    @classmethod
    def _template(cls, v: str | None) -> str | None:
        return _jinja(v, "template")


NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}
_NTFY_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NTFY_TAG = re.compile(r"^[\w+-]{1,40}$")


class NtfyParams(PWModel):
    """``ntfy``: publish to a topic. What is left out comes from ``Settings.notifications``."""

    server: str | None = None
    topic: str | None = None
    token_key: str | None = None  # secret-store key of the access token
    priority: Literal["min", "low", "default", "high", "max"] = "default"
    tags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("server")
    @classmethod
    def _server(cls, v: str | None) -> str | None:
        return _http_url(v) if v else None

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str | None) -> str | None:
        if v and not _NTFY_TOPIC.match(v):
            raise ValueError("a topic uses letters, digits, _ and - (at most 64)")
        return v or None

    @field_validator("token_key")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        return _secret_name(v or None)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            if not _NTFY_TAG.match(tag):
                raise ValueError(f"bad tag {tag!r}")
        return v


_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


class WebhookParams(PWModel):
    """``webhook``: call a URL (Home Assistant, n8n, Discord, Slack, ...). The body is a Jinja2
    template that must render to JSON; without one a standard JSON object is sent."""

    url: str
    method: Literal["POST", "PUT", "PATCH", "GET"] = "POST"
    body: str | None = Field(None, max_length=20000)
    headers: dict[str, str] = Field(default_factory=dict)
    token_key: str | None = None  # secret-store key of a bearer token
    timeout_s: float = Field(15.0, ge=1.0, le=120.0)
    verify_tls: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _http_url(v)

    @field_validator("body")
    @classmethod
    def _body(cls, v: str | None) -> str | None:
        return _jinja(v, "body")

    @field_validator("token_key")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        return _secret_name(v or None)

    @field_validator("headers")
    @classmethod
    def _headers(cls, v: dict[str, str]) -> dict[str, str]:
        for name, value in v.items():
            if not _HEADER_NAME.match(name) or name.lower() in _FORBIDDEN_HEADERS:
                raise ValueError(f"header {name!r} is not allowed")
            if any(c in value for c in "\r\n\x00"):
                raise ValueError(f"header {name!r} has a line break in its value")
        return v


class ExportParams(PWModel):
    """``export``: write the change to a file. ``{id} {name} {datetime} {change_id}
    {info1..3} {date:<fmt>}`` in the path."""

    path: str = Field(min_length=1, max_length=500)
    format: Literal["html", "html_assets", "text", "diff_json"] = "html"
    highlight: bool = True  # html formats: mark the changes (off: the page as it is now)

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        from pagewatch.engine.actions.templating import PATH_VARS

        return _brace(v, PATH_VARS, "path")


class RunProgramParams(PWModel):
    """``run_program``: start a program, no shell. ``{url} {file_new} {file_old} {file_changes}
    {info1..3} {id} {name} {change_id} {datetime}`` in the arguments."""

    executable: str = Field(min_length=1, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=50)
    timeout_s: float = Field(60.0, ge=1.0, le=3600.0)
    cwd: str | None = None
    confirmed: bool = False  # spec: enabling it needs an explicit confirmation

    @field_validator("executable")
    @classmethod
    def _exe(cls, v: str) -> str:
        if any(c in v for c in "\r\n\x00"):
            raise ValueError("the executable path has a line break in it")
        return v

    @field_validator("args")
    @classmethod
    def _args(cls, v: list[str]) -> list[str]:
        from pagewatch.engine.actions.templating import ARG_VARS

        return [_brace(a, ARG_VARS, "argument") for a in v]

    @model_validator(mode="after")
    def _confirmed(self) -> RunProgramParams:
        if not self.confirmed:
            raise ValueError(
                "running a program needs an explicit confirmation (set 'confirmed' to true)"
            )
        return self


_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
SCRAPE_RESERVED = ("change_id", "detected_at", "bookmark_id", "bookmark", "url")


class ScrapeField(PWModel):
    name: str
    kind: Literal["css", "xpath", "regex"]
    expr: str = Field(min_length=1, max_length=1000)
    attr: str | None = None  # css/xpath: read this attribute instead of the element's text
    multiple: bool = False  # all matches joined with " | " (default: the first)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _FIELD_NAME.match(v):
            raise ValueError(f"field name {v!r} must be a plain identifier")
        if v in SCRAPE_RESERVED:
            raise ValueError(f"{v!r} is a column PageWatch adds itself")
        return v

    @model_validator(mode="after")
    def _expr(self) -> ScrapeField:
        if self.kind == "regex":
            try:
                re.compile(self.expr)
            except re.error as exc:
                raise ValueError(f"invalid regex {self.expr!r}: {exc}") from exc
        else:
            _check_selector(self.kind, self.expr)
        return self


class ScrapeParams(PWModel):
    """``scrape``: append one record per change, with the fields read from the new page."""

    path: str = Field(min_length=1, max_length=500)
    format: Literal["csv", "jsonl", "xml"] = "csv"
    fields: list[ScrapeField] = Field(min_length=1, max_length=50)

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        from pagewatch.engine.actions.templating import PATH_VARS

        return _brace(v, PATH_VARS, "path")

    @model_validator(mode="after")
    def _unique(self) -> ScrapeParams:
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("field names must be unique")
        return self


class ScriptStep(PWModel):
    """One step of the ``script`` action (WebSite-Watcher's script actions, declaratively)."""

    op: Literal["check_folder", "pause_bookmark"]
    name: str | None = None  # check_folder: the folder (and its subfolders) to check now
    minutes: int | None = Field(None, ge=1, le=60 * 24 * 30)  # pause_bookmark

    @model_validator(mode="after")
    def _args(self) -> ScriptStep:
        if self.op == "check_folder" and not (self.name or "").strip():
            raise ValueError("check_folder needs a folder 'name'")
        if self.op == "pause_bookmark" and self.minutes is None:
            raise ValueError("pause_bookmark needs 'minutes'")
        return self


class ScriptParams(PWModel):
    steps: list[ScriptStep] = Field(min_length=1, max_length=20)


ACTION_PARAMS: dict[ActionType, type[PWModel]] = {
    ActionType.EMAIL: EmailParams,
    ActionType.NTFY: NtfyParams,
    ActionType.WEBHOOK: WebhookParams,
    ActionType.EXPORT: ExportParams,
    ActionType.RUN_PROGRAM: RunProgramParams,
    ActionType.SCRAPE: ScrapeParams,
    ActionType.SCRIPT: ScriptParams,
}


class SmtpSettings(PWModel):
    """Outgoing mail (spec: SMTP over TLS; the password is a secret-store entry)."""

    host: str = ""
    port: int = Field(587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"  # none: only for a local relay
    username: str | None = None
    password_key: str = "smtp-password"
    from_addr: str = ""
    to: list[str] = Field(default_factory=list)  # default recipients
    timeout_s: float = Field(30.0, ge=1.0, le=300.0)
    verify_tls: bool = True

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if re.search(r"[\s/]", v):
            raise ValueError("expected a host name such as smtp.example.com")
        return v

    @field_validator("from_addr")
    @classmethod
    def _from(cls, v: str) -> str:
        return _address(v) if v.strip() else ""

    @field_validator("to")
    @classmethod
    def _to(cls, v: list[str]) -> list[str]:
        return [_address(a) for a in v]

    @field_validator("password_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _secret_name(v) or "smtp-password"


class NtfySettings(PWModel):
    """ntfy.sh or a self-hosted server. On the public server the topic name is the only
    protection unless the topic has access control (spec: ntfy exposure)."""

    server: str = "https://ntfy.sh"
    topic: str = ""
    token_key: str | None = None  # secret-store key of an access token
    timeout_s: float = Field(15.0, ge=1.0, le=120.0)

    @field_validator("server")
    @classmethod
    def _server(cls, v: str) -> str:
        return _http_url(v).rstrip("/")

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        v = v.strip()
        if v and not _NTFY_TOPIC.match(v):
            raise ValueError("a topic uses letters, digits, _ and - (at most 64)")
        return v

    @field_validator("token_key")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        return _secret_name(v or None)


class NotificationSettings(PWModel):
    smtp: SmtpSettings = Field(default_factory=SmtpSettings)
    ntfy: NtfySettings = Field(default_factory=NtfySettings)


# --------------------------------------------------------------------------------------
# Global settings
# --------------------------------------------------------------------------------------


def normalize_host(text: str) -> str:
    """The key the host limiter uses for a web address: lower-case host name, no scheme, port,
    path, user info or trailing dot (``https://Example.COM:8443/x`` -> ``example.com``)."""
    from urllib.parse import urlsplit

    t = text.strip()
    if "://" in t:
        try:
            t = urlsplit(t).hostname or ""
        except ValueError as exc:
            raise ValueError(f"not a host name: {text!r}") from exc
    else:
        t = t.split("/", 1)[0].rsplit("@", 1)[-1]
        if t.startswith("["):  # [::1]:8080
            t = t[1:].split("]", 1)[0]
        elif t.count(":") == 1:  # host:port (a bare IPv6 address has several colons)
            t = t.split(":", 1)[0]
    t = t.strip().lower().rstrip(".")
    if not t or len(t) > 253 or any(c.isspace() or c in '/\\?#"' for c in t):
        raise ValueError(f"not a host name: {text!r}")
    return t


class HostOverride(PWModel):
    """A website's exception to the politeness defaults (spec: Politeness and network)."""

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
    global_proxy_username: str | None = None  # the proxy's login; its password is a secret
    global_proxy_secret_key: str | None = None
    notify_on_first_check: bool = False
    toast_coalesce_s: float = Field(30.0, ge=0.0)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    keep_changed_versions: int = Field(20, ge=1)
    disk_cap_gb: float = Field(10.0, gt=0.0)
    backlog_warning: int = Field(200, ge=1)
    transient_retry_s: float = Field(60.0, ge=1.0)
    timezone: str | None = None  # IANA name for times/window/days; None = the system zone
    debug_logging: bool = False
    theme: Literal["dark", "light", "system"] = "dark"  # the desktop UI's colour scheme
    autowatch_state: Literal["running", "paused"] = "running"
    autowatch_paused_until: str | None = None
    # -- unattended operation (spec: Sleep, resume and connectivity; Continuous operation) ----
    # HEAD; any reply = online. Plain http on purpose: Microsoft's NCSI host serves no valid
    # certificate for www.msftconnecttest.com, so the https form always failed (hostname mismatch).
    connectivity_url: str = "http://www.msftconnecttest.com/connecttest.txt"
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
    # -- logins and politeness (M6) -------------------------------------------------------
    # Off until the portal experiment has been run and its findings recorded in DECISIONS.md
    # (spec: Stored-password login is opt-in, and only after the experiment).
    stored_password_logins: bool = False
    signin_prompt_enabled: bool = True  # the daily offer to sign in again (semi-attended)
    signin_prompt_time: str = "09:00"  # local clock
    stretch_hosts: bool = True  # stretch an over-limit host's intervals evenly (spec: Capacity)

    @field_validator("backup_time", "maintenance_time", "signin_prompt_time")
    @classmethod
    def _clock_time(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("expected HH:MM")
        return v

    @field_validator("global_proxy")
    @classmethod
    def _global_proxy(cls, v: str | None) -> str | None:
        return check_proxy_url(v) if v and v.strip() else None

    @field_validator("global_proxy_secret_key")
    @classmethod
    def _global_proxy_key(cls, v: str | None) -> str | None:
        return _secret_name(v) if v else None

    @field_validator("host_overrides", mode="before")
    @classmethod
    def _host_keys(cls, v: Any) -> Any:
        """Override keys are matched against ``host_of(url)``, so ``Example.com:8080`` and
        ``https://example.com/`` must both land on ``example.com``."""
        if not isinstance(v, dict):
            return v
        out: dict[str, Any] = {}
        for key, value in v.items():
            out[normalize_host(str(key))] = value
        return out

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


ActionJobStatus = Literal["queued", "done", "failed"]


class ActionJobOut(PWModel):
    """One action of one change (the Problems list shows the failed ones)."""

    id: int
    change_id: int
    bookmark_id: int | None
    bookmark_name: str | None
    action_index: int
    action_type: str
    status: ActionJobStatus
    attempts: int
    next_attempt_at: str | None
    last_error: str | None
    detected_at: str | None


class TestNotificationRequest(PWModel):
    """Send a sample through a channel with the saved settings; ``params`` are the action's own."""

    channel: Literal["email", "ntfy", "webhook"]
    params: dict[str, Any] = Field(default_factory=dict)


class TestNotificationOut(PWModel):
    ok: bool
    detail: str


class SecretIn(PWModel):
    value: str = Field(min_length=1, max_length=4096)


class SecretOut(PWModel):
    """Values are write-only: this says whether a secret exists, never what it is."""

    name: str
    exists: bool
    available: bool = True  # False: this machine has no usable secret store


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


# --------------------------------------------------------------------------------------
# M7: virtual folders, import / export, reports, plugins, the filter assistant
# --------------------------------------------------------------------------------------


class VirtualQuery(PWModel):
    """A saved search (``folder.query_json`` of a virtual folder). Every field that is set must
    match; the list endpoint resolves ``GET /bookmarks?folder=<virtual id>`` through it."""

    q: str | None = None  # substring of the name or the URL
    status: BookmarkStatus | None = None
    unread: bool | None = None
    enabled: bool | None = None
    folder: int | None = None  # a real folder, including its subfolders
    changed_within_h: float | None = Field(None, gt=0)
    keyword_hits: bool | None = None
    source_type: SourceType | None = None
    check_method: CheckMethod | None = None
    plugin: str | None = None
    min_errors: int | None = Field(None, ge=1)  # consecutive errors at least this many

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_none=True)


ImportFormat = Literal["auto", "netscape", "urls", "csv", "xlsx", "json"]
ExportFormat = Literal["json", "opml", "csv"]
EXPORT_VERSION = 1


class ImportRequest(PWModel):
    format: ImportFormat = "auto"
    filename: str | None = None  # a hint for ``auto``
    content: str | None = None  # text formats
    content_base64: str | None = None  # xlsx
    folder_id: int | None = None  # import under this folder
    dry_run: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> ImportRequest:
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("give exactly one of 'content' and 'content_base64'")
        return self


class ImportRowError(PWModel):
    row: int
    message: str


class ImportResult(PWModel):
    format: str
    dry_run: bool
    created: int
    skipped_duplicates: int
    folders_created: int
    errors: list[ImportRowError] = Field(default_factory=list)
    bookmark_ids: list[int] = Field(default_factory=list)


class ExportFolder(PWModel):
    id: int
    parent_id: int | None
    name: str
    sort_order: int = 0
    is_virtual: bool = False
    query: dict[str, Any] | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)


class ExportBookmark(PWModel):
    """One bookmark as exported: its stored overrides, never the inherited values, and no secrets
    (credential fields name a keyring entry, they never hold the password)."""

    name: str
    url: str
    folder_id: int | None = None
    source_type: SourceType = SourceType.AUTO
    check_method: CheckMethod = CheckMethod.AUTO
    enabled: bool = True
    priority: int = 0
    highlight_mode: HighlightMode = HighlightMode.STANDARD
    schedule: dict[str, Any] = Field(default_factory=dict)
    fetch: dict[str, Any] = Field(default_factory=dict)
    filter: dict[str, Any] = Field(default_factory=dict)
    gate: dict[str, Any] = Field(default_factory=dict)
    actions: dict[str, Any] | list[Any] = Field(default_factory=dict)
    plugin: str | None = None
    info1: str | None = None
    info2: str | None = None
    info3: str | None = None
    note: str | None = None


class ExportDocument(PWModel):
    format: Literal["pagewatch-export"] = "pagewatch-export"
    version: int = EXPORT_VERSION
    exported_at: str
    app_version: str
    folders: list[ExportFolder] = Field(default_factory=list)
    bookmarks: list[ExportBookmark] = Field(default_factory=list)


ReportFormat = Literal["html", "csv", "json", "xml"]


class ReportRequest(PWModel):
    format: ReportFormat | None = None  # default: the template's own
    since: str | None = None  # ISO-8601 UTC; default for ``changed``: the last report
    mark: bool = True  # move the "last report" marker to now
    folder_id: int | None = None


class ReportInfo(PWModel):
    name: str
    title: str
    builtin: bool
    formats: list[str]


PLUGIN_HOOKS = (
    "before_check",
    "merge_pages",
    "preprocess",
    "compare",
    "check_keywords",
    "on_change",
    "after_check",
)


class PluginInfo(PWModel):
    name: str
    installed: bool
    example: bool  # shipped with PageWatch
    hooks: list[str] = Field(default_factory=list)
    description: str | None = None
    error: str | None = None  # why it cannot be loaded


class PluginInstallRequest(PWModel):
    name: str = Field(min_length=1, max_length=80)
    overwrite: bool = False


FilterChoiceId = Literal[
    "ignore_element", "watch_element", "ignore_text", "ignore_to_here", "ignore_from_here"
]


class FilterAssistRequest(PWModel):
    """What the viewer's bridge reports: the element(s) holding the selection and its text."""

    path: str = Field(min_length=1)  # DOM path (``data-pw-path``) of the selected element
    end_path: str | None = None  # where the selection ends, if elsewhere
    text: str = ""  # the selected text; empty when an element was right-clicked
    version: Literal["new", "old"] = "new"  # which stored page the user was looking at
    version_id: int | None = None  # ... or exactly this stored version (history view)
    levels_up: int = Field(0, ge=0, le=12)  # widen to the parent element this many times


class FilterChoice(PWModel):
    id: FilterChoiceId
    label: str
    section: Literal["ignore", "watch"]
    rule: FilterRule
    patch: dict[str, Any]  # a ready ``PATCH /bookmarks/{id}`` body that adds the rule
    explanation: str
    warnings: list[str] = Field(default_factory=list)


class FilterAssistElement(PWModel):
    tag: str
    path: str
    selector: str
    selector_kind: Literal["css", "xpath"]
    can_widen: bool  # a parent element (other than the page itself) exists
    levels_up: int


class FilterAssistOut(PWModel):
    element: FilterAssistElement | None
    selection: str  # the selection, whitespace-normalised
    block_text: str | None  # the page text block it falls in
    choices: list[FilterChoice]


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


class ShutdownOut(PWModel):
    stopping: bool
    message: str


ProblemKind = Literal["bookmark_error", "needs_login", "action_failed"]


class ProblemOut(PWModel):
    """One line of the Problems list: a bookmark that is failing, or an action that gave up."""

    kind: ProblemKind
    bookmark_id: int
    bookmark_name: str
    url: str
    message: str  # the last error: a check's failure code (`http_503`, `dns`) or the action's error
    occurred_at: (
        str | None
    )  # when the last failing check ran; for an action, when its change was found
    consecutive_errors: int | None = None  # bookmark problems
    job_id: int | None = None  # action problems
    change_id: int | None = None
    action_type: str | None = None
    attempts: int | None = None


class ProblemsOut(PWModel):
    items: list[ProblemOut]
    total: int  # all current problems, even when `items` was cut by `limit`


class PoolDemand(PWModel):
    """Load on one worker pool: checks per second the schedules ask for, against what the pool can
    do at the recent mean check time (``None`` until enough checks have been timed)."""

    name: Literal["static", "browser"]
    bookmarks: int
    demand_per_s: float
    size: int  # the pool's setting (static_pool / browser_pool)
    mean_check_s: float | None = None
    samples: int = 0
    capacity_per_s: float | None = None
    utilisation: float | None = None  # demand / capacity
    warn: bool = False  # above WARN_UTILISATION


class HostDemand(PWModel):
    host: str
    bookmarks: int
    demand_per_min: float
    ceiling_per_min: float | None = None  # 60 / minimum spacing; None = no spacing configured
    utilisation: float | None = None
    over_limit: bool = False  # demand above the ceiling
    stretch: float = 1.0  # how many times longer this host's checks wait because of it


class CapacityOut(PWModel):
    pools: list[PoolDemand]
    hosts: list[HostDemand]  # busiest first, cut to `hosts_shown`
    hosts_total: int
    hosts_over_limit: int
    warn_utilisation: float
    generated_at: str


# --- Logins: API bodies ------------------------------------------------------------------


class CookieImportIn(PWModel):
    """A Netscape ``cookies.txt``: its text, or the path of a file the engine can read."""

    text: str | None = Field(None, max_length=2_000_000)
    path: str | None = None

    @model_validator(mode="after")
    def _one(self) -> CookieImportIn:
        if (self.text is None) == (self.path is None):
            raise ValueError("give either 'text' or 'path'")
        return self


class CookieStatusOut(PWModel):
    """What a bookmark's imported cookies are, without their values."""

    present: bool = False
    count: int = 0
    expired: int = 0
    domains: list[str] = Field(default_factory=list)
    earliest_expiry: str | None = None  # of the cookies that have not expired
    imported_at: str | None = None
    skipped_lines: int = 0  # lines the parser could not read


class LoginStatusOut(PWModel):
    bookmark_id: int
    signed_in_mode: Literal["none", "server_auth", "cookies", "macro", "semi_attended"]
    needs_login: bool = False
    reason: str | None = None  # why: `login_signal`, `auth_rejected`, `macro_failed`, ...
    semi_attended: bool = False
    macro_id: int | None = None
    stored_password: bool = False  # the macro fills a stored password
    stored_password_allowed: bool = False  # the global switch AND this bookmark's opt-in
    last_signin_at: str | None = None
    last_auto_login_at: str | None = None
    next_auto_login_at: str | None = None  # earliest time a macro may log in again
    auto_login_blocked: bool = False  # a macro login failed: none until the owner resets it
    signing_in: bool = False  # a sign-in window is open right now
    profile_exists: bool = False
    cookies: CookieStatusOut = Field(default_factory=CookieStatusOut)


class SignInOut(PWModel):
    bookmark_id: int
    state: Literal["started", "finished", "idle"]
    message: str = ""


class RecordStartIn(PWModel):
    """Start Playwright codegen on ``url`` (opens a browser window on the owner's desktop)."""

    url: str
    password_secret_key: str = "macro-password"  # where recorded password fields will point

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://[^/\s]+", v, re.IGNORECASE):
            raise ValueError("the address must start with http:// or https://")
        return v

    @field_validator("password_secret_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _secret_name(v)


class RecordOut(PWModel):
    id: str
    state: Literal["running", "done", "failed", "cancelled"]
    steps: list[MacroStep] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


# --- The portal experiment (spec: Portal strategy -> Portal experiment) -----------------------

ChallengeKind = Literal["otp", "captcha", "other"]
DEFAULT_PROBE_MINUTES = (10, 20, 30, 60, 120)  # spec: check at 10, 20, 30, 60 and 120 minutes


class ExperimentIn(PWModel):
    """Run the spec's experiment on one semi-attended bookmark (with a macro for the logins)."""

    bookmark_id: int
    probe_minutes: list[int] = Field(
        default_factory=lambda: list(DEFAULT_PROBE_MINUTES), min_length=1, max_length=12
    )
    logins_per_day: int = Field(3, ge=2, le=4)  # spec: two to four spaced password logins a day
    duration_h: float = Field(48.0, gt=0.0, le=14 * 24.0)

    @field_validator("probe_minutes")
    @classmethod
    def _sorted(cls, v: list[int]) -> list[int]:
        if any(m <= 0 for m in v) or sorted(set(v)) != v:
            raise ValueError("probe times are positive minutes, strictly increasing")
        return v


class Observation(PWModel):
    at: str
    kind: Literal["signin", "probe", "login"]
    minutes_since_signin: float | None = None
    outcome: Literal["session_alive", "session_expired", "logged_in", "challenge", "failed"] | None
    challenge: ChallengeKind | None = None
    snapshot: str | None = None  # file name of a saved challenge page, in the experiment's folder
    note: str | None = None


class ExperimentOut(PWModel):
    id: str
    bookmark_id: int
    state: Literal["waiting_for_signin", "running", "done", "stopped", "cancelled"]
    started_at: str
    probe_minutes: list[int]
    logins_per_day: int
    duration_h: float
    signin_at: str | None = None
    ends_at: str | None = None
    observations: list[Observation] = Field(default_factory=list)
    stopped_because: str | None = None  # a login failed or met a challenge: no more attempts


class ExperimentReport(PWModel):
    id: str
    markdown: str  # paste into DECISIONS.md
    idle_timeout: str
    challenges: int
    login_attempts: int
    logins_succeeded: int
    stored_password_verdict: Literal["looks_safe", "not_safe", "not_enough_data"]


class LockInfo(PWModel):
    pid: int
    port: int
    token: str
    version: str
    started_at: str


EVENT_TYPES = (
    "signin",  # a sign-in window opened or closed: {bookmark_id, state}
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
