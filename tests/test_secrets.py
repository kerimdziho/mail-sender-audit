"""Tests for mail_sender_audit.secrets_store."""

from __future__ import annotations

import pytest

from mail_sender_audit.errors import PasswordUnavailableError
from mail_sender_audit.secrets_store import (
    PASSWORD_ENV_VAR,
    SERVICE_NAME,
    PasswordStore,
    resolve_password,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.data.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.data[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del self.data[(service_name, username)]


class BrokenKeyring:
    def get_password(self, service_name: str, username: str) -> str | None:
        raise RuntimeError("no secret service")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise RuntimeError("no secret service")

    def delete_password(self, service_name: str, username: str) -> None:
        raise RuntimeError("no secret service")


def test_save_get_delete_round_trip() -> None:
    backend = FakeKeyring()
    store = PasswordStore(backend)

    assert store.set("me@yahoo.com", "app-pw")
    assert store.get("me@yahoo.com") == "app-pw"
    assert backend.data == {(SERVICE_NAME, "me@yahoo.com"): "app-pw"}
    store.delete("me@yahoo.com")
    assert store.get("me@yahoo.com") is None
    store.delete("me@yahoo.com")  # deleting again is harmless


@pytest.mark.parametrize("backend", [None, BrokenKeyring()])
def test_missing_or_broken_keyring_degrades_gracefully(backend: BrokenKeyring | None) -> None:
    store = PasswordStore(backend)

    assert store.get("a@b.example") is None
    assert store.set("a@b.example", "pw") is False
    store.delete("a@b.example")


def test_environment_variable_wins_over_the_keyring() -> None:
    store = PasswordStore(FakeKeyring())
    store.set("a@b.example", "from-keyring")

    assert (
        resolve_password("a@b.example", store, environ={PASSWORD_ENV_VAR: "from-env"}) == "from-env"
    )
    assert resolve_password("a@b.example", store, environ={}) == "from-keyring"


def test_prompt_is_the_last_resort_and_nothing_is_cached() -> None:
    store = PasswordStore(FakeKeyring())

    assert resolve_password("a@b.example", store, prompt=lambda: "typed", environ={}) == "typed"
    assert store.get("a@b.example") is None


def test_no_password_anywhere_is_an_actionable_error() -> None:
    store = PasswordStore(None)

    for prompt in (None, lambda: ""):
        with pytest.raises(PasswordUnavailableError) as excinfo:
            resolve_password("a@b.example", store, prompt=prompt, environ={})
        assert PASSWORD_ENV_VAR in (excinfo.value.hint or "")
