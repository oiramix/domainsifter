"""Common Crawl backlinks enricher — wired into the pipeline.

Queries the derived SQLite produced by `scripts/cc_refresh.py` for a
candidate apex domain and returns its inbound source-domain count from the
configured release's CC domain webgraph.

Registered in `scripts.pipeline.ENRICHMENT_MODULES` as of 2026-05-14 (the
wire-in commit, after the 2026-05-13 standalone validation against real
CC data on OVH). Uses Strategy A — query the single latest release only;
multi-release strategies (B/C) deferred (see STRATEGIC_NOTES.md `Multi-
release CC query strategy`).

Plugin contract:
    enrich(domain, config) -> dict

Returns:
    {"cc_source_domain_count": int}  — domain found in the graph
                                       (count may be 0 for danglers)
    {}                                — domain not in graph, query failed,
                                        config missing, or SQLite unavailable

The {} ↔ count=0 distinction matters: per the three-state design
discussion (DNSBL three-state, DNS pre-filter three-state, this), "not in
graph" and "in graph with zero inbound" are different facts that future
scoring logic may want to treat differently.

CLI usage (operator validation):

    python -m scripts.enrichment.cc_backlinks --apex google.com
    python -m scripts.enrichment.cc_backlinks --apex marketglow.com --release cc-main-...

Configuration sources (precedence high → low):
    1. CC_BACKLINKS_RELEASE env var
    2. --release CLI flag
    3. config["cc_backlinks"]["latest_release"]

R2 SQLite is downloaded once per process to a local cache directory
(XDG_CACHE_HOME by default; ~/.cache/domainsifter/cc/ as fallback) and
reused across calls. Override with --cache-dir or CC_BACKLINKS_CACHE_DIR.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Per-process cache: release name → open read-only sqlite3.Connection.
# The pipeline calls enrich() many times in one process; we open the
# connection once and reuse.
_CONNECTION_CACHE: dict[str, sqlite3.Connection] = {}


def _resolve_cache_dir(explicit: str | None = None) -> Path:
    """Where to keep the downloaded SQLite locally.

    Precedence: explicit arg → CC_BACKLINKS_CACHE_DIR env →
    $XDG_CACHE_HOME/domainsifter/cc → ~/.cache/domainsifter/cc.
    """
    if explicit:
        return Path(explicit)
    env_dir = os.environ.get("CC_BACKLINKS_CACHE_DIR")
    if env_dir:
        return Path(env_dir)
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "domainsifter" / "cc"
    return Path.home() / ".cache" / "domainsifter" / "cc"


def _resolve_release(config: dict) -> str:
    """Release the enricher should query.

    Precedence: CC_BACKLINKS_RELEASE env → config["cc_backlinks"]["latest_release"].
    Returns "" if neither is set (caller falls open to empty dict).
    """
    env = os.environ.get("CC_BACKLINKS_RELEASE", "").strip()
    if env:
        return env
    return (config.get("cc_backlinks", {}) or {}).get("latest_release", "")


def _derived_r2_key(release: str, config: dict) -> str:
    template = (
        (config.get("cc_backlinks", {}) or {})
        .get("r2_derived_key_template", "cc/derived/{release}.sqlite")
    )
    return template.format(release=release)


def _ensure_local_sqlite(
    release: str,
    config: dict,
    *,
    cache_dir: Path | None = None,
    s3_client=None,
    bucket: str | None = None,
) -> Path:
    """Return a local path to the derived SQLite for `release`, downloading
    it from R2 if not already cached.

    Cache is keyed by release name; once downloaded, subsequent calls reuse
    the local file. Callers wanting a fresh download must delete the file
    manually.

    `s3_client` / `bucket` are injection points for tests. In production
    they default to the same R2 client used by scripts.diff (one set of
    credentials for the whole process).
    """
    cache_dir = _resolve_cache_dir(str(cache_dir) if cache_dir else None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_path = cache_dir / f"{release}.sqlite"
    if local_path.exists() and local_path.stat().st_size > 0:
        logger.debug("Using cached CC SQLite at %s", local_path)
        return local_path

    if s3_client is None or bucket is None:
        from scripts import diff
        s3_client = s3_client or diff._r2_client()
        bucket = bucket or diff._bucket()

    key = _derived_r2_key(release, config)
    logger.info("Downloading CC SQLite r2://%s/%s → %s", bucket, key, local_path)
    s3_client.download_file(Bucket=bucket, Key=key, Filename=str(local_path))
    return local_path


def _get_connection(
    release: str,
    config: dict,
    *,
    cache_dir: Path | None = None,
    s3_client=None,
    bucket: str | None = None,
) -> sqlite3.Connection:
    """Return a cached read-only sqlite3 connection for `release`, opening
    it on first use. Subsequent calls return the cached connection.

    Read-only mode (`?mode=ro`) prevents accidental writes if a bug ever
    tries to UPDATE — the CC SQLite is a built artifact, immutable for the
    process's lifetime.
    """
    if release in _CONNECTION_CACHE:
        return _CONNECTION_CACHE[release]
    local_path = _ensure_local_sqlite(
        release, config, cache_dir=cache_dir,
        s3_client=s3_client, bucket=bucket,
    )
    uri = f"file:{local_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    _CONNECTION_CACHE[release] = conn
    return conn


def enrich(domain: str, config: dict) -> dict:
    """Plugin-contract enricher. Returns `{}` on any failure; never raises.

    Returns `{"cc_source_domain_count": N}` when `domain` is found in the
    cc_apex table of the configured release's derived SQLite. N is the
    number of distinct source domains observed linking to it in CC's last
    crawl window. May be 0 (dangling: domain seen as a source somewhere
    but no inbound edges captured).
    """
    release = _resolve_release(config)
    if not release:
        # No release configured — silently return empty (operator hasn't
        # opted in yet, and we shouldn't pollute logs at INFO level).
        return {}

    try:
        conn = _get_connection(release, config)
    except Exception as exc:
        logger.warning(
            "cc_backlinks: failed to load SQLite for release %s: %s", release, exc,
        )
        return {}

    try:
        row = conn.execute(
            "SELECT source_domain_count FROM cc_apex WHERE apex_domain = ?",
            (domain.lower(),),
        ).fetchone()
    except Exception as exc:
        logger.warning("cc_backlinks: query failed for %s: %s", domain, exc)
        return {}

    if row is None:
        return {}  # domain not in the graph for this release
    return {"cc_source_domain_count": int(row[0])}


def _load_config() -> dict:
    """Load the project config for CLI-only invocations. The pipeline
    passes its already-loaded config dict to enrich(), so this is only
    used when running this module directly via `python -m`.
    """
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cc_backlinks CLI: failed to load %s: %s", config_path, exc)
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.enrichment.cc_backlinks",
        description=(
            "Query the Common Crawl domain-webgraph SQLite for an apex "
            "domain's inbound source-domain count. Downloads the SQLite "
            "from R2 on first use; caches under ~/.cache/domainsifter/cc/."
        ),
    )
    parser.add_argument(
        "--apex",
        required=True,
        help="Apex domain to query, e.g. google.com or marketglow.com",
    )
    parser.add_argument(
        "--release",
        default=None,
        help="CC release to query. Overrides config['cc_backlinks']['latest_release'].",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Local cache directory for the downloaded SQLite. "
             "Default: $XDG_CACHE_HOME/domainsifter/cc or ~/.cache/domainsifter/cc.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    config = _load_config()
    if args.release:
        # Force-override via env so enrich() picks it up.
        os.environ["CC_BACKLINKS_RELEASE"] = args.release
    if args.cache_dir:
        os.environ["CC_BACKLINKS_CACHE_DIR"] = args.cache_dir

    result = enrich(args.apex, config)
    if not result:
        # Distinguish "not in graph" from "couldn't query at all" — both
        # return {} per the plugin contract, but the operator running the
        # CLI wants to know which.
        release = os.environ.get("CC_BACKLINKS_RELEASE") or _resolve_release(config)
        if not release:
            print("Error: no release configured. Set --release, "
                  "CC_BACKLINKS_RELEASE, or cc_backlinks.latest_release in config.json.",
                  file=sys.stderr)
            return 2
        print(f"{args.apex}: not in CC graph (release {release})")
        return 1

    print(f"{args.apex}: {result['cc_source_domain_count']} source domains")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
