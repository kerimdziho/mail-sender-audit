"""Tests for mail_sender_audit.onboarding."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from mail_sender_audit.errors import CredentialsFileNotFoundError, InvalidClientSecretsError
from mail_sender_audit.onboarding import (
    find_downloaded_client_secrets,
    install_client_secrets,
    validate_client_secrets,
)

DESKTOP = {
    "installed": {
        "client_id": "123.apps.googleusercontent.com",
        "client_secret": "shh",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_desktop_client_file_is_accepted(tmp_path: Path) -> None:
    validate_client_secrets(write_json(tmp_path / "c.json", DESKTOP))


def test_web_client_is_rejected_with_a_helpful_reason(tmp_path: Path) -> None:
    path = write_json(tmp_path / "c.json", {"web": DESKTOP["installed"]})

    with pytest.raises(InvalidClientSecretsError, match="Web application"):
        validate_client_secrets(path)


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"installed": {"client_id": "x"}}', '{"installed": "nope"}', "{}"],
)
def test_malformed_files_are_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(InvalidClientSecretsError):
        validate_client_secrets(path)


def test_missing_file_has_its_own_error(tmp_path: Path) -> None:
    with pytest.raises(CredentialsFileNotFoundError):
        validate_client_secrets(tmp_path / "absent.json")


def test_install_copies_the_file_and_creates_parent_directories(tmp_path: Path) -> None:
    source = write_json(tmp_path / "downloaded.json", DESKTOP)
    destination = tmp_path / "config" / "mail-sender-audit" / "credentials.json"

    result = install_client_secrets(source, destination)

    assert result == destination
    assert json.loads(destination.read_text(encoding="utf-8")) == DESKTOP
    if sys.platform != "win32":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_install_refuses_invalid_input_and_writes_nothing(tmp_path: Path) -> None:
    source = write_json(tmp_path / "downloaded.json", {"web": {}})
    destination = tmp_path / "credentials.json"

    with pytest.raises(InvalidClientSecretsError):
        install_client_secrets(source, destination)

    assert not destination.exists()


def test_downloads_scan_finds_only_valid_client_files_newest_first(tmp_path: Path) -> None:
    old = write_json(tmp_path / "client_secret_old.apps.googleusercontent.com.json", DESKTOP)
    new = write_json(tmp_path / "client_secret_new.json", DESKTOP)
    write_json(tmp_path / "client_secret_web.json", {"web": {}})
    write_json(tmp_path / "something_else.json", DESKTOP)
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    assert find_downloaded_client_secrets([tmp_path]) == [new, old]


def test_missing_downloads_directory_is_fine(tmp_path: Path) -> None:
    assert find_downloaded_client_secrets([tmp_path / "nope"]) == []
