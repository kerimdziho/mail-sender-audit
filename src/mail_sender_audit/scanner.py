"""Scan a mailbox for mailing-list senders, whatever the provider.

Pipeline::

    list ids  ->  fetch headers in batches (thread pool)  ->  group by sender
              ->  fetch one body per sender with no header (fallback)

The scanner only knows the :class:`MailSource` contract, so Gmail (API) and
IMAP providers share this code. Threads rather than asyncio: both client
libraries are synchronous, so asyncio would just wrap them in threads anyway.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import TypeVar

from mail_sender_audit.grouping import group_messages
from mail_sender_audit.models import (
    GroupBy,
    ScanQuery,
    ScanResult,
    SenderGroup,
    UnsubscribeInfo,
    UnsubscribeSource,
    UnsubscribeTarget,
)
from mail_sender_audit.parsing import find_unsubscribe_links
from mail_sender_audit.progress import NullProgress, Progress
from mail_sender_audit.source import MailSource

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class ScanOptions:
    unread_only: bool = False
    newer_than_days: int | None = None
    max_messages: int = 500
    group_by: GroupBy = GroupBy.ADDRESS
    body_fallback: bool = True
    max_workers: int = 8  # an upper bound; the provider may allow fewer

    def __post_init__(self) -> None:
        if self.max_messages < 1:
            raise ValueError("max_messages must be at least 1")
        if self.max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if self.newer_than_days is not None and self.newer_than_days < 1:
            raise ValueError("newer_than_days must be at least 1")

    @property
    def query(self) -> ScanQuery:
        return ScanQuery(unread_only=self.unread_only, newer_than_days=self.newer_than_days)


class InboxScanner:
    """Finds mailing-list senders in the inbox of any :class:`MailSource`."""

    def __init__(
        self,
        source: MailSource,
        options: ScanOptions | None = None,
        progress: Progress | None = None,
    ) -> None:
        self._source = source
        self._options = options or ScanOptions()
        self._progress: Progress = progress or NullProgress()
        self._workers = min(self._options.max_workers, max(1, source.max_concurrency))

    def scan(self) -> ScanResult:
        self._progress.start("Listing inbox", None)
        ids = self._source.list_message_ids(self._options.query, self._options.max_messages)
        if not ids:
            return ScanResult(groups=(), messages_scanned=0, messages_skipped=0)

        self._progress.start("Reading message headers", len(ids))
        batches = _chunks(ids, max(1, self._source.batch_size))
        fetched = self._parallel_map(self._source.fetch_metadata, batches, weight=len)
        messages = [message for batch in fetched for message in batch]

        groups = group_messages(messages, self._options.group_by)
        if self._options.body_fallback:
            groups = self._apply_body_fallback(groups)

        return ScanResult(
            groups=groups,
            messages_scanned=len(messages),
            messages_skipped=len(ids) - len(messages),
        )

    # ------------------------------------------------------------ body fallback

    def _apply_body_fallback(self, groups: tuple[SenderGroup, ...]) -> tuple[SenderGroup, ...]:
        """For senders with no header, scan the newest message's body (one fetch each)."""
        pending = [g for g in groups if not g.targets]
        if not pending:
            return groups

        self._progress.start("Looking for unsubscribe links in message bodies", len(pending))
        found = self._parallel_map(lambda g: self._body_info(g.latest.message_id), pending)
        by_key = {
            g.key: UnsubscribeTarget(address=g.latest.sender.address, info=info)
            for g, info in zip(pending, found, strict=True)
            if info is not None
        }
        return tuple(replace(g, targets=(by_key[g.key],)) if g.key in by_key else g for g in groups)

    def _body_info(self, message_id: str) -> UnsubscribeInfo | None:
        html, text = self._source.fetch_body(message_id)
        links = find_unsubscribe_links(html=html, text=text)
        if not links:
            return None
        return UnsubscribeInfo(source=UnsubscribeSource.BODY, web_urls=links)

    # ---------------------------------------------------------------- plumbing

    def _parallel_map(
        self,
        fn: Callable[[T], R],
        items: Sequence[T],
        weight: Callable[[T], int] = lambda _item: 1,
    ) -> list[R]:
        """Apply ``fn`` to every item on the thread pool, preserving input order.

        Progress advances by ``weight(item)`` as each item completes. The first
        error cancels all queued work and propagates.
        """
        results: dict[int, R] = {}
        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="mail-scan") as pool:
            futures = {pool.submit(fn, item): index for index, item in enumerate(items)}
            try:
                for future in as_completed(futures):
                    index = futures[future]
                    results[index] = future.result()
                    self._progress.advance(weight(items[index]))
            except BaseException:  # includes KeyboardInterrupt: stop queued work promptly
                for future in futures:
                    future.cancel()
                raise
        return [results[index] for index in range(len(items))]


def _chunks(items: Sequence[T], size: int) -> list[list[T]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]
