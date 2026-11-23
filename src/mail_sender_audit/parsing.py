"""Pure parsing helpers: no network and no Gmail client, so they are trivially testable.

Everything here treats its input as hostile. Email headers and bodies are
attacker-controlled, so URIs are scheme-checked and ``mailto:`` fields are
collapsed to a single line to rule out header injection.
"""

from __future__ import annotations

import base64
import codecs
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Final
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from mail_sender_audit.models import (
    Json,
    MailtoTarget,
    ScannedMessage,
    Sender,
    UnsubscribeInfo,
    UnsubscribeSource,
)

_URI_RE: Final = re.compile(r"<([^<>]*)>")  # RFC 2369: URIs are wrapped in angle brackets
_ONE_CLICK: Final = "list-unsubscribe=one-click"  # RFC 8058
_ADDRESS_RE: Final = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+$")
_KEYWORD_RE: Final = re.compile(r"unsubscribe|opt[\s_-]?out", re.IGNORECASE)
_URL_RE: Final = re.compile(r"https?://[^\s<>\"']+")
_CHARSET_RE: Final = re.compile(r"charset\s*=\s*\"?([\w.:\-]+)\"?", re.IGNORECASE)
_TRAILING_PUNCTUATION: Final = ".,;:!?)]}>\"'"
_MAX_HTML_CHARS: Final = 2_000_000
_MAX_MAILTO_BODY: Final = 1_000


# ------------------------------------------------------------ List-Unsubscribe header


def parse_list_unsubscribe(
    value: str | None, post_value: str | None = None
) -> UnsubscribeInfo | None:
    """Parse ``List-Unsubscribe`` (RFC 2369) and ``List-Unsubscribe-Post`` (RFC 8058).

    Returns ``None`` when the header holds no usable ``http(s):`` or ``mailto:`` URI.
    Unknown schemes (``javascript:``, ``ftp:`` ...) are dropped.
    """
    if not value:
        return None

    web_urls: list[str] = []
    mailto: list[MailtoTarget] = []
    for raw in _URI_RE.findall(value):
        uri = "".join(raw.split())  # undo header folding; URIs never contain whitespace
        scheme = uri.partition(":")[0].lower()
        if scheme in {"http", "https"}:
            if _is_web_url(uri) and uri not in web_urls:
                web_urls.append(uri)
        elif scheme == "mailto":
            target = parse_mailto(uri)
            if target is not None and target not in mailto:
                mailto.append(target)

    if not web_urls and not mailto:
        return None

    # One-click is only valid with an HTTPS endpoint to POST to.
    one_click = (
        post_value is not None
        and post_value.strip().lower() == _ONE_CLICK
        and any(u.lower().startswith("https://") for u in web_urls)
    )
    return UnsubscribeInfo(
        source=UnsubscribeSource.HEADER,
        web_urls=tuple(web_urls),
        mailto=tuple(mailto),
        one_click=one_click,
    )


def parse_mailto(uri: str) -> MailtoTarget | None:
    """Parse ``mailto:addr?subject=..&body=..`` (RFC 6068). Returns ``None`` if unsafe."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None

    address = unquote(parts.path).split(",")[0].strip()
    if not _ADDRESS_RE.match(address):  # also rejects CR/LF smuggled in via %0D%0A
        return None

    params = _parse_query(parts.query)
    return MailtoTarget(
        address=address,
        subject=" ".join(params.get("subject", "").split())[:200],
        body=params.get("body", "")[:_MAX_MAILTO_BODY],
    )


def _parse_query(query: str) -> dict[str, str]:
    # Deliberately not parse_qs: in mailto URIs '+' is a literal plus, not a space.
    params: dict[str, str] = {}
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key:
            params.setdefault(unquote(key).lower(), unquote(value))
    return params


def _is_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


# --------------------------------------------------------------------------- senders


def decode_mime_words(value: str) -> str:
    """Decode RFC 2047 encoded-words (``=?UTF-8?B?...?=``); fall back to the raw text."""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        return value


def parse_sender(from_value: str) -> Sender:
    name, address = parseaddr(from_value)
    address = address.strip().lower()
    name = decode_mime_words(name).strip()
    if "@" not in address:
        return Sender(name=name or from_value.strip(), address="unknown")
    return Sender(name=name, address=address)


# ---------------------------------------------------------------- body fallback


def find_unsubscribe_links(
    *, html: str | None = None, text: str | None = None, limit: int = 5
) -> tuple[str, ...]:
    """Find likely unsubscribe links in a message body.

    Links whose *visible text* mentions unsubscribing rank above links that merely
    contain the keyword in the URL or sit on a plain-text line that mentions it.
    Only ``http(s)`` links are returned.
    """
    strong: list[str] = []
    weak: list[str] = []

    if html:
        soup = BeautifulSoup(html[:_MAX_HTML_CHARS], "html.parser")
        for anchor in soup.select("a[href]"):
            href = str(anchor["href"]).strip()
            if not _is_web_url(href):
                continue
            if _KEYWORD_RE.search(anchor.get_text(" ", strip=True)):
                strong.append(href)
            elif _KEYWORD_RE.search(href):
                weak.append(href)

    if text:
        for line in text.splitlines():
            if not _KEYWORD_RE.search(line):
                continue
            for match in _URL_RE.findall(line):
                url = match.rstrip(_TRAILING_PUNCTUATION)
                if _is_web_url(url):
                    weak.append(url)

    return tuple(dict.fromkeys([*strong, *weak]))[:limit]


def extract_bodies(payload: Json) -> tuple[str | None, str | None]:
    """Return ``(html, plain_text)`` from a Gmail ``format=full`` message payload."""
    html_parts: list[str] = []
    text_parts: list[str] = []

    stack: list[Json] = [payload]
    while stack:  # iterative walk: no recursion limit to trip over on nested MIME
        part = stack.pop()
        stack.extend(reversed(part.get("parts") or []))
        data = (part.get("body") or {}).get("data")
        if not data:  # multipart containers, or attachments referenced by id only
            continue
        mime = str(part.get("mimeType", "")).lower()
        if mime == "text/html":
            html_parts.append(_decode_part(part, data))
        elif mime == "text/plain":
            text_parts.append(_decode_part(part, data))

    return "\n".join(html_parts) or None, "\n".join(text_parts) or None


def _decode_part(part: Json, data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except ValueError:  # binascii.Error is a ValueError
        return ""
    return raw.decode(_charset(part), errors="replace")


def _charset(part: Json) -> str:
    for header in part.get("headers") or []:
        if str(header.get("name", "")).lower() == "content-type":
            match = _CHARSET_RE.search(str(header.get("value", "")))
            if match:
                try:
                    return codecs.lookup(match.group(1)).name
                except LookupError:
                    break
    return "utf-8"


# ---------------------------------------------------------------- Gmail message


def build_scanned_message(
    *,
    message_id: str,
    thread_id: str,
    received: datetime,
    unread: bool,
    headers: Mapping[str, str],
) -> ScannedMessage:
    """Assemble a :class:`ScannedMessage` from provider-neutral parts.

    ``headers`` keys must be lower-case.
    """
    return ScannedMessage(
        message_id=message_id,
        thread_id=thread_id,
        sender=parse_sender(headers.get("from", "")),
        subject=decode_mime_words(headers.get("subject", "")),
        received=received,
        unread=unread,
        unsubscribe=parse_list_unsubscribe(
            headers.get("list-unsubscribe"), headers.get("list-unsubscribe-post")
        ),
    )


def parse_message_metadata(raw: Json) -> ScannedMessage | None:
    """Convert a ``messages.get(format=metadata)`` response into a :class:`ScannedMessage`."""
    message_id = raw.get("id")
    if not isinstance(message_id, str):
        return None

    try:
        millis = int(raw.get("internalDate", 0))
    except (TypeError, ValueError):
        millis = 0

    return build_scanned_message(
        message_id=message_id,
        thread_id=str(raw.get("threadId", message_id)),
        received=datetime.fromtimestamp(millis / 1000, tz=UTC),
        unread="UNREAD" in (raw.get("labelIds") or []),
        headers=_header_map(raw),
    )


def _header_map(raw: Json) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in (raw.get("payload") or {}).get("headers") or []:
        headers.setdefault(str(header.get("name", "")).lower(), str(header.get("value", "")))
    return headers
