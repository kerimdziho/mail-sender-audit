"""Carry out (or simulate) unsubscribe requests.

For every mailing list the best available method is tried first, falling back
when it fails:

1. **one-click**: RFC 8058 HTTPS POST ``List-Unsubscribe=One-Click`` to the URL
   from the ``List-Unsubscribe`` header. Header-sourced URLs only.
2. **email**: send the ``mailto:`` request from your own account via the Gmail API.
3. **manual**: hand the link back so a human can open it. Plain web links are
   never requested automatically: a bare GET can be abused, and many sites
   need a confirmation click anyway.

The module is deliberately free of Google and HTTP libraries: the network is
reached through the :class:`OneClickPoster` and :class:`MessageSender`
protocols, which keeps every branch unit-testable.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from typing import Final, Protocol

from mail_sender_audit.backoff import retry_with_backoff
from mail_sender_audit.errors import ProviderError
from mail_sender_audit.models import (
    MailtoTarget,
    Method,
    PlannedAction,
    SenderGroup,
    Status,
    UnsubscribeInfo,
    UnsubscribeOutcome,
)
from mail_sender_audit.netsafety import (
    Resolver,
    UnresolvableHostError,
    UnsafeUrlError,
    ensure_public_https_url,
    system_resolver,
)
from mail_sender_audit.progress import NullProgress, Progress

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS: Final = frozenset({429, 500, 502, 503, 504})
_MAX_MAILTO_ATTEMPTS: Final = 2
_DEFAULT_SUBJECT: Final = "unsubscribe"


class PostError(Exception):
    """A network-level failure while POSTing (timeout, connection reset, TLS error...)."""


class OneClickPoster(Protocol):
    def post(self, url: str) -> int:
        """POST ``List-Unsubscribe=One-Click`` to ``url``; return the HTTP status code.

        Must not follow redirects. Raises :class:`PostError` on network failure.
        """
        ...


class MessageSender(Protocol):
    def send(self, raw_message: bytes) -> None:
        """Send a raw RFC 5322 message; raises ``ProviderError`` on failure."""
        ...


class _StepFailedError(Exception):
    """One delivery method did not work (internal; message is shown to the user)."""


class _TransientHttpError(Exception):
    """A retryable HTTP status (429/5xx)."""


# ------------------------------------------------------------------------ planning


def choose_method(info: UnsubscribeInfo) -> Method | None:
    """Best method for ``info``, or ``None`` when it offers nothing usable."""
    if info.one_click_url is not None:
        return Method.ONE_CLICK
    if info.mailto:
        return Method.EMAIL
    if info.web_urls:
        return Method.MANUAL
    return None


def best_method(group: SenderGroup) -> Method | None:
    """Best method any of the group's mailing lists offers (for display)."""
    order = (Method.ONE_CLICK, Method.EMAIL, Method.MANUAL)
    methods = {choose_method(t.info) for t in group.targets}
    return next((m for m in order if m in methods), None)


def plan_actions(groups: Iterable[SenderGroup]) -> list[PlannedAction]:
    """Expand groups into one action per mailing list (a domain can hold several)."""
    actions: list[PlannedAction] = []
    for group in groups:
        for target in group.targets:
            method = choose_method(target.info)
            if method is not None:
                actions.append(
                    PlannedAction(
                        label=group.label, address=target.address, method=method, info=target.info
                    )
                )
    return actions


def summarize_plan(actions: Sequence[PlannedAction]) -> str:
    """One line for prompts, e.g. ``5 mailing lists: 3 one-click, 1 email, 1 manual link``."""
    counts = Counter(action.method for action in actions)
    parts = [
        f"{counts[method]} {method.value}"
        for method in (Method.ONE_CLICK, Method.EMAIL, Method.MANUAL)
        if counts[method]
    ]
    noun = "mailing list" if len(actions) == 1 else "mailing lists"
    return f"{len(actions)} {noun}: {', '.join(parts)}"


# ------------------------------------------------------------------------ execution


class Unsubscriber:
    """Executes :class:`PlannedAction`s concurrently (or simulates them)."""

    def __init__(
        self,
        poster: OneClickPoster,
        sender: MessageSender,
        *,
        resolver: Resolver = system_resolver,
        max_workers: int = 4,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._poster = poster
        self._sender = sender
        self._resolver = resolver
        self._max_workers = max_workers
        self._attempts = attempts
        self._sleep = sleep

    def run(
        self,
        groups: Sequence[SenderGroup],
        *,
        dry_run: bool = False,
        progress: Progress | None = None,
    ) -> list[UnsubscribeOutcome]:
        """Process every mailing list in ``groups``; results keep the input order.

        With ``dry_run`` nothing is sent, resolved or requested.
        """
        actions = plan_actions(groups)
        sink: Progress = progress or NullProgress()

        if dry_run:
            return [self._simulate(action) for action in actions]

        sink.start("Unsubscribing", len(actions))

        def work(action: PlannedAction) -> UnsubscribeOutcome:
            try:
                return self._execute(action)
            finally:
                sink.advance()

        with ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="mail-sender-audit"
        ) as pool:
            return list(pool.map(work, actions))

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _simulate(action: PlannedAction) -> UnsubscribeOutcome:
        return UnsubscribeOutcome(
            label=action.label,
            address=action.address,
            status=Status.DRY_RUN,
            method=action.method,
            manual_url=_first_web_url(action.info) if action.method is Method.MANUAL else None,
        )

    def _execute(self, action: PlannedAction) -> UnsubscribeOutcome:
        info = action.info
        failures: list[str] = []

        def outcome(status: Status, method: Method | None, detail: str = "") -> UnsubscribeOutcome:
            return UnsubscribeOutcome(
                label=action.label,
                address=action.address,
                status=status,
                method=method,
                detail=detail,
                manual_url=_first_web_url(info) if status is Status.MANUAL else None,
            )

        url = info.one_click_url
        if url is not None:
            try:
                self._one_click(url)
            except UnresolvableHostError as exc:
                failures.append(f"one-click: {exc}")  # likely DNS trouble: try other methods
            except UnsafeUrlError as exc:
                # Points at a private or otherwise forbidden destination: treat the
                # message as hostile, try nothing else and never hand the link to a browser.
                return outcome(Status.FAILED, Method.ONE_CLICK, f"blocked: {exc}")
            except _StepFailedError as exc:
                failures.append(f"one-click: {exc}")
            else:
                return outcome(Status.REQUESTED, Method.ONE_CLICK)

        for target in info.mailto[:_MAX_MAILTO_ATTEMPTS]:
            try:
                self._send_mailto(target)
            except _StepFailedError as exc:
                failures.append(f"email: {exc}")
            else:
                return outcome(Status.REQUESTED, Method.EMAIL)

        note = "; ".join(failures)
        if _first_web_url(info) is not None:
            detail = f"automatic attempt failed ({note})" if note else ""
            return outcome(Status.MANUAL, Method.MANUAL, detail)
        return outcome(Status.FAILED, None, note or "no usable unsubscribe method")

    def _one_click(self, url: str) -> None:
        ensure_public_https_url(url, self._resolver)  # raises UnsafeUrlError

        def attempt() -> int:
            status = self._poster.post(url)
            if status in _TRANSIENT_STATUS:
                raise _TransientHttpError(f"HTTP {status}")
            return status

        try:
            status = retry_with_backoff(
                attempt,
                is_retryable=lambda exc: isinstance(exc, PostError | _TransientHttpError),
                attempts=self._attempts,
                base_delay=1.0,
                max_delay=8.0,
                sleep=self._sleep,
                on_retry=lambda exc, n, delay: logger.info(
                    "One-click POST failed (%s); retry %d in %.1fs", exc, n, delay
                ),
            )
        except (PostError, _TransientHttpError) as exc:
            raise _StepFailedError(str(exc)) from exc

        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise _StepFailedError(f"redirected (HTTP {status}); redirects are not followed")
        raise _StepFailedError(f"HTTP {status}")

    def _send_mailto(self, target: MailtoTarget) -> None:
        message = EmailMessage()
        try:
            message["To"] = target.address
            message["Subject"] = target.subject or _DEFAULT_SUBJECT
            message.set_content(target.body or _DEFAULT_SUBJECT)
            raw = message.as_bytes()
        except ValueError as exc:  # the email package refuses malformed headers
            raise _StepFailedError(f"invalid request ({exc})") from exc

        try:
            self._sender.send(raw)
        except ProviderError as exc:
            raise _StepFailedError(str(exc)) from exc


def _first_web_url(info: UnsubscribeInfo) -> str | None:
    return info.web_urls[0] if info.web_urls else None
