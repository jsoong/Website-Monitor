"""The secret stores: keyring-backed, in memory, and none."""

from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

from pagewatch.engine.secrets import (
    KeyringSecrets,
    MemorySecrets,
    NullSecrets,
    SecretsUnavailable,
    default_secrets,
)


class FakeKeyring(KeyringBackend):
    """A credential store held in a dict, standing in for Windows Credential Manager."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise PasswordDeleteError("not found")
        del self.items[(service, username)]


@pytest.fixture
def backend() -> FakeKeyring:
    previous = keyring.get_keyring()
    fake = FakeKeyring()
    keyring.set_keyring(fake)
    yield fake  # type: ignore[misc]
    keyring.set_keyring(previous)


def test_keyring_secrets_store_read_and_delete(backend: FakeKeyring) -> None:
    store = KeyringSecrets()
    assert store.writable and store.get("smtp-password") is None
    store.set("smtp-password", "hunter2")
    assert store.get("smtp-password") == "hunter2"
    assert backend.items == {("PageWatch", "smtp-password"): "hunter2"}  # under the app's service
    store.delete("smtp-password")
    assert store.get("smtp-password") is None
    store.delete("smtp-password")  # deleting what is not there is fine


def test_default_secrets_uses_the_keyring_when_the_machine_has_one(backend: FakeKeyring) -> None:
    assert isinstance(default_secrets(), KeyringSecrets)


def test_default_secrets_is_null_without_a_usable_keyring() -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        assert isinstance(default_secrets(), NullSecrets)
    finally:
        keyring.set_keyring(previous)


def test_a_failing_keyring_reads_as_missing_and_refuses_writes() -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        store = KeyringSecrets()
        assert store.get("anything") is None  # a missing secret then fails with a clear message
        with pytest.raises(SecretsUnavailable):
            store.set("k", "v")
    finally:
        keyring.set_keyring(previous)


def test_null_secrets_know_nothing_and_cannot_store() -> None:
    store = NullSecrets()
    assert not store.writable and store.get("k") is None
    with pytest.raises(SecretsUnavailable, match="no secure credential store"):
        store.set("k", "v")
    store.delete("k")


def test_memory_secrets() -> None:
    store = MemorySecrets({"a": "1"})
    store.set("b", "2")
    assert (store.get("a"), store.get("b")) == ("1", "2")
    store.delete("a")
    assert store.get("a") is None and store.writable
