"""Shared helper for DNS-based blocklist lookups (SURBL, Spamhaus DBL).

A DNSBL lookup works by appending the blocklist's zone to the queried name
and resolving its A record. The blocklist encodes its answer in the 127/8
address it returns:

    127.0.0.x  — SURBL category flags (e.g. 127.0.0.2 = SC, 127.0.0.4 = WS)
    127.0.1.x  — Spamhaus DBL category codes (e.g. 127.0.1.2 = spam domain)
    NXDOMAIN   — not listed
    127.255.255.x — error band (rate limit, public-resolver block, etc.)

We use socket.gethostbyname_ex via a thin wrapper so tests can monkeypatch.
This keeps us in the standard library — no `dnspython` dependency for v1
(CLAUDE.md hard rule #7: standard library + requests only).

THREE-STATE CONTRACT (changed 2026-05-12):
    True  → genuinely listed (127.0.0.x or 127.0.1.x in the response)
    False → not listed (NXDOMAIN)
    None  → unknown: error-band response, unexpected address, or DNS
            transport failure. Caller must NOT treat None as "not listed".

Why three states: on 2026-05-12 OVH's shared resolver (213.186.33.99) was
rate-limited by Spamhaus, which returned 127.255.255.254 ("query via public
resolver, refused") for every lookup. The previous implementation classed
any 127.x response as listed=True, which mis-rejected 100% of post-
enrichment candidates that day. The fix is to refuse to interpret error-
band responses as listings.
"""

from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)


def is_listed(domain: str, zone: str) -> bool | None:
    """Three-state DNSBL lookup. See module docstring for the contract.

    Returns True for a legitimate listing (response includes a 127.0.0.x
    or 127.0.1.x address), False for NXDOMAIN, None for everything else
    (error-band response such as 127.255.255.254, unexpected address, or
    DNS transport failure).
    """
    query = f"{domain}.{zone}"
    try:
        _, _, addrs = socket.gethostbyname_ex(query)
    except socket.gaierror as exc:
        if getattr(exc, "errno", None) in (socket.EAI_NONAME, -2):
            return False
        if "not known" in str(exc).lower() or "no such host" in str(exc).lower():
            return False
        logger.warning("DNSBL lookup failed for %s: %s", query, exc)
        return None
    except OSError as exc:
        logger.warning("DNSBL lookup OS error for %s: %s", query, exc)
        return None

    if not addrs:
        # gethostbyname_ex normally raises gaierror on NXDOMAIN, so an
        # empty list is "unexpected". Refuse to interpret.
        return None

    # A legitimate listing is encoded in 127.0.0.x (SURBL) or 127.0.1.x
    # (Spamhaus DBL). 127.255.255.x is the well-known error band. Other
    # 127.x addresses or anything outside 127/8 is unexpected — treat as
    # "unknown" rather than over-claiming a listing.
    for addr in addrs:
        if addr.startswith("127.0.0.") or addr.startswith("127.0.1."):
            return True
    logger.warning(
        "DNSBL %s returned non-listing address(es) %s for %s — treating as unknown",
        zone, addrs, query,
    )
    return None
