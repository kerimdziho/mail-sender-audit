"""IMAP (read) and SMTP (send) access for every provider except Gmail's OAuth API.

Standard library only (``imaplib``, ``smtplib``, ``ssl``). Passwords are
supplied by the caller (see ``secrets_store``); for Yahoo, iCloud, Fastmail and
most others this is an *app password* rather than the account password.

All connections verify TLS certificates. Mail is opened read-only and bodies
are fetched with ``BODY.PEEK``, so nothing is ever marked as read.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import smtplib
import ssl
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Final, Protocol, TypeVar

from mail_sender_audit.backoff import RateLimiter, retry_with_backoff
from mail_sender_audit.errors import MailServerError
from mail_sender_audit.models import ScannedMessage, ScanQuery
from mail_sender_audit.parsing import build_scanned_message

logger = logging.getLogger(__name__)

T = TypeVar("T")

_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_METADATA_ITEMS: Final = (
    "(UID FLAGS INTERNALDATE "
    "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])"
)
_BODY_ITEMS: Final = "(BODY.PEEK[]<0.524288>)"  # at most the first 512 KiB of the message
_UID_RE: Final = re.compile(rb"\bUID (\d+)")
_FLAGS_RE: Final = re.compile(rb"\bFLAGS \(([^)]*)\)")
_INTERNALDATE_RE: Final = re.compile(
    rb'INTERNALDATE "\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})"'
)
_BUSY_MARKERS: Final = ("THROTTLED", "UNAVAILABLE", "TOO MANY", "RATE")
_AUTH_HINT: Final = (
    "Most providers need an app password, not your normal password. "
    "Create one in your account's security settings."
)


class Security(StrEnum):
    SSL = "ssl"  # TLS from the first byte (IMAP 993, SMTP 465)
    STARTTLS = "starttls"  # plain connection upgraded to TLS (SMTP 587)


@dataclass(frozen=True, slots=True)
class ImapSettings:
    host: str
    username: str
    port: int = 993
    security: Security = Security.SSL


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    host: str
    username: str
    port: int = 587
    security: Security = Security.STARTTLS


# ------------------------------------------------------------------------ transport


class ImapConnection(Protocol):
    """The slice of ``imaplib.IMAP4`` this module uses (also what tests fake)."""

    def login(self, user: str, password: str) -> tuple[str, list[Any]]: ...

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[Any]]: ...

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]: ...

    def logout(self) -> tuple[str, list[Any]]: ...


def connect_imap(settings: ImapSettings, timeout: float = 30.0) -> ImapConnection:
    """Open a TLS-protected, certificate-verified IMAP connection (not yet logged in)."""
    context = ssl.create_default_context()
    if settings.security is Security.SSL:
        return imaplib.IMAP4_SSL(settings.host, settings.port, ssl_context=context, timeout=timeout)
    plain = imaplib.IMAP4(settings.host, settings.port, timeout=timeout)
    plain.starttls(ssl_context=context)
    return plain


class SmtpConnection(Protocol):
    def send_message(self, msg: Any) -> Any: ...

    def quit(self) -> Any: ...


def connect_smtp(settings: SmtpSettings, password: str, timeout: float = 30.0) -> SmtpConnection:
    """Open a TLS-protected, certificate-verified, authenticated SMTP connection."""
    context = ssl.create_default_context()
    client: smtplib.SMTP
    if settings.security is Security.SSL:
        client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(settings.host, settings.port, timeout=timeout)
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
    client.login(settings.username, password)
    return client


# ------------------------------------------------------------------- pure parsing


def format_imap_date(day: date) -> str:
    """``date`` -> IMAP search format (``7-Nov-2026``), independent of the locale."""
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"


def parse_internaldate(meta: bytes) -> datetime | None:
    """Extract the INTERNALDATE from a FETCH response line as an aware UTC datetime."""
    match = _INTERNALDATE_RE.search(meta)
    if match is None:
        return None
    day, month, year, hour, minute, second, sign, off_h, off_m = match.groups()
    try:
        offset = timedelta(hours=int(off_h), minutes=int(off_m))
        zone = timezone(offset if sign == b"+" else -offset)
        moment = datetime(
            int(year),
            _MONTHS.index(month.decode("ascii").title()) + 1,
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=zone,
        )
    except ValueError:  # unknown month, impossible date, or out-of-range offset
        return None
    return moment.astimezone(UTC)


def _unfold(value: str) -> str:
    return re.sub(r"\r?\n[ \t]+", " ", value).strip()


def _header_map(literal: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in email.message_from_bytes(literal).items():
        headers.setdefault(name.lower(), _unfold(str(value)))
    return headers


def _fetch_records(data: Sequence[object]) -> list[tuple[bytes, bytes]]:
    """Group an ``imaplib`` FETCH reply into ``(response metadata, literal)`` pairs.

    ``imaplib`` returns ``[(prefix, literal), suffix, (prefix, literal), suffix, ...]``.
    Servers differ in whether UID/FLAGS/INTERNALDATE come before the literal
    (in ``prefix``) or after it (in ``suffix``), so both are joined.
    """
    records: list[tuple[bytes, bytes]] = []
    current: list[bytes] | None = None  # [prefix, literal, suffix]

    def flush() -> None:
        if current is not None:
            records.append((current[0] + current[2], current[1]))

    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            flush()
            current = [bytes(item[0]), bytes(item[1]), b""]
        elif isinstance(item, bytes) and current is not None:
            current[2] += item
    flush()
    return records


def parse_fetch_metadata(data: Sequence[object]) -> list[ScannedMessage]:
    """Turn the reply to :data:`_METADATA_ITEMS` into scanned messages."""
    messages: list[ScannedMessage] = []
    for meta, literal in _fetch_records(data):
        uid_match = _UID_RE.search(meta)
        if uid_match is None:
            continue
        uid = uid_match.group(1).decode("ascii")
        flags_match = _FLAGS_RE.search(meta)
        flags = flags_match.group(1).lower().split() if flags_match else []
        received = parse_internaldate(meta) or datetime.fromtimestamp(0, tz=UTC)
        messages.append(
            build_scanned_message(
                message_id=uid,
                thread_id=uid,
                received=received,
                unread=b"\\seen" not in flags,
                headers=_header_map(literal),
            )
        )
    return messages


def parse_fetch_body(data: Sequence[object]) -> tuple[str | None, str | None]:
    """``(html, plain_text)`` from the reply to :data:`_BODY_ITEMS`."""
    records = _fetch_records(data)
    if not records:
        return None, None
    message: Any = email.message_from_bytes(records[0][1], policy=email.policy.default)
    html = _part_text(message.get_body(preferencelist=("html",)))
    text = _part_text(message.get_body(preferencelist=("plain",)))
    return html, text


def _part_text(part: Any) -> str | None:
    if part is None:
        return None
    try:
        return str(part.get_content())
    except (LookupError, ValueError, UnicodeError):  # unknown charset / corrupt part
        return None


# ----------------------------------------------------------------------- IMAP source


class _ServerBusyError(Exception):
    """The server asked us to slow down (retryable)."""


def _is_transient(exc: Exception) -> bool:
    return isinstance(exc, imaplib.IMAP4.abort | OSError | _ServerBusyError)


def _text(exc: Exception) -> str:
    return " ".join(str(arg) for arg in exc.args).strip() or type(exc).__name__


def _safe_logout(conn: ImapConnection) -> None:
    try:
        conn.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


class ImapSource:
    """:class:`mail_sender_audit.source.MailSource` for any IMAP server.

    Each worker thread keeps its own logged-in connection (IMAP connections are
    not thread-safe). Providers cap simultaneous connections, so concurrency is
    deliberately low, and headers are fetched in batches rather than one by one.
    """

    batch_size = 100
    max_concurrency = 3

    def __init__(
        self,
        settings: ImapSettings,
        password: str,
        *,
        connect: Callable[[ImapSettings], ImapConnection] = connect_imap,
        requests_per_second: float = 5.0,
        attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        mailbox: str = "INBOX",
    ) -> None:
        self._settings = settings
        self._password = password
        self._connect = connect
        self._limiter = RateLimiter(requests_per_second)
        self._attempts = attempts
        self._sleep = sleep
        self._mailbox = mailbox
        self._local = threading.local()
        self._lock = threading.Lock()
        self._open_connections: list[ImapConnection] = []

    # ------------------------------------------------------------------ public

    def check(self) -> None:
        """Log in and open the mailbox, raising :class:`MailServerError` if that fails."""
        self._call(lambda conn: None)

    def list_message_ids(self, query: ScanQuery, limit: int) -> list[str]:
        criteria = ["UNSEEN"] if query.unread_only else ["ALL"]
        if query.newer_than_days is not None:
            since = datetime.now(UTC).date() - timedelta(days=query.newer_than_days)
            criteria += ["SINCE", format_imap_date(since)]

        def search(conn: ImapConnection) -> list[Any]:
            typ, data = conn.uid("SEARCH", *criteria)
            self._check(typ, data, "search the inbox")
            return data

        data = self._call(search)
        raw = data[0] if data and isinstance(data[0], bytes) else b""
        uids = sorted((int(token) for token in raw.split()), reverse=True)  # newest first
        return [str(uid) for uid in uids[:limit]]

    def fetch_metadata(self, message_ids: Sequence[str]) -> list[ScannedMessage]:
        if not message_ids:
            return []
        uid_set = _uid_set(message_ids)

        def fetch(conn: ImapConnection) -> list[Any]:
            typ, data = conn.uid("FETCH", uid_set, _METADATA_ITEMS)
            self._check(typ, data, "read message headers")
            return data

        try:
            data = self._call(fetch)
        except MailServerError as exc:
            if exc.fatal:
                raise
            logger.warning("Skipping %d messages: %s", len(message_ids), exc)
            return []
        return parse_fetch_metadata(data)

    def fetch_body(self, message_id: str) -> tuple[str | None, str | None]:
        uid_set = _uid_set([message_id])

        def fetch(conn: ImapConnection) -> list[Any]:
            typ, data = conn.uid("FETCH", uid_set, _BODY_ITEMS)
            self._check(typ, data, "read a message")
            return data

        try:
            data = self._call(fetch)
        except MailServerError as exc:
            if exc.fatal:
                raise
            logger.warning("Skipping body scan of message %s: %s", message_id, exc)
            return None, None
        return parse_fetch_body(data)

    def close(self) -> None:
        """Log out of every open connection."""
        with self._lock:
            connections, self._open_connections = self._open_connections, []
        for conn in connections:
            _safe_logout(conn)

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _check(typ: str, data: Sequence[object], action: str) -> None:
        if typ == "OK":
            return
        reply = " ".join(d.decode("utf-8", "replace") for d in data if isinstance(d, bytes))
        if any(marker in reply.upper() for marker in _BUSY_MARKERS):
            raise _ServerBusyError(reply or "server busy")
        raise MailServerError(f"The server refused to {action}: {reply or typ}", fatal=False)

    def _call(self, fn: Callable[[ImapConnection], T]) -> T:
        def attempt() -> T:
            self._limiter.acquire()
            conn = self._connection()
            try:
                return fn(conn)
            except (imaplib.IMAP4.abort, OSError):
                self._discard_connection()  # reconnect on the next attempt
                raise

        try:
            return retry_with_backoff(
                attempt,
                is_retryable=_is_transient,
                attempts=self._attempts,
                base_delay=1.0,
                max_delay=15.0,
                sleep=self._sleep,
                on_retry=lambda exc, n, delay: logger.info(
                    "IMAP call failed (%s); retry %d in %.1fs", exc, n, delay
                ),
            )
        except MailServerError:
            raise
        except _ServerBusyError as exc:
            raise MailServerError(f"The mail server is busy: {exc}", fatal=False) from exc
        except imaplib.IMAP4.abort as exc:
            raise MailServerError(f"Lost the connection to the mail server: {_text(exc)}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailServerError(f"Mail server error: {_text(exc)}", fatal=False) from exc
        except OSError as exc:
            raise MailServerError(f"Could not reach the mail server: {exc}") from exc

    def _connection(self) -> ImapConnection:
        conn: ImapConnection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
            with self._lock:
                self._open_connections.append(conn)
        return conn

    def _discard_connection(self) -> None:
        conn: ImapConnection | None = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is None:
            return
        with self._lock:
            if conn in self._open_connections:
                self._open_connections.remove(conn)
        _safe_logout(conn)

    def _open(self) -> ImapConnection:
        settings = self._settings
        conn = self._connect(settings)
        try:
            conn.login(settings.username, self._password)
        except imaplib.IMAP4.abort:
            _safe_logout(conn)
            raise
        except imaplib.IMAP4.error as exc:
            _safe_logout(conn)
            raise MailServerError(
                f"The mail server rejected the login for {settings.username}: {_text(exc)}",
                auth_failed=True,
                hint=_AUTH_HINT,
            ) from exc
        typ, data = conn.select(self._mailbox, readonly=True)
        if typ != "OK":
            _safe_logout(conn)
            raise MailServerError(f"Could not open the {self._mailbox} folder: {data}")
        return conn


def _uid_set(message_ids: Sequence[str]) -> str:
    """Validate and join UIDs for a command (they come from the server, but never trust input)."""
    if not all(item.isdigit() for item in message_ids):
        raise ValueError("IMAP message ids must be numeric UIDs")
    return ",".join(message_ids)


# ----------------------------------------------------------------------- SMTP sender


class SmtpSender:
    """:class:`mail_sender_audit.unsubscriber.MessageSender` that submits mail over SMTP.

    One authenticated connection is kept and reused (providers throttle repeated
    logins); calls are serialised with a lock. Most providers do not file mail
    submitted over SMTP in the Sent folder.
    """

    def __init__(
        self,
        settings: SmtpSettings,
        password: str,
        from_address: str,
        *,
        connect: Callable[[SmtpSettings, str], SmtpConnection] = connect_smtp,
    ) -> None:
        self._settings = settings
        self._password = password
        self._from = from_address
        self._connect = connect
        self._lock = threading.Lock()
        self._smtp: SmtpConnection | None = None

    def check(self) -> None:
        """Log in and disconnect, raising :class:`MailServerError` if that fails."""
        with self._lock:
            self._ensure()
            self._reset()

    def send(self, raw_message: bytes) -> None:
        message = email.message_from_bytes(raw_message, policy=email.policy.default)
        if "From" not in message:
            message["From"] = self._from
        with self._lock:
            try:
                for attempt in (1, 2):
                    try:
                        self._ensure().send_message(message)
                        return
                    except smtplib.SMTPServerDisconnected:
                        self._reset()  # idle connections get dropped; reconnect once
                        if attempt == 2:
                            raise
            except smtplib.SMTPAuthenticationError as exc:
                self._reset()
                raise MailServerError(
                    "The SMTP server rejected the login.", auth_failed=True, hint=_AUTH_HINT
                ) from exc
            except (smtplib.SMTPException, OSError) as exc:
                self._reset()
                raise MailServerError(
                    f"Could not send the message: {_text(exc)}", fatal=False
                ) from exc

    def close(self) -> None:
        with self._lock:
            self._reset()

    def _ensure(self) -> SmtpConnection:
        if self._smtp is None:
            try:
                self._smtp = self._connect(self._settings, self._password)
            except smtplib.SMTPAuthenticationError as exc:
                raise MailServerError(
                    "The SMTP server rejected the login.", auth_failed=True, hint=_AUTH_HINT
                ) from exc
            except (smtplib.SMTPException, OSError) as exc:
                raise MailServerError(f"Could not reach the SMTP server: {_text(exc)}") from exc
        return self._smtp

    def _reset(self) -> None:
        smtp, self._smtp = self._smtp, None
        if smtp is not None:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass
