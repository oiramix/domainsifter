"""Common Crawl domain-webgraph refresh tool — standalone capability.

Downloads a Common Crawl monthly domain-webgraph release from
data.commoncrawl.org, uploads the raw artifacts to R2 IA tier, builds
a derived SQLite mapping `apex_domain → source_domain_count` via DuckDB,
and uploads the SQLite to R2 Standard tier.

Wired into the daily pipeline as of 2026-05-14 via
`scripts/enrichment/cc_backlinks.py`, which queries the derived SQLite for
`cc_backlinks.latest_release`. This module is what produces that artifact.

AUTOMATED as of 2026-09-20 (`--auto`). Before that it had a timer of
exactly zero and had been run once, by hand, on 2026-05-13 — while
`cc_source_domain_count` carried scoring weight 0.30. The data went four
months stale and nothing alarmed. `--auto` is invoked weekly by
systemd/domainsifter-cc-refresh.timer through scripts/run-cc-refresh.sh.

CADENCE CORRECTION (2026-09-20): Common Crawl publishes the domain-level
webgraph MONTHLY as a rolling 3-month window — jan-feb-mar, feb-mar-apr,
mar-apr-may, apr-may-jun, may-jun-jul, jun-jul-aug all exist for 2026.
The previously-recorded belief of "~3x per year" was WRONG. A release
tends to land during the LAST month of its own window (jun-jul-aug was
published 2026-08-25), so discovery walks backwards from the window
ending in the current month until it finds one that actually exists.
The timer is weekly rather than monthly because discovery is two HEAD
requests and a no-op exit, so a weekly tick costs nothing, picks a new
release up within 7 days, and retries a failed run next week instead of
leaving the graph stale for a month.

RETENTION DECISION (2026-09-20): raw vertices+edges are PRUNED to the
newest `refresh.prune_raw_after_releases` releases — raw exists only to
rebuild the derived SQLite, is free to re-download from
data.commoncrawl.org, and is never read by the pipeline. The derived
SQLite is NEVER deleted by this code at any config value: it is the
durable asset and the only way a future multi-release strategy or a
backlink-trend signal could look backwards. The local
~/.cache/domainsifter/cc/ copy is pruned to the active release because
6.6 GB per monthly release fills the box otherwise.

CLI usage:

    # Default — download + upload raw + build derived + upload derived.
    # Idempotent: if R2 already has the artifacts, skip what's done.
    python -m scripts.cc_refresh --release cc-main-2026-feb-mar-apr

    # Force re-do every step (download + build), even if R2 already has data.
    # Useful for re-running after a build bug fix.
    python -m scripts.cc_refresh --release X --force

    # Only download + upload raw to R2; don't build derived SQLite.
    python -m scripts.cc_refresh --release X --download-only

    # Only build derived from raw already in R2; skip the upstream download.
    # Assumes raw is present in R2 (errors loudly otherwise).
    python -m scripts.cc_refresh --release X --build-only

    # Manual build of a named release, then verify + swap config + prune.
    python -m scripts.cc_refresh --release X --install

    # What does CC have right now? (two HEADs; no R2 credentials needed.)
    python -m scripts.cc_refresh --discover-only

    # The automated path: discover → build → verify → swap config → prune.
    python -m scripts.cc_refresh --auto

`--auto` exit codes — the shell wrapper and the systemd unit depend on
these, so they are part of the contract:

    0   installed a new release, or a no-op (already current), or skipped
        by a guard. A skip must not page anyone; a persistent skip is
        caught instead by the staleness line in the daily report.
    1   discovery found nothing, the build blew up, or the derived
        artifact FAILED verification (in which case the config was NOT
        swapped and nothing was pruned).

Every `--auto` run writes `refresh.result_path` atomically with an
`action` of skipped / discovery_failed / noop / installed / build_failed /
verification_failed.

Operational footprint:

    - Local disk needed during a default run: ~45-50 GB TRANSIENT, not the
      "~22 GiB" this docstring claimed until 2026-09-20. Raw on disk is only
      ~10-19 GB; the rest is DuckDB spill. A measured jun-jul-aug build took
      the filesystem from 20 GB used to 49 GB used during the aggregate step
      — ~38 GB above baseline against 10.3 GB of raw — with a 6.4 GB derived
      SQLite still to write. The requirement is therefore computed per
      release from the upstream Content-Length headers times
      `refresh.disk_headroom_multiplier`, and validated up front via
      shutil.disk_usage; the script aborts before download if free space is
      insufficient, because discovering ENOSPC two hours into a build is the
      worst possible place to discover it.
    - That spill is CHOSEN, not a defect. `_DUCKDB_MEMORY_LIMIT = "8GB"` is
      deliberately conservative so a 16 GB dev laptop behaves like the box;
      the box has 125 GB of RAM and would spill far less if the limit were
      raised. Do not "fix" the memory limit thinking it is a bug, and do not
      assume the spill shrinks by itself on a big-RAM machine — it is a
      trade of disk for portability.
    - Wall-clock, measured 2026-09-20 on the production box (edges 9.4 GB):
      20m50s end-to-end — 5m47s download for both files, 612.8s full build
      (of which 333.1s is the edges aggregate), ~1m50s derived upload.
      Output: 119,722,885 rows / 6.38 GB of SQLite. A bigger edges file
      moves the build figure roughly linearly.
      Verification then adds a 6.4 GB R2 download, which is not waste: it
      lands in the pipeline's own cache directory, so the next 09:00 run
      does not pay for it inline.
    - Bandwidth: 21 GiB ingress from data.commoncrawl.org (free, CloudFront)
      + 22 GiB egress to R2 (free, R2 ingress is also free).
    - R2 storage cost per release: ~$0.23/month (21 GiB IA + 1.5 GiB
      Standard) while raw is retained; ~$0.10/month per release afterwards
      once raw is pruned.
    - Guards: refuses to start inside the blackout window that the daily
      pipeline and archive timer own, and refuses while
      `refresh.pipeline_unit` is active.

Environment:

    Reads the same R2 secrets the pipeline uses — R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME. No CZDS or
    enrichment-API credentials required. `--discover-only` needs none of
    them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import requests
from boto3.s3.transfer import TransferConfig

logger = logging.getLogger(__name__)

# 32 MB multipart chunks — covers a 21 GiB upload in ~700 parts (well under
# R2's 10,000-part ceiling). Hardcoded per design discussion: not exposed as
# a config knob until production proves we need to tune.
_MULTIPART_CHUNK_BYTES = 32 * 1024 * 1024

# 16 MB chunks for HTTP downloads — balances per-chunk syscall overhead
# against fine-grained resume granularity. A network drop loses at most
# ~16 MB of work between successful writes.
_DOWNLOAD_CHUNK_BYTES = 16 * 1024 * 1024

# DuckDB memory budget — caps RAM use so behaviour is identical on dev
# laptops (16 GB) and on KS-6 (128 GB). DuckDB spills to its temp dir
# beyond this; the spill is bounded by disk free space, not RAM.
_DUCKDB_MEMORY_LIMIT = "8GB"

# Loose sanity bounds for downloaded file sizes — based on the
# cc-main-2025-26-dec-jan-feb release stats from yesterday's research
# (vertices 953.8 MiB, edges 20.0 GiB). If a download is way outside
# these ranges we log a warning but proceed (CC may release a much
# bigger or smaller graph someday).
_EXPECTED_VERTICES_BYTES_RANGE = (300 * 1024 * 1024, 4 * 1024 * 1024 * 1024)
_EXPECTED_EDGES_BYTES_RANGE = (8 * 1024 * 1024 * 1024, 60 * 1024 * 1024 * 1024)

# Disk requirement per build = raw bytes × this multiplier. Measured on the
# production box during the cc-main-2026-jun-jul-aug build, 2026-09-20 —
# filesystem `used`, baseline 11 GB:
#     11 GB  before the run
#     20 GB  raw on disk (10.3 GB: vertices 0.89 + edges 8.78 GiB)
#     49 GB  PEAK, during the DuckDB edge aggregation
#     25 GB  aggregation done, spill released
#     29 GB  writing the 6.4 GB derived SQLite
#     26.8 GB final
# Peak transient demand was therefore ~38 GB above baseline against 10.3 GB of
# raw — about 3.7x. The default is rounded up to 5x, not tuned to 3.7, because
# (a) the sampling was at ~5-minute intervals while the aggregate step ran only
# 5m33s, so the true peak was probably higher than the sample caught; (b) spill
# volume scales with the edges file's ROW count, which only loosely correlates
# with its compressed byte count; and (c) the previous release's edges were
# 18.2 GB against this one's 9.4 GB, so a future release could double the raw
# size and more than double the spill. Being generous costs nothing on a 467 GB
# box; being stingy kills a multi-hour job at the aggregate step.
# Overridable via cc_backlinks.refresh.disk_headroom_multiplier.
_DEFAULT_DISK_HEADROOM_MULTIPLIER = 5.0

# Used when upstream won't tell us the release size (no Content-Length, or a
# network error on the HEAD). Never skip the check — a wrong-but-conservative
# number beats no check at all. 60 GiB sits comfortably above the 38 GB
# observed above with room for a bigger release, and is satisfiable on any
# plausible production volume. Explicitly NOT the old 25 GiB, which the
# 2026-09-20 measurement proved would pass and then die on ENOSPC.
_DISK_REQUIREMENT_FLOOR_BYTES = 60 * 1024 * 1024 * 1024

# Repo root — used to resolve config-relative paths (result_path) the same
# way regardless of the cwd systemd hands us.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "scripts" / "config.json"

# CC names its windows with lowercase 3-letter month abbreviations.
_MONTH_ABBREVS = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)

# Canonical release shape. Two accepted year forms:
#   cc-main-2026-jan-feb-mar        window inside one calendar year
#   cc-main-2025-26-dec-jan-feb     window crossing the year boundary
# The regex only screens the shape; _parse_release proves the name is
# canonical by round-tripping it through _release_name. We never guess at a
# name we don't fully recognise — guessing here would mean deleting or
# installing the wrong artifact.
_RELEASE_RE = re.compile(
    r"^cc-main-(\d{4})(?:-(\d{2}))?-([a-z]{3})-([a-z]{3})-([a-z]{3})$"
)

# The one value in scripts/config.json that an automated run is allowed to
# rewrite. Matched (and replaced) as text, not via a json round-trip — see
# install_release for why.
_LATEST_RELEASE_RE = re.compile(r'("latest_release"\s*:\s*")([^"]*)(")')


# ---------------------------------------------------------------------------
# Paths and URLs
# ---------------------------------------------------------------------------


def _source_url(release: str, file_kind: str) -> str:
    """data.commoncrawl.org URL for one of the two raw files.

    file_kind ∈ {"vertices", "edges"}. The actual filename convention is
    `<release>-domain-<kind>.txt.gz` under the `/domain/` subdirectory.
    """
    return (
        f"https://data.commoncrawl.org/projects/hyperlinkgraph/{release}"
        f"/domain/{release}-domain-{file_kind}.txt.gz"
    )


def _r2_raw_key(release: str, file_kind: str) -> str:
    return f"cc/raw/{release}/{file_kind}.txt.gz"


def _r2_derived_key(release: str) -> str:
    return f"cc/derived/{release}.sqlite"


# ---------------------------------------------------------------------------
# Release-name algebra — pure, no network, no config
# ---------------------------------------------------------------------------


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Move (year, month) by `delta` months. Months are 1-12."""
    index = (year * 12 + (month - 1)) + delta
    return index // 12, (index % 12) + 1


def _release_name(year: int, end_month: int) -> str:
    """Canonical CC release name for the 3-month window ENDING at
    (year, end_month).

        _release_name(2026, 3)  -> "cc-main-2026-jan-feb-mar"
        _release_name(2026, 2)  -> "cc-main-2025-26-dec-jan-feb"
        _release_name(2026, 1)  -> "cc-main-2025-26-nov-dec-jan"

    A window whose first month falls in the previous calendar year uses the
    two-year prefix form `<start_year>-<end_year mod 100>`; a window inside
    one year uses the plain 4-digit year.
    """
    if not 1 <= end_month <= 12:
        raise ValueError(f"end_month must be 1-12, got {end_month}")
    start_year, start_month = _shift_month(year, end_month, -2)
    months = [
        _MONTH_ABBREVS[_shift_month(start_year, start_month, i)[1] - 1]
        for i in range(3)
    ]
    if start_year == year:
        prefix = f"{year:04d}"
    else:
        prefix = f"{start_year:04d}-{year % 100:02d}"
    return "cc-main-" + prefix + "-" + "-".join(months)


def _parse_release(release: str) -> tuple[int, int] | None:
    """Inverse of _release_name: return the chronologically sortable
    `(year, end_month)` key for a canonical release name, else None.

    Returns None — never a guess — for anything that isn't exactly a name
    _release_name would have produced. That includes out-of-order months
    ("cc-main-2026-jan-mar-feb"), an inconsistent two-year prefix
    ("cc-main-2025-27-dec-jan-feb") and plain junk. Callers treat None as
    "leave this alone".
    """
    if not isinstance(release, str):
        return None
    match = _RELEASE_RE.match(release.strip())
    if match is None:
        return None
    first_year_s, second_year_s, _m1, _m2, m3 = match.groups()
    first_year = int(first_year_s)
    if second_year_s is None:
        end_year = first_year
    else:
        # "2025-26" means the window starts in 2025 and ends in 2026.
        end_year = first_year + 1
        if end_year % 100 != int(second_year_s):
            return None
    if m3 not in _MONTH_ABBREVS:
        return None
    end_month = _MONTH_ABBREVS.index(m3) + 1
    # Round-trip proof: the name must be *exactly* what we would generate.
    if _release_name(end_year, end_month) != release.strip():
        return None
    return end_year, end_month


def _release_sort_key(release: str) -> tuple[int, int]:
    """Sort key for release names, oldest first.

    Unparseable names sort before every real release so that "the newest N"
    never accidentally consists of junk. Retention code skips unparseable
    names outright rather than relying on this ordering.
    """
    parsed = _parse_release(release)
    return parsed if parsed is not None else (-1, -1)


# ---------------------------------------------------------------------------
# Config access — every threshold lives in scripts/config.json (hard rule 9)
# ---------------------------------------------------------------------------


def _cc_config(config: dict) -> dict:
    return config.get("cc_backlinks", {}) or {}


def _refresh_config(config: dict) -> dict:
    return _cc_config(config).get("refresh", {}) or {}


def _load_config(config_path: str | Path | None = None) -> dict:
    """Load scripts/config.json. Raises on missing/invalid config — hard
    rule 17: missing config is a crash-the-run error, not a fail-soft one."""
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Discovery — which release does data.commoncrawl.org actually have?
# ---------------------------------------------------------------------------


def _head(url: str, *, timeout: float, session=None):
    """HEAD `url`, returning the response or None if we couldn't get one.

    Every failure mode collapses to None so callers keep going instead of
    crashing the run: a missing release 404s (a response), while a CloudFront
    hiccup or DNS blip yields None. Each exception class is handled and
    logged distinctly per the CLAUDE.md HTTP convention, so the operator can
    tell a real 404 from a network problem in the log.

    One mechanism for both HEAD users — discovery and disk sizing — so they
    can never disagree about timeouts, redirects or error handling.
    """
    head = session.head if session is not None else requests.head
    try:
        resp = head(url, timeout=timeout, allow_redirects=True)
    except requests.Timeout as exc:
        logger.info("HEAD %s timed out (%s)", url, exc)
        return None
    except requests.ConnectionError as exc:
        logger.info("HEAD %s connection error (%s)", url, exc)
        return None
    except requests.HTTPError as exc:
        logger.info("HEAD %s HTTP error (%s)", url, exc)
        return None
    except requests.RequestException as exc:
        logger.info("HEAD %s failed (%s)", url, exc)
        return None
    logger.info("HEAD %s -> %s", url, getattr(resp, "status_code", None))
    return resp


def _head_ok(url: str, *, timeout: float, session=None) -> bool:
    """True when `url` answers a HEAD with 200."""
    resp = _head(url, timeout=timeout, session=session)
    return resp is not None and getattr(resp, "status_code", None) == 200


def _remote_size(url: str, *, timeout: float, session=None) -> int | None:
    """Content-Length for `url`, or None when upstream doesn't say.

    data.commoncrawl.org does serve Content-Length on HEAD for both raw
    files (verified 2026-09-20), but a CDN that stops doing so must degrade
    to "unknown", never to a bogus small number that would let a doomed
    build start.
    """
    resp = _head(url, timeout=timeout, session=session)
    if resp is None or getattr(resp, "status_code", None) != 200:
        return None
    headers = getattr(resp, "headers", None) or {}
    try:
        size = int(headers.get("Content-Length"))
    except (TypeError, ValueError):
        logger.info("HEAD %s gave no usable Content-Length", url)
        return None
    return size if size > 0 else None


def _release_available(release: str, *, timeout: float, session=None) -> bool:
    """A release counts as available only when BOTH raw files are there.

    CC publishes vertices and edges as separate objects; a window with only
    vertices up is mid-publication and would build a graph with no edges at
    all — every count would be 0 and verification's canary would (rightly)
    reject it hours later. Cheaper to notice now.
    """
    for kind in ("vertices", "edges"):
        if not _head_ok(_source_url(release, kind), timeout=timeout, session=session):
            return False
    return True


def discover_latest_release(
    config: dict,
    *,
    session=None,
    today: date | None = None,
) -> str | None:
    """Newest CC release whose vertices AND edges are both published.

    Walks backwards from the window ending in the current month, at most
    `refresh.discover_max_windows_back` windows. Returns None when nothing
    in that range is available — the caller must fail soft on None, because
    a transient upstream outage is not a reason to crash a weekly timer.
    """
    refresh = _refresh_config(config)
    max_back = int(refresh.get("discover_max_windows_back", 6))
    timeout = float(refresh.get("head_timeout_seconds", 20))
    today = today or datetime.now(timezone.utc).date()

    for offset in range(max_back):
        year, month = _shift_month(today.year, today.month, -offset)
        release = _release_name(year, month)
        logger.info("Probing CC release %s (%d windows back)", release, offset)
        if _release_available(release, timeout=timeout, session=session):
            logger.info("Newest available CC release: %s", release)
            return release
    logger.warning(
        "No available CC release found in the last %d windows from %s",
        max_back, today.isoformat(),
    )
    return None


# ---------------------------------------------------------------------------
# Guards — when a multi-hour job must not start
# ---------------------------------------------------------------------------


def _systemd_unit_active(unit: str) -> bool:
    """True when `systemctl is-active --quiet <unit>` exits 0.

    Anything else — no systemctl binary (dev laptop), no such unit, a
    permission problem — means "not active". A refresh that refuses to run
    because we couldn't ask systemd would be worse than one that overlaps,
    and the blackout window is the primary interlock anyway.
    """
    try:
        completed = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.debug("systemctl not found; treating %s as not active", unit)
        return False
    except OSError as exc:
        logger.debug("systemctl failed for %s (%s); treating as not active", unit, exc)
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("systemctl raised for %s (%s); treating as not active", unit, exc)
        return False
    return completed.returncode == 0


def _in_blackout(hour: int, start: int, end: int) -> bool:
    """Half-open [start, end) membership. start == end disables the window;
    start > end wraps past midnight."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _refresh_blocked_reason(
    config: dict,
    *,
    now: datetime | None = None,
    unit_active_fn: Callable[[str], bool] | None = None,
) -> str | None:
    """Human-readable reason the refresh must not start, or None to proceed.

    `now` must be UTC — the blackout hours in config are UTC hours, which is
    what the systemd timer and the daily pipeline schedule are expressed in.
    `unit_active_fn` is the injection point for tests; production passes
    None and gets the real systemctl probe.
    """
    refresh = _refresh_config(config)
    if not refresh.get("enabled", False):
        return "cc_backlinks.refresh.enabled is false in config"

    now = now or datetime.now(timezone.utc)
    start = int(refresh.get("blackout_start_utc_hour", 0))
    end = int(refresh.get("blackout_end_utc_hour", 0))
    if _in_blackout(now.hour, start, end):
        return (
            f"inside the blackout window [{start:02d}:00, {end:02d}:00) UTC "
            f"(now {now.hour:02d}:xx UTC) — the daily pipeline and archive "
            "timer own the box then"
        )

    unit = str(refresh.get("pipeline_unit", "") or "")
    if unit:
        probe = unit_active_fn or _systemd_unit_active
        if probe(unit):
            return f"{unit} is currently active"
    return None


# ---------------------------------------------------------------------------
# Idempotency: check whether an R2 object already exists
# ---------------------------------------------------------------------------


def _r2_object_exists(s3, bucket: str, key: str) -> tuple[bool, int]:
    """Return (exists, size_bytes). Treats any 404-class ClientError as
    'not exists'. Other errors propagate."""
    from botocore.exceptions import ClientError

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return False, 0
        raise
    return True, int(head.get("ContentLength", 0))


# ---------------------------------------------------------------------------
# Download with resume + retry
# ---------------------------------------------------------------------------


def _download_with_resume(
    url: str,
    local_path: Path,
    *,
    chunk_size: int = _DOWNLOAD_CHUNK_BYTES,
    max_retries: int = 5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Download `url` to `local_path`, resuming from existing partial content
    via HTTP Range. Retries up to `max_retries` times on transient network
    failure with exponential backoff (1s, 2s, 4s, 8s, 16s).

    Returns the total byte count written.

    data.commoncrawl.org is served via CloudFront which honours `Range`, so
    a network drop at byte N causes only ~chunk_size bytes of rework, not
    a full restart.
    """
    completed = local_path.stat().st_size if local_path.exists() else 0
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        headers = {"Range": f"bytes={completed}-"} if completed > 0 else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
                # 206 = Partial Content (resume), 200 = full download.
                # Anything else is a hard error.
                if resp.status_code not in (200, 206):
                    resp.raise_for_status()
                mode = "ab" if completed > 0 else "wb"
                with open(local_path, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            fh.write(chunk)
                            completed += len(chunk)
            return completed
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            delay = 2 ** (attempt - 1)
            logger.warning(
                "Download error at byte %d for %s: %s; retry %d/%d in %ds",
                completed, url, exc, attempt, max_retries, delay,
            )
            sleep_fn(delay)
    raise RuntimeError(
        f"Download failed after {max_retries} retries (last error: {last_exc})"
    )


def _validate_size(file_kind: str, actual: int) -> None:
    """Warn (don't fail) when downloaded size is outside expected bounds.
    CC graphs grow over time, so a slow upward drift is normal — but a
    50%-shrink or 10x-grow indicates something is off with the source."""
    lo, hi = (
        _EXPECTED_VERTICES_BYTES_RANGE
        if file_kind == "vertices"
        else _EXPECTED_EDGES_BYTES_RANGE
    )
    if actual < lo or actual > hi:
        logger.warning(
            "%s size %d bytes is outside expected range [%d, %d] — proceed but verify",
            file_kind, actual, lo, hi,
        )
    else:
        logger.info("%s size %d bytes — within expected range", file_kind, actual)


# ---------------------------------------------------------------------------
# R2 upload + download helpers (use boto3 TransferManager)
# ---------------------------------------------------------------------------


def _upload_to_r2(
    s3, bucket: str, key: str, local_path: Path, storage_class: str,
) -> None:
    """Multipart-upload `local_path` to R2 at `key` with the given storage
    class. 32 MB part size — see _MULTIPART_CHUNK_BYTES rationale."""
    transfer_config = TransferConfig(
        multipart_threshold=_MULTIPART_CHUNK_BYTES,
        multipart_chunksize=_MULTIPART_CHUNK_BYTES,
        max_concurrency=4,
        use_threads=True,
    )
    extra = {"StorageClass": storage_class} if storage_class else {}
    logger.info(
        "Uploading %s (%d bytes) → r2://%s/%s [class=%s]",
        local_path, local_path.stat().st_size, bucket, key, storage_class or "STANDARD",
    )
    s3.upload_file(
        Filename=str(local_path),
        Bucket=bucket,
        Key=key,
        ExtraArgs=extra,
        Config=transfer_config,
    )


def _download_from_r2(s3, bucket: str, key: str, local_path: Path) -> None:
    transfer_config = TransferConfig(
        multipart_threshold=_MULTIPART_CHUNK_BYTES,
        multipart_chunksize=_MULTIPART_CHUNK_BYTES,
        max_concurrency=4,
        use_threads=True,
    )
    logger.info("Downloading r2://%s/%s → %s", bucket, key, local_path)
    s3.download_file(
        Bucket=bucket, Key=key, Filename=str(local_path), Config=transfer_config,
    )


# ---------------------------------------------------------------------------
# DuckDB-driven build: vertices + edges (gzipped TSV) → derived SQLite
# ---------------------------------------------------------------------------


def _build_derived_sqlite(
    vertices_path: Path,
    edges_path: Path,
    output_path: Path,
    release: str,
    source_urls: dict[str, str],
) -> int:
    """Aggregate edges + join vertices and write the result to a SQLite at
    `output_path`. Returns the row count of the `cc_apex` table.

    DuckDB does the heavy lifting: parallel out-of-core aggregation against
    gzipped TSV input, then writes to SQLite via the `sqlite` extension.

    Schema written:
        meta (key TEXT PRIMARY KEY, value TEXT)
            ('release', '<release>')
            ('built_at', '<ISO8601>')
            ('schema_version', '1')
            ('vertices_source_url', '...')
            ('edges_source_url', '...')

        cc_apex (apex_domain TEXT PRIMARY KEY, source_domain_count INTEGER)
            Includes every vertex with COALESCE(inbound, 0) — danglers
            (vertices with no inbound edges) get source_domain_count = 0.
            This preserves the three-state distinction at lookup time:
            row absent → not in graph; row present with 0 → seen, no inbound.

    The reversed-domain un-reversal converts CC's "com.example" format back
    to "example.com" via split('.') → list_reverse() → array_to_string().
    """
    import duckdb  # lazy import — only the refresh path needs it

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()  # DuckDB's ATTACH won't overwrite

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"SET memory_limit='{_DUCKDB_MEMORY_LIMIT}'")
        con.execute("INSTALL sqlite")
        con.execute("LOAD sqlite")

        logger.info("Aggregating edges (this is the slow step) ...")
        agg_start = time.monotonic()
        con.execute(
            """
            CREATE TEMP TABLE inbound AS
            SELECT to_id, COUNT(DISTINCT from_id) AS source_domain_count
            FROM read_csv(?, delim='\t', header=false,
                          columns={'from_id': 'INTEGER', 'to_id': 'INTEGER'})
            GROUP BY to_id
            """,
            [str(edges_path)],
        )
        logger.info("Edges aggregated in %.1fs", time.monotonic() - agg_start)

        logger.info("Loading vertices ...")
        con.execute(
            """
            CREATE TEMP TABLE vertices AS
            SELECT * FROM read_csv(?, delim='\t', header=false,
                                   columns={'id': 'INTEGER',
                                            'reversed_domain': 'VARCHAR',
                                            'num_hosts': 'INTEGER'})
            """,
            [str(vertices_path)],
        )

        logger.info("Writing derived SQLite to %s ...", output_path)
        con.execute(f"ATTACH '{output_path}' AS s (TYPE SQLITE)")

        con.execute(
            """
            CREATE TABLE s.meta (key TEXT PRIMARY KEY, value TEXT)
            """
        )
        con.execute(
            "INSERT INTO s.meta VALUES (?, ?), (?, ?), (?, ?), (?, ?), (?, ?)",
            [
                "release", release,
                "built_at", built_at,
                "schema_version", "1",
                "vertices_source_url", source_urls.get("vertices", ""),
                "edges_source_url", source_urls.get("edges", ""),
            ],
        )

        # Un-reverse "com.example" → "example.com" via split/reverse/join.
        # COALESCE ensures danglers (vertices with no inbound edges) appear
        # with count=0 rather than being silently dropped.
        con.execute(
            """
            CREATE TABLE s.cc_apex AS
            SELECT
                array_to_string(list_reverse(string_split(v.reversed_domain, '.')), '.') AS apex_domain,
                COALESCE(i.source_domain_count, 0) AS source_domain_count
            FROM vertices v
            LEFT JOIN inbound i ON v.id = i.to_id
            """
        )
        con.execute("CREATE INDEX idx_cc_apex_domain ON s.main.cc_apex(apex_domain)")

        row_count = con.execute("SELECT COUNT(*) FROM s.cc_apex").fetchone()[0]
        logger.info(
            "Derived SQLite built: %d rows, %d bytes on disk",
            row_count, output_path.stat().st_size,
        )
        return int(row_count)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Top-level phases
# ---------------------------------------------------------------------------


def _required_disk_bytes(
    release: str,
    config: dict,
    *,
    session=None,
) -> int:
    """How much free space this release's build really needs.

    Sized from the release itself — the sum of the two raw files'
    Content-Length times `refresh.disk_headroom_multiplier` — because the
    dominant term is DuckDB's spill, which scales with the edges file, and
    because CC's graph changes size in both directions (see
    _DEFAULT_DISK_HEADROOM_MULTIPLIER for the measurement). A fixed constant
    was wrong both ways: too small for a big release, needlessly strict for a
    small one.

    If either size can't be determined we return the conservative absolute
    floor rather than skipping the check. The config key may not exist yet
    (it lands in a separate commit), hence the in-code default.
    """
    refresh = _refresh_config(config)
    timeout = float(refresh.get("head_timeout_seconds", 20))
    multiplier = float(
        refresh.get("disk_headroom_multiplier", _DEFAULT_DISK_HEADROOM_MULTIPLIER)
    )

    sizes: dict[str, int] = {}
    for kind in ("vertices", "edges"):
        size = _remote_size(
            _source_url(release, kind), timeout=timeout, session=session,
        )
        if size is None:
            logger.info(
                "Could not size %s %s from upstream headers; requiring the "
                "conservative floor of %.1f GiB free",
                release, kind, _DISK_REQUIREMENT_FLOOR_BYTES / 1024**3,
            )
            return _DISK_REQUIREMENT_FLOOR_BYTES
        sizes[kind] = size

    raw_total = sum(sizes.values())
    required = int(raw_total * multiplier)
    logger.info(
        "Disk requirement for %s: raw %.1f GiB (vertices %.1f + edges %.1f) "
        "x %.1f headroom = %.1f GiB free needed",
        release, raw_total / 1024**3, sizes["vertices"] / 1024**3,
        sizes["edges"] / 1024**3, multiplier, required / 1024**3,
    )
    return required


def _ensure_disk_space(workdir: Path, required_bytes: int) -> None:
    free = shutil.disk_usage(workdir).free
    if free < required_bytes:
        raise RuntimeError(
            f"Insufficient disk space at {workdir}: "
            f"{free} bytes free, need ~{required_bytes}. "
            "Free up space or set --workdir to a larger volume."
        )


def _phase_download_and_upload_raw(
    *,
    s3,
    bucket: str,
    release: str,
    workdir: Path,
    force: bool,
) -> dict[str, Path]:
    """For each of {vertices, edges}:
      1. If R2 already has the raw artifact and not --force, skip download.
      2. Otherwise: download from data.commoncrawl.org with resume, then
         upload to R2 IA tier.

    Returns {file_kind: local_path} for the files that are on the local
    filesystem after this phase (used by the build phase). When a file is
    skipped because R2 already has it, its local_path will not exist on
    disk — the build phase downloads it from R2 if needed.
    """
    local_paths: dict[str, Path] = {}
    for kind in ("vertices", "edges"):
        url = _source_url(release, kind)
        r2_key = _r2_raw_key(release, kind)
        local_path = workdir / f"{kind}.txt.gz"

        exists, size = _r2_object_exists(s3, bucket, r2_key)
        if exists and not force:
            logger.info(
                "R2 already has %s (%d bytes); skipping download/upload "
                "[--force to re-do]", r2_key, size,
            )
            continue

        logger.info("Downloading %s → %s", url, local_path)
        download_start = time.monotonic()
        written = _download_with_resume(url, local_path)
        elapsed = time.monotonic() - download_start
        rate = written / elapsed / (1024 * 1024) if elapsed > 0 else 0
        logger.info(
            "Downloaded %d bytes in %.1fs (%.1f MB/s)", written, elapsed, rate,
        )
        _validate_size(kind, written)

        # R2's S3-compatible API uses the AWS S3 storage-class name
        # `STANDARD_IA`, NOT Cloudflare's `InfrequentAccess` (that's the
        # Workers API spelling). Misusing the Workers spelling here failed
        # with `InvalidStorageClass` on the first OVH run 2026-05-13.
        _upload_to_r2(s3, bucket, r2_key, local_path, storage_class="STANDARD_IA")
        local_paths[kind] = local_path
    return local_paths


def _phase_build_and_upload_derived(
    *,
    s3,
    bucket: str,
    release: str,
    workdir: Path,
    local_raw: dict[str, Path],
    force: bool,
) -> None:
    """Build the derived SQLite from raw files and upload to R2 Standard.

    If raw files are not present locally (because the download phase
    short-circuited), pull them from R2 first.

    If the derived SQLite already exists on R2 and not --force, skip.
    """
    derived_key = _r2_derived_key(release)
    exists, size = _r2_object_exists(s3, bucket, derived_key)
    if exists and not force:
        logger.info(
            "R2 already has %s (%d bytes); skipping build [--force to re-do]",
            derived_key, size,
        )
        return

    # Make sure both raw files are on the local filesystem.
    for kind in ("vertices", "edges"):
        if kind not in local_raw or not local_raw[kind].exists():
            local_path = workdir / f"{kind}.txt.gz"
            _download_from_r2(s3, bucket, _r2_raw_key(release, kind), local_path)
            local_raw[kind] = local_path

    sqlite_path = workdir / f"{release}.sqlite"
    source_urls = {
        "vertices": _source_url(release, "vertices"),
        "edges": _source_url(release, "edges"),
    }
    build_start = time.monotonic()
    row_count = _build_derived_sqlite(
        local_raw["vertices"], local_raw["edges"], sqlite_path,
        release=release, source_urls=source_urls,
    )
    logger.info(
        "Build completed in %.1fs; %d rows", time.monotonic() - build_start, row_count,
    )

    _upload_to_r2(s3, bucket, derived_key, sqlite_path, storage_class="")  # Standard


# ---------------------------------------------------------------------------
# Verification — the gate in front of the config swap
# ---------------------------------------------------------------------------


class VerificationError(RuntimeError):
    """A derived release failed a verification check.

    Carries the name of the specific check that failed so the result file
    and the operational email can say *what* was wrong, not just "bad".
    """

    def __init__(self, check: str, detail: str) -> None:
        super().__init__(f"{check}: {detail}")
        self.check = check
        self.detail = detail


def _verify_checks(conn: sqlite3.Connection, release: str, vcfg: dict) -> dict:
    """Run every configured check against an open read-only connection.
    Returns the measurements; raises VerificationError on the first failure.
    All thresholds come from config (hard rule 9)."""
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if meta.get("release") != release:
        raise VerificationError(
            "meta_release",
            f"meta.release is {meta.get('release')!r}, expected {release!r}",
        )

    rows = int(conn.execute("SELECT COUNT(*) FROM cc_apex").fetchone()[0])
    min_rows = int(vcfg.get("min_cc_apex_rows", 0))
    if rows < min_rows:
        raise VerificationError(
            "min_cc_apex_rows",
            f"cc_apex has {rows} rows, need at least {min_rows}",
        )

    canaries: dict[str, int] = {}
    for apex, floor in (vcfg.get("canary_present", {}) or {}).items():
        row = conn.execute(
            "SELECT source_domain_count FROM cc_apex WHERE apex_domain = ?",
            (apex.lower(),),
        ).fetchone()
        if row is None:
            raise VerificationError(
                "canary_present", f"{apex} is absent from cc_apex",
            )
        count = int(row[0])
        canaries[apex] = count
        if count < int(floor):
            raise VerificationError(
                "canary_present",
                f"{apex} has source_domain_count {count}, need at least {floor}",
            )

    for apex in (vcfg.get("canary_absent", []) or []):
        row = conn.execute(
            "SELECT source_domain_count FROM cc_apex WHERE apex_domain = ?",
            (apex.lower(),),
        ).fetchone()
        if row is not None:
            raise VerificationError(
                "canary_absent",
                f"{apex} should not be in the graph but has count {row[0]}",
            )

    return {
        "release": release,
        "rows": rows,
        "min_cc_apex_rows": min_rows,
        "canaries": canaries,
        "canary_absent_ok": list(vcfg.get("canary_absent", []) or []),
        "meta": meta,
    }


def verify_derived_release(
    release: str,
    config: dict,
    *,
    cache_dir: Path | None = None,
    s3_client=None,
    bucket: str | None = None,
) -> dict:
    """Prove the derived SQLite for `release` is good AS IT EXISTS IN R2.

    Verifying the locally-built file would only prove DuckDB ran; it would
    not prove the 6.6 GB upload round-tripped. So we delete any cached copy,
    download the artifact fresh from R2 through the *pipeline's own* loader
    (`cc_backlinks._ensure_local_sqlite`, single source of truth for the
    cache path), and query that. The useful side effect: the pipeline's
    cache is left pre-warmed, so the 09:00 run never pays for a 6.6 GB
    inline download.

    Raises VerificationError — and deletes the downloaded file — on any
    failure, so a bad artifact can never be served to the pipeline from
    cache. Returns everything measured, for the log and the result file.
    """
    from scripts.enrichment import cc_backlinks

    vcfg = _refresh_config(config).get("verification", {}) or {}
    resolved_dir = cc_backlinks._resolve_cache_dir(
        str(cache_dir) if cache_dir else None
    )
    resolved_dir.mkdir(parents=True, exist_ok=True)
    stale = resolved_dir / f"{release}.sqlite"
    if stale.exists():
        logger.info("Removing cached %s so verification downloads fresh", stale)
        stale.unlink()

    local_path = cc_backlinks._ensure_local_sqlite(
        release, config, cache_dir=resolved_dir,
        s3_client=s3_client, bucket=bucket,
    )

    logger.info("Verifying %s ...", local_path)
    try:
        conn = sqlite3.connect(f"file:{local_path.as_posix()}?mode=ro", uri=True)
        try:
            measured = _verify_checks(conn, release, vcfg)
        finally:
            conn.close()
    except VerificationError as exc:
        logger.error("Verification FAILED for %s — %s", release, exc)
        _delete_quietly(local_path)
        raise
    except Exception as exc:
        logger.error("Verification could not read %s: %s", local_path, exc)
        _delete_quietly(local_path)
        raise VerificationError("unreadable", str(exc)) from exc

    logger.info(
        "Verification PASSED for %s: %d rows, canaries %s",
        release, measured["rows"], measured["canaries"],
    )
    measured["local_path"] = str(local_path)
    return measured


def _delete_quietly(path: Path) -> None:
    """Best-effort unlink. Used on failure paths where the caller is already
    raising and a second exception would bury the real reason."""
    try:
        if path.exists():
            path.unlink()
            logger.info("Deleted %s", path)
    except OSError as exc:
        logger.warning("Could not delete %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Config swap — surgical, byte-preserving
# ---------------------------------------------------------------------------


def install_release(
    release: str,
    config_path: str | Path,
    *,
    previous: str | None = None,
) -> str:
    """Point `cc_backlinks.latest_release` at `release`. Returns the old value.

    Text edit, NOT a json.load/json.dump round-trip. config.json is valid
    UTF-8 but its `_doc` strings carry pre-existing mojibake from earlier
    edits; a round-trip would reformat all 38 KB, re-escape those bytes and
    make every future diff unreadable. So: replace exactly the one
    "latest_release" value and leave every other byte — key order,
    indentation, the trailing newline — untouched. `newline=""` keeps LF
    endings LF even when this runs on Windows.

    Asserts exactly one match, and re-parses the file afterwards; if the
    result no longer parses or the value didn't take, the original bytes are
    restored and the call raises. A half-written config is the one failure
    this function must never leave behind, because the daily pipeline reads
    it.
    """
    path = Path(config_path)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        original = fh.read()

    matches = list(_LATEST_RELEASE_RE.finditer(original))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one \"latest_release\" value in {path}, "
            f"found {len(matches)} — refusing to edit"
        )
    match = matches[0]
    old_value = match.group(2)
    if previous is not None and old_value != previous:
        raise RuntimeError(
            f"{path} has latest_release={old_value!r} but caller expected "
            f"{previous!r} — refusing to edit (concurrent change?)"
        )
    if old_value == release:
        logger.info("latest_release already %s; no edit needed", release)
        return old_value

    updated = (
        original[: match.start()]
        + match.group(1) + release + match.group(3)
        + original[match.end():]
    )
    _atomic_write_text(path, updated)

    try:
        reparsed = _load_config(path)
    except Exception as exc:
        _atomic_write_text(path, original)
        raise RuntimeError(
            f"{path} no longer parses after the latest_release edit "
            f"({exc}); original restored"
        ) from exc
    installed = _cc_config(reparsed).get("latest_release")
    if installed != release:
        _atomic_write_text(path, original)
        raise RuntimeError(
            f"{path} still reports latest_release={installed!r} after the "
            f"edit; original restored"
        )

    logger.info("Installed CC release %s (was %s) in %s", release, old_value, path)
    return old_value


def _atomic_write_text(path: Path, text: str) -> None:
    """Temp file + os.replace, byte-exact (newline="" — no translation)."""
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent), suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Retention — raw is disposable, derived is forever
# ---------------------------------------------------------------------------


def _list_keys(s3, bucket: str, prefix: str) -> list[str]:
    """Every key under `prefix`, following continuation tokens."""
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []) or []:
            key = obj.get("Key")
            if key:
                keys.append(key)
        token = resp.get("NextContinuationToken")
        if not (resp.get("IsTruncated") and token):
            break
    return keys


def prune_raw_releases(
    config: dict,
    *,
    s3_client=None,
    bucket: str | None = None,
    keep_release: str | None = None,
) -> list[str]:
    """Delete raw vertices+edges for all but the newest N releases.

    Returns the release names whose raw objects were deleted.

    Absolute safety rules:
      - Only ever touches keys under `cc/raw/`. `cc/derived/` is the durable
        asset and is NEVER deleted by this code at any config value — a
        rebuilt derived SQLite costs 25-35 minutes and a re-download; a lost
        one costs the historical record.
      - Never deletes raw for `keep_release` or for the currently-configured
        `latest_release`, regardless of how they sort.
      - Skips any release name `_parse_release` can't parse — an unknown
        naming scheme is a reason to leave data alone, not to delete it.
      - `prune_raw_after_releases == 0` disables pruning entirely.
      - Fails soft: a prune problem is logged as a warning and never fails
        the run. The refresh already succeeded by this point; leftover raw
        costs ~$0.21/month, a non-zero exit costs an operator's evening.
    """
    refresh = _refresh_config(config)
    keep_n = int(refresh.get("prune_raw_after_releases", 0))
    if keep_n <= 0:
        logger.info("Raw pruning disabled (prune_raw_after_releases=%d)", keep_n)
        return []

    if s3_client is None or bucket is None:
        from scripts import diff
        s3_client = s3_client or diff._r2_client()
        bucket = bucket or diff._bucket()

    deleted: list[str] = []
    try:
        keys = _list_keys(s3_client, bucket, "cc/raw/")
    except Exception as exc:
        logger.warning("Could not list cc/raw/ for pruning: %s", exc)
        return deleted

    by_release: dict[str, list[str]] = {}
    for key in keys:
        parts = key.split("/")
        if len(parts) < 4 or parts[0] != "cc" or parts[1] != "raw":
            continue
        by_release.setdefault(parts[2], []).append(key)

    protected = {
        r for r in (keep_release, _cc_config(config).get("latest_release")) if r
    }
    parseable = []
    for name in by_release:
        if _parse_release(name) is None:
            logger.warning(
                "Skipping unparseable raw release directory cc/raw/%s/ — "
                "leaving it alone", name,
            )
            continue
        parseable.append(name)

    newest_first = sorted(parseable, key=_release_sort_key, reverse=True)
    protected.update(newest_first[:keep_n])
    logger.info(
        "Raw retention: keeping %s; candidates for deletion %s",
        sorted(protected), [r for r in newest_first if r not in protected],
    )

    for name in newest_first:
        if name in protected:
            continue
        try:
            for key in by_release[name]:
                # Belt-and-braces: even though we listed with the cc/raw/
                # prefix, never issue a delete for anything outside it.
                if not key.startswith("cc/raw/"):
                    logger.error("Refusing to delete out-of-scope key %s", key)
                    continue
                logger.info("Deleting raw object r2://%s/%s", bucket, key)
                s3_client.delete_object(Bucket=bucket, Key=key)
            deleted.append(name)
        except Exception as exc:
            logger.warning("Prune failed for raw release %s: %s", name, exc)
            return deleted
    return deleted


def prune_local_cache(config: dict, keep_release: str) -> list[str]:
    """Delete every cached *.sqlite except `keep_release`'s.

    The cache lives in whatever directory cc_backlinks resolves, so the
    pipeline and this pruner can never disagree about which files matter.
    6.6 GB per monthly release fills a box quickly. Fails soft.
    """
    if not _refresh_config(config).get("prune_local_cache", False):
        logger.info("Local cache pruning disabled")
        return []

    from scripts.enrichment import cc_backlinks

    removed: list[str] = []
    try:
        cache_dir = cc_backlinks._resolve_cache_dir()
        if not cache_dir.exists():
            return removed
        for path in sorted(cache_dir.glob("*.sqlite")):
            if path.stem == keep_release:
                continue
            logger.info("Deleting stale local CC cache file %s", path)
            path.unlink()
            removed.append(path.name)
    except Exception as exc:
        logger.warning("Local cache prune failed: %s", exc)
    return removed


# ---------------------------------------------------------------------------
# Result file — what the daily report and the shell wrapper read
# ---------------------------------------------------------------------------


def _result_path(config: dict) -> Path:
    raw = str(
        _refresh_config(config).get("result_path", "scripts/state/cc_refresh_result.json")
    )
    path = Path(raw)
    return path if path.is_absolute() else _REPO_ROOT / path


def write_result(config: dict, payload: dict) -> None:
    """Atomically write the run result. Never raises.

    A failure to write the result must not change the exit code — the exit
    code describes the refresh, not the bookkeeping. A missing result file
    shows up as staleness in the daily report anyway.
    """
    payload = dict(payload)
    payload.setdefault(
        "finished_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    try:
        path = _result_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=path.name + ".", dir=str(path.parent), suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info("Wrote refresh result %s (%s)", path, payload.get("action"))
    except Exception as exc:
        logger.warning("Could not write refresh result: %s", exc)


# ---------------------------------------------------------------------------
# --auto orchestration
# ---------------------------------------------------------------------------


def _verify_install_prune(
    *,
    release: str,
    config: dict,
    config_path: Path,
    s3,
    bucket: str,
) -> dict:
    """Verify → swap config → prune, in that order and no other.

    The order is the whole point: nothing is installed before the artifact
    in R2 has been proven readable and sane, and nothing is pruned before
    the new release is the installed one.
    """
    measured = verify_derived_release(
        release, config, s3_client=s3, bucket=bucket,
    )
    previous = install_release(release, config_path)
    pruned_raw = prune_raw_releases(
        config, s3_client=s3, bucket=bucket, keep_release=release,
    )
    pruned_local = prune_local_cache(config, release)
    return {
        "release": release,
        "previous_release": previous,
        "rows": measured["rows"],
        "canaries": measured["canaries"],
        "pruned_raw_releases": pruned_raw,
        "pruned_local_cache": pruned_local,
    }


def _run_auto(
    *,
    config: dict,
    config_path: Path,
    s3_factory: Callable[[], tuple],
    workdir_arg: str | None,
    force: bool,
) -> int:
    """The automated weekly path. See the module docstring for exit codes."""
    blocked = _refresh_blocked_reason(config)
    if blocked:
        logger.warning("CC refresh skipped: %s", blocked)
        write_result(config, {"action": "skipped", "reason": blocked})
        return 0

    newest = discover_latest_release(config)
    if newest is None:
        logger.error(
            "CC release discovery failed — no window in range has both raw "
            "files. Will retry on the next timer tick.",
        )
        write_result(config, {"action": "discovery_failed"})
        return 1

    current = _cc_config(config).get("latest_release", "")
    if newest == current:
        logger.info("CC release up to date (%s)", newest)
        write_result(config, {"action": "noop", "release": newest})
        return 0

    logger.info("CC release %s is newer than installed %s", newest, current or "(none)")
    s3, bucket = s3_factory()

    workdir_ctx: tempfile.TemporaryDirectory | None = None
    if workdir_arg:
        workdir = Path(workdir_arg)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir_ctx = tempfile.TemporaryDirectory(prefix=f"cc-refresh-{newest}-")
        workdir = Path(workdir_ctx.name)

    try:
        _ensure_disk_space(
            workdir, required_bytes=_required_disk_bytes(newest, config),
        )
        local_raw = _phase_download_and_upload_raw(
            s3=s3, bucket=bucket, release=newest, workdir=workdir, force=force,
        )
        _phase_build_and_upload_derived(
            s3=s3, bucket=bucket, release=newest, workdir=workdir,
            local_raw=local_raw, force=force,
        )
    except Exception as exc:
        logger.error("CC refresh build phase failed for %s: %s", newest, exc)
        write_result(
            config,
            {"action": "build_failed", "release": newest, "reason": str(exc)},
        )
        return 1
    finally:
        if workdir_ctx is not None:
            workdir_ctx.cleanup()

    try:
        summary = _verify_install_prune(
            release=newest, config=config, config_path=config_path,
            s3=s3, bucket=bucket,
        )
    except VerificationError as exc:
        logger.error(
            "CC release %s FAILED verification (%s) — config NOT swapped, "
            "nothing pruned. The pipeline keeps using %s.",
            newest, exc, current or "(none)",
        )
        write_result(
            config,
            {
                "action": "verification_failed",
                "release": newest,
                "reason": str(exc),
                "failed_check": exc.check,
            },
        )
        return 1
    except Exception as exc:
        logger.error("CC release %s install failed: %s", newest, exc)
        write_result(
            config,
            {"action": "install_failed", "release": newest, "reason": str(exc)},
        )
        return 1

    summary["action"] = "installed"
    logger.info(
        "CC release %s installed (was %s); %d rows",
        newest, summary["previous_release"] or "(none)", summary["rows"],
    )
    write_result(config, summary)
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.cc_refresh",
        description="Refresh a Common Crawl domain-webgraph release on R2.",
    )
    parser.add_argument(
        "--release",
        default=None,
        help="Release name, e.g. cc-main-2026-feb-mar-apr. Required unless "
             "--auto or --discover-only is given.",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Local working directory for downloads. Defaults to a tempdir "
             "cleaned up after the run. Specify to keep raw files around for "
             "debugging or to point at a larger volume.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-do every phase even if R2 already has the artifacts.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Download + upload raw only; do not build the derived SQLite.",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="Build + upload derived only; assume raw is already on R2.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Discover the newest published release, build it if it is newer "
             "than the installed one, verify it, swap config, prune raw. "
             "The path the weekly systemd timer takes.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="After the build phases, verify the derived artifact in R2 and "
             "swap cc_backlinks.latest_release to --release, then prune.",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Print the newest published release and exit. No R2 credentials "
             "needed. Exits 1 if nothing is available.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to config.json. Default: {_DEFAULT_CONFIG_PATH}",
    )
    args = parser.parse_args(argv)

    if args.download_only and args.build_only:
        parser.error("--download-only and --build-only are mutually exclusive")
    if args.auto:
        # --auto owns release selection and runs both phases; combining it
        # with a manual release or a half-run would make the result file lie.
        for flag, value in (
            ("--release", args.release),
            ("--download-only", args.download_only),
            ("--build-only", args.build_only),
        ):
            if value:
                parser.error(f"--auto and {flag} are mutually exclusive")
    if args.install and args.download_only:
        # Nothing to verify: --download-only never builds the derived SQLite.
        parser.error("--install and --download-only are mutually exclusive")
    if not args.release and not (args.auto or args.discover_only):
        parser.error("--release is required unless --auto or --discover-only")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    config_path = Path(args.config) if args.config else _DEFAULT_CONFIG_PATH
    config = _load_config(config_path)

    if args.discover_only:
        newest = discover_latest_release(config)
        if newest is None:
            logger.error("No available CC release found")
            return 1
        print(newest)
        return 0

    def _r2() -> tuple:
        """Build the R2 client, validating secrets first so we fail before a
        multi-GB download rather than in the middle of one."""
        # Late import so this module can be unit-tested without R2 env vars set.
        from scripts import diff
        from scripts import env_check

        missing = [
            v for v in (
                "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME",
            )
            if not os.environ.get(v)
        ]
        if missing:
            raise env_check.MissingEnvVarsError(missing)
        return diff._r2_client(), diff._bucket()

    if args.auto:
        return _run_auto(
            config=config,
            config_path=config_path,
            s3_factory=_r2,
            workdir_arg=args.workdir,
            force=args.force,
        )

    s3, bucket = _r2()

    workdir_ctx: tempfile.TemporaryDirectory | None = None
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir_ctx = tempfile.TemporaryDirectory(prefix=f"cc-refresh-{args.release}-")
        workdir = Path(workdir_ctx.name)

    try:
        # Safety check — abort BEFORE any download if the volume can't hold
        # raw + DuckDB spill + derived. Sized from this release's actual
        # bytes; --build-only needs it too, because the spill is the big
        # term and it happens on this volume either way.
        _ensure_disk_space(
            workdir, required_bytes=_required_disk_bytes(args.release, config),
        )

        local_raw: dict[str, Path] = {}
        if not args.build_only:
            local_raw = _phase_download_and_upload_raw(
                s3=s3, bucket=bucket, release=args.release,
                workdir=workdir, force=args.force,
            )

        if not args.download_only:
            _phase_build_and_upload_derived(
                s3=s3, bucket=bucket, release=args.release,
                workdir=workdir, local_raw=local_raw, force=args.force,
            )
    finally:
        if workdir_ctx is not None:
            workdir_ctx.cleanup()

    if args.install:
        # Same gate as --auto: verify the artifact in R2, then swap, then
        # prune. A manual install must be no less safe than an automated one.
        try:
            summary = _verify_install_prune(
                release=args.release, config=config, config_path=config_path,
                s3=s3, bucket=bucket,
            )
        except VerificationError as exc:
            logger.error(
                "CC release %s FAILED verification (%s) — config NOT swapped",
                args.release, exc,
            )
            return 1
        logger.info(
            "CC release %s installed (was %s); %d rows",
            args.release, summary["previous_release"] or "(none)", summary["rows"],
        )

    logger.info("cc_refresh complete for release %s", args.release)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
