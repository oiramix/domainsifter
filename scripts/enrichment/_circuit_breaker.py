"""Per-source circuit breaker + 429 backoff helper.

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
full circuit-open delay. Per project guidance.

Thread-safe — pipeline runs candidates concurrently in a ThreadPoolExecutor.
"""

from __future__ import annotations

import logging
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
# 429 backoff helper
# ---------------------------------------------------------------------------


def request_with_429_backoff(
    call_fn: Callable[[], "object"],
    *,
    delays: tuple[float, ...] = (1.0, 2.0),
    sleep: Callable[[float], None] = time.sleep,
):
    """Invoke `call_fn()` (which must return a `requests.Response`-like
    object exposing `status_code`). On 429, sleep for delays[i] then retry.
    After exhausting `delays`, the next 429 is returned to the caller as-is —
    the caller treats it as a failure (records breaker failure, returns {}).

    With the default delays=(1.0, 2.0), the call sequence is:
        attempt 1 → if 429, sleep 1s
        attempt 2 → if 429, sleep 2s
        attempt 3 → return whatever (3rd 429 = give up)

    Total worst-case wall time: 3 × HTTP latency + 3 seconds of sleeps.
    """
    for delay in (*delays, None):
        resp = call_fn()
        status = getattr(resp, "status_code", None)
        if status != 429 or delay is None:
            return resp
        sleep(delay)
    # Unreachable — the loop always returns or sleeps then returns on next iter.
    raise RuntimeError("request_with_429_backoff fell through")  # pragma: no cover
