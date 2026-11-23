"""Tests for mail_sender_audit.imap against fake IMAP and SMTP servers (no network)."""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import pytest

from mail_sender_audit.errors import MailServerError
from mail_sender_audit.imap import (
    ImapSettings,
    ImapSource,
    Security,
    SmtpSender,
    SmtpSettings,
    format_imap_date,
    parse_fetch_body,
    parse_fetch_metadata,
    parse_internaldate,
)
from mail_sender_audit.models import ScanQuery

SETTINGS = ImapSettings(host="imap.example.com", username="me@example.com")
FIELDS = "FROM SUBJECT LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST"


def headers(sender: str, subject: str, unsub: str | None = None, one_click: bool = False) -> bytes:
    lines = [f"From: {sender}", f"Subject: {subject}"]
    if unsub:
        lines.append(f"List-Unsubscribe: {unsub}")
    if one_click:
        lines.append("List-Unsubscribe-Post: List-Unsubscribe=One-Click")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def metadata_reply(
    uid: int, flags: str, date: str, literal: bytes, *, suffix_style: bool = False
) -> list[Any]:
    """A FETCH reply entry. Servers differ in where UID/FLAGS/INTERNALDATE are placed."""
    if suffix_style:
        prefix = f"{uid} (BODY[HEADER.FIELDS ({FIELDS})] {{{len(literal)}}}".encode()
        return [(prefix, literal), f' UID {uid} FLAGS ({flags}) INTERNALDATE "{date}")'.encode()]
    prefix = (
        f'{uid} (UID {uid} FLAGS ({flags}) INTERNALDATE "{date}" '
        f"BODY[HEADER.FIELDS ({FIELDS})] {{{len(literal)}}}"
    ).encode()
    return [(prefix, literal), b")"]


@dataclass
class FakeMessage:
    flags: str = ""
    date: str = "17-Jul-2026 02:44:25 -0700"
    header_bytes: bytes = b""
    raw: bytes = b""


class FakeImap:
    """Just enough of imaplib.IMAP4 for ImapSource."""

    def __init__(
        self,
        messages: dict[int, FakeMessage] | None = None,
        *,
        login_error: Exception | None = None,
        scripted: list[Any] | None = None,
    ) -> None:
        self.messages = messages or {}
        self.login_error = login_error
        self.scripted = scripted or []  # replies/exceptions consumed by uid() first
        self.logins: list[tuple[str, str]] = []
        self.selected: list[tuple[str, bool]] = []
        self.commands: list[tuple[str, ...]] = []
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[Any]]:
        self.logins.append((user, password))
        if self.login_error is not None:
            raise self.login_error
        return "OK", [b"Logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[Any]]:
        self.selected.append((mailbox, readonly))
        return "OK", [b"3"]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.commands.append((command, *args))
        if self.scripted:
            item = self.scripted.pop(0)
            if isinstance(item, Exception):
                raise item
            return item  # type: ignore[no-any-return]
        if command == "SEARCH":
            wanted = [
                uid
                for uid, m in sorted(self.messages.items())
                if "UNSEEN" not in args or "\\Seen" not in m.flags
            ]
            return "OK", [" ".join(map(str, wanted)).encode()]
        if command == "FETCH":
            data: list[Any] = []
            for token in args[0].split(","):
                uid = int(token)
                message = self.messages[uid]
                if "HEADER.FIELDS" in args[1]:
                    data += metadata_reply(uid, message.flags, message.date, message.header_bytes)
                else:
                    prefix = f"{uid} (UID {uid} BODY[]<0> {{{len(message.raw)}}}".encode()
                    data += [(prefix, message.raw), b")"]
            return "OK", data
        raise AssertionError(f"unexpected command {command}")

    def logout(self) -> tuple[str, list[Any]]:
        self.logged_out = True
        return "BYE", [b"bye"]


def source_for(*connections: FakeImap, **kwargs: Any) -> tuple[ImapSource, list[float]]:
    """An ImapSource that hands out the given connections in order (then repeats the last)."""
    pool = list(connections)
    sleeps: list[float] = []

    def connect(settings: ImapSettings) -> FakeImap:
        return pool.pop(0) if len(pool) > 1 else pool[0]

    source = ImapSource(
        SETTINGS,
        "app-password",
        connect=connect,
        requests_per_second=10_000,
        sleep=sleeps.append,
        **kwargs,
    )
    return source, sleeps


# ------------------------------------------------------------------- pure parsing


def test_format_imap_date_ignores_locale() -> None:
    assert format_imap_date(datetime(2026, 11, 7, tzinfo=UTC).date()) == "7-Nov-2026"


def test_internaldate_is_converted_to_utc() -> None:
    parsed = parse_internaldate(b'5 (INTERNALDATE "17-Jul-2026 02:44:25 -0700")')

    assert parsed == datetime(2026, 7, 17, 9, 44, 25, tzinfo=UTC)
    assert parse_internaldate(b'INTERNALDATE "17-Foo-2026 02:44:25 +0000"') is None
    assert parse_internaldate(b"no date here") is None


def test_metadata_parsing_handles_both_server_response_layouts() -> None:
    data = [
        *metadata_reply(
            11,
            "\\Seen",
            "17-Jul-2026 02:44:25 -0700",
            headers("Shop <news@shop.example>", "Sale", "<https://shop.example/u>", True),
        ),
        *metadata_reply(
            12,
            "",
            "18-Jul-2026 10:00:00 +0200",
            headers("=?UTF-8?B?Q2Fmw6k=?= <hi@cafe.example>", "Hello", "<mailto:u@cafe.example>"),
            suffix_style=True,
        ),
    ]

    read, unread = parse_fetch_metadata(data)

    assert read.message_id == "11"
    assert not read.unread
    assert read.sender.address == "news@shop.example"
    assert read.received == datetime(2026, 7, 17, 9, 44, 25, tzinfo=UTC)
    assert read.unsubscribe is not None
    assert read.unsubscribe.one_click_url == "https://shop.example/u"
    assert unread.message_id == "12"
    assert unread.unread
    assert unread.sender.name == "Café"
    assert unread.unsubscribe is not None
    assert unread.unsubscribe.mailto[0].address == "u@cafe.example"


def test_folded_headers_are_unfolded() -> None:
    literal = b"From: A <a@x.example>\r\nSubject: Hi\r\nList-Unsubscribe: <https://x.example/u?a=1>,\r\n <mailto:u@x.example>\r\n\r\n"

    [message] = parse_fetch_metadata(metadata_reply(1, "", "01-Jan-2026 00:00:00 +0000", literal))

    assert message.unsubscribe is not None
    assert message.unsubscribe.web_urls == ("https://x.example/u?a=1",)
    assert message.unsubscribe.mailto[0].address == "u@x.example"


def test_body_parsing_returns_html_and_text() -> None:
    message = EmailMessage()
    message["Subject"] = "Promo"
    message.set_content("To unsubscribe visit https://x.example/u")
    message.add_alternative('<a href="https://x.example/u">Unsubscribe</a>', subtype="html")
    raw = message.as_bytes()

    html, text = parse_fetch_body([(b"1 (UID 1 BODY[]<0> {%d}" % len(raw), raw), b")"])

    assert html is not None
    assert "Unsubscribe</a>" in html
    assert text is not None
    assert "https://x.example/u" in text
    assert parse_fetch_body([]) == (None, None)


# ----------------------------------------------------------------- the IMAP source


def test_list_returns_newest_first_and_respects_the_limit() -> None:
    conn = FakeImap({uid: FakeMessage() for uid in (1, 2, 3, 4, 5)})
    source, _ = source_for(conn)

    assert source.list_message_ids(ScanQuery(), limit=3) == ["5", "4", "3"]
    assert conn.commands == [("SEARCH", "ALL")]
    assert conn.logins == [("me@example.com", "app-password")]
    assert conn.selected == [("INBOX", True)]  # read-only


def test_unread_and_recent_filters_become_imap_criteria() -> None:
    conn = FakeImap({1: FakeMessage(flags="\\Seen"), 2: FakeMessage()})
    source, _ = source_for(conn)

    ids = source.list_message_ids(ScanQuery(unread_only=True, newer_than_days=30), limit=10)

    assert ids == ["2"]
    command = conn.commands[0]
    assert command[:3] == ("SEARCH", "UNSEEN", "SINCE")
    assert re.fullmatch(r"\d{1,2}-[A-Z][a-z]{2}-\d{4}", command[3])


def test_fetch_metadata_peeks_so_mail_is_not_marked_read() -> None:
    conn = FakeImap(
        {
            7: FakeMessage(header_bytes=headers("A <a@x.example>", "One", "<https://x.example/u>")),
            8: FakeMessage(flags="\\Seen", header_bytes=headers("B <b@x.example>", "Two")),
        }
    )
    source, _ = source_for(conn)

    messages = source.fetch_metadata(["7", "8"])

    assert [m.message_id for m in messages] == ["7", "8"]
    assert [m.unread for m in messages] == [True, False]
    assert "BODY.PEEK" in conn.commands[0][2]
    assert conn.commands[0][1] == "7,8"


def test_non_numeric_ids_are_rejected_before_reaching_the_server() -> None:
    conn = FakeImap()
    source, _ = source_for(conn)

    with pytest.raises(ValueError, match="numeric"):
        source.fetch_metadata(["1", "2:*; DELETE"])
    assert conn.commands == []


def test_fetch_body_reads_the_message() -> None:
    message = EmailMessage()
    message.set_content("plain")
    message.add_alternative("<p>html</p>", subtype="html")
    conn = FakeImap({4: FakeMessage(raw=message.as_bytes())})
    source, _ = source_for(conn)

    html, text = source.fetch_body("4")

    assert html is not None
    assert "<p>html</p>" in html
    assert text is not None
    assert text.strip() == "plain"
    assert "BODY.PEEK" in conn.commands[0][2]


# ----------------------------------------------------------------- failure handling


def test_bad_credentials_are_a_fatal_auth_failure() -> None:
    conn = FakeImap(login_error=imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials"))
    source, _ = source_for(conn)

    with pytest.raises(MailServerError) as excinfo:
        source.check()

    assert excinfo.value.auth_failed
    assert excinfo.value.fatal
    assert "app password" in (excinfo.value.hint or "")
    assert conn.logged_out


def test_a_dropped_connection_is_retried_on_a_fresh_one() -> None:
    first = FakeImap({1: FakeMessage()}, scripted=[imaplib.IMAP4.abort("socket error: EOF")])
    second = FakeImap({1: FakeMessage()})
    source, sleeps = source_for(first, second)

    assert source.list_message_ids(ScanQuery(), limit=5) == ["1"]
    assert first.logged_out
    assert len(second.logins) == 1
    assert len(sleeps) == 1


def test_throttling_replies_are_retried() -> None:
    conn = FakeImap(
        {1: FakeMessage()}, scripted=[("NO", [b"[THROTTLED] Too many requests, slow down"])]
    )
    source, sleeps = source_for(conn)

    assert source.list_message_ids(ScanQuery(), limit=5) == ["1"]
    assert len(sleeps) == 1


def test_a_refused_fetch_skips_the_batch_instead_of_aborting() -> None:
    conn = FakeImap(scripted=[("NO", [b"[SERVERBUG] cannot fetch"])])
    source, _ = source_for(conn)

    assert source.fetch_metadata(["1", "2"]) == []


def test_unreachable_server_is_fatal_after_retries() -> None:
    sleeps: list[float] = []

    def refuse(settings: ImapSettings) -> FakeImap:
        raise ConnectionRefusedError("refused")

    source = ImapSource(
        SETTINGS,
        "pw",
        connect=refuse,
        requests_per_second=10_000,
        attempts=3,
        sleep=sleeps.append,
    )

    with pytest.raises(MailServerError) as excinfo:
        source.list_message_ids(ScanQuery(), limit=1)

    assert excinfo.value.fatal
    assert not excinfo.value.auth_failed
    assert len(sleeps) == 2


def test_each_thread_gets_its_own_connection_and_close_logs_all_out() -> None:
    connections = [FakeImap({1: FakeMessage()}) for _ in range(2)]
    source, _ = source_for(*connections)

    source.list_message_ids(ScanQuery(), limit=1)
    worker = threading.Thread(target=lambda: source.list_message_ids(ScanQuery(), limit=1))
    worker.start()
    worker.join()
    source.close()

    assert all(len(c.logins) == 1 for c in connections)
    assert all(c.logged_out for c in connections)


# ------------------------------------------------------------------- SMTP sender


class FakeSmtp:
    def __init__(self, *, fail_sends: list[Exception] | None = None) -> None:
        self.sent: list[Any] = []
        self.quit_called = False
        self._fail_sends = fail_sends or []

    def send_message(self, msg: Any) -> Any:
        if self._fail_sends:
            raise self._fail_sends.pop(0)
        self.sent.append(msg)

    def quit(self) -> Any:
        self.quit_called = True


def raw_unsubscribe() -> bytes:
    message = EmailMessage()
    message["To"] = "unsub@shop.example"
    message["Subject"] = "unsubscribe"
    message.set_content("unsubscribe")
    return message.as_bytes()


def smtp_sender(*connections: FakeSmtp) -> tuple[SmtpSender, list[int]]:
    pool = list(connections)
    connects: list[int] = []

    def connect(settings: SmtpSettings, password: str) -> FakeSmtp:
        connects.append(1)
        return pool.pop(0) if len(pool) > 1 else pool[0]

    sender = SmtpSender(
        SmtpSettings(host="smtp.example.com", username="me@example.com"),
        "pw",
        "me@example.com",
        connect=connect,
    )
    return sender, connects


def test_smtp_adds_the_from_header_and_reuses_the_connection() -> None:
    smtp = FakeSmtp()
    sender, connects = smtp_sender(smtp)

    sender.send(raw_unsubscribe())
    sender.send(raw_unsubscribe())

    assert len(smtp.sent) == 2
    assert smtp.sent[0]["From"] == "me@example.com"
    assert smtp.sent[0]["To"] == "unsub@shop.example"
    assert len(connects) == 1


def test_smtp_reconnects_once_when_the_server_dropped_us() -> None:
    stale = FakeSmtp(fail_sends=[smtplib.SMTPServerDisconnected("idle timeout")])
    fresh = FakeSmtp()
    sender, connects = smtp_sender(stale, fresh)

    sender.send(raw_unsubscribe())

    assert len(fresh.sent) == 1
    assert stale.quit_called
    assert len(connects) == 2


def test_smtp_login_failure_is_an_auth_error() -> None:
    def reject(settings: SmtpSettings, password: str) -> FakeSmtp:
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    sender = SmtpSender(
        SmtpSettings(host="smtp.example.com", username="me@example.com"),
        "pw",
        "me@example.com",
        connect=reject,
    )

    with pytest.raises(MailServerError) as excinfo:
        sender.check()

    assert excinfo.value.auth_failed


def test_smtp_rejection_is_reported_without_aborting_everything() -> None:
    smtp = FakeSmtp(fail_sends=[smtplib.SMTPRecipientsRefused({"a@x.example": (550, b"no")})])
    sender, _ = smtp_sender(smtp)

    with pytest.raises(MailServerError) as excinfo:
        sender.send(raw_unsubscribe())

    assert not excinfo.value.fatal


def test_smtp_check_logs_in_and_disconnects() -> None:
    smtp = FakeSmtp()
    sender, connects = smtp_sender(smtp)

    sender.check()

    assert smtp.quit_called
    assert len(connects) == 1


def test_security_modes_exist() -> None:
    assert {Security.SSL.value, Security.STARTTLS.value} == {"ssl", "starttls"}
    assert email.policy.default is not None  # the stdlib policy the module relies on
