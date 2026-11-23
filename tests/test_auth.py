"""Tests for mail_sender_audit.auth. No network, no browser: the OAuth flow is stubbed."""

from __future__ import annotations

import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials

from mail_sender_audit import auth
from mail_sender_audit.config import SCOPES, AppPaths
from mail_sender_audit.errors import AuthNetworkError, CredentialsFileNotFoundError

READONLY = SCOPES[0]


def make_creds(
    *,
    scopes: tuple[str, ...] = SCOPES,
    token: str = "access",
    expired: bool = False,
) -> Credentials:
    creds = Credentials(
        token=token,
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=list(scopes),
    )
    # A saved token with no expiry is read back as already expired, so valid tokens
    # need a real expiry, as Google's always have.
    if expired:
        creds.expiry = datetime(2000, 1, 1)
    else:
        creds.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    return creds


class FlowSpy:
    """Stand-in for ``_run_interactive_flow`` that records whether it was called."""

    def __init__(self, result: Credentials | None = None) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, paths: AppPaths, *, open_browser: bool) -> Credentials:
        self.calls += 1
        if self.result is None:
            raise AssertionError("interactive flow must not run in this scenario")
        return self.result


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        client_secrets=tmp_path / "credentials.json",
        token_file=tmp_path / "data" / "token.json",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_token_roundtrip_is_private(paths: AppPaths) -> None:
    store = auth.TokenStore(paths.token_file)
    store.save(make_creds())

    assert stat.S_IMODE(paths.token_file.stat().st_mode) == 0o600
    loaded = store.load()
    assert loaded is not None
    assert loaded.refresh_token == "refresh"
    assert loaded.scopes == list(SCOPES)


def test_corrupt_token_is_ignored(paths: AppPaths) -> None:
    paths.token_file.parent.mkdir(parents=True)
    paths.token_file.write_text("{not json", encoding="utf-8")
    assert auth.TokenStore(paths.token_file).load() is None


def test_valid_cached_token_skips_flow(paths: AppPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    auth.TokenStore(paths.token_file).save(make_creds())
    spy = FlowSpy()
    monkeypatch.setattr(auth, "_run_interactive_flow", spy)

    creds = auth.authenticate(paths)

    assert creds.token == "access"
    assert spy.calls == 0


def test_expired_token_is_refreshed_and_saved(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = auth.TokenStore(paths.token_file)
    store.save(make_creds(expired=True))

    def fake_refresh(self: Credentials, request: object) -> None:
        self.token = "refreshed"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    spy = FlowSpy()
    monkeypatch.setattr(auth, "_run_interactive_flow", spy)

    creds = auth.authenticate(paths)

    assert creds.token == "refreshed"
    assert spy.calls == 0
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.token == "refreshed"


def test_revoked_refresh_token_falls_back_to_flow(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth.TokenStore(paths.token_file).save(make_creds(expired=True))

    def failing_refresh(self: Credentials, request: object) -> None:
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", failing_refresh)
    spy = FlowSpy(result=make_creds(token="brand-new"))
    monkeypatch.setattr(auth, "_run_interactive_flow", spy)

    creds = auth.authenticate(paths)

    assert creds.token == "brand-new"
    assert spy.calls == 1
    stored = auth.TokenStore(paths.token_file).load()
    assert stored is not None
    assert stored.token == "brand-new"


def test_network_failure_during_refresh_is_not_a_reauth(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth.TokenStore(paths.token_file).save(make_creds(expired=True))

    def offline(self: Credentials, request: object) -> None:
        raise TransportError("connection refused")

    monkeypatch.setattr(Credentials, "refresh", offline)
    monkeypatch.setattr(auth, "_run_interactive_flow", FlowSpy())

    with pytest.raises(AuthNetworkError):
        auth.authenticate(paths)


def test_token_with_missing_scope_triggers_flow(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth.TokenStore(paths.token_file).save(make_creds(scopes=(READONLY,)))
    spy = FlowSpy(result=make_creds(token="full-scope"))
    monkeypatch.setattr(auth, "_run_interactive_flow", spy)

    creds = auth.authenticate(paths)

    assert creds.token == "full-scope"
    assert spy.calls == 1


def test_missing_client_secrets_gives_actionable_error(paths: AppPaths) -> None:
    with pytest.raises(CredentialsFileNotFoundError) as excinfo:
        auth.authenticate(paths)

    assert excinfo.value.path == paths.client_secrets
    assert excinfo.value.hint is not None
