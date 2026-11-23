"""Immutable value objects shared by the scanner, the UI and the unsubscriber."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeAlias

#: A decoded JSON object as returned by the Gmail API.
Json: TypeAlias = dict[str, Any]


class GroupBy(StrEnum):
    """How scanned messages are bucketed for display and selection."""

    ADDRESS = "address"
    DOMAIN = "domain"


class UnsubscribeSource(StrEnum):
    """Where an unsubscribe option came from, which decides how far we trust it."""

    HEADER = "header"  # List-Unsubscribe header: sender-declared and machine-readable
    BODY = "body"  # scraped from the message body: only ever opened by a human


class Method(StrEnum):
    """How an unsubscribe request is (or would be) delivered, best first."""

    ONE_CLICK = "one-click"  # RFC 8058 HTTPS POST
    EMAIL = "email"  # mailto: request sent through the Gmail API
    MANUAL = "manual link"  # needs a human in a browser


class Status(StrEnum):
    REQUESTED = "requested"  # request delivered; the sender processes it on their side
    MANUAL = "manual"  # automatic methods unavailable or failed; open the link yourself
    FAILED = "failed"
    DRY_RUN = "dry run"  # nothing was sent


@dataclass(frozen=True, slots=True)
class ScanQuery:
    """Provider-neutral description of which inbox messages to look at."""

    unread_only: bool = False
    newer_than_days: int | None = None


@dataclass(frozen=True, slots=True)
class MailtoTarget:
    """A ``mailto:`` unsubscribe request, already sanitised against header injection."""

    address: str
    subject: str = ""
    body: str = ""


@dataclass(frozen=True, slots=True)
class UnsubscribeInfo:
    """Every way a sender offers to unsubscribe us."""

    source: UnsubscribeSource
    web_urls: tuple[str, ...] = ()
    mailto: tuple[MailtoTarget, ...] = ()
    one_click: bool = False  # List-Unsubscribe-Post: List-Unsubscribe=One-Click (RFC 8058)

    @property
    def one_click_url(self) -> str | None:
        """The HTTPS URL to POST to, or ``None`` if one-click is not available.

        Only header-sourced data qualifies: a link scraped from a message body
        must never be POSTed to automatically.
        """
        if not self.one_click or self.source is not UnsubscribeSource.HEADER:
            return None
        return next((u for u in self.web_urls if u.lower().startswith("https://")), None)


@dataclass(frozen=True, slots=True)
class Sender:
    name: str
    address: str  # lower-cased; "unknown" when the From header is unparsable

    @property
    def domain(self) -> str:
        if "@" not in self.address:
            return "unknown"
        return self.address.rpartition("@")[2]


@dataclass(frozen=True, slots=True)
class ScannedMessage:
    message_id: str
    thread_id: str
    sender: Sender
    subject: str
    received: datetime  # timezone-aware, UTC
    unread: bool
    unsubscribe: UnsubscribeInfo | None


@dataclass(frozen=True, slots=True)
class UnsubscribeTarget:
    """One mailing list: the sender address that sends it and how to leave it."""

    address: str
    info: UnsubscribeInfo


@dataclass(frozen=True, slots=True)
class SenderGroup:
    """All scanned messages from one sender (or domain), newest first."""

    key: str
    label: str
    messages: tuple[ScannedMessage, ...]
    #: One entry per distinct sender address that offers a way out, newest message
    #: wins. Address grouping therefore yields at most one target; domain grouping
    #: can yield several (``news@`` and ``billing@`` are separate lists).
    targets: tuple[UnsubscribeTarget, ...] = ()

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def unread_count(self) -> int:
        return sum(1 for m in self.messages if m.unread)

    @property
    def latest(self) -> ScannedMessage:
        return self.messages[0]

    @property
    def unsubscribe(self) -> UnsubscribeInfo | None:
        """The primary option (the first target's), for display."""
        return self.targets[0].info if self.targets else None


@dataclass(frozen=True, slots=True)
class ScanResult:
    groups: tuple[SenderGroup, ...]
    messages_scanned: int
    messages_skipped: int  # deleted mid-scan or failed after retries

    @property
    def unsubscribable(self) -> tuple[SenderGroup, ...]:
        return tuple(g for g in self.groups if g.targets)


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """What will happen for one mailing list if the user proceeds."""

    label: str
    address: str
    method: Method
    info: UnsubscribeInfo


@dataclass(frozen=True, slots=True)
class UnsubscribeOutcome:
    label: str
    address: str
    status: Status
    method: Method | None = None
    detail: str = ""
    manual_url: str | None = None
