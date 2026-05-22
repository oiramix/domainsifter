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

from scripts.enrichment._circuit_breaker import (
    CircuitBreaker,
    GLOBAL_HOST_COOLDOWN,
    HostCooldown,
    MAX_429_SLEEP_SECONDS,
    parse_retry_after,
    request_with_429_backoff,
)


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
    """With jitter disabled (jitter_factor_range=(1,1)), the wait is exactly
    the remaining nominal interval — deterministic for sleep-amount asserts."""
    from scripts.enrichment._circuit_breaker import HostThrottle
    throttle = HostThrottle()
    sleeps: list[float] = []
    fake_clock_now = [100.0]

    def fake_sleep(s):
        sleeps.append(s)
        fake_clock_now[0] += s  # simulate time advancing during sleep

    # First acquire — no wait, slot taken at t=100.0
    throttle.acquire("a.com", 1.0, jitter_factor_range=(1.0, 1.0),
                     clock=lambda: fake_clock_now[0], sleep=fake_sleep)
    # 0.3s elapses
    fake_clock_now[0] = 100.3
    # Second acquire — should wait 0.7s to reach 1.0s interval
    throttle.acquire("a.com", 1.0, jitter_factor_range=(1.0, 1.0),
                     clock=lambda: fake_clock_now[0], sleep=fake_sleep)
    assert sleeps and abs(sleeps[0] - 0.7) < 0.001


def test_throttle_jitter_varies_actual_interval():
    """Multiplicative jitter — actual effective interval is 75-125% of the
    configured value. Sample many acquires and assert the spread."""
    import random as _random
    from scripts.enrichment._circuit_breaker import HostThrottle
    throttle = HostThrottle()
    _random.seed(42)  # deterministic for the assertion
    sleeps: list[float] = []
    fake_clock_now = [100.0]

    def fake_sleep(s):
        sleeps.append(s)
        fake_clock_now[0] += s

    # Prime the slot.
    throttle.acquire("a.com", 1.0, jitter_factor_range=(0.75, 1.25),
                     clock=lambda: fake_clock_now[0], sleep=fake_sleep)
    # 100 acquires back-to-back; each should sleep ~ effective_interval
    # (jitter-scaled) since the clock only advances via fake_sleep.
    for _ in range(100):
        throttle.acquire("a.com", 1.0, jitter_factor_range=(0.75, 1.25),
                         clock=lambda: fake_clock_now[0], sleep=fake_sleep)
    assert all(0.74 <= s <= 1.26 for s in sleeps), f"out-of-range sleep: {sleeps}"
    # And there's actual variance (not all identical).
    assert max(sleeps) - min(sleeps) > 0.1


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


def test_throttle_serialises_concurrent_acquirers_on_same_host():
    """Five threads acquire the same host with interval=0.1s and deterministic
    jitter (1.0×). The HostThrottle's reserve-then-sleep pattern must
    serialise them so total wall-clock is at least (n-1)*interval — proves
    that the per-host RDAP concurrency design (multiple workers per host)
    cannot exceed the configured per-host rate.
    """
    import threading
    import time as _time

    from scripts.enrichment._circuit_breaker import HostThrottle

    throttle = HostThrottle()
    n = 5
    interval = 0.1
    barrier = threading.Barrier(n)
    finish_times: list[float] = []
    finish_lock = threading.Lock()

    def worker():
        barrier.wait()  # release all five at once
        throttle.acquire(
            "shared.example",
            interval,
            jitter_factor_range=(1.0, 1.0),  # deterministic for assertion
        )
        with finish_lock:
            finish_times.append(_time.monotonic())

    start = _time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = max(finish_times) - start

    # First acquirer returns ~immediately; each subsequent one waits the
    # interval after the previous reservation. Minimum wall-clock for n
    # serialised acquires = (n-1) * interval. Allow a small slack for
    # scheduler jitter on the lower bound; upper bound proves we're not
    # accidentally ×2 sleeping.
    minimum = (n - 1) * interval
    assert elapsed >= minimum * 0.9, (
        f"throttle did not serialise: elapsed={elapsed:.3f}s, expected >= {minimum:.3f}s"
    )
    assert elapsed < minimum * 3.0, (
        f"throttle over-slept: elapsed={elapsed:.3f}s, expected ~{minimum:.3f}s"
    )


# --- retry_on_timeout --------------------------------------------------------


def test_retry_on_timeout_returns_immediately_on_success():
    from scripts.enrichment._circuit_breaker import retry_on_timeout
    sleeps: list[float] = []
    calls = [_resp(200)]
    resp = retry_on_timeout(lambda: calls.pop(0), label="x", sleep=sleeps.append)
    assert resp.status_code == 200
    assert sleeps == []


def test_retry_on_timeout_succeeds_on_second_attempt():
    from requests.exceptions import ReadTimeout
    from scripts.enrichment._circuit_breaker import retry_on_timeout
    sleeps: list[float] = []
    seq = [ReadTimeout("first"), _resp(200)]

    def call_fn():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    resp = retry_on_timeout(call_fn, label="x", sleep=sleeps.append)
    assert resp.status_code == 200
    assert sleeps == [5.0]  # one backoff between attempts 1 and 2


def test_retry_on_timeout_succeeds_on_third_attempt():
    from requests.exceptions import ConnectTimeout
    from scripts.enrichment._circuit_breaker import retry_on_timeout
    sleeps: list[float] = []
    seq = [ConnectTimeout("a"), ConnectTimeout("b"), _resp(200)]

    def call_fn():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    resp = retry_on_timeout(call_fn, label="x", sleep=sleeps.append)
    assert resp.status_code == 200
    assert sleeps == [5.0, 15.0]


def test_retry_on_timeout_reraises_after_three_attempts():
    from requests.exceptions import ReadTimeout
    from scripts.enrichment._circuit_breaker import retry_on_timeout
    sleeps: list[float] = []
    seq = [ReadTimeout("a"), ReadTimeout("b"), ReadTimeout("c")]

    def call_fn():
        raise seq.pop(0)

    with pytest.raises(ReadTimeout):
        retry_on_timeout(call_fn, label="x", sleep=sleeps.append)
    assert sleeps == [5.0, 15.0]  # backoffs between 1->2 and 2->3 only


def test_retry_on_timeout_does_not_retry_other_exceptions():
    """ConnectionError, HTTPError, ValueError must propagate immediately —
    only Connect/Read/Timeout trigger retry."""
    from requests.exceptions import ConnectionError as ReqConnError
    from scripts.enrichment._circuit_breaker import retry_on_timeout
    sleeps: list[float] = []
    calls: list[int] = []

    def call_fn():
        calls.append(1)
        raise ReqConnError("network down")

    with pytest.raises(ReqConnError):
        retry_on_timeout(call_fn, label="x", sleep=sleeps.append)
    assert len(calls) == 1  # exactly one attempt, no retry
    assert sleeps == []


def test_retry_on_timeout_custom_delays():
    from requests.exceptions import ReadTimeout
    from scripts.enrichment._circuit_breaker import retry_on_timeout
    sleeps: list[float] = []
    seq = [ReadTimeout("a"), ReadTimeout("b"), ReadTimeout("c"), ReadTimeout("d")]

    def call_fn():
        raise seq.pop(0)

    with pytest.raises(ReadTimeout):
        retry_on_timeout(
            call_fn, label="x", delays=(1.0, 2.0, 3.0), sleep=sleeps.append,
        )
    assert sleeps == [1.0, 2.0, 3.0]  # 4 attempts, 3 backoffs


# --- parse_retry_after (RFC 7231 §7.1.3) -------------------------------------


def test_parse_retry_after_positive_int():
    """delta-seconds form — a sane positive integer."""
    assert parse_retry_after("120") == 120.0


def test_parse_retry_after_zero_is_zero_not_none():
    """'0' IS a valid delta-seconds value (GMO Registry sends exactly this).
    It must parse to 0.0, NOT None — the caller distinguishes 'header said 0'
    from 'no header' and applies the floor to both, but parse stays faithful."""
    assert parse_retry_after("0") == 0.0


def test_parse_retry_after_http_date():
    """HTTP-date form — wait is (date - now)."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Fri, 22 May 2026 12:02:00 GMT", now=now) == 120.0


def test_parse_retry_after_http_date_in_past_clamps_to_zero():
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Fri, 22 May 2026 11:00:00 GMT", now=now) == 0.0


def test_parse_retry_after_absent_is_none():
    assert parse_retry_after(None) is None


def test_parse_retry_after_empty_or_blank_is_none():
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None


def test_parse_retry_after_garbage_is_none():
    """Unparseable values → None. Caller floors None the same as it floors 0."""
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-5") is None        # negative — not delta-seconds
    assert parse_retry_after("12.5") is None      # fractional — not an integer
    assert parse_retry_after("Notaday, 99 Zzz 2026") is None  # not an HTTP-date


# --- HostCooldown ------------------------------------------------------------


class _FloatClock:
    """Mutable monotonic-style clock for HostCooldown tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_cooldown_start_then_cooling():
    clk = _FloatClock()
    cd = HostCooldown()
    cd.start("h.example", 60, clock=clk)
    assert cd.is_cooling("h.example", clock=clk)
    assert abs(cd.seconds_remaining("h.example", clock=clk) - 60.0) < 0.001


def test_cooldown_unknown_host_not_cooling():
    cd = HostCooldown()
    assert not cd.is_cooling("never-seen.example")
    assert cd.seconds_remaining("never-seen.example") == 0.0


def test_cooldown_expires_and_traffic_resumes():
    """The window elapses → seconds_remaining returns 0 and is_cooling flips
    False, so callers resume issuing requests."""
    clk = _FloatClock()
    cd = HostCooldown()
    cd.start("h.example", 60, clock=clk)
    assert cd.is_cooling("h.example", clock=clk)

    clk.advance(59)
    assert cd.is_cooling("h.example", clock=clk), "still inside the 60s window"

    clk.advance(2)
    assert not cd.is_cooling("h.example", clock=clk), "window elapsed"
    assert cd.seconds_remaining("h.example", clock=clk) == 0.0


def test_cooldown_other_host_unaffected():
    clk = _FloatClock()
    cd = HostCooldown()
    cd.start("host-a.example", 60, clock=clk)
    assert cd.is_cooling("host-a.example", clock=clk)
    assert not cd.is_cooling("host-b.example", clock=clk)


def test_cooldown_extends_never_shortens():
    """Two workers racing on the same 429 must not let a shorter window win."""
    clk = _FloatClock()
    cd = HostCooldown()
    cd.start("h.example", 60, clock=clk)
    cd.start("h.example", 10, clock=clk)  # shorter — ignored
    assert abs(cd.seconds_remaining("h.example", clock=clk) - 60.0) < 0.001
    cd.start("h.example", 120, clock=clk)  # longer — extends
    assert abs(cd.seconds_remaining("h.example", clock=clk) - 120.0) < 0.001


def test_cooldown_zero_or_negative_seconds_is_noop():
    cd = HostCooldown()
    cd.start("h.example", 0)
    cd.start("h.example", -5)
    assert not cd.is_cooling("h.example")


def test_cooldown_reset_clears_state():
    cd = HostCooldown()
    cd.start("h.example", 60)
    assert cd.is_cooling("h.example")
    cd.reset()
    assert not cd.is_cooling("h.example")


# --- request_with_429_backoff: RETRY-AFTER MODE ------------------------------


def _resp429(retry_after=None):
    """A 429 response mock with a real (case-sensitive) headers dict so the
    Retry-After read path behaves like a requests.Response."""
    r = MagicMock()
    r.status_code = 429
    r.headers = {} if retry_after is None else {"Retry-After": retry_after}
    return r


def test_retry_after_mode_one_retry_then_success():
    """First 429 → wait the floored cooldown → ONE retry → success."""
    sleeps: list[float] = []
    calls = [_resp429("0"), _resp(200)]
    resp = request_with_429_backoff(
        lambda: calls.pop(0),
        host="gmo.example", min_interval=0.0,
        retry_after_floor=60.0, sleep=sleeps.append,
    )
    assert resp.status_code == 200
    assert sleeps == [60.0]   # Retry-After:0 floored up to 60
    assert calls == []        # exactly 2 attempts consumed (initial + 1 retry)


def test_retry_after_mode_floor_applied_when_header_zero():
    sleeps: list[float] = []
    calls = [_resp429("0"), _resp(200)]
    request_with_429_backoff(
        lambda: calls.pop(0), host="h.example",
        retry_after_floor=60.0, sleep=sleeps.append,
    )
    assert sleeps == [60.0]


def test_retry_after_mode_floor_applied_when_header_absent():
    sleeps: list[float] = []
    calls = [_resp429(None), _resp(200)]
    request_with_429_backoff(
        lambda: calls.pop(0), host="h.example",
        retry_after_floor=60.0, sleep=sleeps.append,
    )
    assert sleeps == [60.0]


def test_retry_after_mode_floor_applied_when_header_garbage():
    sleeps: list[float] = []
    calls = [_resp429("soon"), _resp(200)]
    request_with_429_backoff(
        lambda: calls.pop(0), host="h.example",
        retry_after_floor=60.0, sleep=sleeps.append,
    )
    assert sleeps == [60.0]


def test_retry_after_mode_honors_header_when_larger_than_floor():
    """A sane Retry-After larger than the floor is honored as-is."""
    sleeps: list[float] = []
    calls = [_resp429("90"), _resp(200)]
    request_with_429_backoff(
        lambda: calls.pop(0), host="h.example",
        retry_after_floor=60.0, sleep=sleeps.append,
    )
    assert sleeps == [90.0]


def test_retry_after_mode_exactly_one_retry_on_repeated_429():
    """Both attempts 429 → return the last 429 to the caller. EXACTLY two
    attempts (not three) — one sleep, one retry."""
    sleeps: list[float] = []
    calls = [_resp429("0"), _resp429("0"), _resp(200)]
    resp = request_with_429_backoff(
        lambda: calls.pop(0), host="h.example",
        retry_after_floor=60.0, sleep=sleeps.append,
    )
    assert resp.status_code == 429
    assert sleeps == [60.0]          # one sleep only
    assert len(calls) == 1           # third response never consumed


def test_retry_after_mode_arms_host_cooldown():
    GLOBAL_HOST_COOLDOWN.reset()
    calls = [_resp429("0"), _resp429("0")]
    request_with_429_backoff(
        lambda: calls.pop(0), host="cooldown.example",
        retry_after_floor=60.0, sleep=lambda *_: None,
    )
    assert GLOBAL_HOST_COOLDOWN.is_cooling("cooldown.example")
    assert GLOBAL_HOST_COOLDOWN.seconds_remaining("cooldown.example") > 50.0


def test_retry_after_mode_success_first_attempt_no_sleep_no_cooldown():
    """No 429 at all → no sleep, no cooldown armed."""
    GLOBAL_HOST_COOLDOWN.reset()
    sleeps: list[float] = []
    resp = request_with_429_backoff(
        lambda: _resp(200), host="clean.example",
        retry_after_floor=60.0, sleep=sleeps.append,
    )
    assert resp.status_code == 200
    assert sleeps == []
    assert not GLOBAL_HOST_COOLDOWN.is_cooling("clean.example")


def test_retry_after_mode_sleep_capped_for_huge_retry_after():
    """An explicit multi-hour ban (Identity Digital once sent 86397s): the
    worker sleeps at most MAX_429_SLEEP_SECONDS, but the per-host cooldown is
    armed for the FULL ban so subsequent domains skip the host all run."""
    GLOBAL_HOST_COOLDOWN.reset()
    sleeps: list[float] = []
    calls = [_resp429("86397"), _resp429("86397")]
    request_with_429_backoff(
        lambda: calls.pop(0), host="banned.example",
        retry_after_floor=60.0, sleep=sleeps.append,
    )
    assert sleeps == [MAX_429_SLEEP_SECONDS]  # worker sleep is capped
    # ...but the cooldown reflects the registry's full 24h ban.
    assert GLOBAL_HOST_COOLDOWN.seconds_remaining("banned.example") > 80000.0


def test_retry_after_mode_requires_host():
    """retry_after_floor > 0 needs a host (the cooldown + throttle are
    host-keyed) — misuse is a programming error, raised eagerly."""
    with pytest.raises(ValueError):
        request_with_429_backoff(
            lambda: _resp(200), retry_after_floor=60.0, sleep=lambda *_: None,
        )


def test_legacy_mode_unaffected_by_new_param_default():
    """retry_after_floor defaults to 0.0 → legacy 3-attempt 1s/2s path,
    Retry-After ignored. Guards every other request_with_429_backoff caller
    (crt.sh / OPR / Safe Browsing / rdap.enrich)."""
    sleeps: list[float] = []
    calls = [_resp429("999"), _resp429("999"), _resp(200)]
    resp = request_with_429_backoff(lambda: calls.pop(0), sleep=sleeps.append)
    assert resp.status_code == 200
    assert sleeps == [1.0, 2.0]  # fixed legacy delays, header value ignored
