"""Where passwords and tokens live. The database stores key names only (spec: Secrets).

* ``KeyringSecrets`` is the Windows Credential Manager (through ``keyring``): the SMTP password,
  the ntfy / webhook tokens and FTP passwords.
* ``NullSecrets`` is what a machine without a usable keyring gets: it knows nothing and refuses
  to store anything, so a bookmark that names a secret fails with a clear message instead of
  trying an empty password.
* ``MemorySecrets`` is a plain mapping, for tests and for anything that already holds the values.

Exports and backups never include secrets; the UI lists which credentials to re-enter.
"""

from __future__ import annotations

from typing import Protocol

from pagewatch.engine.logs import get_logger

log = get_logger("pagewatch.secrets")

SERVICE_NAME = "PageWatch"


class SecretsUnavailable(RuntimeError):
    """This machine has no secure place to keep secrets (or it refused the request)."""


class SecretStore(Protocol):
    @property
    def writable(self) -> bool: ...

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...


class NullSecrets:
    writable = False

    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str) -> None:
        raise SecretsUnavailable(
            "there is no secure credential store on this machine (Windows Credential Manager "
            "is used on Windows)"
        )

    def delete(self, key: str) -> None:
        return None


class MemorySecrets:
    """A mapping in memory; used by tests and by anything that already holds the values."""

    writable = True

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


class KeyringSecrets:
    """The OS credential store via ``keyring``. Every call can block (a COM or D-Bus round trip),
    so code on the event loop calls these through ``asyncio.to_thread``."""

    writable = True

    def __init__(self, service: str = SERVICE_NAME) -> None:
        self.service = service

    def get(self, key: str) -> str | None:
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(self.service, key)
        except KeyringError as exc:
            log.warning("keyring_read_failed", key=key, error=type(exc).__name__)
            return None

    def set(self, key: str, value: str) -> None:
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.set_password(self.service, key, value)
        except KeyringError as exc:
            raise SecretsUnavailable(f"the credential store refused to save it: {exc}") from exc

    def delete(self, key: str) -> None:
        import keyring
        from keyring.errors import KeyringError, PasswordDeleteError

        try:
            keyring.delete_password(self.service, key)
        except PasswordDeleteError:
            return  # nothing stored under that name: already what was asked for
        except KeyringError as exc:
            raise SecretsUnavailable(f"the credential store refused to delete it: {exc}") from exc


def default_secrets() -> SecretStore:
    """The OS credential store when this machine has a real one, else ``NullSecrets``."""
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring):
            return NullSecrets()
        return KeyringSecrets()
    except Exception:  # keyring missing, or its backend discovery failed
        log.warning("keyring_unavailable", exc_info=True)
        return NullSecrets()
