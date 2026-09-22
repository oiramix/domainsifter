"""OpenPageRank enrichment.

Authority score for the domain.

MIGRATED 2026-09-22. The service moved from DomCop to Keywords Everywhere
and the old API was switched off, not deprecated — `openpagerank.com`
301-redirects to `openpagerank.keywordseverywhere.com`, which 404s the old
path. Because `requests` follows redirects by default the failure surfaced
as a plain "404 Not Found" against a URL nobody had configured, which is
why it read like a transient outage rather than a migration.

It cost two days of publishing. OPR is present in 215 of 216 published
rows and is one of only three obtainable fields in the publish
completeness gate (`previous_registrar` is never populated for an
AVAILABLE domain and `cert_history` depends on crt.sh, which 502s most
days). So when OPR went dark, candidates scored 2/5 = 0.40 against a 0.50
threshold and EVERY new domain was rejected on completeness while the
filters themselves behaved perfectly normally. First failure 2026-09-21;
diagnosed 2026-09-22.

Old API (dead):
    GET  https://openpagerank.com/api/v1.0/getPageRank?domains[]=x
    Auth: header `API-OPR: <key>`
    Body: {"response": [{"page_rank_decimal": 3.7}]}

New API:
    POST https://openpagerank.keywordseverywhere.com/v1/domains/bulk
    Auth: header `Authorization: Bearer <key>`
    Send: {"domains": ["example.com"], "include_history": false}
    Body: {"as_of": "...", "count": 1,
           "results": [{"domain": "example.com", "found": true,
                        "open_page_rank": 9.67, "rank": 30,
                        "referring_domains": 282833}]}

Returned fields (UNCHANGED — the output contract is not affected):
    {"open_page_rank": float}

The key is a NEW credential: legacy DomCop keys do not work. Mint one from
the Keywords Everywhere dashboard and put it in `OPENPAGERANK_KEY`. The
free tier is 30,000 domains/month against our ~2,400, so this stays free.

`found: false` returns `{"open_page_rank": 0.0}`, NOT an empty dict. This
mirrors the old API, which returned `page_rank_decimal: 0` for a domain it
had never indexed, and the distinction is load-bearing: an empty dict
leaves the field ABSENT and costs the candidate a point of enrichment
completeness at the publish gate, whereas 0.0 is a real measurement
meaning "indexed nowhere, zero authority". Returning {} here would quietly
reject exactly the obscure dropped domains this project exists to surface.

The endpoint accepts up to 100 domains per request. We deliberately still
send one domain per call: the plugin contract is `enrich(domain, config)`
(CLAUDE.md rule 8) and at 25-80 domains/day the quota is a non-issue.
Batching is a latency optimisation available later, not a requirement.

API key from `os.environ["OPENPAGERANK_KEY"]`. If unset, returns empty
dict (silently skipped — never crashes the pipeline).

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

_DEFAULT_ENDPOINT = "https://openpagerank.keywordseverywhere.com/v1/domains/bulk"
_KEY_ENV_VAR = "OPENPAGERANK_KEY"
_BREAKER = CircuitBreaker("open_page_rank")

# Rate-limit / breaker bucket key. Must track the host we ACTUALLY talk to:
# pacing against the old hostname would silently stop pacing anything.
_HOST = "openpagerank.keywordseverywhere.com"


def _extract_score(body: object) -> dict:
    """Pull `open_page_rank` out of a bulk response for a single domain.

    Returns {} when the payload is not the documented shape — a malformed
    body is a failure to measure, not a measurement of zero. That is the
    opposite of `found: false`, which IS a measurement (see module
    docstring) and is handled here as 0.0.
    """
    if not isinstance(body, dict):
        return {}
    results = body.get("results")
    if not isinstance(results, list) or not results:
        return {}
    entry = results[0]
    if not isinstance(entry, dict):
        return {}

    if entry.get("found") is False:
        return {"open_page_rank": 0.0}

    raw = entry.get("open_page_rank", 0)
    try:
        score = float(raw)
    except (TypeError, ValueError):
        score = 0.0
    return {"open_page_rank": score}


def enrich(domain: str, config: dict) -> dict:
    api_key = os.environ.get(_KEY_ENV_VAR)
    if not api_key:
        logger.debug("OPENPAGERANK_KEY unset; skipping OPR for %s", domain)
        return {}

    if _BREAKER.is_open():
        return {}

    endpoint = config.get("api_endpoints", {}).get("open_page_rank", _DEFAULT_ENDPOINT)
    # Per-source timeout, falling back to the global. The new API is much
    # slower and far more variable than the old DomCop one: measured on the
    # box 2026-09-22 over 10 single-domain calls, min 2.56s / median 6.57s /
    # max 29.91s, with 3 of 10 exceeding the global 10s. At that rate the
    # breaker (5 consecutive failures) would open and take OPR dark for the
    # rest of a run -- which is the exact outage this migration fix exists to
    # end. Mirrors the existing api_min_interval_seconds per-source dict.
    timeout = config.get("api_timeout_seconds", {}).get(
        "open_page_rank", config.get("request_timeout_seconds", 10),
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"domains": [domain], "include_history": False}

    min_interval = float(config.get("api_min_interval_seconds", {}).get("open_page_rank", 0.4))
    try:
        response = request_with_429_backoff(
            lambda: requests.post(
                endpoint, headers=headers, json=payload, timeout=timeout,
            ),
            host=_HOST,
            min_interval=min_interval,
        )
        if response.status_code == 429:
            logger.warning("OPR persistent 429 for %s", domain)
            _BREAKER.record_failure()
            return {}
        if response.status_code in (401, 403):
            # Distinct from a generic 4xx: the key is missing, wrong, or a
            # legacy DomCop key. That is an operator problem no retry fixes,
            # and it silently zeroes the publish gate, so say so loudly.
            logger.error(
                "OPR auth rejected (HTTP %d) for %s — OPENPAGERANK_KEY is invalid "
                "or is a legacy DomCop key; mint a new one at %s",
                response.status_code, domain, _HOST,
            )
            _BREAKER.record_failure()
            return {}
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OPR enrich failed for %s: %s", domain, exc)
        _BREAKER.record_failure()
        return {}

    _BREAKER.record_success()
    return _extract_score(body)
