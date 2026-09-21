"""One-shot backfill: snapshot-classify entries in daily-domains.json.

Standalone — does not import from scripts.pipeline or scripts.run_daily.
Touches only these on-disk artifacts:
    src/data/daily-domains.json       — read; mutated; rewritten atomically
    src/data/wayback_excerpts.json    — read (if exists); fed BACK IN as the
                                        classifier's excerpt cache; merged;
                                        rewritten atomically (the sidecar from
                                        design decision (h))

2026-09-21 — the sidecar became an INPUT as well as an output. Until then
snapshot_classifier re-fetched every domain's excerpt from archive.org on
every run, so the same domain was judged on different evidence each time: a
dry run found 9 toxic and the live run 20 minutes later found 8, overlapping
by only 5, and one domain came back toxic in one pass and legitimate in the
next. Because "unknown" means BOTH "not abusive" and "the fetch failed", that
churn silently downgraded already-screened domains back to unscreened — the
mechanism that published a gambling domain with a permanent archive page on
2026-09-20. Reusing the stored excerpt makes the evidence stable and drops
archive.org load. Gated by snapshot_classifier.reuse_cached_excerpts.

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
import inspect
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from scripts import llm_backend, snapshot_classifier, toxic_denylist

logger = logging.getLogger("scripts.classify_carryover")

REPO_ROOT = Path(__file__).resolve().parent.parent
DAILY_DOMAINS_PATH = REPO_ROOT / "src" / "data" / "daily-domains.json"
EXCERPTS_SIDECAR_PATH = REPO_ROOT / "src" / "data" / "wayback_excerpts.json"

GITHUB_REPO_URL_TEMPLATE = (
    "https://x-access-token:{token}@github.com/oiramix/domainsifter.git"
)
GIT_USER_NAME = "domainsifter-classifier"
GIT_USER_EMAIL = "99090280+oiramix@users.noreply.github.com"

# Applied when snapshot_classifier.reuse_cached_excerpts is absent from
# config.json. Hard rule 9 keeps the real value in config; this only decides
# what happens on a config that predates the key. Reuse is ON by default
# because re-fetching is what produced the verdict instability above.
REUSE_CACHED_EXCERPTS_DEFAULT = True

# The four content fields snapshot_classifier actually shows the model. An
# excerpt carrying none of them is not evidence — it is a fetch that returned
# nothing useful — so it never displaces a stored excerpt that has content.
EXCERPT_CONTENT_FIELDS: tuple[str, ...] = (
    "title", "meta_description", "h1", "h2",
)


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


def load_sidecar(path: Path) -> dict[str, dict | None]:
    """Read wayback_excerpts.json, degrading to {} on every failure path.

    Missing file, unreadable file, invalid JSON and a valid-JSON-but-wrong-
    shape file (list, string, null) all yield an empty dict with a warning.
    Hard rule 17: a corrupt sidecar must not crash a classification run — it
    only costs us the reuse benefit for that run.
    """
    raw = _load_json(path, default={})
    if not isinstance(raw, dict):
        logger.warning(
            "Existing %s is not a dict (corrupted?); treating as empty — no "
            "excerpt reuse this run.",
            path,
        )
        return {}
    return raw


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


def split_toxic(
    domains: list[dict], *, remembered_toxic: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Partition domains into (kept, evicted_names).

    A domain is evicted when THIS run classified it toxic, or when it is in
    `remembered_toxic` — the durable denylist of everything ever classified
    toxic (scripts/toxic_denylist.py). Parked / empty / unknown / legitimate
    all stay (parked + empty get verdict-downgraded in Phase 4 but remain
    published; unknown is informational; legitimate is the good path).

    The remembered set matters because `unknown` is ALSO the classifier's
    failure value. This exact function produced the correct `toxic` verdict
    for ridgemotorsports.net on 2026-09-19; the next run's archive.org fetch
    failed, the domain came back `unknown`, and it stayed published and was
    given a permanent archive page. Its archived content is an Indonesian
    online-slot gambling site. A transient network failure must not erase a
    verdict we already reached.

    `remembered_toxic` defaults to None so existing callers are unchanged.
    """
    remembered = remembered_toxic or set()
    kept: list[dict] = []
    evicted: list[str] = []
    for d in domains:
        name = d.get("name", "")
        if d.get("snapshot_category") == "toxic" or name.lower() in remembered:
            evicted.append(name or "<unknown>")
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


def is_usable_excerpt(excerpt: object) -> bool:
    """True when `excerpt` carries at least one of the four content fields.

    Mirrors the usability rule snapshot_classifier applies when deciding
    whether a cached excerpt can stand in for a fetch. `None` (a remembered
    failure), a non-dict, and a dict holding only bookkeeping keys
    (snapshot_url / snapshot_timestamp) are all NOT usable.
    """
    if not isinstance(excerpt, dict):
        return False
    return any(excerpt.get(field) for field in EXCERPT_CONTENT_FIELDS)


def reuse_cached_excerpts_enabled(config: dict | None) -> bool:
    """Read snapshot_classifier.reuse_cached_excerpts (hard rule 9).

    Read here rather than via snapshot_classifier.cfg() so a config or a
    classifier build that predates the key cannot raise KeyError inside the
    backfill tool.
    """
    section = (config or {}).get("snapshot_classifier")
    if not isinstance(section, dict):
        return REUSE_CACHED_EXCERPTS_DEFAULT
    return bool(section.get(
        "reuse_cached_excerpts", REUSE_CACHED_EXCERPTS_DEFAULT,
    ))


def _describe_excerpt_ageing(config: dict | None) -> str:
    """Log fragment naming snapshot_classifier.excerpt_max_age_days, so a low
    "reused" count is interpretable.

    The classifier ages excerpts by their Wayback CAPTURE timestamp, not by
    when we stored them, and these are dropped domains whose last capture is
    usually old: on the 2026-09-21 sidecar only 12 of 249 usable entries fell
    inside the configured 90 days, so ~237 get re-fetched despite being
    cached. Without this fragment an operator reading "12 reused" would
    reasonably conclude reuse was broken. Ageing itself is the classifier's
    business — this function only reports the knob.
    """
    section = (config or {}).get("snapshot_classifier")
    if not isinstance(section, dict):
        return ""
    raw = section.get("excerpt_max_age_days")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return ""
    if days <= 0:
        return "; no age limit, any stored excerpt is reusable"
    return (
        f"; excerpt_max_age_days={days}, so entries whose capture is older "
        f"than that are re-fetched anyway"
    )


def merge_sidecar(
    existing: dict[str, dict | None], updates: dict[str, dict | None],
) -> dict[str, dict | None]:
    """Merge this run's excerpts into the stored sidecar without ever losing
    a good excerpt.

    Three cases, in order:
        1. This run produced a USABLE excerpt → it replaces whatever was
           stored (a fresher capture of real content is always at least as
           good as an older one).
        2. This run produced nothing usable (fetch failed, archive.org 503,
           no snapshot) but a USABLE excerpt is already stored → the stored
           excerpt survives untouched. This is the whole point: the plain
           `{**existing, **updates}` this replaced would overwrite it with
           null, and on the 2026-09-21 sidecar (598 entries, 379 null, 214
           with content) a run whose fetches mostly failed would have
           destroyed the 214.
        3. Neither is usable → the update is stored anyway, so a first-time
           failure is remembered as null (unchanged from today's behaviour;
           snapshot_classifier still retries a cached null).

    Domains absent from `updates` are never touched — the sidecar spans a
    14-day rolling window and more, and this run only sees today's targets.
    """
    merged: dict[str, dict | None] = dict(existing)
    for name, excerpt in updates.items():
        if is_usable_excerpt(excerpt):
            merged[name] = excerpt
        elif is_usable_excerpt(merged.get(name)):
            logger.debug(
                "Keeping stored excerpt for %s — this run's fetch produced "
                "nothing usable.", name,
            )
        else:
            merged[name] = excerpt
    return merged


def count_excerpt_sources(
    targets: list[dict], cache: dict[str, dict | None] | None,
) -> dict[str, int]:
    """Tally where each target's excerpt came from: {reused, fetched, missing}.

    Compared by value against the cache we handed the classifier, so it holds
    whether the classifier reused the very dict object or a copy of it. Call
    this BEFORE strip_inline_excerpts, while the excerpts are still inline.
    """
    counts = {"reused": 0, "fetched": 0, "missing": 0}
    cache = cache or {}
    for record in targets:
        excerpt = record.get("wayback_excerpt")
        if not is_usable_excerpt(excerpt):
            counts["missing"] += 1
            continue
        cached = cache.get(record.get("name", ""))
        if is_usable_excerpt(cached) and cached == excerpt:
            counts["reused"] += 1
        else:
            counts["fetched"] += 1
    return counts


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


def _accepts_excerpt_cache(func) -> bool:
    """True when `func` can be called with excerpt_cache=...

    snapshot_classifier.classify_all grew the keyword argument on 2026-09-21;
    this tool may run against a build that predates it (or a test double).
    Asking the signature first means the common case never depends on
    catching a TypeError after the classifier has already spent model calls.
    A callable whose signature cannot be read (C builtins, some mocks) is
    treated as accepting it — the TypeError fallback below then covers it.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    if "excerpt_cache" in params:
        return True
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def classify_with_cache(
    targets: list[dict],
    *,
    client,
    config: dict | None,
    excerpt_cache: dict[str, dict | None] | None,
    classify_all=None,
) -> tuple[dict[str, int], bool]:
    """Call snapshot_classifier.classify_all, passing the excerpt cache when
    that build supports it. Returns (counts, cache_was_passed).

    Two layers of guard against the older signature, because the classifier
    change may land after this one:
        1. Signature introspection — no cache keyword at all if the callee
           cannot take it.
        2. A TypeError fallback for the "signature says yes but the call
           still rejects it" case. The retry is deliberately narrowed to
           TypeErrors that name the argument, so an unrelated TypeError from
           inside the classifier propagates instead of silently re-running a
           whole classification pass.
    """
    classify_all = classify_all or snapshot_classifier.classify_all
    if not excerpt_cache:
        return classify_all(targets, client=client, config=config), False

    if not _accepts_excerpt_cache(classify_all):
        logger.warning(
            "snapshot_classifier.classify_all does not accept excerpt_cache "
            "(older build) — every excerpt will be re-fetched from "
            "archive.org this run.",
        )
        return classify_all(targets, client=client, config=config), False

    try:
        counts = classify_all(
            targets, client=client, config=config, excerpt_cache=excerpt_cache,
        )
    except TypeError as exc:
        message = str(exc)
        if "excerpt_cache" not in message and "keyword argument" not in message:
            raise
        logger.warning(
            "snapshot_classifier.classify_all rejected excerpt_cache (%s) — "
            "retrying without reuse; this run re-fetches from archive.org.",
            message,
        )
        return classify_all(targets, client=client, config=config), False
    return counts, True


def _format_reuse_summary(counts: dict[str, int], *, cache_used: bool) -> str:
    """One line, once per run — the operator's view of the archive.org load
    drop. Deliberately not per-domain: at 200+ targets that would bury the
    classification summary."""
    return (
        f"excerpt sources: {counts['reused']} reused from sidecar, "
        f"{counts['fetched']} fetched from archive.org, "
        f"{counts['missing']} with no usable excerpt "
        f"(reuse {'on' if cache_used else 'off'})"
    )


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
    config: dict | None = None,
    client_factory=snapshot_classifier.make_default_client,
) -> int:
    """Orchestrate one backfill run. Returns the process exit code.

    `client_factory` is injectable for tests (pass a lambda that accepts a
    config dict and returns a fake client or None). Production callers omit
    it and get the backend named by `config["llm"]["backend"]`.

    `config` carries llm.backend plus the snapshot_classifier knobs; without
    it the classifier silently uses in-code defaults and config.json edits
    would not reach this tool.
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

    # Shadow mode is for the UNATTENDED daily pipeline, where we want verdicts
    # observed before they start evicting. This tool is the opposite: an
    # operator runs it deliberately, reviews --dry-run output, then re-runs to
    # commit. Honouring shadow here would make a wet run silently evict
    # nothing — the exact failure this tool exists to repair. So force it off
    # locally, on a copy, without touching the caller's config.
    config = dict(config or {})
    classifier_cfg = dict(config.get("snapshot_classifier") or {})
    if classifier_cfg.get("shadow"):
        logger.info(
            "snapshot_classifier.shadow is on in config; overriding to OFF for "
            "this backfill so toxic entries are actually evicted."
        )
    classifier_cfg["shadow"] = False
    config["snapshot_classifier"] = classifier_cfg

    client = client_factory(config)
    if client is None and not dry_run:
        # Wet run with no usable backend = nothing useful gets done. Abort
        # with a clear error rather than silently writing 'unknown' over real
        # data. Dry run is fine — it just shows the no-op summary.
        #
        # NOTE: since the 2026-09-18 backend switch this fires only for an
        # unrecognised llm.backend name. A broken `claude` binary or an
        # expired OAuth token now fails per-batch into all-unknown instead,
        # which the shadow/eviction logic treats as "classified nothing".
        logger.error(
            "No usable LLM backend — check llm.backend in scripts/config.json "
            "and the token in %s.",
            llm_backend.cfg(config or {}, "env_file"),
        )
        return 1

    # Feed the sidecar back in as evidence. Read it BEFORE classification (in
    # dry-run too — reading writes nothing) so a domain we already have a good
    # excerpt for is judged on the same evidence as last time instead of on
    # whatever archive.org happens to serve today.
    existing_sidecar = load_sidecar(excerpts_path)
    if reuse_cached_excerpts_enabled(config):
        # A copy, so a classifier that mutated what it was handed could not
        # corrupt the dict we merge into on the way out.
        excerpt_cache: dict[str, dict | None] | None = dict(existing_sidecar)
        logger.info(
            "Excerpt reuse ON — sidecar holds %d entries, %d with usable "
            "content%s.",
            len(excerpt_cache),
            sum(1 for v in excerpt_cache.values() if is_usable_excerpt(v)),
            _describe_excerpt_ageing(config),
        )
    else:
        excerpt_cache = None
        logger.info(
            "snapshot_classifier.reuse_cached_excerpts is false — no cache "
            "passed; every excerpt is re-fetched from archive.org.",
        )

    counts, cache_used = classify_with_cache(
        targets, client=client, config=config, excerpt_cache=excerpt_cache,
    )
    logger.info(_format_reuse_summary(
        count_excerpt_sources(targets, excerpt_cache), cache_used=cache_used,
    ))
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

    # existing_sidecar was loaded (and shape-checked) before classification.
    sidecar_updates = build_sidecar_updates(targets)
    merged_sidecar = merge_sidecar(existing_sidecar, sidecar_updates)

    # daily-domains.json: strip inline excerpts, evict toxics, recompute
    # counts. strip_inline_excerpts runs BEFORE split_toxic so the kept
    # entries have no wayback_excerpt key when written.
    strip_inline_excerpts(domains)

    # Consult AND update the durable toxic memory. This tool is the one that
    # produced the correct verdict which a later fetch failure then erased,
    # so it must both remember what it finds and honour what it already knew.
    if toxic_denylist.is_enabled(config):
        toxic_denylist.record_toxic(
            [d.get("name", "") for d in domains
             if d.get("snapshot_category") == "toxic" and d.get("name")],
            today=today,
            classifier_version=snapshot_classifier.CLASSIFIER_VERSION,
        )
        remembered = toxic_denylist.load_denylist()
    else:
        remembered = set()
    kept, evicted = split_toxic(domains, remembered_toxic=remembered)
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
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.json"),
        help="Path to config.json (carries llm.backend + classifier knobs).",
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
        config=_load_json(Path(args.config), default={}) or {},
    )


if __name__ == "__main__":
    raise SystemExit(main())
