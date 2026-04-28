"""RDAP enrichment — previous registrar lookup.

RDAP (Registration Data Access Protocol) is the modern, JSON-based replacement
for WHOIS. Each TLD's registry runs an RDAP server; we discover the right
server via IANA's bootstrap file at https://data.iana.org/rdap/dns.json.

The bootstrap file maps TLDs → list of RDAP base URLs. We fetch it once per
process via `@lru_cache` and reuse it for every subsequent enrichment.

Returned fields:
    {
        "previous_registrar": str,     # e.g., "GoDaddy.com, LLC", or None
        "rdap_status": list[str],      # e.g., ["pending delete", "redemption period"]
    }

Empty dict on bootstrap failure, missing TLD, or RDAP query failure. The
field is purely informational (used for display, not filtering or scoring),
so a per-domain failure is harmless.

Newly-dropped domains often return 404 from the registry — that's fine, we
return empty fields. The pipeline's display layer can show "Unknown".
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import requests

from scripts.enrichment._circuit_breaker import CircuitBreaker, request_with_429_backoff

logger = logging.getLogger(__name__)

_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
_BREAKER = CircuitBreaker("rdap")


@lru_cache(maxsize=8)
def _fetch_bootstrap(url: str, timeout: int) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Fetch and parse the IANA RDAP bootstrap. Returns a tuple-of-tuples so the
    return value is hashable (lru_cache requirement). Returns None on failure;
    failures are NOT cached (caller's next attempt may succeed)."""
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("RDAP bootstrap fetch failed: %s", exc)
        # Drop the failed call from the cache so a retry can succeed.
        _fetch_bootstrap.cache_clear()
        return None

    services = body.get("services") if isinstance(body, dict) else None
    if not isinstance(services, list):
        logger.warning("RDAP bootstrap had no 'services' array")
        return ()

    items: list[tuple[str, tuple[str, ...]]] = []
    for entry in services:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        tlds, urls = entry
        if not isinstance(tlds, list) or not isinstance(urls, list):
            continue
        url_tuple = tuple(u for u in urls if isinstance(u, str))
        for tld in tlds:
            if isinstance(tld, str):
                items.append((tld.lower(), url_tuple))
    return tuple(items)


def _load_bootstrap(config: dict, timeout: int) -> dict[str, tuple[str, ...]] | None:
    url = config.get("api_endpoints", {}).get("rdap_bootstrap", _BOOTSTRAP_URL)
    items = _fetch_bootstrap(url, timeout)
    if items is None:
        return None
    return dict(items)


def _extract_registrar(record: dict[str, Any]) -> str | None:
    entities = record.get("entities")
    if not isinstance(entities, list):
        return None
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles")
        if not isinstance(roles, list) or "registrar" not in roles:
            continue
        vcard = ent.get("vcardArray")
        if not isinstance(vcard, list) or len(vcard) < 2 or not isinstance(vcard[1], list):
            continue
        for prop in vcard[1]:
            if isinstance(prop, list) and len(prop) >= 4 and prop[0] == "fn":
                return str(prop[3])
    return None


def enrich(domain: str, config: dict) -> dict:
    if _BREAKER.is_open():
        return {}

    timeout = config.get("request_timeout_seconds", 10)
    bootstrap = _load_bootstrap(config, timeout)
    if bootstrap is None or not bootstrap:
        return {}

    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    if not tld:
        return {}
    bases = bootstrap.get(tld)
    if not bases:
        logger.debug("No RDAP server for .%s", tld)
        return {}

    base = bases[0].rstrip("/")
    url = f"{base}/domain/{domain}"
    # RDAP servers vary per-TLD. Throttle is keyed on the actual host so
    # each registry's limit is respected independently.
    from urllib.parse import urlparse
    rdap_host = urlparse(base).hostname or "rdap"
    min_interval = float(config.get("api_min_interval_seconds", {}).get("rdap", 0.2))
    try:
        response = request_with_429_backoff(
            lambda: requests.get(url, timeout=timeout),
            host=rdap_host,
            min_interval=min_interval,
        )
    except requests.RequestException as exc:
        logger.warning("RDAP query for %s failed: %s", domain, exc)
        _BREAKER.record_failure()
        return {}

    if response.status_code == 404:
        # 404 means "registry has no record" — that is a SUCCESSFUL query
        # for a freshly-dropped domain, not a transport failure.
        _BREAKER.record_success()
        return {"previous_registrar": None, "rdap_status": []}
    if response.status_code == 429:
        logger.warning("RDAP persistent 429 for %s", domain)
        _BREAKER.record_failure()
        return {}
    if response.status_code != 200:
        _BREAKER.record_failure()
        return {}

    try:
        record = response.json()
    except ValueError as exc:
        logger.warning("RDAP response for %s was not JSON: %s", domain, exc)
        _BREAKER.record_failure()
        return {}

    _BREAKER.record_success()

    if not isinstance(record, dict):
        return {}

    registrar = _extract_registrar(record)
    status = record.get("status")
    if not isinstance(status, list):
        status = []

    return {"previous_registrar": registrar, "rdap_status": status}
