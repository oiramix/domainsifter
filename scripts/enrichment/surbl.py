"""SURBL DNS blocklist lookup.

Queries `<domain>.multi.surbl.org`. Resolution to 127.0.0.x means listed;
NXDOMAIN means clean; 127.255.255.x and other unexpected responses mean
"unknown" (DNSBL service is rate-limiting our resolver or otherwise
refusing to answer authoritatively).

Returned fields:
    {"surbl_listed": True}   — listed
    {"surbl_listed": False}  — confirmed not listed
    {"surbl_listed": None}   — DNSBL unavailable / rate-limited; caller
                               must treat as "no signal", not "not listed"
    {}                       — circuit breaker open

THREE-STATE CONTRACT (changed 2026-05-12): see scripts/enrichment/spamhaus.py
and scripts/enrichment/_dnsbl.py for the reasoning. The unknown case used
to return `{}`, which the post-enrichment filter conflated with "not
listed". The explicit `None` makes the "no signal" case visible
downstream.

Circuit breaker policy: an unknown response still counts as a failure so
we back off after consecutive unknowns rather than burning budget on a
DNSBL that is currently refusing to answer.
"""

from __future__ import annotations

from scripts.enrichment._circuit_breaker import CircuitBreaker
from scripts.enrichment._dnsbl import is_listed

_ZONE = "multi.surbl.org"
_BREAKER = CircuitBreaker("surbl")


def enrich(domain: str, config: dict) -> dict:
    if _BREAKER.is_open():
        return {}

    zone = config.get("dnsbl", {}).get("surbl_zone", _ZONE)
    listed = is_listed(domain, zone)
    if listed is None:
        _BREAKER.record_failure()
        return {"surbl_listed": None}
    _BREAKER.record_success()
    return {"surbl_listed": listed}
