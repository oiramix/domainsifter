"""One-shot backfill: snapshot-classify entries in daily-domains.json.

Standalone — does not import from scripts.pipeline or scripts.run_daily.
Touches only these on-disk artifacts:
    src/data/daily-domains.json       — read; mutated; rewritten atomically
    src/data/wayback_excerpts.json    — read (if exists); merged; rewritten
                                        atomically (the sidecar from design
                                        decision (h))

Network dependencies:
    archive.org (via scripts.wayback_excerpt) — one fetch per target with
                                                a wayback_last_snapshot
    Anthropic API (via scripts.snapshot_classifier)

Use cases:
    1. First-time backfill: classify every entry that predates the classifier
       rollout. Default mode (no flags).
    2. Catch-up after an Anthropic outage: re-classify entries that ended up
       "unknown" despite having a wayback_last_snapshot. --only-unknown.
    3. Manual re-classification after a prompt change: --force re-classifies
       every entry regardless of existing label.

Output:
    - daily-domains.json gets snapshot_category + snapshot_classifier_version
      per classified entry. wayback_excerpt is stripped out (it lives in the
      sidecar per design decision (h) — keeps the JSON the frontend loads
      small).
    - Toxic entries are EVICTED from daily-domains.json's domains array
      before write (design decision: hard-reject in same commit, don't leave
      labeled-toxic entries in the published list even briefly). The top-
      level counts (domain_count, today_count, carryover_count) are
      recomputed after eviction.
    - wayback_excerpts.json is keyed by domain name; existing entries are
      preserved, this-run entries are added/updated.

Git:
    Live mode writes both files atomically, commits both, and pushes. Token
    in argv only (same pattern as scripts/run-daily.sh + scripts/
    archive_generator.py). Commit body includes the classification summary
    AND lists any evicted toxic names so the history captures the eviction.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from scripts import snapshot_classifier

logger = logging.getLogger("scripts.classify_carryover")

REPO_ROOT = Path(__file__).resolve().parent.parent
DAILY_DOMAINS_PATH = REPO_ROOT / "src" / "data" / "daily-domains.json"
EXCERPTS_SIDECAR_PATH = REPO_ROOT / "src" / "data" / "wayback_excerpts.json"

GITHUB_REPO_URL_TEMPLATE = (
    "https://x-access-token:{token}@github.com/oiramix/domainsifter.git"
)
GIT_USER_NAME = "domainsifter-classifier"
GIT_USER_EMAIL = "99090280+oiramix@users.noreply.github.com"


# --- I/O helpers ------------------------------------------------------------


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s (%s); using default.", path, exc)
        return default


def _atomic_write_json(path: Path, payload) -> None:
    """Temp-file + os.replace pattern. Same shape as output.py / archive_
    generator.py — keeps half-written files from being served by CF Pages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Pure helpers (unit-tested) ---------------------------------------------


def filter_targets(
    domains: list[dict],
    *,
    force: bool,
    only_unknown: bool,
    limit: int | None,
) -> list[dict]:
    """Pick the entries to classify this run.

    Three mutually-exclusive modes (CLI rejects --force + --only-unknown
    together):
        default (force=False, only_unknown=False):
            Entries WITHOUT snapshot_category. Idempotent on re-runs —
            already-classified entries are skipped.
        only_unknown=True:
            Entries with snapshot_category=="unknown" AND wayback_last_snapshot
            set. Catch-up mode for entries that ended up unknown because
            Anthropic was down OR an earlier --dry-run sample chose them.
            Skips truly snapshot-less entries (they will always be unknown).
        force=True:
            Every entry, regardless. For prompt-change reclassification.

    --limit selects the top N by score desc — when the cap bites, we'd
    rather classify the high-impact entries first.
    """
    if only_unknown:
        targets = [
            d for d in domains
            if d.get("snapshot_category") == snapshot_classifier.UNKNOWN_CATEGORY
            and d.get("wayback_last_snapshot")
        ]
    elif force:
        targets = list(domains)
    else:
        targets = [d for d in domains if not d.get("snapshot_category")]

    if limit is not None:
        targets.sort(key=lambda d: -float(d.get("score") or 0))
        targets = targets[:limit]

    return targets


def split_toxic(domains: list[dict]) -> tuple[list[dict], list[str]]:
    """Partition domains into (kept, evicted_names).

    Only snapshot_category=="toxic" is evicted. Parked / empty / unknown /
    legitimate all stay in the list (parked + empty get verdict-downgraded
    in Phase 4 but remain published; unknown is informational; legitimate
    is the good path).
    """
    kept: list[dict] = []
    evicted: list[str] = []
    for d in domains:
        if d.get("snapshot_category") == "toxic":
            evicted.append(d.get("name", "<unknown>"))
        else:
            kept.append(d)
    return kept, evicted


def update_counts(payload: dict) -> dict:
    """Recompute the top-level domain_count / today_count / carryover_count
    after eviction. Preserves every other top-level key (generated_at,
    total_candidates_evaluated, etc.) untouched."""
    domains = payload.get("domains") or []
    payload["domain_count"] = len(domains)
    payload["today_count"] = sum(
        1 for d in domains if (d.get("days_listed") or 0) == 0
    )
    payload["carryover_count"] = len(domains) - payload["today_count"]
    return payload


def build_sidecar_updates(targets: list[dict]) -> dict[str, dict | None]:
    """Map of name → wayback_excerpt for the entries we just touched. Used
    as a delta to merge into the existing sidecar (preserves earlier
    excerpts on entries this run didn't reclassify).

    Includes toxic entries' excerpts deliberately — even though they're
    evicted from daily-domains.json, the sidecar retains the content
    snapshot for future forensics (why was X classified toxic?).
    """
    out: dict[str, dict | None] = {}
    for d in targets:
        name = d.get("name")
        if not name:
            continue
        # Only include entries that were actually classified by this run
        # (snapshot_classifier_version is the proof).
        if d.get("snapshot_classifier_version"):
            out[name] = d.get("wayback_excerpt")
    return out


def strip_inline_excerpts(domains: list[dict]) -> None:
    """Remove the wayback_excerpt key from each domain dict. The excerpt
    lives in the sidecar (design decision (h)) — keeping it inline would
    blow daily-domains.json from ~150 KB to ~500 KB at 188 entries and
    slow down the frontend's first paint for no benefit (the frontend
    never reads it; only archive_generator does)."""
    for d in domains:
        if "wayback_excerpt" in d:
            del d["wayback_excerpt"]


# --- Git operations ---------------------------------------------------------


def _git(args: list[str], *, cwd: Path = REPO_ROOT, check: bool = True):
    """Run a git subprocess. Captures output for the log."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _build_commit_message(
    summary_line: str, evicted: list[str], today: date,
) -> tuple[str, str]:
    """Return (title, body). Body lists evicted toxics for git-log
    discoverability — same pattern as the deepsand.net surgical commit."""
    title = f"data: classify carryover snapshots ({today.isoformat()})"
    body_lines = [summary_line]
    if evicted:
        body_lines.append("")
        body_lines.append(
            f"Evicted {len(evicted)} toxic domain(s) from the published list:"
        )
        for name in sorted(evicted):
            body_lines.append(f"  - {name}")
    return title, "\n".join(body_lines)


def _git_commit_and_push(
    summary_line: str,
    evicted: list[str],
    today: date,
    github_token: str,
) -> None:
    """Stage the two JSONs, commit, push. Token in argv only — never
    written to .git/config. Mirrors scripts/run-daily.sh and
    scripts/archive_generator.py for the auth path."""
    _git(["config", "user.name", GIT_USER_NAME])
    _git(["config", "user.email", GIT_USER_EMAIL])
    _git([
        "add",
        "src/data/daily-domains.json",
        "src/data/wayback_excerpts.json",
    ])
    diff = _git(["diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        logger.info("No staged changes to commit.")
        return

    title, body = _build_commit_message(summary_line, evicted, today)
    _git(["commit", "-m", title + "\n\n" + body])

    push_url = GITHUB_REPO_URL_TEMPLATE.format(token=github_token)
    push = subprocess.run(
        ["git", "push", push_url, "main"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )

    # The token appears in the URL we passed as argv and may be echoed
    # back by git in either stdout or stderr (varies by git version and
    # by the specific error path). Redact before logging anything to
    # avoid leaking the token to journalctl / report emails / log files.
    def _redact(text: str | None) -> str:
        if not text:
            return ""
        return text.replace(github_token, "[REDACTED]")

    sanitized_stdout = _redact(push.stdout).strip()
    sanitized_stderr = _redact(push.stderr).strip()

    # ALWAYS surface git's actual output, regardless of returncode. On
    # success this contains the `<old>..<new>  main -> main` line that
    # proves the ref was actually updated; on the silent-success-no-op
    # path it instead says `Everything up-to-date`; on failure it
    # contains the real error message. The 2026-05-18 first-backfill
    # incident lost ~$0.20 of Anthropic spend because returncode=0
    # was reported but origin/main was unchanged — without the stderr
    # logged, the operator had no signal between "push worked" and
    # "push silently did nothing".
    if sanitized_stdout:
        logger.info("git push stdout:\n%s", sanitized_stdout)
    if sanitized_stderr:
        logger.info("git push stderr:\n%s", sanitized_stderr)

    if push.returncode != 0:
        raise RuntimeError(
            f"git push failed with exit code {push.returncode}. "
            f"Local commit is in place. See `git push stderr` log line above "
            f"for the actual git error; inspect and resolve manually before "
            f"retry."
        )

    # Belt-and-suspenders post-push verification. Catches the silent-
    # success class (returncode 0 but origin/main unchanged) even without
    # knowing the root cause. Fetches the same ref the push targeted,
    # compares local HEAD to origin/main, and raises if they diverge.
    # The fetch is cheap; the RuntimeError preserves the local commit
    # for operator inspection.
    _git(["fetch", "origin", "main"])
    local_head = _git(["rev-parse", "HEAD"]).stdout.strip()
    origin_head = _git(["rev-parse", "origin/main"]).stdout.strip()
    if local_head != origin_head:
        raise RuntimeError(
            f"git push reported success but origin/main is at "
            f"{origin_head[:7]} while local HEAD is {local_head[:7]}. "
            f"The push did not actually update the remote ref. Local "
            f"commit is in place. Inspect remote state, do not retry "
            f"the backfill until this is resolved (a blind retry would "
            f"either silently lose data again OR push a different "
            f"classifier output if the now-stale classifications get "
            f"re-run)."
        )

    logger.info(
        "Pushed commit: %s (origin/main now at %s)", title, origin_head[:7],
    )


# --- Orchestration ---------------------------------------------------------


def _format_summary(target_count: int, counts: dict[str, int]) -> str:
    return (
        f"classified {target_count} entries: "
        f"{counts['legitimate']} legitimate, "
        f"{counts['parked']} parked, "
        f"{counts['toxic']} toxic, "
        f"{counts['empty']} empty, "
        f"{counts['unknown']} unknown"
    )


def run(
    *,
    daily_path: Path,
    excerpts_path: Path,
    force: bool,
    only_unknown: bool,
    limit: int | None,
    dry_run: bool,
    no_push: bool,
    today: date,
    client_factory=snapshot_classifier.make_default_client,
) -> int:
    """Orchestrate one backfill run. Returns the process exit code.

    `client_factory` is injectable for tests (pass a lambda that returns
    a FakeClient or None). Production callers omit it and get the
    default ANTHROPIC_API_KEY-from-env behaviour.
    """
    payload = _load_json(daily_path, default=None)
    if payload is None:
        logger.error("daily-domains.json missing at %s", daily_path)
        return 1

    domains = payload.get("domains") or []
    if not domains:
        logger.info("daily-domains.json has no entries; nothing to classify.")
        return 0

    targets = filter_targets(
        domains, force=force, only_unknown=only_unknown, limit=limit,
    )
    if not targets:
        logger.info("No candidates match the selection criteria; nothing to do.")
        return 0

    logger.info(
        "Selected %d / %d entries for classification.",
        len(targets), len(domains),
    )

    client = client_factory()
    if client is None and not dry_run:
        # Wet run with no key = nothing useful gets done. Abort with a clear
        # error rather than silently writing 'unknown' over real data. Dry
        # run with no key is fine — it just shows the no-op summary.
        logger.error(
            "ANTHROPIC_API_KEY missing — set it in .env before running for real."
        )
        return 1

    counts = snapshot_classifier.classify_all(targets, client=client)
    summary = _format_summary(len(targets), counts)
    logger.info(summary)

    if dry_run:
        would_evict = [
            d.get("name") for d in targets if d.get("snapshot_category") == "toxic"
        ]
        if would_evict:
            logger.info(
                "Would evict %d toxic entries: %s",
                len(would_evict), ", ".join(sorted(would_evict)),
            )
        logger.info("Dry run — no files written, no git operations.")
        return 0

    # ---- Live mode ----

    existing_sidecar = _load_json(excerpts_path, default={})
    if not isinstance(existing_sidecar, dict):
        # Defensive: a corrupted sidecar shouldn't lose all earlier excerpts
        # silently. Warn loudly and reset (the rewrite below preserves at
        # least this run's data).
        logger.warning(
            "Existing %s is not a dict (corrupted?); resetting to empty.",
            excerpts_path,
        )
        existing_sidecar = {}

    sidecar_updates = build_sidecar_updates(targets)
    merged_sidecar = {**existing_sidecar, **sidecar_updates}

    # daily-domains.json: strip inline excerpts, evict toxics, recompute
    # counts. strip_inline_excerpts runs BEFORE split_toxic so the kept
    # entries have no wayback_excerpt key when written.
    strip_inline_excerpts(domains)
    kept, evicted = split_toxic(domains)
    payload["domains"] = kept
    update_counts(payload)
    payload["generated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    _atomic_write_json(daily_path, payload)
    _atomic_write_json(excerpts_path, merged_sidecar)
    logger.info(
        "Wrote %s (%d entries, %d evicted) and %s (%d excerpts)",
        daily_path, len(kept), len(evicted), excerpts_path, len(merged_sidecar),
    )

    if no_push:
        logger.info("--no-push set; skipping commit + push.")
        return 0

    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        logger.error("GITHUB_TOKEN missing — required for push.")
        return 1

    try:
        _git_commit_and_push(summary, evicted, today, token)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.classify_carryover",
        description=(
            "Backfill snapshot classification for entries in daily-domains.json. "
            "Use --dry-run first; review classifications; then re-run without "
            "--dry-run to commit + push."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Classify and print the summary, but write nothing and skip git.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Classify at most N entries (top-N by score descending).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-classify entries that already have a snapshot_category.",
    )
    parser.add_argument(
        "--only-unknown", action="store_true",
        help=(
            "Catch-up mode: only entries with snapshot_category='unknown' AND "
            "wayback_last_snapshot set. Used after an Anthropic outage."
        ),
    )
    parser.add_argument(
        "--no-push", action="store_true",
        help="Write files and commit, but skip the push step.",
    )
    parser.add_argument(
        "--daily-path", default=str(DAILY_DOMAINS_PATH),
        help="Override daily-domains.json path (testing).",
    )
    parser.add_argument(
        "--excerpts-path", default=str(EXCERPTS_SIDECAR_PATH),
        help="Override wayback_excerpts.json sidecar path (testing).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.force and args.only_unknown:
        parser.error("--force and --only-unknown are mutually exclusive")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    return run(
        daily_path=Path(args.daily_path),
        excerpts_path=Path(args.excerpts_path),
        force=args.force,
        only_unknown=args.only_unknown,
        limit=args.limit,
        dry_run=args.dry_run,
        no_push=args.no_push,
        today=date.today(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
