"""Wayback Machine CDX enrichment.

Queries the Wayback CDX API for snapshot history of the domain. We hit the
host alone (no path) with `matchType=domain` to count snapshots across all
URLs ever archived under that domain.

Returned fields:
    {
        "wayback_snapshots": int,                # total snapshot count
        "wayback_last_snapshot": "YYYY-MM-DD",   # date of most recent snapshot, or None
    }

Endpoint: https://web.archive.org/cdx/search/cdx
Docs: https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server

Returns empty dict on any failure (network, 5xx, malformed JSON, OR an open
circuit breaker — see scripts.enrichment._circuit_breaker for the why).
429 responses are retried with exponential backoff before counting as failure.
"""

from __future__ import annotations

import logging
import time

import requests

from scripts.enrichment._circuit_breaker import CircuitBreaker, request_with_429_backoff

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
_BREAKER = CircuitBreaker("wayback")


def enrich(domain: str, config: dict) -> dict:
    if _BREAKER.is_open():
        return {}

    endpoint = config.get("api_endpoints", {}).get("wayback_cdx", _DEFAULT_ENDPOINT)
    timeout = config.get("request_timeout_seconds", 10)
    params = {
        "url": domain,
        "matchType": "domain",
        "output": "json",
        "fl": "timestamp",
        "limit": 10000,
    }
    min_interval = float(config.get("api_min_interval_seconds", {}).get("wayback", 1.0))

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("wayback request initiated host=web.archive.org domain=%s", domain)
    request_started_at = time.monotonic()

    try:
        response = request_with_429_backoff(
            lambda: requests.get(endpoint, params=params, timeout=timeout),
            host="web.archive.org",
            min_interval=min_interval,
        )
        elapsed_ms = (time.monotonic() - request_started_at) * 1000.0
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "wayback response host=web.archive.org domain=%s status=%s elapsed=%.0fms",
                domain, response.status_code, elapsed_ms,
            )
        if response.status_code == 429:
            logger.warning("Wayback persistent 429 for %s", domain)
            _BREAKER.record_failure()
            return {}
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        elapsed_ms = (time.monotonic() - request_started_at) * 1000.0
        logger.warning("Wayback enrich failed for %s after %.0fms: %s", domain, elapsed_ms, exc)
        _BREAKER.record_failure()
        return {}

    _BREAKER.record_success()

    if not isinstance(rows, list) or len(rows) <= 1:
        return {"wayback_snapshots": 0, "wayback_last_snapshot": None}

    data_rows = rows[1:]
    snapshot_count = len(data_rows)
    timestamps = [row[0] for row in data_rows if row and isinstance(row[0], str)]
    last_snapshot = None
    if timestamps:
        latest = max(timestamps)
        if len(latest) >= 8:
            last_snapshot = f"{latest[0:4]}-{latest[4:6]}-{latest[6:8]}"

    return {
        "wayback_snapshots": snapshot_count,
        "wayback_last_snapshot": last_snapshot,
    }
