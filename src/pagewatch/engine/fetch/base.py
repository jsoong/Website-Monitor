"""Fetcher interface and the result every fetcher returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pagewatch.engine.config import Resolved
from pagewatch.engine.fetch.login import LoginPlan
from pagewatch.models import FetchErrorKind, Settings


@dataclass(slots=True)
class FetchError:
    kind: FetchErrorKind
    message: str
    status: int | None = None
    retry_after_s: float | None = None

    @property
    def transient(self) -> bool:
        """Worth one quick retry before it counts: timeouts, DNS, resets, 5xx and 429."""
        if self.kind in (FetchErrorKind.TIMEOUT, FetchErrorKind.DNS, FetchErrorKind.CONNECTION):
            return True
        if self.kind is FetchErrorKind.HTTP and self.status is not None:
            return self.status >= 500 or self.status in (408, 425, 429)
        return False

    @property
    def reason(self) -> str:
        """Short code recorded in ``check_run.reason``: ``http_503``, ``timeout``..."""
        if self.kind is FetchErrorKind.HTTP and self.status is not None:
            return f"http_{self.status}"
        return self.kind.value


@dataclass(slots=True)
class FetchResult:
    final_url: str
    status: int | None
    headers: dict[str, str]  # lower-cased names
    content_type: str
    body: bytes  # raw bytes; the browser method returns the serialised DOM
    screenshot_png: bytes | None = None
    elapsed_ms: int = 0
    not_modified: bool = False  # HTTP 304, or unchanged mtime+size for files
    error: FetchError | None = None
    extra: dict[str, str] = field(default_factory=dict)
    # -- logins (M6) ----------------------------------------------------------------------
    # The page the check ended on is a login page: the session is gone (spec: Session expiry).
    login_required: bool = False
    login_reason: str | None = None  # `login_signal`, `auth_rejected`, `macro_failed`, ...
    macro_ran: bool = False  # a Check-Macro logged in during this fetch
    macro_error: str | None = None

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


@dataclass(slots=True)
class FetchRequest:
    url: str
    resolved: Resolved
    settings: Settings
    etag: str | None = None  # of the latest version, for conditional GET
    last_modified: str | None = None
    force: bool = False
    bookmark_id: int | None = None  # signed-in checks keep a session of their own per bookmark
    login: LoginPlan | None = None  # macro / semi-attended sign-in (browser checks)


class Fetcher(Protocol):
    async def fetch(self, request: FetchRequest) -> FetchResult: ...

    async def aclose(self) -> None: ...
