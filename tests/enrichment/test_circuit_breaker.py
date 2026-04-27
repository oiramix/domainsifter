"""Unit tests for scripts/enrichment/_circuit_breaker.py.

Two units under test:
  - CircuitBreaker class — open/closed/half-open transitions, threshold,
    timeout, thread safety basics.
  - request_with_429_backoff helper — retry semantics, sleep injection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from scripts.enrichment._circuit_breaker import CircuitBreaker, request_with_429_backoff


# --- helpers -----------------------------------------------------------------


class FakeClock:
    """Minimal mockable clock — `now()` returns the current value, `tick(s)`
    advances it. Lets us assert breaker timeout behavior without sleeping."""

    def __init__(self, start: datetime | None = None) -> None:
        self._t = start or datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._t

    def tick(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


# --- CircuitBreaker basics ---------------------------------------------------


def test_breaker_starts_closed():
    cb = CircuitBreaker("test")
    assert not cb.is_open()
    assert cb.consecutive_failures == 0


def test_breaker_opens_after_threshold_failures():
    clock = FakeClock()
    cb = CircuitBreaker("test", failure_threshold=3, clock=clock.now)

    for _ in range(2):
        cb.record_failure()
    assert not cb.is_open(), "should still be closed under threshold"

    cb.record_failure()
    assert cb.is_open(), "should open at threshold"


def test_breaker_stays_open_for_full_duration():
    clock = FakeClock()
    cb = CircuitBreaker(
        "test", failure_threshold=2, open_duration_seconds=600, clock=clock.now
    )
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open()

    clock.tick(599)
    assert cb.is_open(), "still inside open window"

    clock.tick(2)
    assert not cb.is_open(), "open window has elapsed → half-open / closed"


def test_breaker_resets_failure_count_on_success():
    cb = CircuitBreaker("test", failure_threshold=5)
    for _ in range(3):
        cb.record_failure()
    assert cb.consecutive_failures == 3

    cb.record_success()
    assert cb.consecutive_failures == 0
    assert not cb.is_open()


def test_breaker_reopens_after_failure_in_half_open_state():
    clock = FakeClock()
    cb = CircuitBreaker(
        "test", failure_threshold=2, open_duration_seconds=300, clock=clock.now
    )
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open()

    clock.tick(301)  # elapse the open window
    # is_open() implicitly transitions to half-open
    assert not cb.is_open()

    # In half-open, a single failure trips the breaker again because the
    # consecutive_failures count was preserved across the half-open transition
    # (it's only reset on success).
    cb.record_failure()
    assert cb.is_open()


def test_breaker_reset_clears_state():
    cb = CircuitBreaker("test", failure_threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open()

    cb.reset()
    assert not cb.is_open()
    assert cb.consecutive_failures == 0


def test_breaker_consecutive_failures_property():
    cb = CircuitBreaker("test")
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.consecutive_failures == 3


# --- request_with_429_backoff ------------------------------------------------


def _resp(status: int):
    r = MagicMock()
    r.status_code = status
    return r


def test_backoff_returns_immediately_on_success():
    sleeps: list[float] = []
    calls = [_resp(200)]

    def call_fn():
        return calls.pop(0)

    resp = request_with_429_backoff(call_fn, sleep=sleeps.append)
    assert resp.status_code == 200
    assert sleeps == []


def test_backoff_retries_after_first_429():
    sleeps: list[float] = []
    calls = [_resp(429), _resp(200)]

    def call_fn():
        return calls.pop(0)

    resp = request_with_429_backoff(call_fn, sleep=sleeps.append)
    assert resp.status_code == 200
    assert sleeps == [1.0]


def test_backoff_retries_twice_on_repeated_429():
    sleeps: list[float] = []
    calls = [_resp(429), _resp(429), _resp(200)]

    def call_fn():
        return calls.pop(0)

    resp = request_with_429_backoff(call_fn, sleep=sleeps.append)
    assert resp.status_code == 200
    assert sleeps == [1.0, 2.0]


def test_backoff_returns_third_429_to_caller():
    """Three consecutive 429s — caller sees the last 429 and treats as failure."""
    sleeps: list[float] = []
    calls = [_resp(429), _resp(429), _resp(429)]

    def call_fn():
        return calls.pop(0)

    resp = request_with_429_backoff(call_fn, sleep=sleeps.append)
    assert resp.status_code == 429
    assert sleeps == [1.0, 2.0]  # only two sleeps; third call returns directly


def test_backoff_custom_delays():
    sleeps: list[float] = []
    calls = [_resp(429), _resp(429), _resp(200)]

    def call_fn():
        return calls.pop(0)

    request_with_429_backoff(call_fn, delays=(0.5, 1.5), sleep=sleeps.append)
    assert sleeps == [0.5, 1.5]


def test_backoff_passes_through_non_429_status_codes():
    """500/503/etc. are NOT retried — they're real failures, not rate limits."""
    sleeps: list[float] = []
    calls = [_resp(503)]

    def call_fn():
        return calls.pop(0)

    resp = request_with_429_backoff(call_fn, sleep=sleeps.append)
    assert resp.status_code == 503
    assert sleeps == []
