"""Tests for mail_sender_audit.netsafety (SSRF guard)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mail_sender_audit.netsafety import (
    UnresolvableHostError,
    UnsafeUrlError,
    ensure_public_https_url,
)

PUBLIC_IP = "93.184.216.34"


def resolves_to(*addresses: str) -> Callable[[str, int], list[str]]:
    return lambda host, port: list(addresses)


def test_public_https_url_is_allowed() -> None:
    ensure_public_https_url("https://example.com/unsub?id=1", resolves_to(PUBLIC_IP))
    ensure_public_https_url(
        "https://example.com/u", resolves_to("2606:2800:220:1:248:1893:25c8:1946")
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata service
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
    ],
)
def test_non_public_addresses_are_blocked(address: str) -> None:
    with pytest.raises(UnsafeUrlError, match="non-public"):
        ensure_public_https_url("https://example.com/u", resolves_to(address))


def test_one_private_address_among_public_ones_blocks_the_url() -> None:
    with pytest.raises(UnsafeUrlError):
        ensure_public_https_url("https://example.com/u", resolves_to(PUBLIC_IP, "10.0.0.1"))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/u",
        "ftp://example.com/u",
        "https:///nohost",
        "https://user:pass@example.com/u",
        "https://example.com:99999/u",
    ],
)
def test_malformed_or_non_https_urls_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        ensure_public_https_url(url, resolves_to(PUBLIC_IP))


def test_unresolvable_host_is_reported_separately() -> None:
    def broken(host: str, port: int) -> list[str]:
        raise OSError("name or service not known")

    with pytest.raises(UnresolvableHostError):
        ensure_public_https_url("https://example.com/u", broken)
    with pytest.raises(UnresolvableHostError):
        ensure_public_https_url("https://example.com/u", resolves_to())


def test_resolver_receives_the_effective_port() -> None:
    seen: list[tuple[str, int]] = []

    def spy(host: str, port: int) -> list[str]:
        seen.append((host, port))
        return [PUBLIC_IP]

    ensure_public_https_url("https://example.com/u", spy)
    ensure_public_https_url("https://example.com:8443/u", spy)

    assert seen == [("example.com", 443), ("example.com", 8443)]
