"""Tests for mail_sender_audit.gmail (no network: requests and services are fakes)."""

from __future__ import annotations

import base64
import json
import threading
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mail_sender_audit.errors import GmailApiError
from mail_sender_audit.gmail import GmailClient, GmailSender, is_retryable
from mail_sender_audit.models import Json


def http_error(status: int, reason: str | None = None) -> HttpError:
    body: dict[str, object] = {}
    if reason is not None:
        body = {"error": {"errors": [{"reason": reason}]}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


class ScriptedRequest:
    """A request whose ``execute`` plays back a script of results and errors."""

    def __init__(self, *script: Json | Exception) -> None:
        self._script = list(script)
        self.calls = 0

    def execute(self) -> Json:
        self.calls += 1
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, Exception):
            raise item
        return item


def make_client(attempts: int = 6) -> GmailClient:
    def factory() -> Any:
        return object()

    return GmailClient(factory, requests_per_second=10_000, attempts=attempts, sleep=lambda _: None)


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (429, None, True),
        (500, None, True),
        (503, None, True),
        (403, "rateLimitExceeded", True),
        (403, "userRateLimitExceeded", True),
        (403, "forbidden", False),
        (401, None, False),
        (404, None, False),
    ],
)
def test_is_retryable_http_errors(status: int, reason: str | None, expected: bool) -> None:
    assert is_retryable(http_error(status, reason)) is expected


def test_is_retryable_network_errors() -> None:
    assert is_retryable(ConnectionResetError("reset"))
    assert is_retryable(httplib2.ServerNotFoundError("dns"))
    assert not is_retryable(ValueError("bug"))


def test_transient_errors_are_retried_then_succeed() -> None:
    request = ScriptedRequest(http_error(503), http_error(429), {"ok": True})

    result = make_client(attempts=4).execute(lambda svc: request)

    assert result == {"ok": True}
    assert request.calls == 3


def test_http_errors_become_gmail_api_errors_with_status() -> None:
    request = ScriptedRequest(http_error(404))

    with pytest.raises(GmailApiError) as excinfo:
        make_client().execute(lambda svc: request)

    assert excinfo.value.status == 404
    assert request.calls == 1  # not retryable


def test_exhausted_network_failures_become_gmail_api_errors_without_status() -> None:
    request = ScriptedRequest(ConnectionResetError("reset"))

    with pytest.raises(GmailApiError) as excinfo:
        make_client(attempts=2).execute(lambda svc: request)

    assert excinfo.value.status is None
    assert request.calls == 2


def test_each_thread_gets_its_own_service_built_once() -> None:
    built: list[int] = []

    def factory() -> Any:
        built.append(threading.get_ident())
        return object()

    client = GmailClient(factory, requests_per_second=10_000)
    request = ScriptedRequest({"ok": True})

    def call() -> None:
        client.execute(lambda svc: request)
        client.execute(lambda svc: request)

    call()
    worker = threading.Thread(target=call)
    worker.start()
    worker.join()

    assert len(built) == 2
    assert len(set(built)) == 2


def test_sender_posts_base64url_raw_message() -> None:
    captured: list[Json] = []

    class Messages:
        def send(self, *, userId: str, body: Json) -> ScriptedRequest:  # noqa: N803
            captured.append({"userId": userId, **body})
            return ScriptedRequest({"id": "sent"})

    class Users:
        def messages(self) -> Messages:
            return Messages()

    class Service:
        def users(self) -> Users:
            return Users()

    def service_factory() -> Any:
        return Service()

    client = GmailClient(service_factory, requests_per_second=10_000)
    GmailSender(client).send(b"To: a@example.com\r\n\r\nhi\r\n")

    assert captured[0]["userId"] == "me"
    assert base64.urlsafe_b64decode(captured[0]["raw"]) == b"To: a@example.com\r\n\r\nhi\r\n"
