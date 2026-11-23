"""Where IMAP passwords are kept: the operating system's keyring.

Passwords are never written to the accounts file or any other plain file. Where
no keyring is available (some headless Linux setups) the tool asks for the
password each run, or reads it from the ``MAIL_SENDER_AUDIT_PASSWORD`` environment
variable, which is also the way to use the tool from scripts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Final, Protocol

from mail_sender_audit.errors import PasswordUnavailableError

logger = logging.getLogger(__name__)

SERVICE_NAME: Final = "mail-sender-audit"
PASSWORD_ENV_VAR: Final = "MAIL_SENDER_AUDIT_PASSWORD"


class KeyringBackend(Protocol):
    """The three functions of the ``keyring`` module that are used."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


def system_keyring() -> KeyringBackend | None:
    """The ``keyring`` module, or ``None`` if it cannot be imported."""
    try:
        import keyring
    except ImportError:
        return None
    backend: KeyringBackend = keyring
    return backend


class PasswordStore:
    """Saves and retrieves passwords. Every method tolerates a missing or broken keyring."""

    def __init__(self, backend: KeyringBackend | None) -> None:
        self._backend = backend

    @classmethod
    def system(cls) -> PasswordStore:
        return cls(system_keyring())

    def get(self, email: str) -> str | None:
        if self._backend is None:
            return None
        try:
            return self._backend.get_password(SERVICE_NAME, email)
        except (
            Exception
        ) as exc:  # keyring backends raise assorted errors; none should be fatal here
            logger.debug("Keyring lookup failed: %s", exc)
            return None

    def set(self, email: str, password: str) -> bool:
        """Save ``password``. Returns ``False`` if there is no usable keyring."""
        if self._backend is None:
            return False
        try:
            self._backend.set_password(SERVICE_NAME, email, password)
        except Exception as exc:  # see get()
            logger.debug("Keyring save failed: %s", exc)
            return False
        return True

    def delete(self, email: str) -> None:
        if self._backend is None:
            return
        try:
            self._backend.delete_password(SERVICE_NAME, email)
        except Exception as exc:  # includes "no such password"; both are fine
            logger.debug("Keyring delete failed: %s", exc)


def resolve_password(
    email: str,
    store: PasswordStore,
    *,
    prompt: Callable[[], str] | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    """Find the password for ``email``: environment variable, keyring, then ``prompt``."""
    env = os.environ if environ is None else environ
    from_env = env.get(PASSWORD_ENV_VAR)
    if from_env:
        return from_env
    stored = store.get(email)
    if stored:
        return stored
    if prompt is not None:
        entered = prompt()
        if entered:
            return entered
    raise PasswordUnavailableError(
        f"No saved password for {email}.",
        hint=(
            "Run in a terminal to be asked for it, set the "
            f"{PASSWORD_ENV_VAR} environment variable, or reconnect with: "
            "mail-sender-audit accounts add"
        ),
    )
