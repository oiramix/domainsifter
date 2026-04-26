"""SURBL DNS blocklist lookup.

Queries `<domain>.multi.surbl.org`. Resolution to 127.0.0.x means listed.
NXDOMAIN means clean.

Returned fields:
    {"surbl_listed": bool}

Empty dict on transient DNS failure — never crashes the pipeline.
"""

from __future__ import annotations

from scripts.enrichment._dnsbl import is_listed

_ZONE = "multi.surbl.org"


def enrich(domain: str, config: dict) -> dict:
    zone = config.get("dnsbl", {}).get("surbl_zone", _ZONE)
    listed = is_listed(domain, zone)
    if listed is None:
        return {}
    return {"surbl_listed": listed}
