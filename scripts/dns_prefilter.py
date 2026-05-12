"""DNS-based pre-filter stage.

Runs AFTER the lexical filter and BEFORE the RDAP bucket assignment in
scripts/pipeline.py. For each candidate apex, issues a single NS-record
query via the system resolver (Quad9 since 2026-05-12); the answer
determines whether the candidate proceeds to RDAP.

Module location is `scripts/`, not `scripts/enrichment/`, because this is
a pipeline-stage helper, not an enrichment plugin. It does NOT conform to
the `enrich(domain, config) -> dict` contract used by ENRICHMENT_MODULES:

  - The lifecycle is different (runs once across all candidates with its
    own ThreadPoolExecutor sized from `dns_check.workers`, not inside the
    enrichment phase's max_concurrent_enrichments=1 sequential loop).
  - The decision is filter-shaped (reject vs proceed), not signal-shaped
    (merge fields onto the candidate for scoring).
  - It runs BEFORE the RDAP availability check, where enrichers run AFTER.

Three-state output (mirrors the DNSBL three-state contract — see
scripts/enrichment/_dnsbl.py):

    dns_available = True   — NXDOMAIN: registry has removed delegation,
                             candidate likely available → KEEP for RDAP
    dns_available = False  — NS records present: registry still delegates
                             this apex (in transfer, grace period, parked,
                             whatever) → REJECT here, RDAP wouldn't have
                             approved it either
    dns_available = None   — lookup failed (timeout, transport error, the
                             NoAnswer / name-exists-but-no-NS edge case,
                             or any other dns.exception) → KEEP for RDAP
                             (fail-open; epistemic honesty)

Accuracy-preservation property: a domain that is genuinely RDAP-available
(HTTP 404, registry returned no record) has had its NS records removed at
the registry, so this stage's `False` rejection only catches candidates
RDAP would have rejected too. Net effect on the daily output: same final
list, much less RDAP load.

Why this matters operationally: the RDAP throttle budget is the current
bottleneck on TLD scale-up. Adding .com (~10× the apex count of the next-
largest TLD) under today's per-host throttles would either need a much
larger time budget (slow daily runs) or accept losing volume to
skipped_budget. Pre-filtering 80-95% of candidates with a free, fast DNS
NS query removes the pressure.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import dns.exception
import dns.resolver

from scripts.enrichment._circuit_breaker import GLOBAL_HOST_THROTTLE

logger = logging.getLogger(__name__)

# Throttle bucket key — logical name, NOT a real host. The system resolver
# (Quad9, configured in /etc/systemd/resolved.conf on 2026-05-12) is where
# queries actually land; we pace aggregate queries against it as a single
# bucket so worker count and throttle interact cleanly.
_THROTTLE_HOST = "dns_prefilter"


def check_dns_availability(
    apex_domain: str,
    *,
    timeout_seconds: float = 3.0,
) -> dict:
    """Query NS records for `apex_domain` and classify the response.

    Returns a dict with `dns_available` (True / False / None) and
    `ns_records` (list of NS hostnames, empty unless dns_available is
    False). Never raises — every dnspython exception is mapped onto the
    three-state contract.

    Trailing dots on NS targets are stripped so downstream consumers
    don't have to handle the DNS-wire-format detail.
    """
    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout_seconds
    resolver.lifetime = timeout_seconds

    try:
        answer = resolver.resolve(apex_domain, "NS")
    except dns.resolver.NXDOMAIN:
        return {"dns_available": True, "ns_records": []}
    except dns.resolver.NoAnswer:
        # Name exists in DNS but has no NS records at the queried level —
        # an edge case (e.g. resolver chasing a CNAME) where epistemic
        # honesty matters. Fail open to RDAP rather than guessing.
        logger.debug("DNS NoAnswer for %s — name exists but no NS records", apex_domain)
        return {"dns_available": None, "ns_records": []}
    except dns.resolver.NoNameservers:
        # No upstream resolver could be reached at all. Fail open.
        logger.debug("DNS NoNameservers for %s", apex_domain)
        return {"dns_available": None, "ns_records": []}
    except dns.exception.Timeout:
        logger.debug(
            "DNS NS lookup timed out for %s after %.1fs", apex_domain, timeout_seconds,
        )
        return {"dns_available": None, "ns_records": []}
    except dns.exception.DNSException as exc:
        # Catch-all for the remaining dnspython exception hierarchy
        # (FormError, BadResponse, etc.). Same fail-open posture.
        logger.debug("DNS NS lookup error for %s: %s", apex_domain, exc)
        return {"dns_available": None, "ns_records": []}

    ns_records: list[str] = []
    for rdata in answer:
        try:
            ns_records.append(str(rdata.target).rstrip("."))
        except Exception:  # pragma: no cover — defensive against malformed records
            continue

    if not ns_records:
        # Empty answer with no triggering exception is anomalous — refuse
        # to interpret as either available or registered. Fail open.
        return {"dns_available": None, "ns_records": []}

    return {"dns_available": False, "ns_records": ns_records}


def filter_candidates(candidates: list[dict], config: dict) -> list[dict]:
    """Pipeline stage: run check_dns_availability concurrently across all
    candidates, annotate each one in place, return the subset that should
    proceed to RDAP (dns_available is True OR None).

    Mutates each candidate dict: adds `dns_available` and `ns_records`
    fields. Candidates that the stage rejects keep the fields too (useful
    for debug exports), but aren't included in the returned list.

    Reads pacing knobs from `config["dns_check"]`:
        enabled         — if False, skip the whole stage (pass through unchanged)
        workers         — ThreadPoolExecutor size
        timeout_seconds — per-call dnspython timeout AND lifetime
        throttle_seconds — minimum interval between requests (aggregate
                           across all workers, via GLOBAL_HOST_THROTTLE);
                           0.0 disables throttling
    """
    cfg = config.get("dns_check", {}) or {}
    if not cfg.get("enabled", True):
        logger.info("DNS pre-filter disabled (dns_check.enabled=false); skipping")
        return list(candidates)

    if not candidates:
        logger.info("DNS pre-filter: no candidates to check")
        return []

    workers = max(1, int(cfg.get("workers", 20)))
    timeout = float(cfg.get("timeout_seconds", 3.0))
    throttle = float(cfg.get("throttle_seconds", 0.0))

    def _check_one(cand: dict) -> dict:
        if throttle > 0:
            GLOBAL_HOST_THROTTLE.acquire(_THROTTLE_HOST, throttle)
        name = cand.get("name", "")
        try:
            result = check_dns_availability(name, timeout_seconds=timeout)
        except Exception as exc:  # pragma: no cover — check_dns_availability never raises
            logger.warning("DNS pre-filter raised for %s: %s", name, exc)
            result = {"dns_available": None, "ns_records": []}
        cand.update(result)
        return cand

    total = len(candidates)
    logger.info(
        "DNS pre-filter starting: %d candidates, %d workers, %.1fs timeout, %.2fs throttle",
        total, workers, timeout, throttle,
    )
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dns-prefilter") as pool:
        # `map` blocks until all done; we don't need the return values
        # since _check_one mutates in place. Exhaust the iterator to
        # surface any worker exceptions (defensive — _check_one swallows
        # all of them, but a misbehaving thread pool would).
        for _ in pool.map(_check_one, candidates):
            pass
    elapsed = time.monotonic() - start

    kept = [c for c in candidates if c.get("dns_available") is not False]
    unknown = sum(1 for c in candidates if c.get("dns_available") is None)
    rejected = total - len(kept)
    pct_rejected = (100.0 * rejected / total) if total else 0.0

    logger.info(
        "DNS pre-filter: %d candidates → %d kept (%d%% rejected), "
        "%d unknown (proceed to RDAP); elapsed %.1fs",
        total, len(kept), int(round(pct_rejected)), unknown, elapsed,
    )
    return kept
