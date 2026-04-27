"""SURBL DNS blocklist lookup.

Queries `<domain>.multi.surbl.org`. Resolution to 127.0.0.x means listed.
NXDOMAIN means clean.

Returned fields:
    {"surbl_listed": bool}

Empty dict on transient DNS failure — never crashes the pipeline. Wrapped
in a circuit breaker so a struggling local resolver doesn't drag every
candidate through the timeout.
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
        return {}
    _BREAKER.record_success()
    return {"surbl_listed": listed}
