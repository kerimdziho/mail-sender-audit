"""OAuth 2.0 authentication for the Gmail API.

Flow (desktop "installed app" loopback flow, no client secret leaves the machine):

1. Load the cached token from disk, if any.
2. Token valid and covering our scopes -> use it.
3. Token expired but refreshable      -> refresh silently, persist the result.
4. Otherwise                          -> open the browser for consent, persist the token.

The token file is written atomically with ``0600`` permissions.

Thread-safety: ``googleapiclient`` services wrap ``httplib2``, which is *not*
thread-safe. Share the :class:`Credentials` between worker threads, but call
:func:`build_service` once per thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from mail_sender_audit.config import SCOPES, AppPaths
from mail_sender_audit.errors import (
    AuthFlowError,
    AuthNetworkError,
    CredentialsFileNotFoundError,
    GmailApiError,
    InsufficientScopesError,
    InvalidClientSecretsError,
    TokenStorageError,
)
from mail_sender_audit.fsutil import write_private_text

logger = logging.getLogger(__name__)

REVOKE_URL: Final = "https://oauth2.googleapis.com/revoke"
_REQUIRED_SCOPES: Final = frozenset(SCOPES)


# ----------------------------------------------------------------- token persistence


class TokenStore:
    """Reads and writes OAuth credentials as JSON with owner-only permissions."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Credentials | None:
        """Return stored credentials, or ``None`` if absent or unreadable.

        A corrupt token is not fatal: we log it and let the caller start a fresh
        sign-in, which will overwrite the file.
        """
        if not self._path.is_file():
            return None
        try:
            # scopes=None -> use the scopes that were *granted*, as saved in the file.
            creds: Credentials = Credentials.from_authorized_user_file(str(self._path))
            return creds
        except (ValueError, KeyError, OSError) as exc:
            logger.warning("Ignoring unreadable token file %s: %s", self._path, exc)
            return None

    def save(self, creds: Credentials) -> None:
        """Atomically persist ``creds`` with owner-only permissions."""
        try:
            write_private_text(self._path, creds.to_json())
        except OSError as exc:
            raise TokenStorageError(f"Could not write token file {self._path}: {exc}") from exc

    def delete(self) -> bool:
        """Delete the token file. Returns ``False`` if there was nothing to delete."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise TokenStorageError(f"Could not delete token file {self._path}: {exc}") from exc
        return True


# ------------------------------------------------------------------- public interface


def authenticate(
    paths: AppPaths,
    *,
    force_reauth: bool = False,
    open_browser: bool = True,
) -> Credentials:
    """Return valid Gmail credentials, signing in interactively only if needed."""
    store = TokenStore(paths.token_file)
    creds = None if force_reauth else store.load()

    if creds is not None and not _has_required_scopes(creds):
        logger.info("Stored token lacks required scopes; signing in again.")
        creds = None

    if creds is not None:
        if creds.valid:
            logger.debug("Using cached access token.")
            return creds
        if creds.refresh_token and _try_refresh(creds, store):
            return creds

    creds = _run_interactive_flow(paths, open_browser=open_browser)
    store.save(creds)
    return creds


def build_service(creds: Credentials) -> Resource:
    """Build a Gmail API client. Call once per thread (see module docstring)."""
    # cache_discovery=False avoids a noisy, pointless file-cache warning.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_account_email(service: Resource) -> str:
    """Return the signed-in address; doubles as a cheap "is the token usable?" check."""
    try:
        profile = service.users().getProfile(userId="me").execute()
    except HttpError as exc:
        raise GmailApiError(
            f"Gmail rejected the profile request: {exc.reason}",
            status=exc.resp.status,
        ) from exc
    except OSError as exc:
        raise GmailApiError(f"Could not reach the Gmail API: {exc}") from exc
    return str(profile["emailAddress"])


@dataclass(frozen=True, slots=True)
class LogoutResult:
    local_removed: bool
    remote_revoked: bool


def logout(paths: AppPaths) -> LogoutResult:
    """Revoke the grant at Google (best effort) and delete the local token."""
    store = TokenStore(paths.token_file)
    creds = store.load()
    revoked = False
    if creds is not None:
        token = creds.refresh_token or creds.token
        if token:
            revoked = _revoke(token)
    return LogoutResult(local_removed=store.delete(), remote_revoked=revoked)


# ------------------------------------------------------------------------- internals


def _has_required_scopes(creds: Credentials) -> bool:
    return _REQUIRED_SCOPES <= set(creds.scopes or ())


def _try_refresh(creds: Credentials, store: TokenStore) -> bool:
    """Refresh in place. ``False`` means "token is dead, sign in again"."""
    try:
        creds.refresh(Request())
    except TransportError as exc:  # network down: signing in again would not help
        raise AuthNetworkError(
            f"Could not reach Google to refresh the access token: {exc}",
            hint="Check your internet connection and try again.",
        ) from exc
    except RefreshError as exc:  # revoked, expired (e.g. 7-day Testing-mode limit), ...
        logger.info("Stored token could not be refreshed (%s); signing in again.", exc)
        return False
    store.save(creds)
    logger.debug("Access token refreshed.")
    return True


def _run_interactive_flow(paths: AppPaths, *, open_browser: bool) -> Credentials:
    if not paths.client_secrets.is_file():
        raise CredentialsFileNotFoundError(paths.client_secrets)

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(paths.client_secrets), scopes=list(SCOPES)
        )
    except (ValueError, OSError) as exc:  # JSONDecodeError is a ValueError
        raise InvalidClientSecretsError(paths.client_secrets) from exc

    try:
        # port=0 -> the OS picks a free loopback port; the redirect never leaves localhost.
        # prompt="consent" guarantees Google issues a refresh token.
        creds: Credentials = flow.run_local_server(
            port=0,
            open_browser=open_browser,
            prompt="consent",
            authorization_prompt_message="Open this URL to sign in with Google:\n\n  {url}\n",
            success_message="Signed in. You can close this tab and return to the terminal.",
        )
    except (OAuth2Error, Warning) as exc:
        # oauthlib raises a bare ``Warning`` when the user unticks a scope on the
        # (granular) consent screen, so it must be caught explicitly.
        raise AuthFlowError(f"Google sign-in failed: {exc}") from exc
    except OSError as exc:
        raise AuthFlowError(f"Could not run the local sign-in server: {exc}") from exc

    missing = _REQUIRED_SCOPES - set(creds.scopes or ())
    if missing:
        raise InsufficientScopesError(frozenset(missing))
    return creds


def _revoke(token: str) -> bool:
    try:
        # Token goes in the form body, not the URL, so it can't leak via logs.
        response = httpx.post(REVOKE_URL, data={"token": token}, timeout=10.0)
    except httpx.HTTPError as exc:
        logger.warning("Could not reach Google to revoke the token: %s", exc)
        return False
    return response.status_code == httpx.codes.OK
