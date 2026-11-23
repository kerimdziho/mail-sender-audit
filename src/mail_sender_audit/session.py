"""Open a connected account as a ready-to-use mail source and sender."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType

from mail_sender_audit import auth
from mail_sender_audit.accounts import Account, AccountKind
from mail_sender_audit.config import AppPaths
from mail_sender_audit.gmail import GmailClient, GmailSender, GmailSource
from mail_sender_audit.imap import ImapSource, SmtpSender
from mail_sender_audit.secrets_store import PasswordStore, resolve_password
from mail_sender_audit.source import MailSource
from mail_sender_audit.unsubscriber import MessageSender

#: Gentler than Gmail's API quota: IMAP providers throttle by connection and login.
_MAX_IMAP_REQUESTS_PER_SECOND = 5.0


class MailSession:
    """One account's reader and sender. Use as a context manager so connections are closed."""

    def __init__(
        self,
        account: Account,
        source: MailSource,
        sender: MessageSender,
        closers: tuple[Callable[[], None], ...] = (),
    ) -> None:
        self.account = account
        self.source = source
        self.sender = sender
        self._closers = closers

    def close(self) -> None:
        for close in self._closers:
            close()

    def __enter__(self) -> MailSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def open_session(
    account: Account,
    paths: AppPaths,
    *,
    passwords: PasswordStore,
    requests_per_second: float = 20.0,
    prompt_password: Callable[[], str] | None = None,
) -> MailSession:
    """Authenticate to ``account`` and return a session for it.

    Gmail accounts sign in with OAuth (opening the browser if the token expired).
    IMAP accounts take their password from the environment, the keyring, or
    ``prompt_password``.
    """
    if account.kind is AccountKind.GMAIL:
        creds = auth.authenticate(paths.for_account(account.email))
        client = GmailClient(
            lambda: auth.build_service(creds), requests_per_second=requests_per_second
        )
        return MailSession(account, GmailSource(client), GmailSender(client))

    assert account.imap is not None and account.smtp is not None  # guaranteed by Account
    password = resolve_password(account.email, passwords, prompt=prompt_password)
    imap = ImapSource(
        account.imap,
        password,
        requests_per_second=min(requests_per_second, _MAX_IMAP_REQUESTS_PER_SECOND),
    )
    smtp = SmtpSender(account.smtp, password, account.email)
    return MailSession(account, imap, smtp, closers=(imap.close, smtp.close))
