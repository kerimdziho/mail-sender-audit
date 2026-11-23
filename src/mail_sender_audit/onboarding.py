"""First-run setup: validate and install the Google OAuth client file.

Pure standard library (no Google or terminal dependencies), so it is easy to test.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from mail_sender_audit.errors import (
    AuthError,
    CredentialsFileNotFoundError,
    InvalidClientSecretsError,
    MailSenderAuditError,
)
from mail_sender_audit.fsutil import write_private_text

_REQUIRED_KEYS: Final = ("client_id", "client_secret", "auth_uri", "token_uri")

SETUP_STEPS: Final[tuple[str, ...]] = (
    "Create a Google Cloud project: https://console.cloud.google.com/projectcreate",
    "Enable the Gmail API: https://console.cloud.google.com/apis/library/gmail.googleapis.com",
    "Configure Google Auth Platform (consent screen): https://console.cloud.google.com/auth/overview\n"
    "     Audience = External, and add your own Gmail address as a test user.",
    "Under Data Access, add the scopes gmail.readonly and gmail.send.",
    "Under Clients, create a client of type 'Desktop app' and download its JSON file.",
)


def validate_client_secrets(path: Path) -> None:
    """Raise unless ``path`` is a Google OAuth *Desktop app* client file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CredentialsFileNotFoundError(path) from exc
    except (OSError, ValueError) as exc:
        raise InvalidClientSecretsError(path, "not valid JSON") from exc

    if not isinstance(data, dict):
        raise InvalidClientSecretsError(path, "unexpected file layout")
    if "installed" not in data:
        reason = (
            "this is a 'Web application' client; create a 'Desktop app' client instead"
            if "web" in data
            else "no 'installed' section; is this the file from Google Cloud Console?"
        )
        raise InvalidClientSecretsError(path, reason)

    section = data["installed"]
    if not isinstance(section, dict) or any(
        not isinstance(section.get(key), str) or not section[key] for key in _REQUIRED_KEYS
    ):
        raise InvalidClientSecretsError(path, "missing client_id / client_secret / endpoints")


def install_client_secrets(source: Path, destination: Path) -> Path:
    """Validate ``source`` and store a private copy at ``destination``."""
    validate_client_secrets(source)
    try:
        write_private_text(destination, source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AuthError(f"Could not save the OAuth client file to {destination}: {exc}") from exc
    return destination


def find_downloaded_client_secrets(search_dirs: Iterable[Path] | None = None) -> list[Path]:
    """Valid ``client_secret*.json`` files in the Downloads folder, newest first."""
    directories = list(search_dirs) if search_dirs is not None else [Path.home() / "Downloads"]
    candidates: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob("client_secret*.json"):
            try:
                validate_client_secrets(path)
            except MailSenderAuditError:
                continue
            candidates.append(path)
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
