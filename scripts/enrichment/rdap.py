"""RDAP enrichment + availability validation.

RDAP (Registration Data Access Protocol) is the modern, JSON-based replacement
for WHOIS. Each TLD's registry runs an RDAP server; we discover the right
server via IANA's bootstrap file at https://data.iana.org/rdap/dns.json.

The bootstrap file maps TLDs → list of RDAP base URLs. We fetch it once per
process via `@lru_cache` and reuse it for every subsequent query.

Two public functions:

    enrich(domain, config) -> dict
        Display-only metadata for a domain we already believe is available.
        Kept for back-compat; not currently wired into the enrichment chain
        (validate_availability supersedes it).

    check_availability(domain, config) -> dict
        Authoritative availability validation. Distinguishes truly-available
        (HTTP 404) from owned/redemption/held (HTTP 200) and from unknown
        (transport failure / breaker open / unknown TLD).

Why availability validation matters:
A domain disappearing from a TLD zone file is NOT proof that it's available
to register. Domains drop out of zones for many reasons that don't make them
registerable: clientHold/serverHold status, redemption period (lapsed but
recoverable by old owner), DNSSEC re-signing, registrar transfers. The April
2026 audit found ~95% of zone-diff "drops" were either still owned or in
redemption — not actually available. RDAP is the single source of truth
because every ICANN-accredited registry runs one, and HTTP 404 is the only
status that proves the registry has no record at all.
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


def _extract_expiration(record: dict[str, Any]) -> str | None:
    """Pull the registry-side expiration date out of the RDAP events array.

    RDAP records expose lifecycle as a list of events, each tagged with an
    eventAction such as "registration", "expiration", "last changed",
    "transfer". The registry's authoritative expiration is the event with
    eventAction == "expiration"; "registrar expiration" exists too but
    we prefer the registry value because we trust the registry over the
    sponsoring registrar (registrar value can be stale by days).

    Returns the eventDate string trimmed to YYYY-MM-DD, or None.
    """
    events = record.get("events")
    if not isinstance(events, list):
        return None
    # Prefer "expiration", fall back to "registrar expiration".
    primary = None
    fallback = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        action = ev.get("eventAction", "")
        date = ev.get("eventDate", "")
        if not isinstance(date, str) or not date:
            continue
        if action == "expiration" and primary is None:
            primary = date
        elif action == "registrar expiration" and fallback is None:
            fallback = date
    chosen = primary or fallback
    if chosen is None:
        return None
    # Trim to YYYY-MM-DD even if a full ISO timestamp was returned.
    return chosen[:10]


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


def _empty_unknown() -> dict:
    """Sentinel return for transport failures, bootstrap fail, unknown TLD,
    or open breaker. is_available=None signals 'unknown' to callers, which
    treat unknown as REJECT (better to under-publish than over-publish).
    """
    return {
        "is_available": None,
        "rdap_status": [],
        "rdap_expiration": None,
        "previous_registrar": None,
        "rdap_http": None,
    }


def check_availability(domain: str, config: dict) -> dict:
    """Verify whether a domain is actually available to register.

    Returns a dict with these keys ALWAYS present:
        is_available:       True | False | None
        rdap_status:        list[str]    (registry status flags)
        rdap_expiration:    str | None   (YYYY-MM-DD)
        previous_registrar: str | None
        rdap_http:          int | None   (status code, or None on transport fail)

    Decision rule:
        HTTP 404                 → is_available=True   (registry has no record)
        HTTP 200                 → is_available=False  (record exists; owned,
                                                        in-redemption, on-hold)
        HTTP 429 after backoff   → is_available=None   (rate-limited)
        HTTP 5xx / other         → is_available=None
        Transport / bootstrap    → is_available=None

    Per CLAUDE.md plugin contract: never raises.

    Why is_available=None ≠ False:
        Callers must treat None as "REJECT (cautious)" — same effect as
        False — but distinguishing the two keeps logs informative. A run
        with many None values means RDAP infrastructure is degraded;
        many False values means the zone-diff signal is just bad.
    """
    if _BREAKER.is_open():
        return _empty_unknown()

    timeout = config.get("request_timeout_seconds", 10)
    bootstrap = _load_bootstrap(config, timeout)
    if bootstrap is None or not bootstrap:
        return _empty_unknown()

    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    if not tld:
        return _empty_unknown()
    bases = bootstrap.get(tld)
    if not bases:
        logger.debug("No RDAP server for .%s", tld)
        return _empty_unknown()

    base = bases[0].rstrip("/")
    url = f"{base}/domain/{domain}"
    from urllib.parse import urlparse
    rdap_host = urlparse(base).hostname or "rdap"
    # Per-host override falls through to the global `rdap` interval when the
    # host isn't listed. Added 2026-05-01 because GMO Registry
    # (rdap.gmoregistry.net, serving .shop + 46 other TLDs) rate-limits much
    # more aggressively than Verisign / PIR — see config.json _doc for the
    # probe data. Order: per-host override → global rdap → 0.2 final fallback.
    intervals = config.get("api_min_interval_seconds", {})
    min_interval = float(
        intervals.get("rdap_per_host", {}).get(rdap_host, intervals.get("rdap", 0.2))
    )

    try:
        response = request_with_429_backoff(
            lambda: requests.get(url, timeout=timeout),
            host=rdap_host,
            min_interval=min_interval,
        )
    except requests.RequestException as exc:
        logger.warning("RDAP availability check for %s failed: %s", domain, exc)
        _BREAKER.record_failure()
        return _empty_unknown()

    if response.status_code == 404:
        # Registry truly has no record. AVAILABLE.
        _BREAKER.record_success()
        return {
            "is_available": True,
            "rdap_status": [],
            "rdap_expiration": None,
            "previous_registrar": None,
            "rdap_http": 404,
        }

    if response.status_code == 429:
        # Surface the registry's Retry-After header (if any) so future
        # diagnostics don't need a manual probe to discover it.
        retry_after = response.headers.get("Retry-After") if hasattr(response, "headers") else None
        logger.warning(
            "RDAP persistent 429 for %s (Retry-After: %s)",
            domain, retry_after if retry_after is not None else "absent",
        )
        _BREAKER.record_failure()
        return {**_empty_unknown(), "rdap_http": 429}

    if response.status_code != 200:
        _BREAKER.record_failure()
        return {**_empty_unknown(), "rdap_http": response.status_code}

    try:
        record = response.json()
    except ValueError as exc:
        logger.warning("RDAP response for %s was not JSON: %s", domain, exc)
        _BREAKER.record_failure()
        return _empty_unknown()

    _BREAKER.record_success()

    if not isinstance(record, dict):
        # 200 with non-dict body — registry exists but we can't parse.
        # Still definitive: NOT available.
        return {
            "is_available": False,
            "rdap_status": [],
            "rdap_expiration": None,
            "previous_registrar": None,
            "rdap_http": 200,
        }

    status = record.get("status")
    if not isinstance(status, list):
        status = []

    return {
        "is_available": False,  # 200 = registry has a record = not available
        "rdap_status": [s for s in status if isinstance(s, str)],
        "rdap_expiration": _extract_expiration(record),
        "previous_registrar": _extract_registrar(record),
        "rdap_http": 200,
    }
