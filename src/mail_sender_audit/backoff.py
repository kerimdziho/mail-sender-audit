"""Dependency-free rate limiting and retry with exponential backoff.

Kept free of any Google or HTTP library so it can be reused for every kind of
outbound call (Gmail API, one-click POSTs) and tested without a network.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class RateLimiter:
    """Thread-safe pacing limiter: at most ``rate_per_second`` acquisitions per second.

    Slots are handed out evenly (no bursts), which suits Gmail's per-second
    quota. The clock and sleep functions are injectable so tests need not wait.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_slot = clock()

    def acquire(self) -> None:
        """Block until the caller's slot arrives."""
        with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            self._sleep(wait)


def retry_with_backoff(
    call: Callable[[], T],
    *,
    is_retryable: Callable[[Exception], bool],
    attempts: int = 6,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    on_retry: Callable[[Exception, int, float], None] | None = None,
) -> T:
    """Call ``call`` until it succeeds, retrying retryable errors with backoff.

    The delay doubles each attempt (``base_delay``, 2x, 4x ... capped at
    ``max_delay``) and is then scaled by "equal jitter" into the range
    ``[50%, 100%]`` so parallel workers do not retry in lock-step. A
    non-retryable error, or the last attempt's error, propagates unchanged.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    attempt = 1
    while True:
        try:
            return call()
        except Exception as exc:
            if attempt >= attempts or not is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay *= 0.5 + 0.5 * jitter()
            if on_retry is not None:
                on_retry(exc, attempt, delay)
            sleep(delay)
            attempt += 1
