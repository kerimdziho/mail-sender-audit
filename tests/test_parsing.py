"""Tests for mail_sender_audit.parsing (pure functions, no network)."""

from __future__ import annotations

import base64

from mail_sender_audit.models import UnsubscribeSource
from mail_sender_audit.parsing import (
    extract_bodies,
    find_unsubscribe_links,
    parse_list_unsubscribe,
    parse_mailto,
    parse_message_metadata,
    parse_sender,
)


def b64(text: str, encoding: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(encoding)).decode("ascii")


# ----------------------------------------------------------- List-Unsubscribe


def test_header_with_https_mailto_and_one_click() -> None:
    info = parse_list_unsubscribe(
        "<mailto:unsub@shop.com?subject=Unsubscribe>, <https://shop.com/u?id=1>",
        "List-Unsubscribe=One-Click",
    )

    assert info is not None
    assert info.source is UnsubscribeSource.HEADER
    assert info.web_urls == ("https://shop.com/u?id=1",)
    assert info.mailto[0].address == "unsub@shop.com"
    assert info.mailto[0].subject == "Unsubscribe"
    assert info.one_click_url == "https://shop.com/u?id=1"


def test_folded_header_and_comments_are_tolerated() -> None:
    value = "(Unsubscribe) <https://a.example/un\r\n subscribe?x=1>,\r\n <mailto:u@a.example>"

    info = parse_list_unsubscribe(value)

    assert info is not None
    assert info.web_urls == ("https://a.example/unsubscribe?x=1",)
    assert info.mailto[0].address == "u@a.example"
    assert not info.one_click


def test_one_click_requires_an_https_endpoint() -> None:
    http_only = parse_list_unsubscribe("<http://a.example/u>", "List-Unsubscribe=One-Click")
    mailto_only = parse_list_unsubscribe("<mailto:u@a.example>", "List-Unsubscribe=One-Click")

    assert http_only is not None
    assert http_only.one_click_url is None
    assert mailto_only is not None
    assert not mailto_only.one_click


def test_unknown_schemes_and_empty_values_yield_nothing() -> None:
    assert parse_list_unsubscribe("<javascript:alert(1)>, <ftp://x.example/y>") is None
    assert parse_list_unsubscribe("") is None
    assert parse_list_unsubscribe(None) is None


# --------------------------------------------------------------------- mailto


def test_mailto_is_collapsed_to_one_line_and_plus_stays_literal() -> None:
    target = parse_mailto("mailto:u@x.com?subject=hi%0D%0ABcc:evil@x.com&body=a+b")

    assert target is not None
    assert "\r" not in target.subject
    assert "\n" not in target.subject
    assert target.body == "a+b"


def test_mailto_rejects_bad_or_injected_addresses() -> None:
    assert parse_mailto("mailto:not-an-address") is None
    assert parse_mailto("mailto:a@x.com%0D%0Abcc:b@y.com") is None


def test_mailto_with_several_recipients_uses_the_first() -> None:
    target = parse_mailto("mailto:a@x.com,b@x.com")

    assert target is not None
    assert target.address == "a@x.com"


# -------------------------------------------------------------------- senders


def test_sender_address_is_lowercased_and_quoted_name_kept() -> None:
    sender = parse_sender('"Acme, Inc." <News@Acme.COM>')

    assert sender.name == "Acme, Inc."
    assert sender.address == "news@acme.com"
    assert sender.domain == "acme.com"


def test_sender_name_decodes_rfc2047() -> None:
    sender = parse_sender("=?UTF-8?B?Q2Fmw6k=?= <hello@cafe.example>")

    assert sender.name == "Café"


def test_unparsable_sender_falls_back_to_unknown() -> None:
    sender = parse_sender("undisclosed recipients")

    assert sender.address == "unknown"
    assert sender.domain == "unknown"


# ---------------------------------------------------------------- body fallback


def test_html_links_rank_by_visible_text_first() -> None:
    html = """
    <p><a href="https://x.example/prefs?unsubscribe=1"><img src="b.png"></a></p>
    <a href="https://x.example/u/abc">Unsubscribe</a>
    <a href="mailto:u@x.example">Unsubscribe by email</a>
    <a href="javascript:void(0)">Unsubscribe</a>
    <a href="https://x.example/u/abc">click to unsubscribe</a>
    <a href="https://x.example/home">Home</a>
    """

    assert find_unsubscribe_links(html=html) == (
        "https://x.example/u/abc",
        "https://x.example/prefs?unsubscribe=1",
    )


def test_plain_text_links_are_found_and_trailing_punctuation_stripped() -> None:
    text = "Hello\nTo unsubscribe visit https://x.example/u/1.\nOther link https://x.example/promo"

    assert find_unsubscribe_links(text=text) == ("https://x.example/u/1",)


def test_no_links_when_nothing_mentions_unsubscribing() -> None:
    assert find_unsubscribe_links(html='<a href="https://x.example/">Home</a>') == ()


def test_extract_bodies_handles_charsets_padding_and_attachments() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/plain",
                "headers": [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}],
                "body": {"data": b64("caf\xe9", "latin-1")},
            },
            # Gmail omits base64 padding.
            {"mimeType": "text/html", "body": {"data": b64("<p>Hi!</p>").rstrip("=")}},
            {"mimeType": "application/pdf", "body": {"attachmentId": "abc"}},
        ],
    }

    html, text = extract_bodies(payload)

    assert html == "<p>Hi!</p>"
    assert text == "café"


# ------------------------------------------------------------ Gmail message


def test_parse_message_metadata_end_to_end() -> None:
    raw = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1700000000000",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Shop <news@shop.com>"},
                {"name": "Subject", "value": "Sale"},
                {"name": "List-Unsubscribe", "value": "<https://shop.com/u>"},
                {"name": "list-unsubscribe-post", "value": "List-Unsubscribe=One-Click"},
            ]
        },
    }

    message = parse_message_metadata(raw)

    assert message is not None
    assert message.unread
    assert message.sender.address == "news@shop.com"
    assert message.received.year == 2023
    assert message.unsubscribe is not None
    assert message.unsubscribe.one_click_url == "https://shop.com/u"


def test_parse_message_metadata_rejects_garbage() -> None:
    assert parse_message_metadata({}) is None
