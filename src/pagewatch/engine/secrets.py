"""Where fetchers look up passwords. The database stores key names only (spec: Secrets).

M4 only needs the interface (FTP logins); the Windows Credential Manager implementation arrives
with M6's logins. Until then the engine's default store knows nothing, so a bookmark that names a
secret fails with a clear message instead of trying an empty password.
"""

from __future__ import annotations

from typing import Protocol


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...


class NullSecrets:
    def get(self, key: str) -> str | None:
        return None


class MemorySecrets:
    """A fixed mapping; used by tests and by anything that already holds the values."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str) -> str | None:
        return self._values.get(key)
