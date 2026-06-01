"""DomainSifter daily pipeline orchestrator.

Order of operations:
    1. validate_env()                     — fail loud on missing secrets
    2. authenticate(CZDS)                 — fatal if it fails
    3. list_zone_links → filter to approved TLDs
    4. for each TLD:
         a. download zone to a tempdir
         b. parse_zone → today's set
         c. diff vs yesterday's R2 snapshot → drops
         d. commit_today (overwrite snapshot for tomorrow's run)
         e. delete the temp zone file
    5. structural filter        (filter.filter_candidates_structural)
                                — punycode, length, all-numeric, keyword
    6. lexical filter           (lexical_filter.filter_candidates)
                                — digit/vowel/entropy/repeats/pronounceability
    7. evaluation safety cap    (global_cap, in _bucket_and_cap_for_availability)
                                — when lexical survivors exceed global_cap,
                                  rank by trigram-match-count desc + apex-
                                  length asc and keep the top global_cap.
                                  Per-host buckets then cap further by
                                  RDAP runtime budget. DNS pre-filter was
                                  removed 2026-05-17 — it cost 20-25 min
                                  per run and rejected ~0% of candidates
                                  across May 15-17 audit.
    8. validate_availability    (RDAP per-candidate; the AUTHORITATIVE check)
                                — only HTTP 404 is "available"; everything
                                  else (owned, redemption, hold, transport
                                  failure) is rejected. MOVED HERE 2026-04-30
                                  from the post-score position because:
                                    a) availability is cheap per call (one
                                       RDAP query) vs enrichment (six API
                                       calls per candidate). Doing it first
                                       eliminates 95%+ of work upstream.
                                    b) running enrichment on confirmed-
                                       available domains only means we can
                                       drop concurrency to 1 and pace much
                                       more politely without blowing the
                                       time budget.
    9. enrich each survivor sequentially within a wall-clock budget
       (max_workers=1 by default, budget from enrichment_time_budget_seconds).
       Typical post-availability set is 5-50 candidates; pacing room is now
       generous.
   10. post-enrichment filter   (strict_spam_check=True)
   11. score + sort             — null components excluded from normalization
   12. publication cap          (max_candidates_for_publication, in build_payload)
                                — CEILING, not quota; never pad with weak
   13. write_output → src/data/daily-domains.json

Logging: each module gets its own logger; root config writes INFO+ to stdout
so GitHub Actions surfaces everything in the run log.

Per CLAUDE.md rule #17: auth/config errors crash; per-zone or per-domain
failures log and continue.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Callable

from scripts import (
    carryover,
    czds_client,
    debug_export,
    diff,
    env_check,
    filter as filter_mod,
    lexical_filter,
    output,
    phase2_ranker,
    score,
    snapshot_classifier,
    zone_parser,
)

logger = logging.getLogger("scripts.pipeline")

ENRICHMENT_MODULES = (
    "wayback",
    "open_page_rank",
    "spam_check",
    "surbl",
    "spamhaus",
    "crtsh",
    # `cc_backlinks` joined 2026-05-14 (wire-in commit). Returns
    # `cc_source_domain_count` (int) when the apex is in the latest CC
    # release's graph; empty dict otherwise — which the score formula
    # treats as null and excludes from the average. SQLite for
    # `cc_backlinks.latest_release` is fetched from R2 once on first call
    # and cached at `~/.cache/domainsifter/cc/` for the rest of the run.
    "cc_backlinks",
    # `rdap` is intentionally NOT in this list any more. It's now run as a
    # dedicated post-score availability check (validate_availability), where
    # HTTP 404 is the only signal that proves "actually registerable." The
    # previous behaviour — running rdap during enrichment for `previous_registrar`
    # display only — let owned domains slip through to publication.
)


def _load_enrichers() -> list[tuple[str, Callable[[str, dict], dict]]]:
    enrichers: list[tuple[str, Callable[[str, dict], dict]]] = []
    for name in ENRICHMENT_MODULES:
        mod = import_module(f"scripts.enrichment.{name}")
        enrichers.append((name, mod.enrich))
    return enrichers


def load_config(config_path: str | Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _approved_tlds(config: dict) -> set[str]:
    return {t.lower() for t in config.get("tlds", {}).get("approved", [])}


def _filename_to_tld(url: str) -> str:
    """CZDS download URLs end with `.../<tld>.zone`. Extract the TLD."""
    tail = url.rsplit("/", 1)[-1]
    return tail.split(".", 1)[0].lower()


def collect_drops(
    config: dict,
    access_token: str,
    today: date,
    *,
    r2_client=None,
    r2_bucket: str | None = None,
    carryover_candidates: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Download every approved-TLD zone, diff vs yesterday's R2 snapshot,
    revalidate carryover entries against today's zone, overwrite the
    snapshot for tomorrow, and return (today_drops, retained_carryover).

    Yesterday's snapshots live in Cloudflare R2 (see scripts/diff.py header
    for why). The R2 client + bucket name are resolved once and reused
    across all TLDs; tests inject mocks via `r2_client=` / `r2_bucket=`.

    Carryover validation happens INSIDE the per-TLD loop because today_set
    can be 10-50M apex names per TLD; holding all 13 in memory at once is
    a non-starter on the 7 GB GHA runner. We validate the TLD's carryover
    while its today_set is still in scope, then let it go.

    A TLD whose zone download or parse fails is treated as "unknown" for
    carryover: those entries pass through with last_validated_date
    UNCHANGED (we don't drop them just because we couldn't reach the
    registry — they age out naturally if the failure persists 14 days).
    """
    api_base = config.get("api_endpoints", {}).get("czds_api_base")
    download_timeout = config.get("download_timeout_seconds", 120)
    approved = _approved_tlds(config)
    s3 = r2_client if r2_client is not None else diff._r2_client()
    bucket = r2_bucket if r2_bucket is not None else diff._bucket()

    # Index carryover by TLD so we can validate per-TLD while today_set is
    # in memory. Entries for TLDs not in this run's approved list (e.g. an
    # operator removed a TLD) pass through unvalidated — same fail-open
    # posture as a TLD whose zone download failed.
    carryover_by_tld: dict[str, list[dict]] = {}
    if carryover_candidates:
        for entry in carryover_candidates:
            carryover_by_tld.setdefault(entry.get("tld", ""), []).append(entry)
    validated_tlds: set[str] = set()

    all_links = czds_client.list_zone_links(access_token, api_base) if api_base else czds_client.list_zone_links(access_token)
    targeted = [u for u in all_links if _filename_to_tld(u) in approved]
    logger.info("CZDS approved %d zones; %d match our TLD list", len(all_links), len(targeted))

    drops_total: list[dict] = []
    retained_carryover: list[dict] = []
    dropped_date_str = today.isoformat()
    for url in targeted:
        tld = _filename_to_tld(url)
        with tempfile.TemporaryDirectory(prefix="czds-") as tmpdir:
            zone_path = Path(tmpdir) / f"{tld}.zone.gz"
            try:
                czds_client.download_zone(url, access_token, str(zone_path), timeout=download_timeout)
            except czds_client.CzdsApiError as exc:
                logger.warning(".%s zone download failed, skipping: %s", tld, exc)
                continue
            try:
                today_set = zone_parser.parse_zone(zone_path)
            except OSError as exc:
                logger.warning(".%s zone parse failed, skipping: %s", tld, exc)
                continue

        yesterday_set = diff.load_yesterday(tld, client=s3, bucket=bucket)
        drops = diff.compute_drops(yesterday_set, today_set)
        diff.commit_today(tld, today_set, client=s3, bucket=bucket)
        logger.info(".%s: %d in zone today, %d dropped since yesterday", tld, len(today_set), len(drops))

        for name in drops:
            drops_total.append({"name": name, "tld": tld, "dropped_date": dropped_date_str})

        # Validate this TLD's carryover against today_set while it's still
        # in scope. After this iteration today_set is collected.
        tld_carryover = carryover_by_tld.get(tld, [])
        if tld_carryover:
            kept, registered = carryover.validate_against_zone(tld_carryover, today_set, today)
            retained_carryover.extend(kept)
            logger.info(
                ".%s carryover: %d kept, %d registered (in today's zone)",
                tld, len(kept), registered,
            )
            validated_tlds.add(tld)

    # Carryover for TLDs we couldn't validate this run (zone download/parse
    # failed, or TLD not in approved list any more): pass through with
    # last_validated_date unchanged. They'll age out naturally if the
    # failure persists.
    for tld, entries in carryover_by_tld.items():
        if tld in validated_tlds:
            continue
        retained_carryover.extend(entries)
        if entries:
            logger.warning(
                ".%s carryover: %d entries passed through unvalidated (TLD zone unavailable)",
                tld, len(entries),
            )

    logger.info(
        "Collected %d total drops across %d TLDs; %d carryover entries retained",
        len(drops_total), len(targeted), len(retained_carryover),
    )
    return drops_total, retained_carryover


def _enrich_one(
    candidate: dict,
    config: dict,
    enrichers: list[tuple[str, Callable[[str, dict], dict]]],
) -> dict:
    """Apply all enrichers to one candidate and merge results in-place.

    Per-source exceptions are logged and treated as empty dict EXCEPT for
    `SpamCheckConfigError`, which is fatal — it means the operator's secrets
    are misconfigured and we must not produce a daily list with degraded
    malware filtering. We re-raise it so the orchestrator aborts the run.
    """
    from scripts.enrichment.spam_check import SpamCheckConfigError

    for name, fn in enrichers:
        try:
            result = fn(candidate["name"], config)
        except SpamCheckConfigError:
            raise
        except Exception as exc:  # pragma: no cover — defence-in-depth
            logger.warning("Enricher %s raised on %s: %s", name, candidate["name"], exc)
            result = {}
        if isinstance(result, dict):
            candidate.update(result)
    return candidate


def enrich_all(candidates: list[dict], config: dict) -> list[dict]:
    """Enrich every candidate concurrently, capped by a wall-clock budget.

    Submission strategy:
      - Fill the pool up to max_workers.
      - Each time a future completes, submit the next queued candidate IF
        the wall-clock budget hasn't expired.
      - Once expired: stop submitting new ones, give in-flight workers up
        to `grace` seconds to finish, then stop.

    The point: enrichment that times out under rate limits should still
    publish whatever it managed to enrich, not lose the entire run.
    Per project guidance: 200 properly-enriched > 0 because of timeout.
    """
    if not candidates:
        return list(candidates)

    max_workers = max(1, int(config.get("max_concurrent_enrichments", 10)))
    budget = float(config.get("enrichment_time_budget_seconds", 2100))
    grace = 60.0

    enrichers = _load_enrichers()
    logger.info(
        "Enriching %d candidates: %d workers, %.0fs budget + %.0fs grace",
        len(candidates), max_workers, budget, grace,
    )

    queue = list(candidates)
    enriched: list[dict] = []
    spam_check_error: BaseException | None = None
    start = time.monotonic()
    deadline = start + budget
    grace_deadline = deadline + grace

    from scripts.enrichment.spam_check import SpamCheckConfigError

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        in_flight: set = set()

        def _submit_one() -> None:
            cand = queue.pop(0)
            in_flight.add(pool.submit(_enrich_one, cand, config, enrichers))

        # Prime the pool.
        while queue and len(in_flight) < max_workers:
            _submit_one()

        budget_warned = False
        while in_flight:
            now = time.monotonic()
            if now >= grace_deadline:
                logger.warning(
                    "Enrichment grace period exhausted; abandoning %d in-flight",
                    len(in_flight),
                )
                for f in in_flight:
                    f.cancel()
                break

            timeout = max(0.1, grace_deadline - now)
            done, in_flight = wait(in_flight, timeout=timeout, return_when=FIRST_COMPLETED)

            for fut in done:
                try:
                    enriched.append(fut.result())
                except SpamCheckConfigError as exc:
                    spam_check_error = exc
                    # Cancel pending work; we'll re-raise after teardown.
                    for f in in_flight:
                        f.cancel()
                    in_flight = set()
                    queue.clear()
                    break

            if spam_check_error is not None:
                break

            # Refill only while budget remains.
            if time.monotonic() < deadline:
                while queue and len(in_flight) < max_workers:
                    _submit_one()
            elif not budget_warned and (queue or in_flight):
                logger.warning(
                    "Enrichment time budget (%.0fs) exhausted at %d/%d enriched; "
                    "%d skipped, %d in-flight (grace period now active)",
                    budget, len(enriched), len(candidates), len(queue), len(in_flight),
                )
                budget_warned = True

    if spam_check_error is not None:
        raise spam_check_error

    skipped = len(candidates) - len(enriched)
    if skipped:
        logger.info(
            "Enrichment summary: %d enriched, %d skipped (budget/grace), elapsed %.1fs",
            len(enriched), skipped, time.monotonic() - start,
        )
    else:
        logger.info(
            "Enrichment summary: %d enriched (all candidates), elapsed %.1fs",
            len(enriched), time.monotonic() - start,
        )
    return enriched


def _check_availability_concurrent(
    candidates: list[dict],
    config: dict,
    deadline: float,
    *,
    per_host_stats: dict[str, dict] | None = None,
) -> dict[str, int]:
    """Run RDAP availability checks in per-host buckets, all buckets in
    parallel.

    Architecture:
      - Group candidates by their resolved RDAP host (one bucket per host).
        Unknown / unresolvable TLDs share an "_unknown" bucket; they still
        run through `check_availability`, which returns is_available=None.
      - Each bucket gets its own ThreadPoolExecutor sized via
        `rdap_concurrency.per_host[host]` → `rdap_concurrency.default_workers_per_host` → 1.
      - All bucket pools run simultaneously. Within a bucket, workers
        serialise on the existing thread-safe HostThrottle, so per-host
        request rate never exceeds the configured min_interval regardless
        of worker count.

    Mutates each candidate dict in place: merges check_availability result
    + sets availability_verified_at. Candidates that hit the deadline get
    is_available=None and no availability_verified_at — same shape as the
    previous sequential implementation.

    Returns counts dict {available, not_available, unknown, skipped_budget};
    the caller derives `kept` by filtering the original candidate list.
    """
    from datetime import datetime, timezone

    from scripts.enrichment import rdap

    cc = config.get("rdap_concurrency", {}) or {}
    default_workers = max(1, int(cc.get("default_workers_per_host", 1) or 1))
    per_host_workers = cc.get("per_host", {}) or {}

    # Bucket candidates by host BEFORE submission so we can size each pool
    # correctly. Unknown / unresolved hosts share "_unknown" — they're still
    # processed (check_availability handles the None-host case internally).
    buckets: dict[str, list[dict]] = {}
    for cand in candidates:
        host = rdap.resolve_rdap_host(cand.get("name", ""), config) or "_unknown"
        buckets.setdefault(host, []).append(cand)

    counts = {"available": 0, "not_available": 0, "unknown": 0, "skipped_budget": 0}
    counts_lock = threading.Lock()

    def _process_one(cand: dict) -> None:
        # Deadline check is intra-worker so each candidate sees the latest
        # clock — different from a one-shot pre-check, which would skip the
        # whole bucket if the deadline elapsed during submission.
        if time.monotonic() >= deadline:
            cand["is_available"] = None
            with counts_lock:
                counts["skipped_budget"] += 1
            return
        result = rdap.check_availability(cand.get("name", ""), config)
        cand.update(result)
        cand["availability_verified_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        v = result.get("is_available")
        with counts_lock:
            if v is True:
                counts["available"] += 1
            elif v is False:
                counts["not_available"] += 1
            else:
                counts["unknown"] += 1

    # Submit every bucket's work to its own pool first — pools are alive and
    # threads are scheduled the moment we call submit(), so all buckets run
    # concurrently. The wait loop afterwards just collects completion.
    pools_meta: list[tuple[str, ThreadPoolExecutor, list, float, list[dict]]] = []
    for host, group in buckets.items():
        workers = max(1, int(per_host_workers.get(host, default_workers) or default_workers))
        pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"rdap-{host}",
        )
        # When the orchestrator pre-computed bucket caps, surface the
        # original (pre-cap) bucket size in the log so production runs show
        # whether any host actually hit its cap.
        host_stats = (per_host_stats or {}).get(host)
        if host_stats and host_stats.get("before", 0) > host_stats.get("after", 0):
            logger.info(
                "RDAP host bucket [%s]: %d candidates (capped from %d), %d workers",
                host, len(group), host_stats["before"], workers,
            )
        else:
            logger.info(
                "RDAP host bucket [%s]: %d candidates, %d workers",
                host, len(group), workers,
            )
        host_start = time.monotonic()
        futures = [pool.submit(_process_one, c) for c in group]
        pools_meta.append((host, pool, futures, host_start, group))

    # Drain. Each pool is independent — earlier pools may finish while we
    # still wait on later ones; no head-of-line blocking across hosts.
    for host, pool, futures, host_start, group in pools_meta:
        for f in futures:
            try:
                f.result()
            except Exception as exc:  # pragma: no cover — check_availability is contracted not to raise
                logger.warning("RDAP availability check raised on host %s: %s", host, exc)
        pool.shutdown(wait=True)
        elapsed = time.monotonic() - host_start
        host_avail = sum(1 for c in group if c.get("is_available") is True)
        host_unavail = sum(1 for c in group if c.get("is_available") is False)
        host_unknown = len(group) - host_avail - host_unavail
        logger.info(
            "RDAP host bucket [%s] done in %.1fs: %d available, %d not_available, %d unknown",
            host, elapsed, host_avail, host_unavail, host_unknown,
        )

    return counts


def validate_availability(
    candidates: list[dict],
    config: dict,
    *,
    per_host_stats: dict[str, dict] | None = None,
) -> list[dict]:
    """RDAP-validate each candidate; keep only the ones registry says are
    actually available (HTTP 404).

    Walks the input in score order (caller is responsible for sorting first).
    For each candidate, calls `rdap.check_availability(...)` and merges the
    returned fields onto the candidate dict so downstream stages can emit
    them in the JSON payload.

    Decision per candidate:
        is_available = True  → KEEP
        is_available = False → REJECT (owned / redemption / on-hold)
        is_available = None  → REJECT (transport failure / breaker / etc.)

    Why None defaults to REJECT:
        Better to under-publish than to publish a domain we can't prove is
        registerable. The False-vs-None distinction stays in the logs as
        diagnostic signal: many None values means our RDAP path is degraded
        (rate limits, bootstrap unreachable); many False values means the
        zone-diff signal itself is just noisy.

    Wall-clock budget: stops calling RDAP after `availability_budget_seconds`
    (default 600). Untouched candidates get is_available=None and are
    rejected. This is the same fail-closed posture used elsewhere.

    Sets `availability_verified_at` (ISO 8601 Z) on every candidate that
    actually had an RDAP attempt — useful for downstream debugging and
    surfacing "checked at HH:MM" in the JSON payload.

    Concurrency: candidates run through `_check_availability_concurrent`,
    which buckets by RDAP host and runs each bucket in its own
    ThreadPoolExecutor (all buckets in parallel). Per-host request rate is
    unchanged — the existing HostThrottle serialises within a bucket. See
    `scripts/config.json` → `rdap_concurrency`.
    """
    if not candidates:
        return list(candidates)

    budget = float(config.get("availability_budget_seconds", 600))
    deadline = time.monotonic() + budget

    counts = _check_availability_concurrent(
        candidates, config, deadline, per_host_stats=per_host_stats,
    )

    # Preserve input order in the kept list (callers sort upstream by score).
    kept = [c for c in candidates if c.get("is_available") is True]

    logger.info(
        "Availability check: %d available, %d not available, %d unknown, %d skipped (budget) — kept %d / %d",
        counts["available"],
        counts["not_available"],
        counts["unknown"],
        counts["skipped_budget"],
        len(kept),
        len(candidates),
    )

    # If >50% of attempts came back unknown, RDAP itself is degraded — this
    # is a CRITICAL signal because availability is now the gate that decides
    # what gets enriched at all. The pipeline continues (better fewer than
    # wrong) but the operator should investigate.
    attempts = counts["available"] + counts["not_available"] + counts["unknown"]
    if attempts > 0 and counts["unknown"] / attempts > 0.5:
        logger.critical(
            "RDAP availability degraded: %d/%d (%.0f%%) returned unknown — "
            "today's published list will be much smaller than usual",
            counts["unknown"], attempts, 100.0 * counts["unknown"] / attempts,
        )

    return kept


def _resolve_host_throttle(host: str, config: dict) -> float:
    """Effective per-request throttle for a given RDAP host. Order:
    rdap_per_host[host] → global rdap → 0.4 final fallback. Mirrors the
    same lookup chain rdap.check_availability uses internally so cap math
    matches actual runtime pacing."""
    intervals = config.get("api_min_interval_seconds", {}) or {}
    return float(
        intervals.get("rdap_per_host", {}).get(host, intervals.get("rdap", 0.4))
    )


def _resolve_host_workers(host: str, config: dict) -> int:
    """Effective per-host worker count. Order: rdap_concurrency.per_host[host]
    → rdap_concurrency.default_workers_per_host → 1. Same lookup chain
    _check_availability_concurrent uses to size each ThreadPoolExecutor."""
    cc = config.get("rdap_concurrency", {}) or {}
    default = max(1, int(cc.get("default_workers_per_host", 1) or 1))
    per_host = cc.get("per_host", {}) or {}
    return max(1, int(per_host.get(host, default) or default))


def _per_host_cap(host: str, config: dict) -> int:
    """How many candidates this host's bucket can hold within the configured
    runtime budget. With N concurrent workers serialising on one HostThrottle
    queue at interval I, wall-clock for K candidates ≈ K/N × I (large-K
    approximation). Solving for K at the runtime budget:
        cap = floor(max_runtime_per_host_seconds / (throttle / workers))
            = floor(max_runtime_per_host_seconds × workers / throttle)
    The floor ensures we never schedule a bucket whose math implies running
    past the budget.
    """
    ac = config.get("availability_check", {}) or {}
    runtime = float(ac.get("max_runtime_per_host_seconds", 900))
    throttle = _resolve_host_throttle(host, config)
    workers = _resolve_host_workers(host, config)
    if throttle <= 0:
        # Degenerate: no throttle means no cap (defer to global cap).
        return 10**9
    return max(1, int(runtime * workers / throttle))


def _bucket_and_cap_for_availability(
    candidates: list[dict], config: dict, *, today: date | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Replace the prior length-asc trim with per-host bucket caps derived
    from a runtime budget.

    Algorithm:
      1. Apply global_cap as a safety net first (today's lexical-survivor
         counts ~14k fit comfortably; this guards pathological days when
         filter behaviour changes upstream and survivors balloon).
      2. Bucket the remaining candidates by RDAP host using the same
         resolve_rdap_host() the orchestrator uses — guarantees bucketing
         is consistent with later host-pool sizing.
      3. For each bucket, compute a cap = floor(runtime × workers / throttle).
         If the bucket exceeds its cap, RANDOM-SHUFFLE within the bucket and
         truncate. Random not length-asc: the 2026-05-04 audit showed the
         length-asc heuristic was quality-neutral on Wayback signal but
         biased toward already-registered names (shorter ⇒ more likely
         already taken). Random sampling removes that bias without claiming
         to add quality.
      4. Same shuffle seed across buckets ⇒ reproducible per-day for
         debugging. Seed = today's date as int (YYYYMMDD).

    Returns (final_candidates, per_host_stats) where per_host_stats maps
    host → {"before": int, "after": int, "cap": int, "throttle": float,
    "workers": int}. The orchestrator passes per_host_stats into the
    concurrent runner so its log lines can report 'capped from N' shape
    without recomputing.
    """
    today = today or date.today()
    seed = int(today.strftime("%Y%m%d"))

    ac = config.get("availability_check", {}) or {}
    global_cap = int(ac.get("global_cap", 15000))

    pool: list[dict] = list(candidates)
    if global_cap > 0 and len(pool) > global_cap:
        # Trim by quality-prior instead of random shuffle (changed 2026-05-17).
        # At this stage no enrichment fields exist; the only quantitative
        # quality signals derivable from `name` are trigram match count (how
        # English-like the apex looks) and apex length (shorter = stronger
        # brandable prior). Sort by (-trigram_matches, apex_len, name) so the
        # discarded 3-7k on overflow days are the lowest-quality ones, not
        # random. Random seed kept around for any future tie-breaking, but
        # the deterministic sort makes per-day results reproducible without
        # it. Tertiary `name` key locks the order for ties.
        before = len(pool)

        def _rank_key(cand: dict) -> tuple[int, int, str]:
            name = cand.get("name", "")
            apex = name.split(".", 1)[0]
            return (-lexical_filter.trigram_match_count(name), len(apex), name)

        pool.sort(key=_rank_key)
        pool = pool[:global_cap]
        logger.warning(
            "Lexical survivors (%d) exceed global_cap (%d); trim by trigram-match-count "
            "desc + apex-length asc — kept %d, discarded %d",
            before, global_cap, len(pool), before - len(pool),
        )

    from scripts.enrichment import rdap

    buckets: dict[str, list[dict]] = {}
    for cand in pool:
        host = rdap.resolve_rdap_host(cand.get("name", ""), config) or "_unknown"
        buckets.setdefault(host, []).append(cand)

    final: list[dict] = []
    stats: dict[str, dict] = {}
    for host, group in buckets.items():
        cap = _per_host_cap(host, config)
        throttle = _resolve_host_throttle(host, config)
        workers = _resolve_host_workers(host, config)
        before = len(group)
        if before > cap:
            # Phase 2 ranker (2026-06-02): if any candidate in this bucket
            # carries a `phase2_score`, sort by score desc and take the top
            # `cap` slots — quality-driven selection. Otherwise (no scores,
            # ranker disabled / fallback / cost-ceiling-below-min-eligible)
            # use the existing random-shuffle path bit-for-bit. This keeps
            # the mechanical-selection fallback parity guaranteed.
            scored_present = any("phase2_score" in c for c in group)
            if scored_present:
                group.sort(
                    key=lambda c: (
                        -int(c.get("phase2_score", 0)),
                        c.get("name", ""),
                    )
                )
                group = group[:cap]
                logger.warning(
                    "RDAP host bucket [%s]: %d candidates exceed cap %d "
                    "(throttle=%.2fs, workers=%d); phase2 score-desc trim "
                    "kept top %d",
                    host, before, cap, throttle, workers, len(group),
                )
            else:
                rng = random.Random(seed)
                rng.shuffle(group)
                group = group[:cap]
                logger.warning(
                    "RDAP host bucket [%s]: %d candidates exceed cap %d (throttle=%.2fs, workers=%d); "
                    "random-shuffle trim",
                    host, before, cap, throttle, workers,
                )
        final.extend(group)
        stats[host] = {
            "before": before,
            "after": len(group),
            "cap": cap,
            "throttle": throttle,
            "workers": workers,
        }

    return final, stats


def _write_sidecar_excerpts(
    classified: list[dict],
    sidecar_path: Path,
) -> int:
    """Persist the wayback_excerpt field from classified candidates to the
    sidecar JSON at `sidecar_path`. Mutates input — strips the inline
    wayback_excerpt key from each record after writing so it doesn't
    propagate to downstream stages (output's CONTRACT_FIELDS doesn't list
    it, but stripping early keeps the in-memory dicts smaller and matches
    scripts/classify_carryover.py's strip_inline_excerpts pattern).

    Merge behavior: existing sidecar entries for names NOT in `classified`
    are preserved (today's classifier only sees today's enriched set —
    historical sidecar entries from prior runs survive). Names in
    `classified` overwrite the sidecar entry — today's excerpt is fresher
    than yesterday's if the entry was re-classified.

    Returns the count of sidecar entries written (today's new + existing
    preserved). 0 means nothing to write because the classifier didn't
    touch any record (empty candidate set / classifier short-circuited).

    The sidecar write happens BEFORE the post-enrichment filter so toxic
    entries that get rejected still have their excerpt in the sidecar for
    forensics (`why was X classified toxic?` — answerable months later
    from git history of this file).
    """
    updates: dict[str, dict | None] = {}
    for record in classified:
        name = record.get("name")
        if not name:
            continue
        # snapshot_classifier_version is stamped on every classifier-touched
        # record (success, soft-fail, or no-client pass-through). Its
        # absence means the classifier never ran on this record.
        if not record.get("snapshot_classifier_version"):
            continue
        updates[name] = record.get("wayback_excerpt")

    if not updates:
        logger.info(
            "Sidecar wayback excerpts: no classifier-touched records this run; "
            "sidecar untouched at %s",
            sidecar_path,
        )
        return 0

    existing: dict = {}
    if sidecar_path.exists():
        try:
            with open(sidecar_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                existing = loaded
            else:
                logger.warning(
                    "Sidecar %s is not a dict (corrupted?); resetting to empty.",
                    sidecar_path,
                )
        except (OSError, ValueError) as exc:
            logger.warning(
                "Sidecar %s unreadable (%s); starting fresh (prior entries lost).",
                sidecar_path, exc,
            )

    merged = {**existing, **updates}

    # Atomic write — temp file + os.replace, same pattern as
    # output.write_output and classify_carryover._atomic_write_json.
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=sidecar_path.name + ".",
        dir=str(sidecar_path.parent),
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_name, sidecar_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # Strip inline wayback_excerpt — sidecar is the canonical location.
    for record in classified:
        record.pop("wayback_excerpt", None)

    logger.info(
        "Sidecar wayback excerpts: wrote %d total entries (%d new/updated from today's classifier) to %s",
        len(merged), len(updates), sidecar_path,
    )
    return len(merged)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DomainSifter daily pipeline")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.json"),
        help="Path to config.json",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override output path (default: from config)",
    )
    parser.add_argument(
        "--debug-export",
        default=None,
        metavar="PATH",
        help=(
            "Optional directory to dump intermediate filter/trim lists to "
            "(lexical_rejects, lexical_survivors, trim_kept, trim_discards, "
            "published). Off by default; production runs are unaffected."
        ),
    )
    args = parser.parse_args(argv)
    debug_export_dir = args.debug_export

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    env_check.validate_env()

    config = load_config(args.config)
    logger.info("Loaded config v%s", config.get("version", "?"))

    # Diagnostic logging: turns on DEBUG output for the throttle and the
    # two enrichers we've had pacing trouble with. Off by default — only
    # flip on when investigating.
    if config.get("diagnostic_logging"):
        for name in (
            "scripts.enrichment._circuit_breaker",
            "scripts.enrichment.wayback",
            "scripts.enrichment.crtsh",
        ):
            logging.getLogger(name).setLevel(logging.DEBUG)
        logger.info("Diagnostic logging ENABLED for throttle + wayback + crtsh")

    auth_base = config.get("api_endpoints", {}).get("czds_auth_base")
    auth_kwargs = {"auth_base_url": auth_base} if auth_base else {}
    access_token = czds_client.authenticate(
        os.environ["CZDS_USERNAME"],
        os.environ["CZDS_PASSWORD"],
        **auth_kwargs,
    )

    today = date.today()

    # Persistent rolling list — read existing daily-domains.json so today's
    # run can carry over still-available entries from prior days. Missing /
    # malformed file → empty existing list (graceful first-run / corruption
    # handling).
    output_path = Path(args.output or config.get("output_path", "src/data/daily-domains.json"))
    existing = carryover.load_existing(output_path)
    fresh_carryover, dropped_by_age = carryover.filter_by_age(
        existing, today, max_age_days=int(config.get("carryover_max_age_days", 14)),
    )
    # 2026-05-17: age out wayback_unknown entries after 3 consecutive runs
    # of the flag persisting. Same pattern as filter_by_age. crt.sh's
    # equivalent (cert_history_unknown) is queued for the same treatment in
    # a follow-up — see scripts/enrichment/crtsh.py.
    fresh_carryover, dropped_by_wb_unknown = carryover.age_out_wayback_unknown(
        fresh_carryover,
        max_unknown_days=int(
            config.get(
                "carryover_max_wayback_unknown_days",
                carryover.DEFAULT_MAX_WAYBACK_UNKNOWN_DAYS,
            )
        ),
    )
    logger.info(
        "Existing list: %d entries; %d within age window, %d aged out (>14 days), "
        "%d aged out (wayback_unknown for >%d consecutive days)",
        len(existing), len(fresh_carryover), dropped_by_age,
        dropped_by_wb_unknown,
        int(config.get(
            "carryover_max_wayback_unknown_days",
            carryover.DEFAULT_MAX_WAYBACK_UNKNOWN_DAYS,
        )),
    )

    drops, retained_carryover = collect_drops(
        config, access_token, today=today, carryover_candidates=fresh_carryover,
    )

    registered_count = len(fresh_carryover) - len(retained_carryover)
    logger.info(
        "Carryover validation: %d kept, %d registered (now in today's zone), %d aged out",
        len(retained_carryover), registered_count, dropped_by_age,
    )
    logger.info("Today's new drops: %d", len(drops))

    # Stage 1: structural filter (cheap; pre-enrichment) — applies ONLY to
    # today's new drops. Carryover already passed structural+lexical when
    # they were first published; their data is stable.
    structural_kept = filter_mod.filter_candidates_structural(drops, config)

    # Stage 2: lexical filter (cheap; pre-network). When --debug-export is
    # set, capture per-rejection (name, rule) tuples through the side-channel
    # kwarg; otherwise pass None so nothing extra is allocated.
    lexical_rejections: list[tuple[str, str]] | None = (
        [] if debug_export_dir else None
    )
    lexical_kept = lexical_filter.filter_candidates(
        structural_kept, config, rejections_out=lexical_rejections,
    )

    # Stage 2b: Phase 2 LLM name-quality ranker (2026-06-02). Pre-narrows
    # to what the daily budget can rank, sends to Haiku for 0-100 scoring,
    # gates at score_gate (default 60), and returns the above-gate set
    # sorted by score desc with `phase2_score` + `phase2_reason` fields
    # attached. _bucket_and_cap_for_availability detects the score field
    # presence and sorts each over-cap RDAP bucket by score desc instead
    # of random-shuffle — quality-driven RDAP selection.
    #
    # Fallback is wired to FAILURE only (per product policy "smaller but
    # cleaner is better"): API error, cost-ceiling-below-min-eligible,
    # uncaught exception, or missing API key trigger fallback. Thin yield
    # (above-gate count < publish floor) does NOT trigger fallback as long
    # as it clears phase2.min_eligible (default 10) — the ranker drives a
    # short quality list rather than reverting to a longer mechanical one.
    #
    # When status mode is 'disabled' or 'fallback', the returned list is
    # `lexical_kept` unchanged (no phase2_score field), so the bucket-and-cap
    # below runs its existing random-shuffle path bit-for-bit. Test #16
    # in tests/test_phase2_ranker.py is the regression guard for that parity.
    candidates_for_bucketing, phase2_status = phase2_ranker.rank_and_select(
        lexical_kept, config, today=today,
    )

    # Bucket lexical survivors by RDAP host and apply per-host caps derived
    # from the configured runtime budget (availability_check.max_runtime_per_host_seconds).
    # global_cap overflow (when lexical survivors > 15k) is trimmed by
    # trigram-match-count desc + apex-length asc, not random shuffle — the
    # ~3-7k candidates discarded on overflow days should be the lowest-
    # quality by the available pre-enrichment signals.
    candidates_to_evaluate, per_host_stats = _bucket_and_cap_for_availability(
        candidates_for_bucketing, config, today=today,
    )

    # Phase 2 overflow: above-gate candidates that didn't fit any RDAP bucket
    # get persisted to R2 (state/phase2_overflow.jsonl, aged out at 14 days)
    # for a future second RDAP pass or paid-tier consumer. No-op when status
    # is 'disabled' or 'fallback'. Side-effect only; R2 errors are logged
    # and swallowed — never block the daily run on this step.
    phase2_ranker.record_overflow(
        above_gate=candidates_for_bucketing,
        selected_for_rdap=candidates_to_evaluate,
        config=config, today=today, status=phase2_status,
    )
    total_evaluated = len(candidates_to_evaluate)
    logger.info(
        "Availability buckets: %d candidates across %d hosts after capping",
        total_evaluated, len(per_host_stats),
    )

    # Stage 3: AUTHORITATIVE availability check via RDAP — the gate that
    # eliminates owned/in-redemption domains BEFORE we burn enrichment
    # budget on them. Only HTTP 404 passes through. ~95% of zone-diff
    # "drops" reject here on a typical day.
    available = validate_availability(
        candidates_to_evaluate, config, per_host_stats=per_host_stats,
    )

    # Stage 4: enrichment (sequential, paced) — runs only on confirmed-
    # available candidates. Set is typically small (5-50).
    enriched = enrich_all(available, config)

    # Stage 4b: snapshot content classification (added 2026-05-20).
    # Mutates each enriched record in place to add:
    #   wayback_excerpt — dict|None, moved to sidecar by the call below
    #   snapshot_category — one of legitimate / parked / toxic / empty /
    #                       unknown
    #   snapshot_classifier_version — currently "v1"
    # Soft-fail by design (per env_check's OPTIONAL_ENV_VAR_WARNINGS): if
    # ANTHROPIC_API_KEY is missing OR every Haiku call errors, every entry
    # gets snapshot_category="unknown" and the pipeline continues. Toxic
    # is rejected by Stage 5 (snapshot_toxic reason); parked + empty get
    # verdict-downgraded by output._compute_verdict at write time.
    # pause_seconds=1.0 paces archive.org's Availability API at ~1 req/s,
    # matching scripts/archive_generator.py's pre-existing cadence.
    classifier_client = snapshot_classifier.make_default_client()
    snapshot_classifier.classify_all(
        enriched, client=classifier_client, pause_seconds=1.0,
    )

    # Persist excerpts to sidecar BEFORE Stage 5 so toxic-rejected entries
    # still have their excerpt available for forensics. Mutates `enriched`
    # to strip the inline wayback_excerpt key after writing — sidecar is
    # the canonical location.
    sidecar_path = Path(config.get(
        "sidecar_excerpts_path", "src/data/wayback_excerpts.json",
    ))
    _write_sidecar_excerpts(enriched, sidecar_path)

    # Stage 5: post-enrichment filter
    survivors = filter_mod.filter_candidates_post_enrichment(
        enriched, config, strict_spam_check=True
    )

    # Stage 6: score + sort (null components excluded from normalization,
    # so a domain with partial enrichment scores on what's actually populated
    # rather than being artificially capped).
    score.score_candidates(survivors, config)

    # Stage 7: persistent rolling list — annotate today's drops with
    # first_seen=today/days_listed=0, compute days_listed for carryover,
    # then merge. The merged list is the input to write_output.
    carryover.annotate_today_drops(survivors, today)
    carryover.annotate_carryover_days_listed(retained_carryover, today)
    final_list = carryover.merge(today_drops=survivors, carryover=retained_carryover)

    # Stage 8: write — output.build_payload applies the quality floor
    # (publish_min_score + publish_min_enrichment_completeness) and the
    # publication cap. The payload includes today_count + carryover_count
    # so the frontend can split into the two-card layout. total_evaluated
    # is what entered availability check today (the dominant filter).
    written_path = output.write_output(
        final_list,
        config,
        output_path=args.output,
        total_evaluated=total_evaluated,
    )

    publication_cap = int(config.get("max_candidates_for_publication", 300))
    logger.info(
        "Pipeline complete: %d evaluated today, %d available, %d post-enrich, %d scored today + %d carryover = %d total, cap=%d",
        total_evaluated, len(available), len(enriched), len(survivors),
        len(retained_carryover), len(final_list), publication_cap,
    )

    # Optional intermediate-list dumps for manual quality audits. Strictly
    # gated on --debug-export; production runs that don't pass the flag never
    # collect the lists or call into debug_export.
    if debug_export_dir:
        from datetime import datetime, timezone

        trim_kept_set = {c.get("name", "") for c in candidates_to_evaluate}
        published_payload = json.loads(written_path.read_text(encoding="utf-8"))
        published_names = [d.get("name", "") for d in published_payload.get("domains", [])]
        ac_cfg = config.get("availability_check", {}) or {}
        meta = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "availability_check": {
                "max_runtime_per_host_seconds": ac_cfg.get("max_runtime_per_host_seconds"),
                "global_cap": ac_cfg.get("global_cap"),
                "per_host_stats": per_host_stats,
            },
            "counts": {
                "structural_kept": len(structural_kept),
                "lexical_rejects": len(lexical_rejections or []),
                "lexical_survivors": len(lexical_kept),
                "trim_kept": len(candidates_to_evaluate),
                "trim_discards": max(0, len(lexical_kept) - len(candidates_to_evaluate)),
                "available": len(available),
                "enriched": len(enriched),
                "post_enrichment_survivors": len(survivors),
                "published": len(published_names),
            },
        }
        debug_export.write_dumps(
            debug_export_dir,
            lexical_rejects=lexical_rejections or [],
            lexical_survivors=[c.get("name", "") for c in lexical_kept],
            trim_kept=list(trim_kept_set),
            trim_discards=[
                # Candidates that PASSED lexical but didn't survive global_cap
                # ranking or per-host bucket caps.
                c.get("name", "") for c in lexical_kept
                if c.get("name", "") not in trim_kept_set
            ],
            published=published_names,
            meta=meta,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
