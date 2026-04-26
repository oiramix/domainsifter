"""DomainSifter daily pipeline orchestrator.

Order of operations:
    1. validate_env()                     — fail loud on missing secrets
    2. authenticate(CZDS)                 — fatal if it fails
    3. list_zone_links → filter to approved TLDs
    4. for each TLD:
         a. download zone to a tempdir
         b. parse_zone → today's set
         c. diff vs yesterday's snapshot → drops
         d. commit_today (overwrite snapshot for tomorrow's run)
         e. delete the temp zone file
    5. build candidate dicts for every drop across all TLDs
    6. enrich each candidate concurrently across the 7 sources
       (ThreadPoolExecutor, max_workers from config)
    7. filter (strict_spam_check=True)
    8. score + sort
    9. write_output → src/data/daily-domains.json

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Callable

from scripts import czds_client, diff, env_check, filter as filter_mod, output, score, zone_parser

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
    """Enrich every candidate concurrently. Each candidate runs all 7 sources
    sequentially (within one worker); different candidates run in parallel
    across the pool. max_workers from config.max_concurrent_enrichments."""
    if not candidates:
        return candidates

    max_workers = max(1, int(config.get("max_concurrent_enrichments", 10)))
    enrichers = _load_enrichers()
    logger.info("Enriching %d candidates with %d concurrent workers", len(candidates), max_workers)

    enriched: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_enrich_one, c, config, enrichers): c for c in candidates}
        for fut in as_completed(futures):
            enriched.append(fut.result())
    return enriched


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
    enriched = enrich_all(drops, config)
    survivors = filter_mod.filter_candidates(enriched, config, strict_spam_check=True)
    score.score_candidates(survivors, config)
    output.write_output(survivors, config, output_path=args.output)

    logger.info("Pipeline complete: %d domains in output", len(survivors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
