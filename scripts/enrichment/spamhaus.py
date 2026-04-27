"""Spamhaus DBL lookup.

Queries `<domain>.dbl.spamhaus.org`. Resolution to 127.0.1.x means listed.
NXDOMAIN means clean.

Returned fields:
    {"spamhaus_listed": bool}

Empty dict on transient DNS failure. Circuit breaker prevents a flaky
resolver from costing the full timeout per candidate.
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
        return {}
    _BREAKER.record_success()
    return {"spamhaus_listed": listed}
