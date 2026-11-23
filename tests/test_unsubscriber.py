"""Tests for mail_sender_audit.unsubscriber: every delivery path, with fake network and mail."""

from __future__ import annotations

import email
import email.policy

import pytest

from mail_sender_audit.errors import GmailApiError
from mail_sender_audit.models import (
    MailtoTarget,
    Method,
    SenderGroup,
    Status,
    UnsubscribeInfo,
    UnsubscribeSource,
    UnsubscribeTarget,
)
from mail_sender_audit.unsubscriber import (
    PostError,
    Unsubscriber,
    best_method,
    choose_method,
    plan_actions,
    summarize_plan,
)

URL = "https://shop.example/unsub?id=1"
MAILTO = MailtoTarget(
    address="unsub@shop.example", subject="Unsubscribe me", body="Please remove me"
)


def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def info(
    *,
    urls: tuple[str, ...] = (),
    mailto: tuple[MailtoTarget, ...] = (),
    one_click: bool = False,
    source: UnsubscribeSource = UnsubscribeSource.HEADER,
) -> UnsubscribeInfo:
    return UnsubscribeInfo(source=source, web_urls=urls, mailto=mailto, one_click=one_click)


def group(key: str, *infos: UnsubscribeInfo, label: str | None = None) -> SenderGroup:
    targets = tuple(
        UnsubscribeTarget(address=key if i == 0 else f"{i}-{key}", info=item)
        for i, item in enumerate(infos)
    )
    return SenderGroup(key=key, label=label or key, messages=(), targets=targets)


class FakePoster:
    """Plays back results (status codes or errors); repeats the last one when exhausted."""

    def __init__(self, *results: int | Exception) -> None:
        self._results = list(results)
        self.urls: list[str] = []

    def post(self, url: str) -> int:
        self.urls.append(url)
        item = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeSender:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[bytes] = []
        self._error = error

    def send(self, raw_message: bytes) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append(raw_message)


def make(
    poster: FakePoster | None = None,
    sender: FakeSender | None = None,
    *,
    sleeps: list[float] | None = None,
    **kwargs: int,
) -> Unsubscriber:
    return Unsubscriber(
        poster or FakePoster(200),
        sender or FakeSender(),
        resolver=public_resolver,
        sleep=(sleeps.append if sleeps is not None else (lambda _: None)),
        max_workers=kwargs.pop("max_workers", 1),
        **kwargs,
    )


# ------------------------------------------------------------------- planning


def test_choose_method_prefers_one_click_then_email_then_manual() -> None:
    assert choose_method(info(urls=(URL,), mailto=(MAILTO,), one_click=True)) is Method.ONE_CLICK
    assert choose_method(info(urls=(URL,), mailto=(MAILTO,))) is Method.EMAIL
    assert choose_method(info(urls=(URL,))) is Method.MANUAL
    assert choose_method(info()) is None


def test_body_sourced_links_are_never_one_click() -> None:
    scraped = info(urls=(URL,), one_click=True, source=UnsubscribeSource.BODY)

    assert choose_method(scraped) is Method.MANUAL


def test_plan_expands_every_target_of_a_domain_group() -> None:
    domain = group("shop.example", info(urls=(URL,), one_click=True), info(mailto=(MAILTO,)))

    actions = plan_actions([domain])

    assert [a.method for a in actions] == [Method.ONE_CLICK, Method.EMAIL]
    assert best_method(domain) is Method.ONE_CLICK


def test_summarize_plan_counts_methods() -> None:
    groups = [
        group("a@x.com", info(urls=(URL,), one_click=True)),
        group("b@x.com", info(urls=(URL,), one_click=True)),
        group("c@x.com", info(mailto=(MAILTO,))),
        group("d@x.com", info(urls=("http://x.com/u",))),
    ]

    assert summarize_plan(plan_actions(groups)) == (
        "4 mailing lists: 2 one-click, 1 email, 1 manual link"
    )
    assert summarize_plan(plan_actions(groups[:1])) == "1 mailing list: 1 one-click"


# ------------------------------------------------------------------- one-click


def test_one_click_success() -> None:
    poster, sender = FakePoster(200), FakeSender()

    [result] = make(poster, sender).run([group("a@x.com", info(urls=(URL,), one_click=True))])

    assert (result.status, result.method) == (Status.REQUESTED, Method.ONE_CLICK)
    assert poster.urls == [URL]
    assert sender.sent == []


def test_one_click_rejection_falls_back_to_email() -> None:
    poster, sender = FakePoster(405), FakeSender()
    target = info(urls=(URL,), mailto=(MAILTO,), one_click=True)

    [result] = make(poster, sender).run([group("a@x.com", target)])

    assert (result.status, result.method) == (Status.REQUESTED, Method.EMAIL)
    assert len(sender.sent) == 1


def test_redirects_are_not_followed_and_become_a_manual_step() -> None:
    poster = FakePoster(302)

    [result] = make(poster).run([group("a@x.com", info(urls=(URL,), one_click=True))])

    assert result.status is Status.MANUAL
    assert result.manual_url == URL
    assert "redirect" in result.detail
    assert poster.urls == [URL]  # exactly one request


def test_transient_status_is_retried_with_backoff() -> None:
    poster = FakePoster(503, 429, 204)
    sleeps: list[float] = []

    [result] = make(poster, sleeps=sleeps).run(
        [group("a@x.com", info(urls=(URL,), one_click=True))]
    )

    assert result.status is Status.REQUESTED
    assert len(poster.urls) == 3
    assert len(sleeps) == 2


def test_network_errors_are_retried_then_reported() -> None:
    poster = FakePoster(PostError("timeout"))

    [result] = make(poster, attempts=2).run([group("a@x.com", info(urls=(URL,), one_click=True))])

    assert len(poster.urls) == 2
    assert result.status is Status.MANUAL  # the link is still there for a human
    assert "timeout" in result.detail


def test_link_resolving_to_a_private_address_is_blocked_outright() -> None:
    poster, sender = FakePoster(200), FakeSender()
    unsubscriber = Unsubscriber(
        poster, sender, resolver=lambda host, port: ["127.0.0.1"], sleep=lambda _: None
    )
    hostile = info(urls=(URL,), mailto=(MAILTO,), one_click=True)

    [result] = unsubscriber.run([group("evil@x.com", hostile)])

    assert result.status is Status.FAILED
    assert result.detail.startswith("blocked")
    assert result.manual_url is None  # never handed to the user's browser
    assert poster.urls == []
    assert sender.sent == []


def test_dns_failure_is_not_treated_as_hostile() -> None:
    def broken_dns(host: str, port: int) -> list[str]:
        raise OSError("no dns")

    poster, sender = FakePoster(200), FakeSender()
    unsubscriber = Unsubscriber(poster, sender, resolver=broken_dns, sleep=lambda _: None)

    [result] = unsubscriber.run(
        [group("a@x.com", info(urls=(URL,), mailto=(MAILTO,), one_click=True))]
    )

    assert (result.status, result.method) == (Status.REQUESTED, Method.EMAIL)
    assert poster.urls == []


# ----------------------------------------------------------------------- email


def test_mailto_message_content() -> None:
    sender = FakeSender()

    [result] = make(sender=sender).run([group("a@x.com", info(mailto=(MAILTO,)))])

    assert (result.status, result.method) == (Status.REQUESTED, Method.EMAIL)
    message = email.message_from_bytes(sender.sent[0], policy=email.policy.default)
    assert message["To"] == "unsub@shop.example"
    assert message["Subject"] == "Unsubscribe me"
    assert message.get_content().strip() == "Please remove me"


def test_mailto_without_subject_or_body_gets_sensible_defaults() -> None:
    sender = FakeSender()
    bare = MailtoTarget(address="unsub@shop.example")

    make(sender=sender).run([group("a@x.com", info(mailto=(bare,)))])

    message = email.message_from_bytes(sender.sent[0], policy=email.policy.default)
    assert message["Subject"] == "unsubscribe"
    assert message.get_content().strip() == "unsubscribe"


def test_send_failure_falls_back_to_the_link_or_fails() -> None:
    sender = FakeSender(error=GmailApiError("quota", status=429))

    [with_link, without_link] = make(sender=sender).run(
        [
            group("a@x.com", info(urls=("http://shop.example/u",), mailto=(MAILTO,))),
            group("b@x.com", info(mailto=(MAILTO,))),
        ]
    )

    assert with_link.status is Status.MANUAL
    assert with_link.manual_url == "http://shop.example/u"
    assert without_link.status is Status.FAILED
    assert "email" in without_link.detail


# ---------------------------------------------------------------------- manual


def test_plain_web_links_are_never_requested_automatically() -> None:
    poster = FakePoster(AssertionError("must not be called"))

    [result] = make(poster).run([group("a@x.com", info(urls=("https://shop.example/u",)))])

    assert (result.status, result.method) == (Status.MANUAL, Method.MANUAL)
    assert result.manual_url == "https://shop.example/u"
    assert poster.urls == []


# -------------------------------------------------------------------- dry run


def test_dry_run_performs_no_io_at_all() -> None:
    def forbidden_resolver(host: str, port: int) -> list[str]:
        raise AssertionError("dry run must not touch DNS")

    poster = FakePoster(AssertionError("no requests in a dry run"))
    sender = FakeSender(error=AssertionError("no email in a dry run"))
    unsubscriber = Unsubscriber(poster, sender, resolver=forbidden_resolver)

    results = unsubscriber.run(
        [
            group("a@x.com", info(urls=(URL,), one_click=True)),
            group("b@x.com", info(mailto=(MAILTO,))),
            group("c@x.com", info(urls=("https://shop.example/m",))),
        ],
        dry_run=True,
    )

    assert [r.status for r in results] == [Status.DRY_RUN] * 3
    assert [r.method for r in results] == [Method.ONE_CLICK, Method.EMAIL, Method.MANUAL]
    assert results[2].manual_url == "https://shop.example/m"
    assert poster.urls == []


# ----------------------------------------------------------------- orchestration


def test_results_keep_input_order_with_concurrent_workers() -> None:
    groups = [group(f"s{i}@x.com", info(urls=(URL,), one_click=True)) for i in range(25)]

    results = make(FakePoster(200), max_workers=5).run(groups)

    assert [r.address for r in results] == [f"s{i}@x.com" for i in range(25)]
    assert all(r.status is Status.REQUESTED for r in results)


def test_progress_is_reported() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.started: list[tuple[str, int | None]] = []
            self.advanced = 0

        def start(self, description: str, total: int | None) -> None:
            self.started.append((description, total))

        def advance(self, step: int = 1) -> None:
            self.advanced += step

    recorder = Recorder()
    groups = [group(f"s{i}@x.com", info(urls=(URL,), one_click=True)) for i in range(3)]

    make().run(groups, progress=recorder)

    assert recorder.started == [("Unsubscribing", 3)]
    assert recorder.advanced == 3


def test_max_workers_is_validated() -> None:
    with pytest.raises(ValueError, match="max_workers"):
        Unsubscriber(FakePoster(200), FakeSender(), max_workers=0)
