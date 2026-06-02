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
# Per-host run-scoped STOP (the 429 safety kill-switch)
# ---------------------------------------------------------------------------


class HostStop:
    """Per-host kill-switch for the remainder of a pipeline run.

    Once an RDAP host returns a 429 (rate limit) or a 403 (block), it is
    STOPPED for the rest of THIS run: no further requests are issued to it,
    no retry, no resume after any cooldown window expires. Callers check
    `is_stopped(host)` before every request and skip stopped hosts entirely.

    Why a permanent run-stop, not just the time-bounded HostCooldown:
    stopping the host is the *maximum* possible rate-decrease, which is
    exactly what RFC 7480 §5.5 asks a client to do on a 429 ("SHOULD decrease
    its query rate"). It is also the surest defense against escalating a
    survivable 429 into a catastrophic 403 IP-block — the GMO outcome where
    continued access during a penalty window got our egress IP blocked at the
    registry's edge. A 429 means "you found the rate edge on THIS host — back
    off this host for today," NOT "abort the whole run": other hosts run in
    their own buckets and are completely unaffected.

    Trade-off (intentional): a single transient 429 retires that host's whole
    remaining bucket for the day. The candidates left unchecked are logged
    loudly (pipeline._check_availability_concurrent) so the operator sees
    exactly what was left on the table. Under-checking is the safe failure —
    an unchecked candidate is rejected, never wrongly published.

    Per-process, in-memory, thread-safe. One module-level singleton
    (GLOBAL_HOST_STOP); reset between tests via the enrichment conftest.
    Records (monotonic time, reason) of the first stop for diagnostics.
    """

    def __init__(self) -> None:
        self._stopped: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def stop(
        self,
        host: str,
        *,
        reason: str = "429",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Mark `host` stopped for the rest of the run. First stop wins —
        the recorded (time, reason) reflects what first tripped the host, so
        a later 403 doesn't overwrite the original 429 timestamp (and vice
        versa)."""
        with self._lock:
            if host not in self._stopped:
                self._stopped[host] = (clock(), reason)

    def is_stopped(self, host: str) -> bool:
        with self._lock:
            return host in self._stopped

    def stopped_hosts(self) -> dict[str, tuple[float, str]]:
        """Snapshot of stopped hosts → (monotonic_stop_time, reason)."""
        with self._lock:
            return dict(self._stopped)

    def reset(self) -> None:
        """For tests — clear all stop state."""
        with self._lock:
            self._stopped.clear()


# Module-level singleton — the RDAP availability check arms (on 429/403) and
# reads this. One per Python process; reset between tests via the conftest.
GLOBAL_HOST_STOP = HostStop()


# ---------------------------------------------------------------------------
# Per-host cumulative 429 strike counter
# ---------------------------------------------------------------------------


class HostStrikes:
    """Per-host cumulative 429 strike counter for a pipeline run.

    Each 429 from a host is one strike. `record(host)` increments and returns
    the new total. Strikes accumulate CUMULATIVELY across the whole bucket —
    a successful (non-429) query does NOT reset the counter — so a host that
    429s intermittently still trends toward its strike cap and cannot evade it
    by spacing the 429s out. The RDAP 429 handler stops the host (GLOBAL_HOST_
    STOP) once the count reaches the configured strike limit; below that it
    honors Retry-After and resumes. See config.json:rdap_429_strike_limit and
    request_with_429_backoff's RETRY-AFTER MODE.

    Why cumulative, not strictly-consecutive: a registry under load may answer
    A→429, B→200, C→429, D→200, E→429. Those are three real rate-limit signals
    spread across successes; a consecutive-only counter would reset on each 200
    and never trip. Cumulative counting treats the run's total pushback as the
    signal, which is what "the registry means it" actually looks like.

    Per-process, in-memory, thread-safe. One module-level singleton
    (GLOBAL_HOST_STRIKES); reset between tests via the enrichment conftest.
    """

    def __init__(self) -> None:
        self._strikes: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, host: str) -> int:
        """Add one strike to `host` and return its new cumulative total."""
        with self._lock:
            total = self._strikes.get(host, 0) + 1
            self._strikes[host] = total
            return total

    def count(self, host: str) -> int:
        with self._lock:
            return self._strikes.get(host, 0)

    def total(self) -> int:
        """Sum of strikes across all hosts this run (for the daily report)."""
        with self._lock:
            return sum(self._strikes.values())

    def reset(self) -> None:
        """For tests — clear all strike state."""
        with self._lock:
            self._strikes.clear()


# Module-level singleton — the RDAP 429 handler records strikes here and stops
# the host at the configured limit. One per process; reset between tests.
GLOBAL_HOST_STRIKES = HostStrikes()


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
    strike_limit: int = 3,
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
        On 429, honor the Retry-After header (RFC 7231 7.1.3, floored at
        `retry_after_floor`) by arming a per-host cooldown on
        GLOBAL_HOST_COOLDOWN (so subsequent same-host domains skip during the
        window, then the host RESUMES at its normal pace), record one strike
        on GLOBAL_HOST_STRIKES, and return the 429 to the caller as unknown.
        No sleep, no same-domain retry. Once a host accumulates `strike_limit`
        cumulative strikes (default 3), it is STOPPED for the rest of the run
        on GLOBAL_HOST_STOP. Used by rdap.check_availability — see
        config.json:rdap_429_strike_limit / rdap_429_backoff_floor_seconds and
        the HostStrikes / HostStop docstrings.

        Why a 3-strike rule, not stop-on-first (changed 2026-06-02): per RFC
        7480 §5.5 a 429 is the survivable "decrease your rate" signal, not the
        catastrophic one. A single transient 429 must not retire a host's whole
        bucket for the day; sustained pushback (3 cumulative strikes) is the
        heuristic for "the registry means it." 403 remains an immediate hard
        stop (handled in rdap.check_availability) — it is the catastrophic
        block and one is enough. Other hosts are unaffected.
    """
    if retry_after_floor > 0:
        if not host:
            raise ValueError("retry_after_floor > 0 requires host to be set")
        return _request_honoring_retry_after(
            call_fn,
            host=host,
            min_interval=min_interval,
            retry_after_floor=retry_after_floor,
            strike_limit=strike_limit,
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
    strike_limit: int,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
):
    """429 handler implementing the per-host 3-strike rule. See
    request_with_429_backoff's RETRY-AFTER MODE docstring for the public
    contract and the 2026-06-02 rationale.

    On 429 we do three non-blocking things before returning the 429:
      1. Parse the Retry-After header (floored at `retry_after_floor`) and arm
         GLOBAL_HOST_COOLDOWN for that duration — honors the registry's stated
         wait, so subsequent same-host domains skip during the window and the
         host then RESUMES at its normal throttled pace.
      2. Record one cumulative strike on GLOBAL_HOST_STRIKES.
      3. Only if the strike count has reached `strike_limit` do we arm
         GLOBAL_HOST_STOP — a permanent run-stop for that host. Below the
         limit the host keeps going.

    The request is throttle-paced (GLOBAL_HOST_THROTTLE) so the steady-state
    per-host rate is unchanged. We never retry the SAME domain — the 429'd
    domain is returned as unknown and the worker moves on; pacing/recovery is
    handled by the cooldown skip, not a re-query.
    """
    if min_interval > 0:
        GLOBAL_HOST_THROTTLE.acquire(host, min_interval, sleep=sleep)
    resp = call_fn()
    if getattr(resp, "status_code", None) != 429:
        return resp

    raw = _retry_after_header(resp)
    parsed = parse_retry_after(raw)
    cooldown_seconds = max(parsed if parsed is not None else 0.0, retry_after_floor)
    GLOBAL_HOST_COOLDOWN.start(host, cooldown_seconds, clock=clock)
    strikes = GLOBAL_HOST_STRIKES.record(host)

    if strikes >= strike_limit:
        GLOBAL_HOST_STOP.stop(host, reason="429", clock=clock)
        logger.warning(
            "429 from %s — strike %d/%d: STRIKE LIMIT reached, STOPPING this "
            "host for the rest of the run (no resume). Honored %.0fs cooldown "
            "(Retry-After header=%r). Other RDAP hosts continue normally.",
            host, strikes, strike_limit, cooldown_seconds, raw,
        )
    else:
        logger.warning(
            "429 from %s — strike %d/%d: honored %.0fs cooldown (Retry-After "
            "header=%r), resuming this host at its normal pace.",
            host, strikes, strike_limit, cooldown_seconds, raw,
        )
    return resp
