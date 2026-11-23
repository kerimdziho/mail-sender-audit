"""The contract every mail provider implements so the scanner can stay provider-neutral."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from mail_sender_audit.models import ScannedMessage, ScanQuery


class MailSource(Protocol):
    """Read access to one mailbox.

    Implementations must be safe to call from several threads at once.
    Per-message problems (a message deleted mid-scan, say) are logged and the
    message is left out of the result; only errors that make further work
    pointless (bad credentials, server unreachable) are raised, as
    :class:`mail_sender_audit.errors.ProviderError` with ``fatal`` true.
    """

    @property
    def batch_size(self) -> int:
        """How many message ids :meth:`fetch_metadata` handles per call."""
        ...

    @property
    def max_concurrency(self) -> int:
        """The most simultaneous calls this provider tolerates."""
        ...

    def list_message_ids(self, query: ScanQuery, limit: int) -> list[str]:
        """Ids of up to ``limit`` inbox messages matching ``query``, newest first."""
        ...

    def fetch_metadata(self, message_ids: Sequence[str]) -> list[ScannedMessage]:
        """Headers of the given messages. Messages that cannot be read are omitted."""
        ...

    def fetch_body(self, message_id: str) -> tuple[str | None, str | None]:
        """``(html, plain_text)`` bodies of one message; ``(None, None)`` if unreadable."""
        ...
