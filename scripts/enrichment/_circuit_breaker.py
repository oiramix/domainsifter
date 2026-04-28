"""Per-source circuit breaker + 429 backoff helper + per-host throttle.

Each enrichment source instantiates one CircuitBreaker at module level and
calls `record_success()` / `record_failure()` after every network attempt.
When `consecutive_failures >= failure_threshold`, the breaker opens for
`open_duration_seconds`. While open, the source's `enrich()` returns an
empty dict immediately without making a request.

Why this matters: day 2's cron run melted because crt.sh and Wayback rate-
limited within 14 seconds, then every subsequent request hung on a 10s
timeout. With 53,125 candidates × 7 sources / 10 workers, that's enough
work to saturate the whole 45-minute budget on dead sources alone. The
breaker is an admission that some sources WILL fail in any given run, and
the right reaction is to skip them, not to keep punching.

429 backoff is layered on top: when a single request gets a 429, retry
after 1s, then after 2s. A third 429 returns to the caller (which then
records a breaker failure). This handles transient bursts without the
full circuit-open delay.

`HostThrottle` is the *prevention* layer added 2026-04-28 after day-3 still
saw the breakers open mid-run on Wayback (293/300 nulls) and crt.sh (298/300
nulls). With ten workers slamming the same host as fast as the network
allows, even a perfectly-healthy API will rate-limit us. The throttle
enforces a minimum interval between requests to a given host across all
threads, so our effective per-host rate stays inside documented or
empirical fair-use limits.

Empirical / documented per-host fair-use rates (calibrated 2026-04-28):
    web.archive.org   — Wayback CDX. No formal limit documented. Community
                        guidance + empirical observation says ~1 req/sec
                        sustained per source IP. Bursts of >5 simultaneous
                        connections trigger 429.
    crt.sh            — Sectigo's CT viewer. No formal limit. Notoriously
                        slow under load — sustained query rates above
                        ~1 req/sec degrade to multi-second response times
                        and eventually 502/timeout. Conservative 1 req/sec.
    openpagerank.com  — 10,000 calls/hour documented (= 2.78 req/sec
                        sustained). Use 0.4s interval (2.5 req/sec) to
                        leave headroom for retries within the cap.
    rdap registries   — Per-TLD; .com (Verisign) tolerates ~10 req/sec,
                        smaller registries less. 0.2s default = 5 req/sec.

These intervals can be overridden in config["api_min_interval_seconds"].

Thread-safe — pipeline runs candidates concurrently in a ThreadPoolExecutor.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        open_duration_seconds: int = 900,  # 15 minutes
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.open_duration = timedelta(seconds=open_duration_seconds)
        self._consecutive_failures = 0
        self._open_until: datetime | None = None
        self._lock = threading.Lock()
        # Injected for tests; defaults to UTC wall-clock.
        self._now: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))

    def is_open(self) -> bool:
        """True iff the circuit is currently open. If the open window has
        elapsed, transitions to half-open implicitly: the caller's next
        attempt is allowed through, and a failure re-opens the circuit."""
        with self._lock:
            if self._open_until is None:
                return False
            if self._now() >= self._open_until:
                self._open_until = None
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = None

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._open_until = self._now() + self.open_duration
                logger.warning(
                    "Circuit breaker [%s] opened until %s (%d consecutive failures)",
                    self.name,
                    self._open_until.isoformat(),
                    self._consecutive_failures,
                )

    def reset(self) -> None:
        """Force back to closed state. Tests use this between cases; the
        production pipeline never needs it (each run is a fresh process)."""
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = None

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures


# ---------------------------------------------------------------------------
# Per-host throttle
# ---------------------------------------------------------------------------


class HostThrottle:
    """Per-host minimum-interval rate limiter, thread-safe across the pool.

    Multiple workers can call `acquire(host, interval)` concurrently. Each
    call blocks until the elapsed time since the last acquired slot for that
    host is at least `interval` seconds. Adds a small uniform jitter (≤ 100ms
    by default) on top of the wait so a thundering herd doesn't all wake up
    in the same millisecond.

    Per-process state (one instance per Python process). Resets when the
    pipeline run ends — no persistence needed.
    """

    def __init__(self) -> None:
        self._last_request_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(
        self,
        host: str,
        min_interval_seconds: float,
        *,
        jitter_seconds: float = 0.10,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Block until we can issue a new request to `host`. After this
        returns, the caller has the slot for the next `min_interval_seconds`."""
        if min_interval_seconds <= 0:
            return
        while True:
            with self._lock:
                now = clock()
                last = self._last_request_at.get(host, 0.0)
                elapsed = now - last
                if elapsed >= min_interval_seconds:
                    self._last_request_at[host] = now
                    return
                wait = min_interval_seconds - elapsed
            # Sleep WITHOUT holding the lock so other threads can check.
            sleep(wait + (random.uniform(0, jitter_seconds) if jitter_seconds > 0 else 0))

    def reset(self) -> None:
        """For tests — clear all per-host state."""
        with self._lock:
            self._last_request_at.clear()


# Module-level singleton — every enrichment source acquires through this.
# One per Python process; reset between tests via the conftest fixture.
GLOBAL_HOST_THROTTLE = HostThrottle()


# ---------------------------------------------------------------------------
# Combined helper: throttle → call → 429-backoff
# ---------------------------------------------------------------------------


def request_with_429_backoff(
    call_fn: Callable[[], "object"],
    *,
    host: str | None = None,
    min_interval: float = 0.0,
    delays: tuple[float, ...] = (1.0, 2.0),
    sleep: Callable[[float], None] = time.sleep,
):
    """Invoke `call_fn()` (which must return a `requests.Response`-like
    object exposing `status_code`).

    If `host` and `min_interval > 0`, the global host throttle is acquired
    BEFORE the first call (and re-acquired before each retry, so retries
    don't punch through the rate limit).

    On 429, sleep for delays[i] then retry. After exhausting `delays`,
    the next 429 is returned to the caller as-is — the caller treats it as
    a failure (records breaker failure, returns {}).

    With the default delays=(1.0, 2.0), the call sequence is:
        throttle → attempt 1 → if 429, sleep 1s
        throttle → attempt 2 → if 429, sleep 2s
        throttle → attempt 3 → return whatever (3rd 429 = give up)

    Total worst-case wall time: 3 × HTTP latency + 3 seconds of sleeps
    + up to 3 × min_interval of throttle waits.
    """
    for delay in (*delays, None):
        if host and min_interval > 0:
            GLOBAL_HOST_THROTTLE.acquire(host, min_interval, sleep=sleep)
        resp = call_fn()
        status = getattr(resp, "status_code", None)
        if status != 429 or delay is None:
            return resp
        sleep(delay)
    # Unreachable — the loop always returns or sleeps then returns on next iter.
    raise RuntimeError("request_with_429_backoff fell through")  # pragma: no cover
