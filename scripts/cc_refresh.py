"""Common Crawl domain-webgraph refresh tool — standalone capability.

Downloads a Common Crawl monthly domain-webgraph release from
data.commoncrawl.org, uploads the raw artifacts to R2 IA tier, builds
a derived SQLite mapping `apex_domain → source_domain_count` via DuckDB,
and uploads the SQLite to R2 Standard tier.

NOT wired into the daily pipeline as of 2026-05-13 — see STATE.md
"Common Crawl integration (standalone)" section for context. The
intent is to validate the standalone capability against real CC data
before deciding how to integrate scoring + display.

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

Operational footprint:

    - Local disk needed during a default run: ~22 GiB (21 GiB raw + 1.5 GiB
      derived). Validated up front via shutil.disk_usage; the script aborts
      before download if free space is insufficient.
    - Wall-clock on OVH KS-6: ~5 min download + ~15-25 min DuckDB build +
      ~2 min upload ≈ 25-35 min end-to-end.
    - Bandwidth: 21 GiB ingress from data.commoncrawl.org (free, CloudFront)
      + 22 GiB egress to R2 (free, R2 ingress is also free).
    - R2 storage cost per release: ~$0.23/month (21 GiB IA + 1.5 GiB Standard).
      Accumulation: never delete old releases per the strategic decision
      logged in STATE.md.

Environment:

    Reads the same R2 secrets the pipeline uses — R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME. No CZDS or
    enrichment-API credentials required.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
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
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.cc_refresh",
        description="Refresh a Common Crawl domain-webgraph release on R2.",
    )
    parser.add_argument(
        "--release",
        required=True,
        help="Release name, e.g. cc-main-2026-feb-mar-apr",
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
    args = parser.parse_args(argv)

    if args.download_only and args.build_only:
        parser.error("--download-only and --build-only are mutually exclusive")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    # Late import so this module can be unit-tested without R2 env vars set.
    from scripts import diff
    from scripts import env_check

    # Validate R2 secrets up front rather than fail mid-download.
    missing = [
        v for v in
        ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
        if not os.environ.get(v)
    ]
    if missing:
        raise env_check.MissingEnvVarsError(missing)

    s3 = diff._r2_client()
    bucket = diff._bucket()

    workdir_ctx: tempfile.TemporaryDirectory | None = None
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir_ctx = tempfile.TemporaryDirectory(prefix=f"cc-refresh-{args.release}-")
        workdir = Path(workdir_ctx.name)

    try:
        # Safety check — abort early if disk is too small for the raw files.
        # 25 GiB covers the 21 GiB edges + 1 GiB vertices + 1.5 GiB derived +
        # DuckDB spill headroom.
        _ensure_disk_space(workdir, required_bytes=25 * 1024 * 1024 * 1024)

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

        logger.info("cc_refresh complete for release %s", args.release)
        return 0
    finally:
        if workdir_ctx is not None:
            workdir_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
