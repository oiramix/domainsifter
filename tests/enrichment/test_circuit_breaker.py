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


# --- HostThrottle ------------------------------------------------------------


def test_throttle_no_op_when_interval_zero():
    from scripts.enrichment._circuit_breaker import HostThrottle
    throttle = HostThrottle()
    sleeps: list[float] = []
    throttle.acquire("a.com", 0.0, sleep=sleeps.append)
    throttle.acquire("a.com", 0.0, sleep=sleeps.append)
    assert sleeps == []


def test_throttle_first_acquire_does_not_sleep():
    from scripts.enrichment._circuit_breaker import HostThrottle
    throttle = HostThrottle()
    sleeps: list[float] = []
    fake_clock_now = [100.0]
    throttle.acquire(
        "a.com", 1.0,
        clock=lambda: fake_clock_now[0],
        sleep=sleeps.append,
    )
    assert sleeps == []  # first call has no prior request to wait on


def test_throttle_second_acquire_waits_remaining_interval():
    from scripts.enrichment._circuit_breaker import HostThrottle
    throttle = HostThrottle()
    sleeps: list[float] = []
    fake_clock_now = [100.0]

    def fake_sleep(s):
        sleeps.append(s)
        fake_clock_now[0] += s  # simulate time advancing during sleep

    # First acquire — no wait, slot taken at t=100.0
    throttle.acquire("a.com", 1.0, jitter_seconds=0,
                     clock=lambda: fake_clock_now[0], sleep=fake_sleep)
    # 0.3s elapses
    fake_clock_now[0] = 100.3
    # Second acquire — should wait 0.7s to reach 1.0s interval
    throttle.acquire("a.com", 1.0, jitter_seconds=0,
                     clock=lambda: fake_clock_now[0], sleep=fake_sleep)
    assert sleeps and abs(sleeps[0] - 0.7) < 0.001


def test_throttle_separate_hosts_dont_block_each_other():
    from scripts.enrichment._circuit_breaker import HostThrottle
    throttle = HostThrottle()
    sleeps: list[float] = []
    clock = [100.0]
    throttle.acquire("a.com", 1.0, jitter_seconds=0,
                     clock=lambda: clock[0], sleep=sleeps.append)
    # Different host — should pass through immediately
    throttle.acquire("b.com", 1.0, jitter_seconds=0,
                     clock=lambda: clock[0], sleep=sleeps.append)
    assert sleeps == []


def test_throttle_reset_clears_state():
    from scripts.enrichment._circuit_breaker import HostThrottle
    throttle = HostThrottle()
    sleeps: list[float] = []
    clock = [100.0]
    throttle.acquire("a.com", 1.0, jitter_seconds=0,
                     clock=lambda: clock[0], sleep=sleeps.append)
    throttle.reset()
    # After reset, the next acquire treats the host as "never seen"
    throttle.acquire("a.com", 1.0, jitter_seconds=0,
                     clock=lambda: clock[0], sleep=sleeps.append)
    assert sleeps == []


def test_request_with_429_backoff_accepts_host_and_interval():
    """Smoke test — the integrated helper should accept the new kwargs
    without changing behavior when min_interval=0 (back-compat)."""
    sleeps: list[float] = []
    calls = [_resp(200)]
    resp = request_with_429_backoff(
        lambda: calls.pop(0),
        host="a.com", min_interval=0.0,
        sleep=sleeps.append,
    )
    assert resp.status_code == 200
    assert sleeps == []
