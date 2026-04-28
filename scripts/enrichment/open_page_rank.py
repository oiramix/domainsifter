"""OpenPageRank enrichment.

Authority score for the domain. API is free up to 10k requests/day with an
API key registered at https://www.domcop.com/openpagerank/.

Endpoint: https://openpagerank.com/api/v1.0/getPageRank
Auth: header `API-OPR: <key>`

Returned fields:
    {"open_page_rank": float}   — page_rank_decimal as float

API key from `os.environ["OPENPAGERANK_KEY"]`. If unset, returns empty dict
(this enrichment is silently skipped — never crashes the pipeline).

Wrapped in a circuit breaker (see scripts.enrichment._circuit_breaker) and
429 backoff. After 5 consecutive failures the breaker opens for 15 minutes
and `enrich()` returns {} immediately, sparing the budget.
"""

from __future__ import annotations

import logging
import os

import requests

from scripts.enrichment._circuit_breaker import CircuitBreaker, request_with_429_backoff

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://openpagerank.com/api/v1.0/getPageRank"
_KEY_ENV_VAR = "OPENPAGERANK_KEY"
_BREAKER = CircuitBreaker("open_page_rank")


def enrich(domain: str, config: dict) -> dict:
    api_key = os.environ.get(_KEY_ENV_VAR)
    if not api_key:
        logger.debug("OPENPAGERANK_KEY unset; skipping OPR for %s", domain)
        return {}

    if _BREAKER.is_open():
        return {}

    endpoint = config.get("api_endpoints", {}).get("open_page_rank", _DEFAULT_ENDPOINT)
    timeout = config.get("request_timeout_seconds", 10)
    headers = {"API-OPR": api_key}
    params = [("domains[]", domain)]

    min_interval = float(config.get("api_min_interval_seconds", {}).get("open_page_rank", 0.4))
    try:
        response = request_with_429_backoff(
            lambda: requests.get(endpoint, headers=headers, params=params, timeout=timeout),
            host="openpagerank.com",
            min_interval=min_interval,
        )
        if response.status_code == 429:
            logger.warning("OPR persistent 429 for %s", domain)
            _BREAKER.record_failure()
            return {}
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OPR enrich failed for %s: %s", domain, exc)
        _BREAKER.record_failure()
        return {}

    _BREAKER.record_success()

    entries = body.get("response") if isinstance(body, dict) else None
    if not isinstance(entries, list) or not entries:
        return {}

    entry = entries[0]
    if not isinstance(entry, dict):
        return {}

    raw = entry.get("page_rank_decimal", 0)
    try:
        score = float(raw)
    except (TypeError, ValueError):
        score = 0.0

    return {"open_page_rank": score}
