"""Permanent historical archive of registerable dropped domains (private R2).

This is a NEW, separate, NEVER-DELETED strategic asset, distinct from
`state/phase2_overflow.jsonl` (a 14-day-aging work queue — left untouched).
Purpose: lifecycle analysis of the drop market over time — did a domain get
re-registered after dropping, how fast, do low-scoring drops behave
differently from high-scoring ones, etc.

WHAT IT STORES (per run): every RDAP-confirmed-AVAILABLE domain from the run,
REGARDLESS of score or publication — not just the published subset. The
unbiased available-set is the whole point; a low-scoring drop's lifecycle is
as interesting as a high-scoring one's. The above-gate Phase 2 overflow is
deliberately NOT archived — its availability was never checked (unknown), and
unknowns would muddy lifecycle data.

EVENT-BASED, APPEND-ONLY: each archived record is a dated event
("`<domain>` confirmed `<availability_status>` on `<availability_confirmed_date>`").
The same domain reappearing in a later run is a NEW event, never an overwrite,
so drop → re-register → re-drop cycles are visible as a per-domain timeline.
The schema is deliberately shaped so a FUTURE re-check pass (not built here)
can append `availability_status="registered"` events without any migration.

LAYOUT: monthly-partitioned append-only JSONL in the private R2 state bucket,
`state/domain_archive/YYYY-MM.jsonl` (partitioned by the event's
`availability_confirmed_date` month). Small files, cheap appends, query-by-
month. Matches the existing R2 state idiom (`scripts/diff.py` R2 client,
`phase2_ranker.record_overflow` read-modify-write JSONL) — but NEVER aged out.

SOURCE FLAG — be honest about provenance:
  - "live"     captured from a real run via the pipeline's available-set
               emit; the available-set is COMPLETE (every confirmed-available
               domain, not just published).
  - "backfill" reconstructed from past committed `daily-domains.json` files in
               git history. daily-domains.json only ever held the PUBLISHED
               subset (~126/run), NEVER the full available-set, so backfilled
               records are the published subset ONLY. The flag lets queries
               distinguish "we have published history here" from "we have full
               available-set history here". Complete available-set capture
               begins from the first live run forward.

ARCHITECTURE: this module is a SEPARATE post-pipeline process. The pipeline
itself only EMITS the available-set to a local handoff file
(`scripts/state/available_set_latest.jsonl`, gitignored) via
`emit_available_set` — a small, defensive write that can never break or delay
a run. This module then reads that handoff and appends to R2, with its own
exit code, chained after the pipeline as a non-fatal step in run-daily.sh
(same idiom as the newsletter). It is read-only relative to the pipeline.

CLI:
    python -m scripts.domain_archive                 # live: read handoff, append
    python -m scripts.domain_archive --backfill      # backfill from git history
    python -m scripts.domain_archive --dry-run       # build records, skip R2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger("scripts.domain_archive")

# R2 layout — private state bucket, monthly partitions, never aged out.
ARCHIVE_KEY_PREFIX = "state/domain_archive/"

# Local handoff file the pipeline writes (the full available-set for the run)
# and this post-process reads. Under scripts/state/* which is gitignored —
# this is private data, never committed. Path is config-overridable.
DEFAULT_HANDOFF_PATH = "scripts/state/available_set_latest.jsonl"

# Committed pipeline output used as the backfill source (published subset only).
DAILY_JSON_REL_PATH = "src/data/daily-domains.json"

# Enrichment / signal fields carried verbatim into each archive record when
# present on the source candidate. "Keep everything the run had" — storage is
# negligible and richer rows make better lifecycle queries. Absent keys are
# simply omitted (not forced to null) so backfilled rows from older payloads
# don't sprout fields the run never computed.
_SIGNAL_FIELDS = (
    "wayback_snapshots",
    "wayback_last_snapshot",
    "wayback_unknown",
    "open_page_rank",
    "cert_history",
    "cc_source_domain_count",
    "snapshot_category",
    "snapshot_classifier_version",
    "previous_registrar",
    "rdap_status",
    "rdap_expiration",
)


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def _signals(src: dict) -> dict:
    """Copy whatever enrichment signals are present on `src`. Absent → omitted."""
    return {k: src[k] for k in _SIGNAL_FIELDS if k in src}


def _confirmed_date(cand: dict, today: date) -> str:
    """The anchor event date: when availability was confirmed. Prefer the
    RDAP-stamped `availability_verified_at` (trimmed to YYYY-MM-DD); fall back
    to today. Both produce a stable per-run date for the timeline."""
    verified = cand.get("availability_verified_at")
    if isinstance(verified, str) and len(verified) >= 10:
        return verified[:10]
    return today.isoformat()


def build_live_record(
    cand: dict,
    *,
    was_published: bool,
    config: dict,
    today: date,
) -> dict:
    """Build one `source="live"` archive record from a RDAP-confirmed-available
    candidate. Computes the final `score` (if not already scored) and `verdict`
    for EVERY available domain — including the low-scoring tail that never
    reached the published payload — so the archive schema is uniform.

    Imports of score/output are local so this module's import graph stays light
    for callers that only need the R2-append side (and to avoid any chance of
    an import cycle with the pipeline)."""
    from scripts import output, score

    existing = cand.get("score")
    final_score = existing if existing is not None else score.score_candidate(cand, config)
    verdict = output._compute_verdict(cand, config)

    record = {
        "domain": cand.get("name", ""),
        "tld": cand.get("tld", ""),
        "drop_date": cand.get("dropped_date"),
        "availability_status": "available",
        "availability_confirmed_date": _confirmed_date(cand, today),
        "phase2_score": cand.get("phase2_score"),
        "score": final_score,
        "verdict": verdict,
        "was_published": bool(was_published),
        "source": "live",
    }
    record.update(_signals(cand))
    return record


def build_backfill_record(entry: dict, *, confirmed_date: str) -> dict:
    """Build one `source="backfill"` record from a published daily-domains.json
    domain entry. was_published is True by definition (it was IN the published
    file). score/verdict/phase2_score are taken as-is from the entry (older
    payloads may lack verdict/phase2_score → omitted/None)."""
    record = {
        "domain": entry.get("name", ""),
        "tld": entry.get("tld", ""),
        "drop_date": entry.get("dropped_date"),
        "availability_status": "available",
        "availability_confirmed_date": confirmed_date,
        "phase2_score": entry.get("phase2_score"),
        "score": entry.get("score"),
        "verdict": entry.get("verdict"),
        "was_published": True,
        "source": "backfill",
    }
    record.update(_signals(entry))
    return record


def build_backfill_records(
    snapshots: Iterable[tuple[str, dict]],
) -> list[dict]:
    """Reconstruct backfill records from a chronological stream of
    (commit_date, daily-domains payload) snapshots.

    Dedup key is (domain, first_seen_date): a carryover domain appears in many
    consecutive daily snapshots, but that is the SAME drop event, not a fresh
    availability confirmation — so we record it once, anchored on its
    first_seen_date (the actual drop/availability anchor). A domain that drops,
    gets re-registered, then re-drops later gets a NEW first_seen_date and
    therefore a second backfill event — exactly the timeline we want. Older
    payloads without first_seen_date fall back to the commit date.

    First occurrence wins (snapshots are processed in the given order, which the
    caller supplies chronologically)."""
    seen: set[tuple[str, str]] = set()
    records: list[dict] = []
    for commit_date, payload in snapshots:
        domains = payload.get("domains") if isinstance(payload, dict) else None
        if not isinstance(domains, list):
            continue
        for entry in domains:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            if not name:
                continue
            anchor = entry.get("first_seen_date") or commit_date
            key = (name, anchor)
            if key in seen:
                continue
            seen.add(key)
            records.append(build_backfill_record(entry, confirmed_date=anchor))
    return records


# ---------------------------------------------------------------------------
# Available-set emit (pipeline-side; defensive)
# ---------------------------------------------------------------------------


def emit_available_set(
    available: list[dict],
    published_names: set[str],
    config: dict,
    today: date,
    *,
    path: str | os.PathLike | None = None,
) -> int:
    """Write the FULL RDAP-confirmed-available set (every domain the run
    confirmed free to register, regardless of score/publication) to the local
    JSONL handoff file as ready-to-archive `source="live"` records.

    Called by the pipeline AFTER write_output, so `published_names` (the names
    actually in daily-domains.json) is known and `was_published` is accurate.

    Atomic write (tempfile + os.replace). Returns the number of records written.
    The pipeline wraps this in try/except so a failure here can never break or
    delay the run — this function still raises on real errors so that wrapper
    can log them."""
    out_path = Path(path or config.get("available_set_path", DEFAULT_HANDOFF_PATH))
    records = [
        build_live_record(
            cand,
            was_published=cand.get("name", "") in published_names,
            config=config,
            today=today,
        )
        for cand in available
    ]
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=out_path.name + ".", dir=str(out_path.parent), suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp_name, out_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    logger.info(
        "Available-set emit: wrote %d confirmed-available records (%d published) to %s",
        len(records), sum(1 for r in records if r["was_published"]), out_path,
    )
    return len(records)


# ---------------------------------------------------------------------------
# R2 append-only (monthly partitions, event-deduped, NEVER aged out)
# ---------------------------------------------------------------------------


def _month_key(confirmed_date: str) -> str:
    """R2 object key for the month-partition holding `confirmed_date`
    (YYYY-MM-DD → state/domain_archive/YYYY-MM.jsonl). Unparseable dates land
    in an `unknown` partition rather than being dropped."""
    ym = confirmed_date[:7] if isinstance(confirmed_date, str) and len(confirmed_date) >= 7 else "unknown"
    return f"{ARCHIVE_KEY_PREFIX}{ym}.jsonl"


def _event_key(record: dict) -> tuple[str, str, str]:
    """Identity of a single archived event — (domain, confirmed_date, source).
    Dedup on this so re-running a day (or re-running backfill) is idempotent,
    while genuinely new events (a later confirmed_date, or the other source)
    still append. Source is part of the key so a live and a backfill record for
    the same domain+date coexist honestly rather than one masking the other."""
    return (
        record.get("domain", ""),
        record.get("availability_confirmed_date", ""),
        record.get("source", ""),
    )


def _r2_get_object_or_empty(s3: Any, bucket: str, key: str) -> bytes:
    """Read an object from R2; return b'' if it doesn't exist yet. Other errors
    propagate (caller decides whether to swallow)."""
    from botocore.exceptions import ClientError

    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return b""
        raise
    return resp["Body"].read()


def _parse_jsonl(raw: bytes) -> list[dict]:
    out: list[dict] = []
    if not raw:
        return out
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("domain_archive: skipping corrupt JSONL line (%s)", exc)
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _serialize_jsonl(records: list[dict]) -> bytes:
    return ("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)).encode("utf-8")


def append_records(
    records: list[dict],
    *,
    r2_client: Any,
    r2_bucket: str,
) -> int:
    """Append `records` to their month-partitioned R2 JSONL files, append-only
    and event-deduped. For each month touched: read existing → skip records
    whose `_event_key` is already present → append the rest → write back.
    NEVER ages out or deletes. Returns the number of NEW records actually
    appended across all months."""
    if not records:
        return 0

    by_month: dict[str, list[dict]] = {}
    for rec in records:
        by_month.setdefault(_month_key(rec.get("availability_confirmed_date", "")), []).append(rec)

    total_new = 0
    for key, month_records in sorted(by_month.items()):
        existing = _parse_jsonl(_r2_get_object_or_empty(r2_client, r2_bucket, key))
        # Dedup against what's already stored AND within this batch, so a
        # same-day re-run and any intra-batch duplicate both collapse.
        seen = {_event_key(r) for r in existing}
        fresh = []
        for r in month_records:
            k = _event_key(r)
            if k in seen:
                continue
            seen.add(k)
            fresh.append(r)
        if not fresh:
            logger.info("domain_archive: %s — 0 new events (all %d already present)", key, len(month_records))
            continue
        merged = existing + fresh
        r2_client.put_object(
            Bucket=r2_bucket, Key=key,
            Body=_serialize_jsonl(merged), ContentType="application/x-ndjson",
        )
        total_new += len(fresh)
        logger.info("domain_archive: %s — appended %d new events (%d total)", key, len(fresh), len(merged))
    return total_new


# ---------------------------------------------------------------------------
# Git backfill source
# ---------------------------------------------------------------------------


def iter_git_daily_snapshots(
    repo_root: str | os.PathLike,
    json_rel_path: str = DAILY_JSON_REL_PATH,
) -> Iterator[tuple[str, dict]]:
    """Yield (commit_date, payload) for every commit that touched
    daily-domains.json, oldest first. Shells out to git; parse failures are
    skipped with a warning. Kept thin and side-effect-light so backfill logic
    (`build_backfill_records`) can be unit-tested with fake snapshots."""
    repo_root = str(repo_root)
    log = subprocess.run(
        ["git", "-C", repo_root, "log", "--follow", "--reverse",
         "--format=%H %ad", "--date=short", "--", json_rel_path],
        capture_output=True, text=True, check=True,
    )
    for line in log.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        commit_hash, _, commit_date = line.partition(" ")
        show = subprocess.run(
            ["git", "-C", repo_root, "show", f"{commit_hash}:{json_rel_path}"],
            capture_output=True, text=True,
        )
        if show.returncode != 0:
            logger.warning("backfill: cannot read %s at %s (%s)", json_rel_path, commit_hash[:9], show.stderr.strip())
            continue
        try:
            payload = json.loads(show.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("backfill: corrupt JSON at %s (%s)", commit_hash[:9], exc)
            continue
        if isinstance(payload, dict):
            yield commit_date, payload


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _read_handoff(path: str | os.PathLike) -> list[dict]:
    p = Path(path)
    if not p.exists():
        logger.warning("domain_archive: handoff file %s does not exist; nothing to archive", p)
        return []
    return _parse_jsonl(p.read_bytes())


def run_live(
    *,
    handoff_path: str | os.PathLike,
    r2_client: Any,
    r2_bucket: str,
) -> int:
    """Read the pipeline's available-set handoff (already-built live records)
    and append to R2. Returns the number of new events appended."""
    records = _read_handoff(handoff_path)
    if not records:
        return 0
    n = append_records(records, r2_client=r2_client, r2_bucket=r2_bucket)
    logger.info("domain_archive LIVE: %d records in handoff → %d new events archived", len(records), n)
    return n


def run_backfill(
    *,
    repo_root: str | os.PathLike,
    r2_client: Any,
    r2_bucket: str,
) -> int:
    """Reconstruct backfill records from git history of daily-domains.json
    (published subset only, source="backfill") and append to R2."""
    snapshots = list(iter_git_daily_snapshots(repo_root))
    records = build_backfill_records(snapshots)
    logger.info(
        "domain_archive BACKFILL: %d daily snapshots in git → %d deduped published events",
        len(snapshots), len(records),
    )
    n = append_records(records, r2_client=r2_client, r2_bucket=r2_bucket)
    logger.info("domain_archive BACKFILL: %d new events archived", n)
    return n


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="scripts.domain_archive")
    parser.add_argument("--backfill", action="store_true",
                        help="Reconstruct from git history of daily-domains.json (published subset).")
    parser.add_argument("--handoff-path", default=None,
                        help=f"Available-set handoff JSONL (default {DEFAULT_HANDOFF_PATH}).")
    parser.add_argument("--repo-root", default=".", help="Repo root for git backfill.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build records and log counts; do NOT write to R2.")
    args = parser.parse_args(argv)

    handoff_path = args.handoff_path or DEFAULT_HANDOFF_PATH

    if args.dry_run:
        if args.backfill:
            recs = build_backfill_records(list(iter_git_daily_snapshots(args.repo_root)))
        else:
            recs = _read_handoff(handoff_path)
        months: dict[str, int] = {}
        for r in recs:
            months[_month_key(r.get("availability_confirmed_date", ""))] = months.get(
                _month_key(r.get("availability_confirmed_date", "")), 0) + 1
        logger.info("DRY-RUN: %d records across %d month partitions: %s",
                    len(recs), len(months), dict(sorted(months.items())))
        return 0

    try:
        from scripts import diff
        r2_client = diff._r2_client()
        r2_bucket = diff._bucket()
    except Exception as exc:  # pragma: no cover - depends on live R2 env
        logger.error("domain_archive: could not construct R2 client: %s", exc)
        return 1

    try:
        if args.backfill:
            run_backfill(repo_root=args.repo_root, r2_client=r2_client, r2_bucket=r2_bucket)
        else:
            run_live(handoff_path=handoff_path, r2_client=r2_client, r2_bucket=r2_bucket)
    except Exception as exc:  # pragma: no cover - real R2 / git failures
        logger.error("domain_archive: failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
