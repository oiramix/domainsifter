"""Spamhaus DBL lookup.

Queries `<domain>.dbl.spamhaus.org`. Resolution to 127.0.1.x means listed;
NXDOMAIN means clean; 127.255.255.x and other unexpected responses mean
"unknown" (DNSBL service is rate-limiting our resolver or otherwise
refusing to answer authoritatively).

Returned fields:
    {"spamhaus_listed": True}   — listed
    {"spamhaus_listed": False}  — confirmed not listed
    {"spamhaus_listed": None}   — DNSBL unavailable / rate-limited;
                                  caller must treat as "no signal", not
                                  as "not listed"
    {}                          — circuit breaker open

THREE-STATE CONTRACT (changed 2026-05-12): the unknown case used to return
`{}`, which the post-enrichment filter conflated with both "field missing"
and "not listed". The explicit `None` makes the "no signal" case visible
to downstream code (and to the daily report). See scripts/enrichment/_dnsbl.py
for the resolver-level reasoning.

Circuit breaker policy: an unknown response still counts as a failure (we
back off after consecutive unknowns to avoid burning budget on a DNSBL
that is currently refusing to answer).
"""

from __future__ import annotations

from scripts.enrichment._circuit_breaker import CircuitBreaker
from scripts.enrichment._dnsbl import is_listed

_ZONE = "dbl.spamhaus.org"
_BREAKER = CircuitBreaker("spamhaus")


def enrich(domain: str, config: dict) -> dict:
    if _BREAKER.is_open():
        return {}

    zone = config.get("dnsbl", {}).get("spamhaus_zone", _ZONE)
    listed = is_listed(domain, zone)
    if listed is None:
        _BREAKER.record_failure()
        return {"spamhaus_listed": None}
    _BREAKER.record_success()
    return {"spamhaus_listed": listed}
