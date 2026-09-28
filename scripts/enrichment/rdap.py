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
import socket
from functools import lru_cache
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)

from scripts.enrichment._circuit_breaker import (
    CircuitBreaker,
    GLOBAL_HOST_COOLDOWN,
    GLOBAL_HOST_STOP,
    request_with_429_backoff,
)

logger = logging.getLogger(__name__)

_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
_BREAKER = CircuitBreaker("rdap")

# Identify ourselves on every outbound RDAP request. The default
# `python-requests/X.Y.Z` UA is a heavily-flagged automated-client signal in
# WAFs (Cloudflare Bot Management in particular), and the 2026-05-05
# identitydigital 24h ban happened at a rate 4× under their documented WHOIS
# limit — fingerprint-based detection is a plausible secondary contributor
# that throttle calibration alone cannot address. Named, contactable UA is
# industry practice for legitimate scrapers.
USER_AGENT = (
    "DomainSifter/1.0 (+https://domainsifter.com; contact: hello@domainsifter.com)"
)
_DEFAULT_HEADERS = {"User-Agent": USER_AGENT}


# --- IPv6-only transport (added 2026-09-28) --------------------------------
#
# Some RDAP hosts must be reached over IPv6 ONLY. rdap.gmoregistry.net (.shop)
# has blocked our IPv4 address since May 2026 (instant 429 at the Cloudflare
# edge) while IPv6 still gets real answers. Any IPv4 attempt to such a host is
# not just useless but harmful: GMO treats requests inside its 60s post-429
# window as grounds to extend the block. So for hosts listed in
# config.json:rdap_force_ipv6_hosts we resolve AF_INET6 addresses only and
# NEVER fall back to IPv4 — if IPv6 fails, the domain is simply unknown.
#
# This is done per-connection (custom urllib3 connection class mounted on a
# per-request Session), NOT via urllib3.util.connection.allowed_gai_family,
# which is process-wide and would change every other source's behaviour.


def _create_ipv6_connection(
    address: tuple[str, int],
    timeout: Any,
    source_address: tuple[str, int] | None = None,
    socket_options: Any = None,
) -> socket.socket:
    """socket.create_connection restricted to AF_INET6 results.

    Mirrors urllib3.util.connection.create_connection, minus the address
    family negotiation: getaddrinfo is asked for AF_INET6 only, so no IPv4
    address can ever be tried. Raises socket.gaierror when the host has no
    AAAA record, OSError when every IPv6 address fails to connect.
    """
    host, port = address
    host = host.strip("[]")
    err: OSError | None = None
    for family, socktype, proto, _canon, sockaddr in socket.getaddrinfo(
        host, port, socket.AF_INET6, socket.SOCK_STREAM
    ):
        if family != socket.AF_INET6:  # defensive: never touch IPv4
            continue
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            for opt in socket_options or ():
                sock.setsockopt(*opt)
            if isinstance(timeout, (int, float)) or timeout is None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            err = exc
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise socket.gaierror(f"no IPv6 address for {host}")


class _IPv6OnlyConnectionMixin:
    """Overrides urllib3's _new_conn to connect via AF_INET6 only, keeping
    urllib3's exception mapping so requests raises its usual
    ConnectionError / ConnectTimeout (caught by check_availability)."""

    def _new_conn(self) -> socket.socket:
        try:
            return _create_ipv6_connection(
                (self._dns_host, self.port),
                self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc
        except TimeoutError as exc:
            raise ConnectTimeoutError(
                self,
                f"Connection to {self.host} timed out (IPv6-only, "
                f"connect timeout={self.timeout})",
            ) from exc
        except OSError as exc:
            raise NewConnectionError(
                self, f"Failed to establish a new IPv6-only connection: {exc}"
            ) from exc


class _IPv6OnlyHTTPConnection(_IPv6OnlyConnectionMixin, HTTPConnection):
    pass


class _IPv6OnlyHTTPSConnection(_IPv6OnlyConnectionMixin, HTTPSConnection):
    pass


class _IPv6OnlyHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _IPv6OnlyHTTPConnection


class _IPv6OnlyHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _IPv6OnlyHTTPSConnection


class IPv6OnlyAdapter(HTTPAdapter):
    """requests transport adapter whose connections use IPv6 only."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _IPv6OnlyHTTPConnectionPool,
            "https": _IPv6OnlyHTTPSConnectionPool,
        }


def _force_ipv6_hosts(config: dict) -> frozenset[str]:
    """Hosts from config.json:rdap_force_ipv6_hosts.hosts (lower-cased)."""
    block = config.get("rdap_force_ipv6_hosts", {}) or {}
    hosts = block.get("hosts", []) if isinstance(block, dict) else []
    return frozenset(h.lower() for h in hosts if isinstance(h, str))


def _rdap_get(url: str, rdap_host: str, timeout: int, config: dict) -> requests.Response:
    """GET an RDAP URL, over IPv6 only when `rdap_host` is configured so.

    Every other host keeps default requests behaviour (requests.get). A fresh
    Session per call keeps this free of module-level state and thread-safe;
    requests.get does the same internally.
    """
    if rdap_host.lower() not in _force_ipv6_hosts(config):
        return requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
    with requests.Session() as session:
        adapter = IPv6OnlyAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)


@lru_cache(maxsize=8)
def _fetch_bootstrap(url: str, timeout: int) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Fetch and parse the IANA RDAP bootstrap. Returns a tuple-of-tuples so the
    return value is hashable (lru_cache requirement). Returns None on failure;
    failures are NOT cached (caller's next attempt may succeed)."""
    try:
        response = requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
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
            lambda: _rdap_get(url, rdap_host, timeout, config),
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


def _retry_after_floor_seconds(rdap_host: str, config: dict) -> float:
    """Minimum cooldown to honor after a 429 from `rdap_host`, in seconds.

    Lookup order: rdap_429_backoff_floor_seconds.per_host[host]
    → rdap_429_backoff_floor_seconds.default → 5.0.

    GMO Registry (rdap.gmoregistry.net) is configured at 60 — its /help page
    documents "do not access any more for at least a minute" after a 429 and
    threatens temporary IP blocking otherwise. GMO's own Retry-After header
    is a useless "0", so this floor is what actually binds. See config.json.
    """
    block = config.get("rdap_429_backoff_floor_seconds", {}) or {}
    per_host = block.get("per_host", {}) or {}
    return float(per_host.get(rdap_host, block.get("default", 5.0)))


def resolve_rdap_host(domain: str, config: dict) -> str | None:
    """Return the RDAP server hostname that would be queried for `domain`.

    Used by the pipeline orchestrator to partition candidates into per-host
    buckets BEFORE calling `check_availability`. Walks the same bootstrap
    + urlparse path `check_availability` uses internally so the partitioning
    is guaranteed consistent with the actual lookup.

    Returns None when:
      - the IANA bootstrap is unreachable (callers bucket under "_unknown")
      - the TLD has no RDAP server in the bootstrap
      - the domain has no TLD label
      - the bootstrap URL has no parseable host

    None-bucketed candidates still go through `check_availability` normally
    — that function handles the same cases and returns is_available=None.
    The pipeline rejects them like any other unknown.
    """
    timeout = config.get("request_timeout_seconds", 10)
    bootstrap = _load_bootstrap(config, timeout)
    if bootstrap is None or not bootstrap:
        return None
    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    if not tld:
        return None
    bases = bootstrap.get(tld)
    if not bases:
        return None
    base = bases[0].rstrip("/")
    from urllib.parse import urlparse
    return urlparse(base).hostname or None


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

    # Run-scoped STOP (added 2026-06-02). If this host previously returned a
    # 429 or a 403, it is stopped for the rest of the run — skip every
    # subsequent domain WITHOUT a request. This is the maximum rate-decrease
    # and the surest defense against escalating a 429 into a 403 IP-block.
    # `rdap_skipped_reason` lets the orchestrator count what was left
    # unchecked when a host backed off (it is NOT part of the JSON contract;
    # output.py filters to CONTRACT_FIELDS). INFO not WARNING: this is the
    # safety rule working as intended. See _circuit_breaker.HostStop.
    if GLOBAL_HOST_STOP.is_stopped(rdap_host):
        logger.info(
            "RDAP host %s stopped for this run (prior 429/403); skipping %s "
            "without a request",
            rdap_host, domain,
        )
        return {**_empty_unknown(), "rdap_skipped_reason": "host_stopped"}

    # Per-host 429 cooldown (added 2026-05-22). If this host returned a 429
    # within its cooldown window, skip the HTTP call entirely — re-querying
    # would just 429 again, and registries that escalate (GMO Registry's
    # /help page documents temporary IP blocking) treat continued access
    # during the window as grounds to extend the block. INFO not WARNING:
    # this is the backoff design working as intended, not an error.
    cooldown_remaining = GLOBAL_HOST_COOLDOWN.seconds_remaining(rdap_host)
    if cooldown_remaining > 0:
        logger.info(
            "RDAP host %s in 429 cooldown (%.0fs left); skipping %s without a request",
            rdap_host, cooldown_remaining, domain,
        )
        return _empty_unknown()

    # Per-host override falls through to the global `rdap` interval when the
    # host isn't listed. Added 2026-05-01 because GMO Registry
    # (rdap.gmoregistry.net, serving .shop + 46 other TLDs) rate-limits much
    # more aggressively than Verisign / PIR — see config.json _doc for the
    # probe data. Order: per-host override → global rdap → 0.2 final fallback.
    intervals = config.get("api_min_interval_seconds", {})
    min_interval = float(
        intervals.get("rdap_per_host", {}).get(rdap_host, intervals.get("rdap", 0.2))
    )
    # Per-host cumulative 429 strike cap (default 3). Below the cap a 429
    # honors Retry-After and the host resumes; at the cap the host stops for
    # the run. See config.json:rdap_429_strike_limit.
    strike_limit = int(config.get("rdap_429_strike_limit", 3))

    try:
        response = request_with_429_backoff(
            lambda: _rdap_get(url, rdap_host, timeout, config),
            host=rdap_host,
            min_interval=min_interval,
            retry_after_floor=_retry_after_floor_seconds(rdap_host, config),
            strike_limit=strike_limit,
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

    if response.status_code == 403:
        # 403 FORBIDDEN is the CATASTROPHIC signal — not a rate limit but an
        # outright block (typically IP-level at the registry's edge, the GMO
        # outcome). ALARM HARD (logging.critical surfaces in the daily report)
        # and stop the host immediately: we are already blocked, so every
        # further request can only deepen the block. This must never be
        # silently swallowed.
        GLOBAL_HOST_STOP.stop(rdap_host, reason="403")
        logger.critical(
            "RDAP 403 FORBIDDEN from %s on %s — CATASTROPHIC IP-BLOCK signal "
            "(NOT a rate limit). Stopping this host for the run. INVESTIGATE "
            "IMMEDIATELY: the registry has likely blocked our egress IP.",
            rdap_host, domain,
        )
        _BREAKER.record_failure()
        return {**_empty_unknown(), "rdap_http": 403}

    if response.status_code == 429:
        # request_with_429_backoff (RETRY-AFTER MODE) already honored the
        # per-host cooldown, recorded a strike, logged the strike N/limit at
        # WARNING, and — only if the strike limit was reached — armed
        # GLOBAL_HOST_STOP. Below the limit the host resumes after its
        # cooldown window. No extra warning here. Record the breaker failure
        # and treat this domain as unknown.
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
