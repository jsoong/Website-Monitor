"""Local files and folders (``file:///...``).

A file is read as bytes and goes through the same pipeline as a download; an unchanged
``mtime`` + size answers ``not_modified`` without reading it (the spec's shortcut, carried in the
version's ``etag`` column like a conditional GET). A folder becomes a listing table of name,
size and modified time, optionally recursive.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import time
from datetime import UTC, datetime
from email.utils import format_datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.models import FetchErrorKind

_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".log": "text/plain",
    ".htm": "text/html",
    ".html": "text/html",
    ".rss": "application/rss+xml",
    ".atom": "application/atom+xml",
    ".xml": "application/xml",
}


def content_type_for(name: str) -> str:
    ext = os.path.splitext(name.lower())[1]
    return _TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def url_to_path(url: str) -> Path:
    """``file:///C:/dir/a.txt`` and ``file:///home/me/a.txt`` to a ``Path``."""
    parts = urlsplit(url)
    raw = url2pathname(parts.path)
    if parts.netloc and parts.netloc not in ("", "localhost"):
        raw = f"//{parts.netloc}{parts.path}"  # \\server\share (UNC)
    return Path(raw)


def signature(st: os.stat_result) -> str:
    return f"{st.st_size}-{st.st_mtime_ns}"


def listing_html(rows: list[tuple[str, int | None, float | None]]) -> str:
    """A folder or FTP directory as a table: name, size, modified (UTC). Sorted by name."""
    out = []
    for name, size, mtime in sorted(rows, key=lambda r: r[0].casefold()):
        modified = datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%d %H:%M:%S") if mtime else ""
        out.append(
            f"<tr><td>{escape(name)}</td><td>{'' if size is None else size}</td>"
            f"<td>{modified}</td></tr>"
        )
    head = "<tr><th>Name</th><th>Size</th><th>Modified</th></tr>"
    return f"<!DOCTYPE html><html><body><table>{head}{''.join(out)}</table></body></html>"


def _http_error(status: int, message: str) -> FetchError:
    return FetchError(FetchErrorKind.HTTP, message, status=status)


class FileFetcher:
    async def aclose(self) -> None:
        return None

    async def fetch(self, request: FetchRequest) -> FetchResult:
        started = time.perf_counter()
        result = await asyncio.to_thread(self._read, request)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    # runs in a thread: it stats and reads files
    def _read(self, request: FetchRequest) -> FetchResult:
        cfg = request.resolved.fetch
        url = request.url

        def fail(err: FetchError) -> FetchResult:
            return FetchResult(url, None, {}, "", b"", error=err)

        try:
            path = url_to_path(url)
            st = path.stat()
        except FileNotFoundError:
            return fail(_http_error(404, f"not found: {url}"))
        except PermissionError:
            return fail(_http_error(403, f"permission denied: {url}"))
        except (OSError, ValueError) as exc:
            return fail(FetchError(FetchErrorKind.CONNECTION, f"{type(exc).__name__}: {exc}"[:300]))

        if path.is_dir():
            return self._listing(request, path)

        sig = signature(st)
        headers = {
            "etag": sig,
            "last-modified": format_datetime(datetime.fromtimestamp(st.st_mtime, UTC), usegmt=True),
        }
        ctype = content_type_for(path.name)
        if not request.force and request.etag == sig:
            return FetchResult(url, 200, headers, ctype, b"", not_modified=True)
        if st.st_size > cfg.max_bytes:
            return fail(
                FetchError(FetchErrorKind.TOO_LARGE, f"{st.st_size} bytes > {cfg.max_bytes}")
            )
        try:
            data = path.read_bytes()
        except PermissionError:
            return fail(_http_error(403, f"permission denied: {url}"))
        except OSError as exc:
            return fail(FetchError(FetchErrorKind.CONNECTION, f"{type(exc).__name__}: {exc}"[:300]))
        return FetchResult(url, 200, headers, ctype, data)

    def _listing(self, request: FetchRequest, root: Path) -> FetchResult:
        opts = request.resolved.fetch.listing
        rows: list[tuple[str, int | None, float | None]] = []
        try:
            walker = os.walk(root) if opts.recursive else [(str(root), [], os.listdir(root))]
            for dirpath, dirnames, names in walker:
                dirnames.sort()
                base = Path(dirpath)
                if opts.recursive:
                    entries = [(n, True) for n in dirnames] + [(n, False) for n in names]
                else:
                    entries = [(n, (base / n).is_dir()) for n in names]
                for name, is_dir in entries:
                    full = base / name
                    try:
                        st = full.stat()
                    except OSError:
                        continue  # vanished or unreadable between the listing and the stat
                    rel = full.relative_to(root).as_posix() + ("/" if is_dir else "")
                    rows.append((rel, None if is_dir else st.st_size, st.st_mtime))
                    if len(rows) > opts.max_entries:
                        return FetchResult(
                            request.url, None, {}, "", b"",
                            error=FetchError(
                                FetchErrorKind.TOO_LARGE,
                                f"more than {opts.max_entries} entries; raise listing.max_entries",
                            ),
                        )  # fmt: skip
        except PermissionError:
            return FetchResult(
                request.url, None, {}, "", b"",
                error=_http_error(403, f"permission denied: {request.url}"),
            )  # fmt: skip
        except OSError as exc:
            return FetchResult(
                request.url, None, {}, "", b"",
                error=FetchError(FetchErrorKind.CONNECTION, f"{type(exc).__name__}: {exc}"[:300]),
            )  # fmt: skip
        return FetchResult(
            request.url, 200, {}, "text/html; charset=utf-8", listing_html(rows).encode()
        )
