"""Guard against server-side request forgery (SSRF) from hostile unsubscribe URLs.

``List-Unsubscribe`` URLs come straight from the email sender, i.e. from
whoever can send you mail. Without a check, a crafted message could make this
tool POST to ``https://localhost:8080/...``, a router admin page, or a cloud
metadata endpoint on the machine it runs on. Before every automatic request we
therefore require ``https`` and a host that resolves exclusively to public
addresses.

Known limitation: the check resolves the hostname separately from the HTTP
client's own lookup, so a malicious DNS server that answers differently the
second time (DNS rebinding) could still slip through. See SECURITY.md.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import TypeAlias
from urllib.parse import urlsplit

Resolver: TypeAlias = Callable[[str, int], Sequence[str]]


class UnsafeUrlError(ValueError):
    """The URL must not be requested automatically."""


class UnresolvableHostError(UnsafeUrlError):
    """The host could not be resolved. Unlike the other cases this is not evidence of
    hostility (DNS can simply be down), so callers may fall back to another method."""


def system_resolver(host: str, port: int) -> list[str]:
    """Resolve ``host`` to its IP addresses using the operating system."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def ensure_public_https_url(url: str, resolver: Resolver = system_resolver) -> None:
    """Raise :class:`UnsafeUrlError` unless ``url`` is an HTTPS URL to a public host."""
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeUrlError(f"malformed URL: {exc}") from exc

    if parts.scheme.lower() != "https":
        raise UnsafeUrlError("only https:// URLs are requested automatically")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("URL contains embedded credentials")

    try:
        addresses = resolver(host, port)
    except OSError as exc:
        raise UnresolvableHostError(f"could not resolve {host}") from exc
    if not addresses:
        raise UnresolvableHostError(f"{host} did not resolve to any address")

    for text in addresses:
        try:
            ip = ipaddress.ip_address(text.partition("%")[0])  # drop any IPv6 zone id
        except ValueError as exc:
            raise UnsafeUrlError(f"{host} resolved to an invalid address") from exc
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is still loopback
        if not ip.is_global:
            raise UnsafeUrlError(f"{host} resolves to a non-public address")
