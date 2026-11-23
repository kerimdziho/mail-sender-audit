"""Tests for mail_sender_audit.backoff. Time is faked, so nothing really sleeps."""

from __future__ import annotations

import pytest

from mail_sender_audit.backoff import RateLimiter, retry_with_backoff


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def is_connection_error(exc: Exception) -> bool:
    return isinstance(exc, ConnectionError)


def test_rate_limiter_spaces_calls_evenly() -> None:
    clock = FakeClock()
    limiter = RateLimiter(10, clock=clock, sleep=clock.sleep)

    for _ in range(3):
        limiter.acquire()

    assert clock.sleeps == pytest.approx([0.1, 0.1])


def test_rate_limiter_rejects_non_positive_rates() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)


def test_retries_until_success_with_doubling_delays() -> None:
    sleeps: list[float] = []
    calls = 0

    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise ConnectionError("reset")
        return "ok"

    result = retry_with_backoff(
        flaky, is_retryable=is_connection_error, sleep=sleeps.append, jitter=lambda: 1.0
    )

    assert result == "ok"
    assert calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_delay_is_capped_and_jitter_scales_it_into_half_to_full_range() -> None:
    sleeps: list[float] = []

    def always_fails() -> str:
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        retry_with_backoff(
            always_fails,
            is_retryable=is_connection_error,
            attempts=5,
            base_delay=1.0,
            max_delay=3.0,
            sleep=sleeps.append,
            jitter=lambda: 0.0,  # lowest jitter: 50% of the nominal delay
        )

    assert sleeps == [0.5, 1.0, 1.5, 1.5]


def test_non_retryable_errors_propagate_immediately() -> None:
    calls = 0

    def bug() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        retry_with_backoff(bug, is_retryable=is_connection_error, sleep=lambda _: None)
    assert calls == 1


def test_gives_up_after_the_configured_attempts() -> None:
    calls = 0

    def always_fails() -> str:
        nonlocal calls
        calls += 1
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        retry_with_backoff(
            always_fails, is_retryable=is_connection_error, attempts=3, sleep=lambda _: None
        )
    assert calls == 3


def test_on_retry_reports_error_attempt_and_delay() -> None:
    seen: list[tuple[str, int, float]] = []
    calls = 0

    def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("blip")

    retry_with_backoff(
        flaky,
        is_retryable=is_connection_error,
        sleep=lambda _: None,
        jitter=lambda: 1.0,
        on_retry=lambda exc, attempt, delay: seen.append((str(exc), attempt, delay)),
    )

    assert seen == [("blip", 1, 1.0)]


def test_zero_attempts_is_rejected() -> None:
    with pytest.raises(ValueError, match="attempts"):
        retry_with_backoff(lambda: None, is_retryable=is_connection_error, attempts=0)
