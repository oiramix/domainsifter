"""Rebuild shortlist, step 1: which domains link to promising available domains.

WHY
---
`cc_source_domain_count` says HOW MANY distinct domains link to a candidate
in the Common Crawl domain graph, not WHICH ones. Judging whether those
links are editorial and on-topic (step 2, not built) needs the referring
domain names. This tool produces them.

STANDALONE, BY HAND
-------------------
Not wired into pipeline.py, send_report.py or cc_refresh.py. Run it once by
hand after the daily run (it refuses inside the cc_refresh blackout window
or while the pipeline unit is active, unless --force):

    python -m scripts.rebuild_referrers

WHAT IT DOES
------------
1. Reads the pipeline's available-set handoff
   (`rebuild.input_path`, default scripts/state/available_set_latest.jsonl)
   and gates on snapshot_category == "legitimate" AND
   cc_source_domain_count >= `rebuild.min_cc_domains`. Logs the count
   distribution over `rebuild.distribution_buckets` so the gate can be
   calibrated.
2. Lists which Common Crawl releases still have raw vertices + edges in R2
   under cc/raw/{release}/ (cc_refresh prunes raw to the newest
   `prune_raw_after_releases`), and uses `cc_backlinks.latest_release` if
   it is retained, else the newest retained release.
3. Downloads that release's raw vertices + edges from R2 into a temp dir and
   lets DuckDB STREAM them: one pass over vertices to find the gated apexes'
   ids, one pass over edges keeping only edges whose target is one of those
   ids, one more pass over vertices to name the referrers. The graph is
   never loaded into memory; DuckDB's memory limit is cc_refresh's.
   Nothing is downloaded when the gate passes zero domains.
4. Writes `rebuild.output_path` (default
   scripts/state/rebuild_referrers_latest.jsonl), one line per gated apex:

       {"name": "marketglow.com", "cc_source_domain_count": 27,
        "release": "cc-main-2026-jul-aug-sep", "referrers_total": 31,
        "referrers": ["tideblock.org", ...]}

   `referrers` holds up to `rebuild.max_referrers`, the referring domains
   with the most hosts (CC num_hosts, a size proxy) first. `referrers_total`
   is the full count in THIS release, which can differ from
   cc_source_domain_count when that was computed from another release. An
   apex absent from the release's graph gets referrers_total 0 and [].

The file is private (scripts/state/ is gitignored) and is overwritten each run.

Reuses cc_refresh's R2 and release helpers (_list_keys, _r2_raw_key,
_r2_object_exists, _download_from_r2, _parse_release, _release_sort_key,
_ensure_disk_space, _atomic_write_text, _in_blackout,
_systemd_unit_active) and its DuckDB memory limit. The read_csv column
layout matches cc_refresh._build_derived_sqlite.

Exit codes: 0 done (including "nothing passed the gate"), 1 failed,
2 refused by the blackout / pipeline-active guard.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from scripts import cc_refresh

logger = logging.getLogger(__name__)

_FILE_KINDS = ("vertices", "edges")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _rebuild_config(config: dict) -> dict:
    block = config.get("rebuild")
    return block if isinstance(block, dict) else {}


def _blocked_reason(
    config: dict,
    *,
    now: datetime | None = None,
    unit_active_fn: Callable[[str], bool] | None = None,
) -> str | None:
    """Why this run must not start now, or None. Same window and unit as
    cc_refresh: the daily pipeline and the archive timer own the box then."""
    refresh = cc_refresh._refresh_config(config)
    now = now or datetime.now(timezone.utc)
    start = int(refresh.get("blackout_start_utc_hour", 0))
    end = int(refresh.get("blackout_end_utc_hour", 0))
    if cc_refresh._in_blackout(now.hour, start, end):
        return (
            f"inside the blackout window [{start:02d}:00, {end:02d}:00) UTC "
            f"(now {now.hour:02d}:xx UTC)"
        )
    unit = str(refresh.get("pipeline_unit", "") or "")
    if unit and (unit_active_fn or cc_refresh._systemd_unit_active)(unit):
        return f"{unit} is currently active"
    return None


# ---------------------------------------------------------------------------
# Gate + distribution (pure)
# ---------------------------------------------------------------------------


def _cc_count(record: dict) -> int | None:
    value = record.get("cc_source_domain_count")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def load_records(path: Path) -> list[dict]:
    """JSONL records from the available-set handoff. Corrupt lines are
    skipped with a warning; a missing file raises."""
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("%s:%d is not JSON (%s); skipped", path, lineno, exc)
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def gate(records: list[dict], min_cc_domains: int) -> list[dict]:
    """Legitimate records with cc_source_domain_count >= min_cc_domains, as
    [{"name", "cc_source_domain_count"}], one per apex (highest count kept),
    sorted by count desc then name."""
    best: dict[str, int] = {}
    for rec in records:
        if rec.get("snapshot_category") != "legitimate":
            continue
        count = _cc_count(rec)
        if count is None or count < min_cc_domains:
            continue
        name = str(rec.get("domain") or rec.get("name") or "").strip().lower()
        if name and count > best.get(name, -1):
            best[name] = count
    return [
        {"name": name, "cc_source_domain_count": count}
        for name, count in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def distribution(records: list[dict], buckets: list[int]) -> dict[str, Any]:
    """Counts that calibrate min_cc_domains: how many legitimate domains are
    in the graph at all, and how many reach each bucket threshold."""
    legit = [r for r in records if r.get("snapshot_category") == "legitimate"]
    counts = [c for c in (_cc_count(r) for r in legit) if c is not None]
    categories: dict[str, int] = {}
    for rec in records:
        key = str(rec.get("snapshot_category"))
        categories[key] = categories.get(key, 0) + 1
    return {
        "records": len(records),
        "categories": dict(sorted(categories.items())),
        "legitimate": len(legit),
        "legitimate_in_graph": len(counts),
        "legitimate_at_least": {str(b): sum(1 for c in counts if c >= b) for b in buckets},
    }


# ---------------------------------------------------------------------------
# Release selection (R2 listing via cc_refresh helpers)
# ---------------------------------------------------------------------------


def retained_releases(s3: Any, bucket: str) -> list[str]:
    """Releases with BOTH raw files under cc/raw/, oldest first. Names that
    are not canonical release names are ignored."""
    files: dict[str, set[str]] = {}
    for key in cc_refresh._list_keys(s3, bucket, "cc/raw/"):
        for kind in _FILE_KINDS:
            parts = key.split("/")
            if len(parts) == 4 and key == cc_refresh._r2_raw_key(parts[2], kind):
                files.setdefault(parts[2], set()).add(kind)
    complete = [
        release for release, kinds in files.items()
        if kinds == set(_FILE_KINDS) and cc_refresh._parse_release(release) is not None
    ]
    return sorted(complete, key=cc_refresh._release_sort_key)


def choose_release(retained: list[str], config: dict) -> str | None:
    """The configured latest release when its raw is retained, else the
    newest retained one, else None."""
    preferred = cc_refresh._cc_config(config).get("latest_release")
    if preferred in retained:
        return preferred
    return retained[-1] if retained else None


# ---------------------------------------------------------------------------
# The streaming query
# ---------------------------------------------------------------------------


def _flip(domain: str) -> str:
    """example.com <-> com.example (CC stores domains reversed)."""
    return ".".join(reversed(domain.split(".")))


def find_referrers(
    vertices_path: Path,
    edges_path: Path,
    names: list[str],
    max_referrers: int,
) -> dict[str, tuple[int, list[str]]]:
    """{apex: (referrers_total, [top referrers])} for every apex found in the
    graph. Apexes not in the vertices file are absent from the result.

    DuckDB scans the gzipped TSVs in a streaming fashion; only the gated
    apexes' ids, their inbound edges and those referrers' names are kept.
    """
    import duckdb  # lazy, as in cc_refresh

    vertex_cols = "{'id': 'INTEGER', 'reversed_domain': 'VARCHAR', 'num_hosts': 'INTEGER'}"
    edge_cols = "{'from_id': 'INTEGER', 'to_id': 'INTEGER'}"
    vertices = f"read_csv(?, delim='\t', header=false, columns={vertex_cols})"
    edges = f"read_csv(?, delim='\t', header=false, columns={edge_cols})"

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"SET memory_limit='{cc_refresh._DUCKDB_MEMORY_LIMIT}'")
        con.execute("CREATE TEMP TABLE targets (reversed_domain VARCHAR)")
        con.executemany(
            "INSERT INTO targets VALUES (?)", [[_flip(n)] for n in names],
        )

        started = time.monotonic()
        con.execute(
            f"""
            CREATE TEMP TABLE target_v AS
            SELECT id, reversed_domain FROM {vertices}
            WHERE reversed_domain IN (SELECT reversed_domain FROM targets)
            """,
            [str(vertices_path)],
        )
        found = con.execute("SELECT COUNT(*) FROM target_v").fetchone()[0]
        logger.info(
            "Vertices pass: %d of %d gated apexes are in the graph (%.1fs)",
            found, len(names), time.monotonic() - started,
        )
        if not found:
            return {}

        started = time.monotonic()
        con.execute(
            f"""
            CREATE TEMP TABLE hits AS
            SELECT DISTINCT from_id, to_id FROM {edges}
            WHERE to_id IN (SELECT id FROM target_v) AND from_id <> to_id
            """,
            [str(edges_path)],
        )
        edge_count = con.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
        logger.info(
            "Edges pass: %d inbound edges to gated apexes (%.1fs)",
            edge_count, time.monotonic() - started,
        )

        started = time.monotonic()
        con.execute(
            f"""
            CREATE TEMP TABLE ref_v AS
            SELECT id, reversed_domain, num_hosts FROM {vertices}
            WHERE id IN (SELECT from_id FROM hits)
            """,
            [str(vertices_path)],
        )
        logger.info("Referrer names pass (%.1fs)", time.monotonic() - started)

        rows = con.execute(
            """
            SELECT target, referrer, total FROM (
                SELECT t.reversed_domain AS target,
                       r.reversed_domain AS referrer,
                       COUNT(*) OVER (PARTITION BY t.id) AS total,
                       row_number() OVER (
                           PARTITION BY t.id
                           ORDER BY r.num_hosts DESC, r.reversed_domain
                       ) AS rn
                FROM hits h
                JOIN target_v t ON h.to_id = t.id
                JOIN ref_v r ON h.from_id = r.id
            ) WHERE rn <= ?
            ORDER BY target, rn
            """,
            [max_referrers],
        ).fetchall()
        result: dict[str, tuple[int, list[str]]] = {}
        for target_rev, referrer_rev, total in rows:
            apex = _flip(target_rev)
            _, refs = result.setdefault(apex, (int(total), []))
            refs.append(_flip(referrer_rev))
        for rev_name, in con.execute("SELECT reversed_domain FROM target_v").fetchall():
            result.setdefault(_flip(rev_name), (0, []))
        return result
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _peak_rss_mib() -> float | None:
    """Peak resident memory of this process (DuckDB runs in-process)."""
    try:
        import resource
    except ImportError:  # Windows dev box
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _write_output(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    cc_refresh._atomic_write_text(path, text)


def run(
    config: dict,
    *,
    input_path: Path,
    output_path: Path,
    min_cc_domains: int,
    release: str | None,
    s3_factory: Callable[[], tuple[Any, str]],
) -> int:
    rcfg = _rebuild_config(config)
    max_referrers = int(rcfg.get("max_referrers", 100))
    buckets = [int(b) for b in rcfg.get("distribution_buckets", [1, 5, 10, 20, 50, 100])]

    records = load_records(input_path)
    logger.info(
        "Distribution of %s: %s",
        input_path, json.dumps(distribution(records, buckets), sort_keys=True),
    )
    gated = gate(records, min_cc_domains)
    logger.info(
        "Gate (legitimate AND cc_source_domain_count >= %d): %d apex(es) — %s",
        min_cc_domains, len(gated),
        ", ".join(f"{g['name']}={g['cc_source_domain_count']}" for g in gated) or "none",
    )

    s3, bucket = s3_factory()
    retained = retained_releases(s3, bucket)
    logger.info("Raw releases retained in r2://%s/cc/raw/: %s", bucket, retained or "none")
    if release is None:
        release = choose_release(retained, config)
    elif release not in retained:
        logger.error("Release %s has no complete raw in R2 (retained: %s)", release, retained)
        return 1
    if release is None:
        logger.error("No release with both raw files is retained in R2; nothing to read")
        return 1
    logger.info("Using release %s", release)

    if not gated:
        _write_output(output_path, [])
        logger.info(
            "Nothing passed the gate; wrote an empty %s and skipped the R2 download",
            output_path,
        )
        return 0

    sizes = {
        kind: cc_refresh._r2_object_exists(s3, bucket, cc_refresh._r2_raw_key(release, kind))[1]
        for kind in _FILE_KINDS
    }
    workdir_cfg = str(rcfg.get("workdir") or "")
    workdir = Path(workdir_cfg) if workdir_cfg else Path(tempfile.gettempdir())
    workdir.mkdir(parents=True, exist_ok=True)
    headroom = float(rcfg.get("disk_headroom_multiplier", 1.5))
    cc_refresh._ensure_disk_space(workdir, int(sum(sizes.values()) * headroom))

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rebuild-referrers-", dir=str(workdir)) as tmp:
        paths = {kind: Path(tmp) / f"{kind}.txt.gz" for kind in _FILE_KINDS}
        for kind in _FILE_KINDS:
            cc_refresh._download_from_r2(
                s3, bucket, cc_refresh._r2_raw_key(release, kind), paths[kind],
            )
        logger.info(
            "Downloaded raw %s (%.2f GiB) in %.1fs",
            release, sum(sizes.values()) / 1024**3, time.monotonic() - started,
        )
        found = find_referrers(
            paths["vertices"], paths["edges"],
            [g["name"] for g in gated], max_referrers,
        )

    out = []
    for g in gated:
        total, refs = found.get(g["name"], (0, []))
        out.append({
            "name": g["name"],
            "cc_source_domain_count": g["cc_source_domain_count"],
            "release": release,
            "referrers_total": total,
            "referrers": refs,
        })
    _write_output(output_path, out)
    missing = [g["name"] for g in gated if g["name"] not in found]
    peak = _peak_rss_mib()
    logger.info(
        "Wrote %d apex(es) to %s (%d not in %s's graph: %s). Elapsed %.1fs, peak RSS %s",
        len(out), output_path, len(missing), release, ", ".join(missing) or "none",
        time.monotonic() - started,
        f"{peak:.0f} MiB" if peak is not None else "n/a",
    )
    return 0


def main(
    argv: list[str] | None = None,
    *,
    s3_factory: Callable[[], tuple[Any, str]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--config", default=str(cc_refresh._DEFAULT_CONFIG_PATH))
    parser.add_argument("--input", help="override rebuild.input_path")
    parser.add_argument("--output", help="override rebuild.output_path")
    parser.add_argument("--min-cc-domains", type=int, help="override rebuild.min_cc_domains")
    parser.add_argument("--release", help="use this retained release instead of the default")
    parser.add_argument(
        "--force", action="store_true",
        help="run even inside the blackout window or while the pipeline is active",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    config = cc_refresh._load_config(args.config)
    rcfg = _rebuild_config(config)

    if not args.force:
        reason = _blocked_reason(config)
        if reason:
            logger.error("Refusing to run: %s. Use --force to override.", reason)
            return 2

    def _default_s3() -> tuple[Any, str]:
        from scripts import diff

        return diff._r2_client(), diff._bucket()

    repo = cc_refresh._REPO_ROOT

    def _path(override: str | None, key: str, default: str) -> Path:
        p = Path(override or rcfg.get(key) or default)
        return p if p.is_absolute() else repo / p

    try:
        return run(
            config,
            input_path=_path(args.input, "input_path", "scripts/state/available_set_latest.jsonl"),
            output_path=_path(args.output, "output_path", "scripts/state/rebuild_referrers_latest.jsonl"),
            min_cc_domains=(
                args.min_cc_domains if args.min_cc_domains is not None
                else int(rcfg.get("min_cc_domains", 20))
            ),
            release=args.release,
            s3_factory=s3_factory or _default_s3,
        )
    except Exception:
        logger.exception("rebuild_referrers failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
