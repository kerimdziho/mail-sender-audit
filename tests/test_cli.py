"""CLI smoke tests. Skipped automatically when the CLI dependencies are not installed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("typer")
pytest.importorskip("rich")
pytest.importorskip("questionary")
pytest.importorskip("httpx")
pytest.importorskip("google_auth_oauthlib")

from typer.testing import CliRunner  # noqa: E402

from mail_sender_audit import __version__  # noqa: E402
from mail_sender_audit.cli import app  # noqa: E402
from mail_sender_audit.errors import UsageError  # noqa: E402

runner = CliRunner()

DESKTOP = {
    "installed": {
        "client_id": "123.apps.googleusercontent.com",
        "client_secret": "shh",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("setup", "accounts", "scan", "unsubscribe"):
        assert command in result.output


def test_accounts_help_lists_its_commands() -> None:
    result = runner.invoke(app, ["accounts", "--help"])

    assert result.exit_code == 0
    for command in ("add", "list", "remove"):
        assert command in result.output


def test_accounts_list_with_nothing_connected(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--accounts-file", str(tmp_path / "a.json"), "accounts", "list"])

    assert result.exit_code == 0
    assert "No accounts connected" in result.output


def test_scan_without_an_account_explains_what_to_do(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--accounts-file", str(tmp_path / "a.json"), "scan"])

    assert isinstance(result.exception, UsageError)
    assert "accounts add" in (result.exception.hint or "")


@pytest.mark.parametrize(
    ("address", "message"),
    [("bob@outlook.com", "not supported yet"), ("not-an-address", "not a valid email")],
)
def test_accounts_add_rejects_unsupported_or_invalid_addresses(
    tmp_path: Path, address: str, message: str
) -> None:
    result = runner.invoke(
        app, ["--accounts-file", str(tmp_path / "a.json"), "accounts", "add", address]
    )

    assert isinstance(result.exception, UsageError)
    assert message in str(result.exception)


def test_setup_installs_a_valid_client_file(tmp_path: Path) -> None:
    source = tmp_path / "client_secret.json"
    source.write_text(json.dumps(DESKTOP), encoding="utf-8")
    destination = tmp_path / "config" / "credentials.json"

    result = runner.invoke(
        app,
        [
            "--client-secrets",
            str(destination),
            "--token-file",
            str(tmp_path / "token.json"),
            "setup",
            str(source),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(destination.read_text(encoding="utf-8")) == DESKTOP
