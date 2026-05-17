"""Wayback Machine CDX enrichment.

Counts the snapshots IA's CDX API holds for the apex domain (matchType=domain
so all paths and subdomains roll up under the apex). Snapshot count is the
only field the scorer reads; `wayback_last_snapshot` ships in the published
JSON for human display only.

Returned fields:
    {
        "wayback_snapshots": int,                # total snapshot count
        "wayback_last_snapshot": "YYYY-MM-DD",   # date of most recent snapshot, or None
    }

Implementation: uses the EDGI / `internetarchive` `wayback` Python package
(BSD-3-Clause; pinned in requirements.txt). Replaced the hand-rolled
requests-based CDX client on 2026-05-08 because the previous implementation's
5s+15s timeout backoff was burning ~200 seconds per failed call when Wayback
degraded — 45 of 75 RDAP-available candidates skipped enrichment that day.

The package handles 429 responses with the documented 60-second adaptive
delay (per `DEFAULT_RATE_LIMIT_DELAY` in wayback._client). Its retry policy
is configured per-`WaybackSession` (retries × backoff schedule); we set
conservative values so a stuck call returns to the orchestrator within ~75
seconds rather than blocking the whole enrichment phase.

Returns empty dict on any failure (network, 5xx, malformed response, OR an
open circuit breaker — see scripts.enrichment._circuit_breaker for the why).
The orchestrator's circuit breaker is preserved on top of the package's own
retry behaviour: per-session retries reduce per-call failure rate; the
orchestrator-level breaker still trips after N consecutive call failures so
the whole pipeline doesn't burn budget on a definitively-down host.
"""

from __future__ import annotations

import logging
import time

from wayback import WaybackClient, WaybackSession
from wayback.exceptions import WaybackException

from scripts.enrichment._circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

_BREAKER = CircuitBreaker("wayback")


def is_breaker_open() -> bool:
    """Public accessor for the wayback breaker state. Used by the post-
    enrichment filter and pipeline orchestrator to distinguish a candidate
    whose wayback enrichment was deferred (breaker open / call failed) from
    one whose enrichment succeeded with zero snapshots."""
    return _BREAKER.is_open()


def _format_timestamp(ts) -> str | None:
    """`CdxRecord.timestamp` is a `datetime.datetime` per the package's
    parser. Format to YYYY-MM-DD for the published-payload schema. Defensive
    against the package returning a string (older versions) or None."""
    if ts is None:
        return None
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d")
    s = str(ts)
    return s[:10] if len(s) >= 10 else None


def enrich(domain: str, config: dict) -> dict:
    if _BREAKER.is_open():
        # Surface the breaker-open state so the post-enrichment filter and
        # the publish-gate can distinguish "we didn't ask Wayback" from
        # "Wayback says no snapshots." Without this flag, downstream code
        # cannot tell the difference and silently drops good domains on
        # flaky-Wayback days. The flag is internal — see CONTRACT_FIELDS in
        # scripts/output.py for the publish-surface keys.
        return {"wayback_unknown": True}

    timeout = config.get("api_request_timeout_seconds", {}).get(
        "wayback", config.get("request_timeout_seconds", 60)
    )
    # The legacy hand-rolled client read api_min_interval_seconds.wayback as a
    # min-interval (seconds between calls); the package wants the same idea
    # expressed as max calls per second. Translate so existing config keeps
    # the same per-host pacing meaning.
    min_interval = float(config.get("api_min_interval_seconds", {}).get("wayback", 1.0))
    cps = (1.0 / min_interval) if min_interval > 0 else 0  # 0 disables rate-limit

    session = WaybackSession(
        retries=2,           # 3 total attempts; package's 60s 429-delay applies between them
        backoff=2,           # exponential: 0s, 2s, 4s on consecutive non-429 failures
        timeout=timeout,
        search_calls_per_second=cps,
    )
    client = WaybackClient(session=session)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("wayback request initiated host=web.archive.org domain=%s", domain)
    request_started_at = time.monotonic()

    try:
        # match_type='domain' rolls up all subdomains and paths under the apex.
        # limit=10000 matches the prior implementation; we only use the count
        # and the latest timestamp, so capping at 10k is fine.
        snapshot_count = 0
        latest_ts = None
        for record in client.search(domain, match_type="domain", limit=10000):
            snapshot_count += 1
            ts = getattr(record, "timestamp", None)
            if ts is not None and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
    except WaybackException as exc:
        elapsed_ms = (time.monotonic() - request_started_at) * 1000.0
        logger.warning(
            "Wayback enrich failed for %s after %.0fms: %s", domain, elapsed_ms, exc,
        )
        _BREAKER.record_failure()
        # Same wayback_unknown flag for individual failure as for breaker-
        # open above — both mean "we don't actually know if this site had
        # historical snapshots." See the breaker-open path's comment.
        return {"wayback_unknown": True}
    except Exception as exc:  # defence-in-depth — package may surface urllib3 / requests errors
        elapsed_ms = (time.monotonic() - request_started_at) * 1000.0
        logger.warning(
            "Wayback enrich failed for %s after %.0fms: %s", domain, elapsed_ms, exc,
        )
        _BREAKER.record_failure()
        # Same wayback_unknown flag for individual failure as for breaker-
        # open above — both mean "we don't actually know if this site had
        # historical snapshots." See the breaker-open path's comment.
        return {"wayback_unknown": True}
    finally:
        # Close the session's connection pool. Cheap and safe.
        try:
            session.close()
        except Exception:  # pragma: no cover
            pass

    elapsed_ms = (time.monotonic() - request_started_at) * 1000.0
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "wayback response host=web.archive.org domain=%s snapshots=%d elapsed=%.0fms",
            domain, snapshot_count, elapsed_ms,
        )

    _BREAKER.record_success()

    return {
        "wayback_snapshots": snapshot_count,
        "wayback_last_snapshot": _format_timestamp(latest_ts),
    }
