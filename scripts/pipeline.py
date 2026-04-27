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
    7. enrichment safety cap    (max_candidates_for_enrichment)
                                — sort by length asc, take top N
    8. enrich each survivor concurrently within a wall-clock budget
       (max_workers from config; budget from enrichment_time_budget_seconds).
       Candidates not started by deadline are skipped, NOT a failure mode.
    9. post-enrichment filter   (strict_spam_check=True)
   10. score + sort
   11. publication cap          (max_candidates_for_publication)
                                — CEILING, not quota; never pad with weak
   12. write_output → src/data/daily-domains.json

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
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Callable

from scripts import (
    czds_client,
    diff,
    env_check,
    filter as filter_mod,
    lexical_filter,
    output,
    score,
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
    "rdap",
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
) -> list[dict]:
    """Download every approved-TLD zone, diff vs yesterday's R2 snapshot,
    overwrite that snapshot with today's set, and return a flat list of
    candidate dicts.

    Yesterday's snapshots live in Cloudflare R2 (see scripts/diff.py header
    for why). The R2 client + bucket name are resolved once and reused across
    all TLDs; tests inject mocks via `r2_client=` / `r2_bucket=`.
    """
    api_base = config.get("api_endpoints", {}).get("czds_api_base")
    download_timeout = config.get("download_timeout_seconds", 120)
    approved = _approved_tlds(config)
    s3 = r2_client if r2_client is not None else diff._r2_client()
    bucket = r2_bucket if r2_bucket is not None else diff._bucket()

    all_links = czds_client.list_zone_links(access_token, api_base) if api_base else czds_client.list_zone_links(access_token)
    targeted = [u for u in all_links if _filename_to_tld(u) in approved]
    logger.info("CZDS approved %d zones; %d match our TLD list", len(all_links), len(targeted))

    drops_total: list[dict] = []
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

    logger.info("Collected %d total drops across %d TLDs", len(drops_total), len(targeted))
    return drops_total


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


def _trim_for_enrichment(candidates: list[dict], cap: int) -> list[dict]:
    """Safety net: if more candidates survived the lexical filter than we're
    willing to enrich, take the shortest names (proxy for higher quality)."""
    if len(candidates) <= cap:
        return candidates
    logger.warning(
        "Lexical survivors (%d) exceed enrichment cap (%d); trimming by length asc",
        len(candidates), cap,
    )
    return sorted(candidates, key=lambda c: (len(c.get("name", "")), c.get("name", "")))[:cap]


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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    env_check.validate_env()

    config = load_config(args.config)
    logger.info("Loaded config v%s", config.get("version", "?"))

    auth_base = config.get("api_endpoints", {}).get("czds_auth_base")
    auth_kwargs = {"auth_base_url": auth_base} if auth_base else {}
    access_token = czds_client.authenticate(
        os.environ["CZDS_USERNAME"],
        os.environ["CZDS_PASSWORD"],
        **auth_kwargs,
    )

    drops = collect_drops(config, access_token, today=date.today())

    # Stage 1: structural filter (cheap; pre-enrichment)
    structural_kept = filter_mod.filter_candidates_structural(drops, config)

    # Stage 2: lexical filter (cheap; pre-enrichment)
    lexical_kept = lexical_filter.filter_candidates(structural_kept, config)

    # Safety net before paying for enrichment
    enrich_cap = int(config.get("max_candidates_for_enrichment", 1000))
    candidates_to_enrich = _trim_for_enrichment(lexical_kept, enrich_cap)

    # Stage 3: enrichment (with wall-clock budget)
    enriched = enrich_all(candidates_to_enrich, config)

    # Stage 4: post-enrichment filter
    survivors = filter_mod.filter_candidates_post_enrichment(
        enriched, config, strict_spam_check=True
    )

    # Stage 5: score + sort
    score.score_candidates(survivors, config)

    # Stage 6: write (publication cap is applied inside output.build_payload)
    output.write_output(survivors, config, output_path=args.output)

    publication_cap = int(config.get("max_candidates_for_publication", 300))
    published = min(len(survivors), publication_cap)
    logger.info(
        "Pipeline complete: %d survivors → %d published (cap=%d)",
        len(survivors), published, publication_cap,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
