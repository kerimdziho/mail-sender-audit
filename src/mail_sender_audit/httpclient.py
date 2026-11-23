"""HTTP transport for RFC 8058 one-click unsubscribe requests."""

from __future__ import annotations

from http.cookiejar import DefaultCookiePolicy
from types import TracebackType
from typing import Final

import httpx

from mail_sender_audit import __version__
from mail_sender_audit.unsubscriber import PostError

# RFC 8058 section 3.1: the body is exactly this form field.
_ONE_CLICK_FORM: Final = {"List-Unsubscribe": "One-Click"}


class HttpxPoster:
    """Posts one-click requests with ``httpx``.

    * Redirects are never followed: a redirect is a classic way to bounce a
      request somewhere unintended, so it is reported as a failure instead.
    * The response body is never read, only the status, so a hostile server
      cannot make us download gigabytes.
    * Cookies are never stored or sent, so the request carries no context
      from one sender to another.
    """

    def __init__(self, *, timeout: float = 15.0, client: httpx.Client | None = None) -> None:
        if client is None:
            client = httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                headers={"User-Agent": f"mail-sender-audit/{__version__}"},
            )
            # An empty allow-list makes the cookie jar reject every cookie.
            client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        self._client = client

    def post(self, url: str) -> int:
        try:
            with self._client.stream("POST", url, data=_ONE_CLICK_FORM) as response:
                return response.status_code
        except httpx.HTTPError as exc:
            raise PostError(f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpxPoster:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
