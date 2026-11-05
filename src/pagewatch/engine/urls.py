"""URL identity: decide whether two bookmarks point at the same page.

Used to de-duplicate imports and to keep Follow-Links from creating a bookmark twice. The key is
deliberately conservative: it only removes differences that never change which page is served
(scheme and host case, the default port, a fragment, a trailing slash).
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21, "ftps": 990}


def normalize_url(url: str) -> str:
    """A canonical spelling of ``url``; unparseable input is returned stripped and unchanged."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return text
    scheme = parts.scheme.lower()
    if not scheme:
        return text
    if scheme == "file":
        return urlunsplit(("file", "", parts.path, parts.query, ""))
    host = (parts.hostname or "").lower()
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    netloc = host
    if parts.username:
        user = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{user}@{netloc}"
    if port is not None and _DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{netloc}:{port}"
    path = parts.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    if path == "/":
        path = ""
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def host_key(url: str) -> str:
    """The host of ``url`` in lower case with a leading ``www.`` removed ("same site" test)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")
