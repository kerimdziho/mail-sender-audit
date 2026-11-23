"""Tests for mail_sender_audit.grouping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mail_sender_audit.grouping import group_messages
from mail_sender_audit.models import (
    GroupBy,
    ScannedMessage,
    Sender,
    UnsubscribeInfo,
    UnsubscribeSource,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def header_info(url: str) -> UnsubscribeInfo:
    return UnsubscribeInfo(source=UnsubscribeSource.HEADER, web_urls=(url,))


def msg(
    message_id: str,
    address: str,
    *,
    name: str = "",
    minutes: int = 0,
    unsub: UnsubscribeInfo | None = None,
    unread: bool = False,
) -> ScannedMessage:
    return ScannedMessage(
        message_id=message_id,
        thread_id=message_id,
        sender=Sender(name=name, address=address),
        subject="subject",
        received=BASE + timedelta(minutes=minutes),
        unread=unread,
        unsubscribe=unsub,
    )


def sample() -> list[ScannedMessage]:
    return [
        msg("a1", "a@shop.com", name="Shop", minutes=1, unsub=header_info("https://shop.com/1")),
        msg(
            "a2",
            "a@shop.com",
            name="Shop Deals",
            minutes=2,
            unsub=header_info("https://shop.com/2"),
        ),
        msg("a3", "a@shop.com", name="Shop", minutes=3, unread=True),  # newest, no header
        msg("b1", "b@shop.com", name="Billing", minutes=4, unsub=header_info("https://shop.com/b")),
        msg("c1", "c@other.org", minutes=5),
        msg("c2", "c@other.org", minutes=6),
    ]


def test_address_grouping_sorts_by_count_then_key() -> None:
    groups = group_messages(sample(), GroupBy.ADDRESS)

    assert [(g.key, g.count) for g in groups] == [
        ("a@shop.com", 3),
        ("c@other.org", 2),
        ("b@shop.com", 1),
    ]


def test_messages_are_newest_first_and_counts_are_exposed() -> None:
    shop = group_messages(sample(), GroupBy.ADDRESS)[0]

    assert [m.message_id for m in shop.messages] == ["a3", "a2", "a1"]
    assert shop.latest.message_id == "a3"
    assert shop.unread_count == 1


def test_label_is_the_most_common_display_name() -> None:
    shop = group_messages(sample(), GroupBy.ADDRESS)[0]

    assert shop.label == "Shop"


def test_target_comes_from_the_newest_message_that_has_a_header() -> None:
    shop = group_messages(sample(), GroupBy.ADDRESS)[0]

    assert len(shop.targets) == 1
    assert shop.unsubscribe is not None
    assert shop.unsubscribe.web_urls == ("https://shop.com/2",)


def test_sender_without_any_header_has_no_target() -> None:
    groups = {g.key: g for g in group_messages(sample(), GroupBy.ADDRESS)}

    assert groups["c@other.org"].unsubscribe is None


def test_domain_grouping_keeps_one_target_per_address() -> None:
    groups = {g.key: g for g in group_messages(sample(), GroupBy.DOMAIN)}
    shop = groups["shop.com"]

    assert shop.count == 4
    assert shop.label == "shop.com"
    assert [t.info.web_urls[0] for t in shop.targets] == [
        "https://shop.com/b",
        "https://shop.com/2",
    ]
