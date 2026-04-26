"""Shared helper for DNS-based blocklist lookups (SURBL, Spamhaus DBL).

A DNSBL lookup works by appending the blocklist's zone to the queried name
and resolving its A record. If the lookup returns one or more 127.0.0.x IPs,
the name is listed. NXDOMAIN means not listed.

We use socket.gethostbyname_ex via a thin wrapper so tests can monkeypatch.
This keeps us in the standard library — no `dnspython` dependency for v1
(CLAUDE.md hard rule #7: standard library + requests only).
"""

from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)


def is_listed(domain: str, zone: str) -> bool | None:
    """Return True if `{domain}.{zone}` resolves to a 127.0.0.0/8 address,
    False on NXDOMAIN, None on transient resolver failure (caller treats
    None as 'unknown' and returns empty dict).
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
    return any(addr.startswith("127.") for addr in addrs)
