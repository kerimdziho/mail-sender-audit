"""Thin, resilient wrapper around the Gmail API client.

* One Google client per thread (``httplib2`` is not thread-safe), built lazily.
* One shared :class:`RateLimiter`, so total request rate stays under Gmail's
  per-user quota no matter how many threads are working.
* Transient failures (429, 5xx, rate-limit 403s, dropped connections) are
  retried with exponential backoff; everything that escapes is a
  :class:`GmailApiError`.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, TypeAlias

import httplib2
from googleapiclient.errors import HttpError

from mail_sender_audit.backoff import RateLimiter, retry_with_backoff
from mail_sender_audit.errors import GmailApiError
from mail_sender_audit.models import Json, ScannedMessage, ScanQuery
from mail_sender_audit.parsing import extract_bodies, parse_message_metadata

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource
    from googleapiclient.http import HttpRequest

logger = logging.getLogger(__name__)

ServiceFactory: TypeAlias = "Callable[[], Resource]"

_RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
_RATE_LIMIT_REASONS: Final = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
)
_LIST_PAGE_SIZE: Final = 500  # messages.list maximum
METADATA_HEADERS: Final = ("From", "Subject", "List-Unsubscribe", "List-Unsubscribe-Post")


def is_retryable(exc: BaseException) -> bool:
    """True for throttling, server-side and transient network errors."""
    if isinstance(exc, HttpError):
        status = int(exc.resp.status)
        if status in _RETRYABLE_STATUS:
            return True
        # Gmail reports per-user throttling as a 403 with a specific reason.
        return status == 403 and bool(_error_reasons(exc) & _RATE_LIMIT_REASONS)
    return isinstance(exc, httplib2.HttpLib2Error | OSError)


def _error_reasons(exc: HttpError) -> frozenset[str]:
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        return frozenset(str(item["reason"]) for item in payload["error"]["errors"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return frozenset()


class GmailClient:
    """Executes Gmail API requests with pacing, retries and error translation."""

    def __init__(
        self,
        service_factory: ServiceFactory,
        *,
        requests_per_second: float = 20.0,
        attempts: int = 6,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._factory = service_factory
        self._limiter = RateLimiter(requests_per_second)
        self._attempts = attempts
        self._sleep = sleep
        self._local = threading.local()

    def execute(self, build_request: Callable[[Resource], HttpRequest]) -> Json:
        """Build and run one request on this thread's client.

        ``build_request`` receives the Gmail service and returns an unexecuted
        request; it is rebuilt on every attempt.
        """
        service = self._service()

        def attempt() -> Json:
            self._limiter.acquire()  # retries are paced too
            result: Json = build_request(service).execute()
            return result

        try:
            return retry_with_backoff(
                attempt,
                is_retryable=is_retryable,
                attempts=self._attempts,
                sleep=self._sleep,
                on_retry=_log_retry,
            )
        except HttpError as exc:
            status = int(exc.resp.status)
            raise GmailApiError(f"Gmail API error {status}: {exc.reason}", status=status) from exc
        except (httplib2.HttpLib2Error, OSError) as exc:
            raise GmailApiError(f"Could not reach the Gmail API: {exc}") from exc

    def _service(self) -> Resource:
        service: Resource | None = getattr(self._local, "service", None)
        if service is None:
            service = self._factory()
            self._local.service = service
        return service


def _log_retry(exc: Exception, attempt: int, delay: float) -> None:
    logger.info("Gmail call failed (%s); retry %d in %.1fs", exc, attempt, delay)


class GmailSender:
    """Sends raw RFC 5322 messages from the signed-in account (used for ``mailto:`` unsubscribes)."""

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def send(self, raw_message: bytes) -> None:
        encoded = base64.urlsafe_b64encode(raw_message).decode("ascii")
        self._client.execute(
            lambda svc: svc.users().messages().send(userId="me", body={"raw": encoded})
        )


def gmail_search_query(query: ScanQuery) -> str:
    """Translate a provider-neutral query into Gmail search syntax."""
    terms = ["in:inbox"]
    if query.unread_only:
        terms.append("is:unread")
    if query.newer_than_days is not None:
        terms.append(f"newer_than:{query.newer_than_days}d")
    return " ".join(terms)


class GmailSource:
    """:class:`mail_sender_audit.source.MailSource` backed by the Gmail API."""

    batch_size = 1  # the API has no batch header fetch; calls run in parallel instead
    max_concurrency = 32

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def list_message_ids(self, query: ScanQuery, limit: int) -> list[str]:
        search = gmail_search_query(query)
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < limit:
            page = self._list_page(search, min(limit - len(ids), _LIST_PAGE_SIZE), page_token)
            ids.extend(str(m["id"]) for m in page.get("messages", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return ids[:limit]

    def fetch_metadata(self, message_ids: Sequence[str]) -> list[ScannedMessage]:
        messages: list[ScannedMessage] = []
        for message_id in message_ids:
            try:
                raw = self._get(message_id, "metadata")
            except GmailApiError as exc:
                if exc.fatal:
                    raise
                logger.warning("Skipping message %s: %s", message_id, exc)
                continue
            message = parse_message_metadata(raw)
            if message is not None:
                messages.append(message)
        return messages

    def fetch_body(self, message_id: str) -> tuple[str | None, str | None]:
        try:
            raw = self._get(message_id, "full")
        except GmailApiError as exc:
            if exc.fatal:
                raise
            logger.warning("Skipping body scan of message %s: %s", message_id, exc)
            return None, None
        return extract_bodies(raw.get("payload") or {})

    # ---------------------------------------------------------------- requests

    def _list_page(self, search: str, page_size: int, page_token: str | None) -> Json:
        params: Json = {"userId": "me", "q": search, "maxResults": page_size}
        if page_token:
            params["pageToken"] = page_token
        return self._client.execute(lambda svc: svc.users().messages().list(**params))

    def _get(self, message_id: str, fmt: str) -> Json:
        if fmt == "metadata":
            return self._client.execute(
                lambda svc: (
                    svc.users()
                    .messages()
                    .get(
                        userId="me",
                        id=message_id,
                        format=fmt,
                        metadataHeaders=list(METADATA_HEADERS),
                    )
                )
            )
        return self._client.execute(
            lambda svc: svc.users().messages().get(userId="me", id=message_id, format=fmt)
        )
