"""Common Crawl backlinks enricher — wired into the pipeline.

Queries the derived SQLite produced by `scripts/cc_refresh.py` for a
candidate apex domain and returns its inbound source-domain count from the
configured release's CC domain webgraph.

Registered in `scripts.pipeline.ENRICHMENT_MODULES` as of 2026-05-14 (the
wire-in commit, after the 2026-05-13 standalone validation against real
CC data on OVH). The SCORED value still comes from Strategy A — the
single `latest_release` — and only from it; see "Backlink history" below
for the display-only multi-release read added 2026-09-20.

Plugin contract:
    enrich(domain, config) -> dict

Returns:
    {"cc_source_domain_count": int}  — domain found in the graph
                                       (count may be 0 for danglers)
    {}                                — domain not in graph, query failed,
                                        config missing, or SQLite unavailable

    plus, optionally, "cc_backlink_history" (see below).

The {} ↔ count=0 distinction matters: per the three-state design
discussion (DNSBL three-state, DNS pre-filter three-state, this), "not in
graph" and "in graph with zero inbound" are different facts that future
scoring logic may want to treat differently.

Backlink history (added 2026-09-20) — DISPLAY ONLY
--------------------------------------------------
R2 accumulates one derived SQLite per monthly CC release and never
deletes them, but until now the pipeline read exactly one of them, which
made the archive inert. `enrich()` now also emits, when
`cc_backlinks.history.enabled` is true:

    {"cc_source_domain_count": 247,
     "cc_backlink_history": [
         {"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 247},
         {"release": "cc-main-2026-may-jun-jul", "source_domain_count": 310},
         {"release": "cc-main-2026-apr-may-jun", "source_domain_count": None},
     ]}

Newest release first; entry 0 is always `latest_release` and its count is
always the same number as `cc_source_domain_count`.
`source_domain_count: None` means "this apex has no row in that release's
graph" — the same not-in-graph ↔ zero-inbound distinction as above, so
None and 0 must never be collapsed by a consumer.

DISPLAY-ONLY BOUNDARY. `cc_source_domain_count` keeps its exact prior
meaning, its value sourced from `latest_release`, and its scoring weight
(0.30). The history is rendered on the site and is NOT read by score.py
or by output.py's completeness gate, so this feature is structurally
incapable of moving the published list or its ordering. The whole history
computation is wrapped: any failure — R2 listing, a missing or corrupt
archive SQLite, a bad config — degrades or omits the history key and
leaves `cc_source_domain_count` byte-identical to what the pre-history
code would have returned.

The window is AUTO-DISCOVERED (`history_window_releases`) by listing the
derived-key prefix in R2, sorting the parseable release names
chronologically (not lexicographically — `may-jun-jul` is newer than
`mar-apr-may`), and truncating to `history.max_releases`. There is
deliberately no hand-maintained release list in config: a manual list is
exactly the step that went four months un-updated and caused this work
item. A newly installed release joins the window with no config edit.

OVERLAP CAVEAT — do not read a confident trend off adjacent entries.
Consecutive CC releases are ROLLING 3-MONTH WINDOWS that share two of
their three months (`may-jun-jul` and `jun-jul-aug` overlap in jun+jul),
so adjacent points are NOT independent observations. At max_releases 6
there are only ~2 independent 3-month samples. Fine to display as a
sparkline; not enough to fit a decay line to, which is why this stays out
of scoring until there is real, calibrated history.

History lookups never download: `enrich()` reads only SQLites already in
the local cache and reports None for a release that is not there, logging
that once per release rather than once per candidate. The refresh path
pre-warms the window via `ensure_history_cached()` so the 09:00 UTC
pipeline never pays a multi-GB inline download mid-enrichment.

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

# Per-process cache: latest_release → the history window (newest first).
# Discovery costs one R2 list call; doing it per candidate would mean
# ~2,500 list calls a run. Documented per-process cache, same idiom as
# _CONNECTION_CACHE above; reset_history_caches() clears it for tests.
_HISTORY_WINDOW_CACHE: dict[str, list[str]] = {}

# Ledger of warnings already emitted this process. A release whose archive
# SQLite isn't cached locally would otherwise log once per candidate —
# 2,500 identical lines that bury the real signal in the run log.
_HISTORY_LOG_ONCE: set[str] = set()


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


def _history_config(config: dict) -> dict:
    """The `cc_backlinks.history` sub-block, or {} when absent.

    `enabled` defaults to False here so a config that predates the history
    feature behaves exactly as before (no new key in enrich()'s output).
    """
    return (config.get("cc_backlinks", {}) or {}).get("history", {}) or {}


def _derived_key_template(config: dict) -> str:
    return (
        (config.get("cc_backlinks", {}) or {})
        .get("r2_derived_key_template", "cc/derived/{release}.sqlite")
    )


def _derived_r2_key(release: str, config: dict) -> str:
    return _derived_key_template(config).format(release=release)


def _derived_key_prefix_and_suffix(config: dict) -> tuple[str, str]:
    """Split the derived-key template around `{release}`.

    Release names are derived from R2 keys by stripping these two affixes,
    so discovery stays consistent with `_derived_r2_key` instead of
    hardcoding `cc/derived/{release}.sqlite` in a second place.

    Raises ValueError for a template without a `{release}` placeholder —
    the caller turns that into the fail-soft fallback.
    """
    template = _derived_key_template(config)
    if "{release}" not in template:
        raise ValueError(
            f"r2_derived_key_template has no {{release}} placeholder: {template!r}"
        )
    prefix, suffix = template.split("{release}", 1)
    return prefix, suffix


def _local_sqlite_path(release: str, cache_dir: Path | None = None) -> Path:
    """Where a release's derived SQLite lives (or would live) locally."""
    resolved = _resolve_cache_dir(str(cache_dir) if cache_dir else None)
    return resolved / f"{release}.sqlite"


def _log_once(key: str, message: str, *args: object) -> None:
    """Emit a WARNING the first time `key` is seen this process.

    Used for per-release history problems: the pipeline calls enrich()
    thousands of times, and a missing archive SQLite is one fact about the
    run, not one fact per candidate.
    """
    if key in _HISTORY_LOG_ONCE:
        return
    _HISTORY_LOG_ONCE.add(key)
    logger.warning(message, *args)


def reset_history_caches() -> None:
    """Clear the per-process history window cache and the log-once ledger.

    Production never calls this (the caches are meant to live for the
    process); tests do, so each one starts from a clean slate.
    """
    _HISTORY_WINDOW_CACHE.clear()
    _HISTORY_LOG_ONCE.clear()


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
    local_path = _local_sqlite_path(release, cache_dir)
    local_path.parent.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# Backlink history — DISPLAY ONLY. Nothing below this line may influence
# cc_source_domain_count, the score, or the published ordering.
# ---------------------------------------------------------------------------


def _discover_derived_releases(
    config: dict,
    *,
    s3_client=None,
    bucket: str | None = None,
) -> list[str]:
    """Release names that have a derived SQLite in R2, in listing order.

    Derives names from the keys under the derived-key prefix, skipping
    anything `cc_refresh._parse_release` can't prove canonical. Raises on a
    listing failure — `history_window_releases` owns the fallback.
    """
    # Lazy import: cc_refresh pulls in boto3/requests at module scope, and
    # it lazily imports this module in prune_local_cache. Importing the
    # release-name algebra here keeps both directions cheap and acyclic.
    # Reused rather than reimplemented — one parser for release names.
    from scripts.cc_refresh import _list_keys, _parse_release

    prefix, suffix = _derived_key_prefix_and_suffix(config)
    if s3_client is None or bucket is None:
        from scripts import diff
        s3_client = s3_client or diff._r2_client()
        bucket = bucket or diff._bucket()

    names: list[str] = []
    for key in _list_keys(s3_client, bucket, prefix):
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        name = key[len(prefix):len(key) - len(suffix)]
        if not name or "/" in name:
            continue  # a nested key, not a release artifact
        if _parse_release(name) is None:
            _log_once(
                f"unparseable:{name}",
                "cc_backlinks history: skipping unparseable derived release "
                "name %r — an unrecognised name is a reason to ignore data, "
                "not to guess at it", name,
            )
            continue
        names.append(name)
    return names


def history_window_releases(
    config: dict,
    *,
    s3_client=None,
    bucket: str | None = None,
) -> list[str]:
    """The releases to report history for, NEWEST FIRST.

    Auto-discovered from R2 (see `_discover_derived_releases`), sorted
    chronologically via `cc_refresh._release_sort_key` — NOT
    lexicographically, under which `mar-apr-may` would beat `may-jun-jul` —
    then truncated to `cc_backlinks.history.max_releases`.

    `latest_release` is always present and always first, even if discovery
    somehow doesn't surface it: the head of this list is the release whose
    count is the scored `cc_source_domain_count`, so it is not negotiable.

    Cached per process (one R2 list call per run, not per candidate);
    `reset_history_caches()` clears it. Fail-soft: any listing or config
    error logs a warning and yields `[latest_release]`, and that fallback
    is cached too — retrying the list once per candidate would turn a
    transient R2 hiccup into thousands of calls.

    Does NOT consult `history.enabled`; that gate belongs to the callers
    that emit or pre-warm. Returns [] when no release is configured at all.

    `s3_client` / `bucket` are injection points for tests.
    """
    latest = _resolve_release(config)
    if not latest:
        return []
    if latest in _HISTORY_WINDOW_CACHE:
        return list(_HISTORY_WINDOW_CACHE[latest])

    max_releases = 1
    try:
        max_releases = max(1, int(_history_config(config).get("max_releases", 1)))
    except (TypeError, ValueError):
        logger.warning(
            "cc_backlinks history: max_releases is not an integer; "
            "reporting %s only", latest,
        )

    try:
        from scripts.cc_refresh import _release_sort_key

        discovered = _discover_derived_releases(
            config, s3_client=s3_client, bucket=bucket,
        )
        ordered = sorted(set(discovered), key=_release_sort_key, reverse=True)
        window = [latest] + [name for name in ordered if name != latest]
        window = window[:max_releases]
    except Exception as exc:
        logger.warning(
            "cc_backlinks history: could not discover the release window "
            "(%s); falling back to %s only", exc, latest,
        )
        window = [latest]

    _HISTORY_WINDOW_CACHE[latest] = window
    logger.info("cc_backlinks history window (newest first): %s", window)
    return list(window)


def ensure_history_cached(
    config: dict,
    *,
    s3_client=None,
    bucket: str | None = None,
) -> list[str]:
    """Download every history-window SQLite that isn't cached locally.

    Returns the releases that are cached locally afterwards, newest first.
    Called by the refresh path (`history.prewarm_on_refresh`) so the 09:00
    UTC pipeline never pays a multi-GB inline download mid-enrichment.

    Fails soft per release: a release R2 can't serve is logged and skipped,
    and the remaining ones are still fetched.
    """
    if not _history_config(config).get("enabled", False):
        logger.info(
            "cc_backlinks history disabled; not pre-warming the archive cache",
        )
        return []

    releases = history_window_releases(config, s3_client=s3_client, bucket=bucket)
    cached: list[str] = []
    fetched: list[str] = []
    skipped: list[str] = []
    for release in releases:
        local = _local_sqlite_path(release)
        already = local.exists() and local.stat().st_size > 0
        try:
            path = _ensure_local_sqlite(
                release, config, s3_client=s3_client, bucket=bucket,
            )
            if not (path.exists() and path.stat().st_size > 0):
                raise RuntimeError(f"download left no usable file at {path}")
        except Exception as exc:
            logger.warning(
                "cc_backlinks history: could not pre-warm release %s: %s",
                release, exc,
            )
            skipped.append(release)
            continue
        cached.append(release)
        if not already:
            fetched.append(release)

    logger.info(
        "cc_backlinks history pre-warm: %d of %d releases cached "
        "(downloaded %s; already present %s; skipped %s)",
        len(cached), len(releases), fetched or "none",
        [r for r in cached if r not in fetched] or "none", skipped or "none",
    )
    return cached


def _history_connection(release: str, config: dict) -> sqlite3.Connection | None:
    """Read-only connection for an ARCHIVE release, or None.

    Deliberately never downloads: a 6 GB fetch inside enrich() would stall
    the daily run. A release whose SQLite isn't in the local cache simply
    reports None for every candidate, warning once (not once per candidate)
    so the operator sees the pre-warm gap without 2,500 log lines.
    """
    if release in _CONNECTION_CACHE:
        return _CONNECTION_CACHE[release]
    local_path = _local_sqlite_path(release)
    if not (local_path.exists() and local_path.stat().st_size > 0):
        _log_once(
            f"uncached:{release}",
            "cc_backlinks history: release %s has no local SQLite at %s — "
            "its history entries will be null until the next pre-warm",
            release, local_path,
        )
        return None
    uri = f"file:{local_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    _CONNECTION_CACHE[release] = conn
    return conn


def _history_count(release: str, domain: str, config: dict) -> int | None:
    """Inbound source-domain count for `domain` in `release`, or None.

    None means BOTH "no row in that release's graph" and "that release was
    unreadable" — the archive read is best-effort display data. A row with
    0 stays 0; the None ↔ 0 distinction is never collapsed.
    """
    try:
        conn = _history_connection(release, config)
        if conn is None:
            return None
        row = conn.execute(
            "SELECT source_domain_count FROM cc_apex WHERE apex_domain = ?",
            (domain.lower(),),
        ).fetchone()
    except Exception as exc:
        _log_once(
            f"query:{release}",
            "cc_backlinks history: query failed against release %s (%s) — "
            "its history entries will be null this run", release, exc,
        )
        return None
    return None if row is None else int(row[0])


def _backlink_history(
    domain: str,
    config: dict,
    *,
    latest_release: str,
    latest_count: int | None,
) -> list[dict]:
    """History entries for `domain`, newest first, or [] to omit the key.

    Entry 0 is always `latest_release`, and its count is the caller's
    already-fetched `latest_count` rather than a second query — that makes
    `history[0]["source_domain_count"] == cc_source_domain_count` true by
    construction.

    Returns [] when history is disabled, and also when the apex has no row
    in ANY release in the window: an all-null list carries no information
    and would change the shape of the module's documented "not in graph"
    result for no gain.
    """
    if not _history_config(config).get("enabled", False):
        return []

    entries: list[dict] = []
    for release in history_window_releases(config):
        count = (
            latest_count
            if release == latest_release
            else _history_count(release, domain, config)
        )
        entries.append({"release": release, "source_domain_count": count})

    if not any(entry["source_domain_count"] is not None for entry in entries):
        return []
    return entries


def enrich(domain: str, config: dict) -> dict:
    """Plugin-contract enricher. Returns `{}` on any failure; never raises.

    Returns `{"cc_source_domain_count": N}` when `domain` is found in the
    cc_apex table of the configured release's derived SQLite. N is the
    number of distinct source domains observed linking to it in CC's last
    crawl window. May be 0 (dangling: domain seen as a source somewhere
    but no inbound edges captured).

    When `cc_backlinks.history.enabled` is true, also returns
    `cc_backlink_history` — a newest-first, DISPLAY-ONLY list across the
    auto-discovered release window (see the module docstring). That whole
    computation is wrapped: no history failure can change, or remove,
    `cc_source_domain_count`.
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

    # The scored field, identical in meaning and provenance to before:
    # latest_release only, empty dict when the apex has no row.
    latest_count = None if row is None else int(row[0])
    result: dict = {} if latest_count is None else {"cc_source_domain_count": latest_count}

    # DISPLAY-ONLY add-on, past the point where `result` is final. The bare
    # `except Exception` is the point of this block, not laziness: whatever
    # goes wrong in the archive read — R2, disk, a corrupt SQLite, a
    # malformed config — `result` must come out exactly as the pre-history
    # code would have returned it.
    try:
        history = _backlink_history(
            domain, config,
            latest_release=release, latest_count=latest_count,
        )
        if history:
            result["cc_backlink_history"] = history
    except Exception as exc:
        _log_once(
            "history-unavailable",
            "cc_backlinks: backlink history unavailable this run (%s); "
            "cc_source_domain_count is unaffected", exc,
        )

    return result


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
    history = result.get("cc_backlink_history") or []
    if "cc_source_domain_count" not in result:
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
        _print_history(history)
        return 1

    print(f"{args.apex}: {result['cc_source_domain_count']} source domains")
    _print_history(history)
    return 0


def _print_history(history: list[dict]) -> None:
    """Operator-readable dump of the display-only history, newest first."""
    for entry in history:
        count = entry.get("source_domain_count")
        rendered = "not in graph" if count is None else f"{count} source domains"
        print(f"  {entry.get('release')}: {rendered}")


if __name__ == "__main__":
    raise SystemExit(main())
