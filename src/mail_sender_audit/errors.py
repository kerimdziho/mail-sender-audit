"""Exception hierarchy for mail-sender-audit.

Every error the application raises *on purpose* derives from
:class:`MailSenderAuditError`. The CLI entry point catches that single base class,
prints a friendly message (plus an optional hint) and exits with the error's
``exit_code``. Anything else is a genuine bug and is allowed to produce a
traceback.

Keeping one base class means library code raises precise, typed exceptions
while only the CLI layer decides how to present them.
"""

from __future__ import annotations

from pathlib import Path


class MailSenderAuditError(Exception):
    """Base class for all expected, user-reportable errors."""

    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class UsageError(MailSenderAuditError):
    """The command was invoked in a way that cannot work (e.g. no terminal for a prompt)."""

    exit_code = 2


class UserCancelledError(MailSenderAuditError):
    """The user aborted an interactive prompt (Ctrl-C / Esc)."""

    exit_code = 130


# --------------------------------------------------------------------------- auth


class AuthError(MailSenderAuditError):
    """Anything that goes wrong while obtaining or storing credentials."""

    exit_code = 2


class CredentialsFileNotFoundError(AuthError):
    """The OAuth client JSON downloaded from Google Cloud Console is missing."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"OAuth client file not found: {path}",
            hint=(
                "Run 'mail-sender-audit setup' for a guided walkthrough, or create a 'Desktop app' "
                "OAuth client in Google Cloud Console, download the JSON and save it to that "
                "path (or pass --client-secrets). See README.md, 'Google Cloud setup'."
            ),
        )
        self.path = path


class InvalidClientSecretsError(AuthError):
    """The OAuth client JSON exists but is malformed or of the wrong type."""

    def __init__(self, path: Path, reason: str | None = None) -> None:
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"Could not use OAuth client file {path}{detail}",
            hint="Make sure you downloaded the JSON for a 'Desktop app' client, not a 'Web' one.",
        )
        self.path = path


class AuthFlowError(AuthError):
    """The interactive browser sign-in failed or was cancelled."""


class InsufficientScopesError(AuthError):
    """The user did not grant every permission the tool needs."""

    def __init__(self, missing: frozenset[str]) -> None:
        names = ", ".join(sorted(missing))
        super().__init__(
            f"Required Gmail permissions were not granted: {names}",
            hint="Run the login again and tick every requested permission on the consent screen.",
        )
        self.missing = missing


class AuthNetworkError(AuthError):
    """Google's token endpoint could not be reached (as opposed to rejecting us)."""


class TokenStorageError(AuthError):
    """The token file could not be written or deleted."""


# --------------------------------------------------------------------- mail providers


class ProviderError(MailSenderAuditError):
    """A mail provider (Gmail API, IMAP or SMTP server) call failed."""

    exit_code = 3

    @property
    def fatal(self) -> bool:
        """Fatal errors abort a scan; non-fatal ones only skip the affected message."""
        return True


class GmailApiError(ProviderError):
    """A Gmail API call failed."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def fatal(self) -> bool:
        # Auth failures and an unreachable network end the scan; anything else
        # (a message deleted mid-scan, say) only skips one message.
        return self.status is None or self.status in {401, 403}


class MailServerError(ProviderError):
    """An IMAP or SMTP server call failed."""

    def __init__(
        self,
        message: str,
        *,
        fatal: bool = True,
        auth_failed: bool = False,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self._fatal = fatal
        self.auth_failed = auth_failed

    @property
    def fatal(self) -> bool:
        return self._fatal


# ------------------------------------------------------------------------- accounts


class AccountsFileError(MailSenderAuditError):
    """The saved account list could not be read or written."""

    exit_code = 2


class PasswordUnavailableError(AuthError):
    """No password is stored for an account and none can be asked for."""
