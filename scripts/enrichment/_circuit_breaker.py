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
from email.utils import parsedate_to_datetime
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
    host is at least `interval × jitter_factor` seconds, where jitter_factor
    is sampled from `jitter_factor_range` (default 0.75–1.25 — i.e. each
    actual interval is between 75% and 125% of the configured value).

    Why multiplicative jitter: a deterministic-clockwork pattern of one
    request per N seconds is suspicious to rate-limited services and is
    one of the things that got us 502/503 from Wayback and crt.sh on
    2026-04-29 even with a 1.0s/host throttle. Real human curiosity is
    bursty and irregular; we mimic that.

    Tests can pass `jitter_factor_range=(1.0, 1.0)` to get deterministic
    waits for sleep-amount assertions.

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
        jitter_factor_range: tuple[float, float] = (0.75, 1.25),
        jitter_seconds: float | None = None,  # back-compat for tests
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Block until we can issue a new request to `host`. After this
        returns, the caller has the slot for the next `effective` seconds,
        where effective = min_interval_seconds × random factor in range.

        `jitter_seconds=0` is recognised for back-compat with older tests
        that wanted purely deterministic waits — it's mapped to
        `jitter_factor_range=(1.0, 1.0)`.
        """
        if min_interval_seconds <= 0:
            return
        if jitter_seconds == 0:
            jitter_factor_range = (1.0, 1.0)
        lo, hi = jitter_factor_range
        factor = random.uniform(lo, hi) if hi > lo else lo
        effective = min_interval_seconds * factor

        # Compute the wait once, reserve the slot, sleep without the lock.
        # No polling loop — `(now + wait) - now` would suffer from float
        # cancellation and could stall in microsleeps, which we hit on the
        # first jitter implementation. Reserve-then-sleep also serialises
        # concurrent callers naturally: thread B reads the slot we just
        # reserved and waits past us.
        with self._lock:
            now = clock()
            last = self._last_request_at.get(host, 0.0)
            time_since_last = now - last if last > 0 else None
            wait = effective - (now - last)
            if wait <= 0:
                self._last_request_at[host] = now
                actual_delay = 0.0
                next_allowed_at = now + effective
            else:
                self._last_request_at[host] = now + wait
                actual_delay = wait
                next_allowed_at = now + wait + effective

        # Detailed pacing trace — gated by logger.isEnabledFor so the string
        # formatting is skipped entirely when DEBUG isn't on (avoids overhead
        # in production runs).
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "throttle host=%s configured=%.3fs factor=%.3f effective=%.3fs "
                "delay_applied=%.3fs since_last=%s next_allowed=t+%.3fs",
                host, min_interval_seconds, factor, effective,
                actual_delay,
                f"{time_since_last:.3f}s" if time_since_last is not None else "first",
                next_allowed_at - now,
            )

        if wait > 0:
            sleep(wait)

    def reset(self) -> None:
        """For tests — clear all per-host state."""
        with self._lock:
            self._last_request_at.clear()


# Module-level singleton — every enrichment source acquires through this.
# One per Python process; reset between tests via the conftest fixture.
GLOBAL_HOST_THROTTLE = HostThrottle()


# ---------------------------------------------------------------------------
# Per-host 429 cooldown
# ---------------------------------------------------------------------------


class HostCooldown:
    """Per-host rate-limit cooldown tracker, thread-safe across the pool.

    When a host returns HTTP 429, the 429 handler records a cooldown via
    `start(host, seconds)`. While that window is active, `seconds_remaining`
    returns a positive value and callers should skip the host entirely —
    issuing a request would just 429 again and, for registries that escalate
    (GMO Registry's /help page documents temporary IP blocking), actively
    makes things worse.

    Added 2026-05-22 — see config.json:rdap_429_backoff_floor_seconds and
    STATE.md for the GMO incident that motivated it.

    Per-process, in-memory. One module-level singleton (GLOBAL_HOST_COOLDOWN);
    no persistence. Reset between tests via the enrichment conftest fixture.
    """

    def __init__(self) -> None:
        self._cooling_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def start(
        self,
        host: str,
        seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Mark `host` as cooling down for `seconds` from now. Extends an
        existing cooldown when the new window ends later; never shortens one
        — two workers racing on the same 429 must not let the shorter win."""
        if seconds <= 0:
            return
        with self._lock:
            until = clock() + seconds
            if until > self._cooling_until.get(host, 0.0):
                self._cooling_until[host] = until

    def seconds_remaining(
        self,
        host: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> float:
        """Seconds left on `host`'s cooldown, or 0.0 if it is not cooling.
        Expired entries are dropped on read so the dict cannot grow without
        bound across a long run."""
        with self._lock:
            until = self._cooling_until.get(host)
            if until is None:
                return 0.0
            remaining = until - clock()
            if remaining <= 0.0:
                del self._cooling_until[host]
                return 0.0
            return remaining

    def is_cooling(
        self,
        host: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        return self.seconds_remaining(host, clock=clock) > 0.0

    def reset(self) -> None:
        """For tests — clear all cooldown state."""
        with self._lock:
            self._cooling_until.clear()


# Module-level singleton — the RDAP availability check arms and reads this.
# One per Python process; reset between tests via the conftest fixture.
GLOBAL_HOST_COOLDOWN = HostCooldown()


# ---------------------------------------------------------------------------
# Combined helper: throttle → call → 429-backoff
# ---------------------------------------------------------------------------


def retry_on_timeout(
    call_fn: Callable[[], "object"],
    *,
    label: str,
    delays: tuple[float, ...] = (5.0, 15.0),
    sleep: Callable[[float], None] | None = None,
    log: logging.Logger | None = None,
):
    """Run `call_fn()` with retry-on-timeout. Retries ONLY on requests'
    Connect/Read/Timeout exceptions; any other exception (including HTTPError,
    ConnectionError, JSON errors) propagates immediately so the caller can
    handle it without burning a retry slot.

    Total attempts = len(delays) + 1 (default: 3 attempts at 0s, 5s, 15s).

    Logs at INFO so production runs can see the retry rate without flipping
    DEBUG. Returns whatever `call_fn` returns on the first successful attempt;
    re-raises the LAST timeout exception if all attempts time out.

    Used by wayback.py and crtsh.py — endpoints that routinely take 15-30s
    under peak load. Other enrichers (RDAP, OPR, blocklists) use
    request_with_429_backoff directly without retry, since timeouts there
    are rare and the wall-clock budget is more precious than coverage.
    """
    # Imported here to keep the breaker module self-contained when requests
    # isn't installed in some hypothetical test setup.
    from requests.exceptions import ConnectTimeout, ReadTimeout, Timeout

    timeout_exc_types = (ConnectTimeout, ReadTimeout, Timeout)
    log = log or logger
    # Resolve sleep at call time (not def time) so monkeypatching time.sleep
    # in tests actually takes effect.
    if sleep is None:
        sleep = time.sleep
    last_exc: BaseException | None = None
    max_attempts = len(delays) + 1

    for attempt in range(1, max_attempts + 1):
        try:
            result = call_fn()
            if attempt > 1:
                log.info("%s: retry succeeded on attempt %d/%d", label, attempt, max_attempts)
            return result
        except timeout_exc_types as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = delays[attempt - 1]
                log.info(
                    "%s: timeout on attempt %d/%d (%s), retrying in %.0fs",
                    label, attempt, max_attempts, exc.__class__.__name__, delay,
                )
                sleep(delay)
                continue
            log.warning(
                "%s: timed out on final attempt %d/%d, giving up (%s)",
                label, attempt, max_attempts, exc.__class__.__name__,
            )
            raise
    # Unreachable — the loop either returns or re-raises.
    raise last_exc if last_exc else RuntimeError("retry_on_timeout fell through")  # pragma: no cover


# Upper bound on how long a single worker will sleep out one 429, even when
# the registry's Retry-After (or per-host floor) asks for longer. A registry
# can send an explicit multi-hour ban (Identity Digital once sent 86397s, a
# 24h ban); a worker must not hang the pipeline that long. When the requested
# cooldown exceeds this cap, the per-host cooldown is still armed for the FULL
# requested duration (subsequent domains correctly skip the host for the whole
# ban) but the worker itself sleeps only the cap, does its one retry, and
# moves on. 120s comfortably covers every documented per-host floor (GMO's
# 60s being the largest) with headroom.
MAX_429_SLEEP_SECONDS = 120.0


def parse_retry_after(
    value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse an HTTP `Retry-After` header value (RFC 7231 section 7.1.3).

    Two legal forms:
      - delta-seconds: a non-negative integer count of seconds.
      - HTTP-date:     an absolute timestamp; the wait is (date - now).

    Returns the wait in seconds as a float >= 0.0, or None when `value` is
    absent or parses as neither form. A delta-seconds of "0" returns 0.0
    (NOT None) — "0" IS a valid value, just a useless one (GMO Registry sends
    exactly this); applying a sensible floor is the caller's job.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # delta-seconds: a bare non-negative integer.
    if text.isdigit():
        return float(int(text))
    # HTTP-date.
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:  # defensive — older Pythons returned None, not raised
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (parsed - reference).total_seconds())


def _retry_after_header(resp: "object") -> str | None:
    """Best-effort read of a response's Retry-After header. Returns the raw
    string, or None when the response exposes no usable headers mapping."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter("Retry-After")
    return value if isinstance(value, str) else None


def request_with_429_backoff(
    call_fn: Callable[[], "object"],
    *,
    host: str | None = None,
    min_interval: float = 0.0,
    delays: tuple[float, ...] = (1.0, 2.0),
    retry_after_floor: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
):
    """Invoke `call_fn()` (which must return a `requests.Response`-like
    object exposing `status_code`).

    If `host` and `min_interval > 0`, the global host throttle is acquired
    BEFORE the first call (and re-acquired before each retry, so retries
    don't punch through the rate limit).

    Two 429-handling modes:

    LEGACY MODE (retry_after_floor <= 0, the default):
        On 429, sleep delays[i] then retry. After exhausting `delays`, the
        next 429 is returned to the caller as-is. With the default
        delays=(1.0, 2.0): up to 3 attempts, fixed 1s/2s backoff, the
        Retry-After header ignored. Used by crt.sh / OPR / Safe Browsing /
        rdap.enrich — endpoints where a fixed short backoff is fine.

    RETRY-AFTER MODE (retry_after_floor > 0, requires `host`):
        On 429, honor the response's Retry-After header (RFC 7231 7.1.3)
        floored at `retry_after_floor` seconds, then make EXACTLY ONE retry.
        A per-host cooldown is recorded on GLOBAL_HOST_COOLDOWN so concurrent
        workers for the same host skip it during the wait. Used by
        rdap.check_availability — see config.json:rdap_429_backoff_floor_seconds.

        Why one retry, not three: at a 60s floor (GMO Registry) three retries
        would burn 180s+ per rate-limited domain and exhaust the per-host
        availability budget after a handful of domains. One retry honors the
        registry's documented cooldown once; a still-429 hands back to the
        caller as unknown.
    """
    if retry_after_floor > 0:
        if not host:
            raise ValueError("retry_after_floor > 0 requires host to be set")
        return _request_honoring_retry_after(
            call_fn,
            host=host,
            min_interval=min_interval,
            retry_after_floor=retry_after_floor,
            sleep=sleep,
            clock=clock,
        )

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


def _request_honoring_retry_after(
    call_fn: Callable[[], "object"],
    *,
    host: str,
    min_interval: float,
    retry_after_floor: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
):
    """429 handler that honors Retry-After with a per-host floor + one retry.
    See request_with_429_backoff's RETRY-AFTER MODE docstring for the public
    contract.

    Cooldown semantics — the cooldown is armed ONCE, on the first 429, for
    `cooldown_seconds`. Its purpose is to shield *concurrent* workers for the
    same host: while this worker sleeps out the registry's penalty window, a
    sibling worker that checks GLOBAL_HOST_COOLDOWN sees the host is cooling
    and skips, rather than firing a fresh request into that window.

    The retry-429 deliberately does NOT re-arm the cooldown. Re-arming would
    push cooling_until past the end of this worker's sleep; a sequential
    single-worker bucket (every RDAP host runs one worker by default) would
    then race through every remaining domain as instant cooldown-skips,
    collapsing the whole bucket to `unknown` after a single double-429.
    Instead each rate-limited domain pays its own one-time sleep, pacing a
    single worker to one request per cooldown window — exactly the cadence
    GMO's /help page asks for ("do not access ... for at least a minute").

    Sleep cap — when a registry sends an explicit long Retry-After, the
    cooldown is armed for the FULL value (subsequent domains correctly skip
    the host for the whole ban) but this worker sleeps at most
    MAX_429_SLEEP_SECONDS so it never hangs the pipeline. See that constant.
    """
    def _attempt():
        if min_interval > 0:
            GLOBAL_HOST_THROTTLE.acquire(host, min_interval, sleep=sleep)
        return call_fn()

    resp = _attempt()
    if getattr(resp, "status_code", None) != 429:
        return resp

    raw = _retry_after_header(resp)
    parsed = parse_retry_after(raw)
    cooldown_seconds = max(parsed if parsed is not None else 0.0, retry_after_floor)
    GLOBAL_HOST_COOLDOWN.start(host, cooldown_seconds, clock=clock)

    sleep_seconds = min(cooldown_seconds, MAX_429_SLEEP_SECONDS)
    logger.warning(
        "429 from %s — honoring %.0fs cooldown (Retry-After header=%r, "
        "per-host floor=%.0fs); sleeping %.0fs then one retry",
        host, cooldown_seconds, raw, retry_after_floor, sleep_seconds,
    )
    sleep(sleep_seconds)

    resp = _attempt()
    if getattr(resp, "status_code", None) == 429:
        logger.warning(
            "429 from %s on retry — domain treated as unknown; host cooldown "
            "has %.0fs of its %.0fs window left",
            host,
            GLOBAL_HOST_COOLDOWN.seconds_remaining(host, clock=clock),
            cooldown_seconds,
        )
    return resp
