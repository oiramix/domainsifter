"""Spamhaus DBL lookup.

Queries `<domain>.dbl.spamhaus.org`. Resolution to 127.0.1.x means listed.
NXDOMAIN means clean.

Note: Spamhaus DBL uses 127.0.1.0/24 specifically, but for our purposes any
127.x response from the DBL zone is treated as "listed" — we don't need to
distinguish between sub-categories (spam, phishing, malware) for v1's
binary reject decision.

Returned fields:
    {"spamhaus_listed": bool}

Empty dict on transient DNS failure.
"""

from __future__ import annotations

from scripts.enrichment._dnsbl import is_listed

_ZONE = "dbl.spamhaus.org"


def enrich(domain: str, config: dict) -> dict:
    zone = config.get("dnsbl", {}).get("spamhaus_zone", _ZONE)
    listed = is_listed(domain, zone)
    if listed is None:
        return {}
    return {"spamhaus_listed": listed}
