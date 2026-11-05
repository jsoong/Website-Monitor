"""Netscape ``cookies.txt`` import (spec: Logins and Check-Macros -> Cookie import).

The format is the one browser extensions and ``curl`` write: seven tab-separated fields per
line (domain, include-subdomains, path, secure, expiry, name, value), ``#`` comments, and the
``#HttpOnly_`` prefix some tools put on the domain. Cookies are turned into an ``httpx`` cookie
jar for static checks and into Playwright cookies for browser checks. Their values are secrets:
nothing here logs them, and the summaries leave them out.
"""

from __future__ import annotations

import http.cookiejar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from pagewatch.models import CookieStatusOut

MAX_COOKIES = 5000
HTTP_ONLY_PREFIX = "#HttpOnly_"


class CookieFileError(ValueError):
    """The file is not a usable cookies.txt (nothing in it could be read)."""


@dataclass(frozen=True, slots=True)
class CookieRecord:
    domain: str  # as written, minus any #HttpOnly_ prefix; a leading dot is kept
    include_subdomains: bool
    path: str
    secure: bool
    expires: int | None  # epoch seconds; None for a session cookie
    name: str
    value: str
    http_only: bool = False

    @property
    def host(self) -> str:
        return self.domain.lstrip(".").lower()

    def expired(self, now: float) -> bool:
        return self.expires is not None and self.expires <= now


@dataclass(slots=True)
class ParsedCookies:
    cookies: list[CookieRecord]
    skipped: int = 0  # lines that looked like cookies but could not be read


def parse_netscape(text: str) -> ParsedCookies:
    cookies: list[CookieRecord] = []
    skipped = 0
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip("\r\n")
        if not line.strip():
            continue
        http_only = line.startswith(HTTP_ONLY_PREFIX)
        if http_only:
            line = line[len("#HttpOnly_") :]
        elif line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            fields = line.split(None, 6)  # tools that write spaces instead of tabs
        if len(fields) != 7:
            skipped += 1
            continue
        domain, subs, path, secure, expiry, name, value = fields
        try:
            expires = int(expiry) if expiry.strip() else 0
        except ValueError:
            skipped += 1
            continue
        if not domain.strip() or not name.strip() or subs.upper() not in ("TRUE", "FALSE"):
            skipped += 1
            continue
        if secure.upper() not in ("TRUE", "FALSE"):
            skipped += 1
            continue
        cookies.append(
            CookieRecord(
                domain=domain.strip(),
                include_subdomains=subs.upper() == "TRUE" or domain.startswith("."),
                path=path or "/",
                secure=secure.upper() == "TRUE",
                expires=expires if expires > 0 else None,
                name=name,
                value=value.rstrip("\r\n"),
                http_only=http_only,
            )
        )
        if len(cookies) > MAX_COOKIES:
            raise CookieFileError(f"more than {MAX_COOKIES} cookies: this is not a cookies.txt")
    return ParsedCookies(cookies, skipped)


def load_cookie_file(path: str | Path) -> ParsedCookies:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CookieFileError(f"cannot read the cookie file: {exc.strerror or exc}") from exc
    return parse_netscape(text)


def summarize(parsed: ParsedCookies, now: float) -> CookieStatusOut:
    """Counts, domains and the earliest expiry. Never the cookie values."""
    live = [c for c in parsed.cookies if not c.expired(now)]
    expiring = [c.expires for c in live if c.expires is not None]
    return CookieStatusOut(
        present=bool(parsed.cookies),
        count=len(parsed.cookies),
        expired=len(parsed.cookies) - len(live),
        domains=sorted({c.host for c in parsed.cookies}),
        earliest_expiry=(
            datetime.fromtimestamp(min(expiring), tz=UTC).isoformat().replace("+00:00", "Z")
            if expiring
            else None
        ),
        skipped_lines=parsed.skipped,
    )


def add_to_httpx(jar: httpx.Cookies, cookies: list[CookieRecord], now: float) -> int:
    """Put the unexpired cookies into an httpx jar. Returns how many were added."""
    added = 0
    for c in cookies:
        if c.expired(now):
            continue
        host = c.host
        # `http.cookiejar` treats a one-label host as "<host>.local" (its rule for localhost).
        domain = host if "." in host else f"{host}.local"
        initial_dot = c.include_subdomains and "." in host
        if initial_dot:
            domain = "." + host
        jar.jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=c.name,
                value=c.value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=initial_dot,
                path=c.path,
                path_specified=True,
                secure=c.secure,
                expires=c.expires,
                discard=c.expires is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if c.http_only else {},
                rfc2109=False,
            )
        )
        added += 1
    return added


def to_playwright(cookies: list[CookieRecord], now: float) -> list[dict[str, Any]]:
    """Playwright's ``add_cookies`` shape (a leading dot means "and subdomains")."""
    out: list[dict[str, Any]] = []
    for c in cookies:
        if c.expired(now):
            continue
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": ("." + c.host) if c.include_subdomains and "." in c.host else c.host,
                "path": c.path,
                "secure": c.secure,
                "httpOnly": c.http_only,
                "expires": float(c.expires) if c.expires is not None else -1.0,
            }
        )
    return out
