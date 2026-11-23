"""End-to-end scanner tests against a fake Gmail service (no network)."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mail_sender_audit.errors import GmailApiError
from mail_sender_audit.gmail import GmailClient, GmailSource, gmail_search_query
from mail_sender_audit.models import Json, ScannedMessage, ScanQuery, Sender, UnsubscribeSource
from mail_sender_audit.scanner import InboxScanner, ScanOptions


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), json.dumps({}).encode())


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode("ascii")


def metadata(
    message_id: str,
    sender: str,
    *,
    internal: int,
    unsub: str | None = None,
    one_click: bool = False,
) -> Json:
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": "Hello"}]
    if unsub:
        headers.append({"name": "List-Unsubscribe", "value": unsub})
    if one_click:
        headers.append({"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"})
    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "internalDate": str(internal),
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": headers},
    }


def full(message_id: str, *, html: str | None = None, text: str | None = None) -> Json:
    parts: list[Json] = []
    if text:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(text)}})
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    return {"id": message_id, "payload": {"mimeType": "multipart/alternative", "parts": parts}}


class FakeRequest:
    def __init__(self, result: Json | Exception) -> None:
        self._result = result

    def execute(self) -> Json:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeMessages:
    def __init__(
        self,
        metadata_store: dict[str, Json],
        full_store: dict[str, Json] | None = None,
        *,
        ghosts: Sequence[str] = (),
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self._metadata = metadata_store
        self._full = full_store or {}
        self._ghosts = ghosts  # listed by Gmail but deleted before we fetch them
        self._failures = failures or {}

    def list(self, **kwargs: object) -> FakeRequest:
        ids = [*self._metadata, *self._ghosts]
        return FakeRequest({"messages": [{"id": message_id} for message_id in ids]})

    def get(self, *, userId: str, id: str, format: str, **kwargs: object) -> FakeRequest:
        if id in self._failures:
            return FakeRequest(self._failures[id])
        store = self._full if format == "full" else self._metadata
        if id not in store:
            return FakeRequest(http_error(404))
        return FakeRequest(store[id])


class FakeUsers:
    def __init__(self, messages: FakeMessages) -> None:
        self._messages = messages

    def messages(self) -> FakeMessages:
        return self._messages


class FakeService:
    def __init__(self, messages: FakeMessages) -> None:
        self._users = FakeUsers(messages)

    def users(self) -> FakeUsers:
        return self._users


FAST = ScanOptions(max_workers=2)


def make_scanner(messages: FakeMessages, options: ScanOptions = FAST) -> InboxScanner:
    def service_factory() -> Any:
        return FakeService(messages)

    client = GmailClient(service_factory, requests_per_second=10_000, sleep=lambda _: None)
    return InboxScanner(GmailSource(client), options)


def test_scan_groups_senders_and_falls_back_to_body_links() -> None:
    messages = FakeMessages(
        {
            "m1": metadata(
                "m1",
                "Shop <news@shop.com>",
                internal=3000,
                unsub="<https://shop.com/u?id=1>",
                one_click=True,
            ),
            "m2": metadata(
                "m2",
                "Shop <news@shop.com>",
                internal=1000,
                unsub="<https://shop.com/u?id=2>",
                one_click=True,
            ),
            "m3": metadata("m3", "Bob <bob@friend.org>", internal=2000),
            "m4": metadata("m4", "Promo <promo@deals.io>", internal=2500),
        },
        {
            "m3": full("m3", text="See you at lunch!"),
            "m4": full("m4", html='<a href="https://deals.io/u?id=9">Unsubscribe here</a>'),
        },
        ghosts=["gone"],
    )

    result = make_scanner(messages).scan()

    assert result.messages_scanned == 4
    assert result.messages_skipped == 1
    assert [g.key for g in result.groups] == ["news@shop.com", "bob@friend.org", "promo@deals.io"]

    shop, friend, promo = result.groups
    assert shop.count == 2
    assert shop.unsubscribe is not None
    assert shop.unsubscribe.one_click_url == "https://shop.com/u?id=1"  # newest message wins
    assert friend.unsubscribe is None
    assert promo.unsubscribe is not None
    assert promo.unsubscribe.source is UnsubscribeSource.BODY
    assert promo.unsubscribe.web_urls == ("https://deals.io/u?id=9",)
    assert promo.unsubscribe.one_click_url is None  # scraped links are never auto-POSTed
    assert len(result.unsubscribable) == 2


def test_body_fallback_can_be_disabled() -> None:
    messages = FakeMessages(
        {"m4": metadata("m4", "Promo <promo@deals.io>", internal=1)},
        {"m4": full("m4", html='<a href="https://deals.io/u">Unsubscribe</a>')},
    )
    options = ScanOptions(max_workers=1, body_fallback=False)

    result = make_scanner(messages, options).scan()

    assert result.groups[0].unsubscribe is None


def test_authentication_failure_aborts_the_scan() -> None:
    messages = FakeMessages(
        {
            "ok": metadata("ok", "A <a@x.com>", internal=1),
            "bad": metadata("bad", "B <b@x.com>", internal=2),
        },
        failures={"bad": http_error(401)},
    )

    with pytest.raises(GmailApiError) as excinfo:
        make_scanner(messages).scan()

    assert excinfo.value.status == 401


def test_empty_inbox_returns_an_empty_result() -> None:
    messages = FakeMessages({})

    result = make_scanner(messages).scan()

    assert result.groups == ()
    assert result.messages_scanned == 0


def test_search_query_and_option_validation() -> None:
    query = ScanQuery(unread_only=True, newer_than_days=7)

    assert gmail_search_query(query) == "in:inbox is:unread newer_than:7d"
    assert ScanOptions(unread_only=True, newer_than_days=7).query == query
    with pytest.raises(ValueError, match="max_messages"):
        ScanOptions(max_messages=0)


# ------------------------------------------------- provider-neutral behaviour (any MailSource)


class RecordingSource:
    """A MailSource that serves canned messages in batches, like an IMAP server would."""

    batch_size = 2
    max_concurrency = 1

    def __init__(self, count: int) -> None:
        self.messages = [
            ScannedMessage(
                message_id=str(uid),
                thread_id=str(uid),
                sender=Sender(name="", address=f"s{uid % 2}@x.example"),
                subject="s",
                received=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=uid),
                unread=False,
                unsubscribe=None,
            )
            for uid in range(1, count + 1)
        ]
        self.batches: list[list[str]] = []
        self.threads: set[str] = set()
        self.query: ScanQuery | None = None
        self.bodies: list[str] = []

    def list_message_ids(self, query: ScanQuery, limit: int) -> list[str]:
        self.query = query
        return [m.message_id for m in self.messages][:limit]

    def fetch_metadata(self, message_ids: Sequence[str]) -> list[ScannedMessage]:
        self.batches.append(list(message_ids))
        self.threads.add(threading.current_thread().name)
        return [m for m in self.messages if m.message_id in message_ids]

    def fetch_body(self, message_id: str) -> tuple[str | None, str | None]:
        self.bodies.append(message_id)
        return '<a href="https://x.example/u">Unsubscribe</a>', None


class CountingProgress:
    def __init__(self) -> None:
        self.total = 0
        self.phases: list[str] = []

    def start(self, description: str, total: int | None) -> None:
        self.phases.append(description)

    def advance(self, step: int = 1) -> None:
        self.total += step


def test_scanner_batches_requests_and_reports_progress_per_message() -> None:
    source = RecordingSource(5)
    progress = CountingProgress()
    options = ScanOptions(max_workers=8, body_fallback=False)

    result = InboxScanner(source, options, progress).scan()

    assert sorted(source.batches) == [["1", "2"], ["3", "4"], ["5"]]
    assert result.messages_scanned == 5
    assert progress.total == 5  # one step per message, not per batch
    assert progress.phases == ["Listing inbox", "Reading message headers"]


def test_scanner_never_exceeds_the_providers_concurrency_limit() -> None:
    source = RecordingSource(40)

    InboxScanner(source, ScanOptions(max_workers=16, body_fallback=False)).scan()

    assert len(source.threads) == 1


def test_scanner_passes_the_query_through_and_uses_the_body_fallback() -> None:
    source = RecordingSource(4)
    options = ScanOptions(unread_only=True, newer_than_days=14, max_messages=4)

    result = InboxScanner(source, options).scan()

    assert source.query == ScanQuery(unread_only=True, newer_than_days=14)
    assert sorted(source.bodies) == ["3", "4"]  # newest message of each sender
    assert all(g.unsubscribe is not None for g in result.groups)
    assert all(
        g.unsubscribe.source is UnsubscribeSource.BODY for g in result.groups if g.unsubscribe
    )
