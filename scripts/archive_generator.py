"""Archive subsystem — generate permanent reference pages for Clean and
Promising domains via Anthropic Haiku, then commit + push.

Independent of the daily pipeline. Runs ~10 minutes after pipeline
completion via its own systemd timer / cron entry:

    10 7 * * * cd /home/domainsifter/domainsifter && \\
      .venv/bin/python -m scripts.archive_generator \\
      >> /var/log/domainsifter/archive.log 2>&1

Reads:
  - src/data/daily-domains.json    (pipeline output)
  - src/data/archive-index.json    (master index; created empty on first run)
  - env ANTHROPIC_API_KEY           (required)
  - env GITHUB_TOKEN                (required for the push)

Writes:
  - src/content/archive/{name}.md  (one Markdown file per new archive entry)
  - src/data/archive-index.json    (appended in place)

Behavior:
  1. Load daily-domains.json.
  2. Filter to entries with verdict in {Clean, Promising} AND not already
     in archive-index (the index is source of truth for "already archived").
  3. For each qualifying entry: call Haiku, write {name}.md with Astro
     frontmatter + the generated body.
  4. Append each new entry to archive-index.json.
  5. Atomic file writes — temp-write + os.replace — so a mid-run failure
     never leaves a partial index.
  6. git add + commit ("archive: add N new pages for {date}") + git push.
     Token goes in the push argv ONLY, not git config (matches
     scripts/run-daily.sh).

Failure modes:
  - Per-domain Haiku failure → log, skip the domain, continue with others.
    Failed domain stays out of archive-index; will retry on the next run.
  - 5 consecutive Haiku failures → circuit breaker opens, exit non-zero
    (cron / log monitor will surface it).
  - git push failure → exit non-zero. Local commit is left in place; next
    day's run will see a clean state again after the operator resolves
    the push (typically a manual pull --ff-only).
  - Idempotent on re-run: never regenerate an existing .md file. The
    archive-index is the source of truth.

Sitemap: NOT regenerated here. The @astrojs/sitemap integration walks all
built pages including the dynamic /d/{name} routes on the next Astro
build (triggered by the push that this script makes). robots.txt already
points to sitemap-index.xml. This script does not write public/sitemap.xml.

Cost: Haiku 4.5 at ~$1/MTok input, ~$5/MTok output; ~1.5k input + 600
output per domain ≈ $0.0045/call. Steady-state ~10-30 new domains/day
≈ $0.05-0.15/day ≈ $1.50-4.50/month. First-run backfill may hit ~50-150
calls depending on how many qualifying entries are currently in
daily-domains.json. Verify pricing at docs.claude.com before bulk runs.

Operational notes — see commit message header.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

from scripts.wayback_excerpt import fetch_excerpt
from typing import Any

logger = logging.getLogger("scripts.archive_generator")


# --- Constants ---------------------------------------------------------------

HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS = 800
HAIKU_TEMPERATURE = 0.4
CONSECUTIVE_FAILURES_ABORT = 5

QUALIFYING_VERDICTS = ("Clean", "Promising")

REPO_ROOT = Path(__file__).resolve().parent.parent
DAILY_DOMAINS_PATH = REPO_ROOT / "src" / "data" / "daily-domains.json"
ARCHIVE_INDEX_PATH = REPO_ROOT / "src" / "data" / "archive-index.json"
ARCHIVE_CONTENT_DIR = REPO_ROOT / "src" / "content" / "archive"
# Sidecar with pre-computed Wayback excerpts (Phase 4, 2026-05-20). Written
# by scripts/pipeline.py's Stage 4b classifier; archive_generator reads it
# instead of refetching from archive.org, eliminating ~60s of wall-clock per
# day's archive run. Falls back to per-domain fetch_excerpt for any name
# not present in the sidecar (covers backfilled carryover from before
# Phase 4 + transient sidecar-write failures).
SIDECAR_EXCERPTS_PATH = REPO_ROOT / "src" / "data" / "wayback_excerpts.json"

GITHUB_REPO_URL_TEMPLATE = "https://x-access-token:{token}@github.com/oiramix/domainsifter.git"
GIT_USER_NAME = "domainsifter-archive"
GIT_USER_EMAIL = "99090280+oiramix@users.noreply.github.com"


# --- Haiku system prompt ---------------------------------------------------
#
# 2026-05-18 revision: Historical use is now strictly grounded in the
# Wayback excerpt fetched per-domain by scripts/wayback_excerpt.py. When
# the excerpt is null or empty, the section is OMITTED. The previous
# language ("based on the name structure", "the linguistic composition
# suggests", "describe what kind of website this likely was") was an
# invitation to hallucinate and produced the deepsand.net / waterangels.net
# style invented descriptions that triggered this rewrite. All such
# speculation-invitation phrases are excised throughout the prompt.

HAIKU_SYSTEM_PROMPT = """You are writing SEO-optimized reference pages for DomainSifter, a domain-research service that publishes daily evaluations of recently-dropped (expired) domains. Each page documents one specific domain at the moment it became available for re-registration, including the historical web presence, authority signals, and DomainSifter's verdict.

YOUR TASK
Given a JSON record describing one domain, write a 250-400 word reference page in clean Markdown format. The page must rank in Google for long-tail searches about this specific domain (e.g., "[domain] expired", "[domain] history", "is [domain] available", "[domain] backlinks").

CONTENT REQUIREMENTS

The page must include, in this order:

1. ## Heading: The domain name plain text (no formatting tricks). This is the H2.

2. Lead paragraph (40-60 words): State plainly what this domain is, when it became available, and DomainSifter's verdict in one sentence. The first sentence must contain the full domain name exactly as a buyer would search it.

3. Authority signals subsection (### Authority and historical presence):
   2-3 short paragraphs interpreting the Wayback snapshot count, OpenPageRank score, and Common Crawl backlinks count in plain language. Tie each number to what it means for someone considering registering this domain. Avoid bullet-point-only sections — mix prose with the numbers. Do NOT speculate about the kind of website that produced these numbers in this subsection — that belongs in "Historical use" only.

4. Historical use subsection (### Historical use):
   This subsection is GROUNDED ONLY in the `wayback_excerpt` field of the input JSON.

   When `wayback_excerpt` is present AND at least one of its fields (title, meta_description, h1, h2) is non-empty:
     - Write 1-2 short paragraphs describing the site based ONLY on those signals. Quote or closely paraphrase the title and meta_description. Mention what the h1 / h2 headings reveal about the site's structure or topical focus.
     - If the title or meta_description indicates a parked page, domain-for-sale notice, or generic placeholder/error page, report that factually (e.g. "The most recent snapshot showed a parked-page placeholder rather than active content").
     - Do NOT speculate beyond what the excerpt directly states. Do NOT extrapolate "the site probably also did X."

   When `wayback_excerpt` is null OR all four of its content fields (title, meta_description, h1, h2) are empty:
     - OMIT the entire "### Historical use" subsection. Skip directly from "### Authority and historical presence" to "### Why we labeled this {verdict}".
     - Do NOT write a stub. Do NOT write "the historical content could not be determined." Do NOT mention the snapshot count again here. Just leave the section out completely.

   NEVER infer historical content from the domain name itself. Without a Wayback excerpt, you have no basis to describe what the site was. Silence is correct.

5. Verdict subsection (### Why we labeled this {verdict}):
   2-3 sentences explaining the verdict in terms a domain buyer cares about. Reference the actual quantitative signals (snapshot count, OPR, backlinks). For Clean: explain what positive signals justified the highest confidence. For Promising: name the signal strengths that matter and any limitations.

6. Closing line: A brief honest disclaimer that the evaluation reflects the state on {dropped_date} and that domain availability changes quickly. Encourage verification at the registrar.

TONE
Neutral, factual, mildly informative. Not salesy. Not overly technical. Reads like Wikipedia or a quality SEO blog, not like AI fluff. No exclamation marks. No "imagine the possibilities" type filler. Every sentence must contain specific information.

CRITICAL CONSTRAINTS
- Never invent facts. If a field is missing or zero, say so honestly or omit.
- Never use generic AI-ese ("In today's digital landscape...", "Discover the potential of...", "Unlock the power of...").
- The exact domain name must appear at least 4 times naturally across the page.
- Use the TLD naturally ("[domain].net" not just "[domain]").
- NEVER describe what a domain "was", "likely was", "may have been", "could have served", or "appears to be" based on the domain name itself. The name is not evidence of historical content. Only the `wayback_excerpt` is.
- NEVER write phrases like "based on the name structure", "the linguistic composition suggests", "the name suggests", or "likely operated as" anywhere on the page. These are speculation invitations and are forbidden.

OUTPUT FORMAT
Return only the Markdown body starting with the ## heading. No frontmatter, no meta-commentary, no preamble. The generator script will add frontmatter.
"""


# --- Pure helpers (unit-tested) ---------------------------------------------


def _filter_qualifying(
    domains: list[dict], already_archived: set[str],
) -> list[dict]:
    """Verdict in {Clean, Promising} AND name not already archived. Domains
    without a verdict (legacy payloads) are rejected — the archive only
    contains entries we've explicitly labeled."""
    out: list[dict] = []
    for d in domains:
        verdict = d.get("verdict")
        if verdict not in QUALIFYING_VERDICTS:
            continue
        name = d.get("name")
        if not name or name in already_archived:
            continue
        out.append(d)
    return out


def _slug_for(name: str) -> str:
    """Filename slug for src/content/archive/{slug}.md. Preserves dots so
    the Astro dynamic route param matches verbatim ('deepsand.net.md' →
    /d/deepsand.net). Lowercased; whitespace stripped; anything that
    isn't a safe filename character (alnum / . / - / _) is rejected by
    raising. We never construct domain names ourselves — they come from
    daily-domains.json which the pipeline normalises — so this is a
    sanity gate, not a sanitiser."""
    s = name.strip().lower()
    if not s:
        raise ValueError("empty name")
    for ch in s:
        if not (ch.isalnum() or ch in ".-_"):
            raise ValueError(f"unsafe character {ch!r} in name {name!r}")
    return s


def _build_haiku_user_message(record: dict) -> str:
    """The user-turn prompt sent alongside the system prompt. JSON-pretty so
    Haiku can pattern-match the fields without ambiguity."""
    return (
        "Generate the archive page for this domain:\n\n"
        + json.dumps(record, indent=2, sort_keys=True)
    )


def _build_frontmatter(record: dict, archived_date: str) -> str:
    """YAML frontmatter that Astro's content collection schema parses. Keys
    match the zod schema in src/content/config.ts — adding or renaming a
    field requires updating both."""
    payload = {
        "name": record["name"],
        "tld": record["tld"],
        "verdict": record["verdict"],
        "score": int(record.get("score") or 0),
        "dropped_date": record.get("dropped_date") or archived_date,
        "archived_date": archived_date,
        "wayback_snapshots": record.get("wayback_snapshots"),
        "wayback_last_snapshot": record.get("wayback_last_snapshot"),
        "open_page_rank": record.get("open_page_rank"),
        "cc_source_domain_count": record.get("cc_source_domain_count"),
        "cert_history": record.get("cert_history"),
        "first_seen_date": record.get("first_seen_date"),
        "availability_verified_at": record.get("availability_verified_at"),
    }
    lines = ["---"]
    for k, v in payload.items():
        if v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            # Wrap strings in double-quotes; escape internal quotes. Domain
            # names and ISO dates never contain quotes, but be safe.
            escaped = str(v).replace('\\', '\\\\').replace('"', '\\"')
            lines.append(f'{k}: "{escaped}"')
    lines.append("---")
    return "\n".join(lines) + "\n"


def _build_markdown_file(record: dict, body: str, archived_date: str) -> str:
    """Full file content: frontmatter + blank line + body + trailing newline."""
    return _build_frontmatter(record, archived_date) + "\n" + body.rstrip() + "\n"


# --- I/O helpers -------------------------------------------------------------


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s (%s); using default.", path, exc)
        return default


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), suffix=".tmp")
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


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Haiku call --------------------------------------------------------------


class HaikuClient:
    """Thin wrapper around anthropic.Anthropic.messages.create so the
    generator's main path can be tested without the real SDK. Real client
    is instantiated lazily so tests that mock `client_factory` never need
    ANTHROPIC_API_KEY in the environment."""

    def __init__(self, api_key: str) -> None:
        # Lazy import — keeps the test path runnable on machines without
        # the anthropic SDK installed (it's added to requirements.txt for
        # OVH; tests mock around it).
        from anthropic import Anthropic
        self._client = Anthropic(api_key=api_key)

    def generate(self, system: str, user: str) -> str:
        resp = self._client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=HAIKU_MAX_TOKENS,
            temperature=HAIKU_TEMPERATURE,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Concatenate text blocks (Anthropic returns a list of content blocks).
        out: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                out.append(text)
        return "".join(out).strip()


# --- Git operations ----------------------------------------------------------


def _git(args: list[str], *, cwd: Path = REPO_ROOT, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git subprocess. Captures output for the log."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _git_commit_and_push(
    new_count: int, today: date, github_token: str,
) -> None:
    """Stage the archive deltas, commit, push. Token goes in the push URL
    argv only — never written to .git/config."""
    _git(["config", "user.name", GIT_USER_NAME])
    _git(["config", "user.email", GIT_USER_EMAIL])
    _git(["add", "src/content/archive", "src/data/archive-index.json"])
    diff = _git(["diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        logger.info("No staged changes to commit (no new archive entries).")
        return
    msg = f"archive: add {new_count} new pages for {today.isoformat()}"
    _git(["commit", "-m", msg])
    push_url = GITHUB_REPO_URL_TEMPLATE.format(token=github_token)
    push = subprocess.run(
        ["git", "push", push_url, "main"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    if push.returncode != 0:
        # Don't dump push.stderr — it may echo back the URL in some git
        # versions. Just log the exit code; operator can inspect manually.
        raise RuntimeError(
            f"git push failed with exit code {push.returncode}; local commit "
            f"is in place. Inspect and resolve manually before next run."
        )
    logger.info("Pushed commit: %s", msg)


# --- Wayback excerpt resolution (sidecar-first, fetch-fallback) ------------


def _load_sidecar_excerpts(
    sidecar_path: Path = SIDECAR_EXCERPTS_PATH,
) -> dict[str, dict | None]:
    """Load the wayback_excerpts.json sidecar into a name → excerpt map.

    Returns an empty dict on missing OR corrupt sidecar — caller then falls
    back to per-domain `fetch_excerpt` for every domain (same path as
    pre-Phase-4 behaviour). A logged warning surfaces the degraded state
    in the daily report email.
    """
    if not sidecar_path.exists():
        logger.info(
            "Sidecar %s not present — will fetch each excerpt from Wayback directly.",
            sidecar_path,
        )
        return {}
    try:
        with open(sidecar_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Sidecar %s unreadable (%s); falling back to per-domain fetch.",
            sidecar_path, exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "Sidecar %s is not a dict (got %s); falling back to per-domain fetch.",
            sidecar_path, type(data).__name__,
        )
        return {}
    return data


def _resolve_excerpt(
    record: dict, sidecar: dict[str, dict | None],
) -> tuple[dict | None, str]:
    """Return (excerpt, source) for a domain record.

    `source` is one of:
      - "sidecar-hit"  — sidecar had a dict; no archive.org call
      - "sidecar-null" — sidecar had explicit None; classifier saw this
                         domain but Wayback returned no usable content;
                         do NOT refetch (would just produce None again
                         for any persistent-state reason)
      - "fetch"        — sidecar absent for this name; called fetch_excerpt
                         (caller should sleep 1s after this — archive.org
                         courtesy)
      - "fetch-error"  — fetch_excerpt raised (transient or persistent;
                         logged warning); caller treats as None and still
                         sleeps to honor pacing
      - "no-snapshot"  — record has no wayback_last_snapshot AND not in
                         sidecar; nothing to do (no API hit, no sleep)

    The sidecar-null branch is the key correctness bit: if Phase 4's
    classifier ran on this domain and got None back from fetch_excerpt,
    refetching here would either get None again (persistent: no closest
    snapshot, all content fields empty, etc.) OR worse — racing the
    classifier's verdict by using a different snapshot than what the
    classifier saw. Trust the classifier's view, don't second-guess it.
    """
    name = record["name"]
    if name in sidecar:
        cached = sidecar[name]
        if cached is None:
            return None, "sidecar-null"
        if isinstance(cached, dict):
            return cached, "sidecar-hit"
        # Sidecar has a non-dict, non-None value — corruption. Fall through
        # to fetch as a defensive fallback rather than passing garbage to
        # Haiku.
        logger.warning(
            "Sidecar entry for %s is not dict|None (got %s); falling back to fetch.",
            name, type(cached).__name__,
        )

    last_snapshot = record.get("wayback_last_snapshot")
    if not last_snapshot:
        return None, "no-snapshot"

    try:
        excerpt = fetch_excerpt(name, last_snapshot)
    except Exception as exc:
        logger.warning(
            "Wayback excerpt fetch failed for %s: %s — proceeding without grounding",
            name, exc,
        )
        return None, "fetch-error"
    return excerpt, "fetch"


# --- Top-level orchestration ------------------------------------------------


def generate_archive(
    *,
    daily_path: Path = DAILY_DOMAINS_PATH,
    index_path: Path = ARCHIVE_INDEX_PATH,
    content_dir: Path = ARCHIVE_CONTENT_DIR,
    client: HaikuClient | None = None,
    today: date | None = None,
    git_push: bool = True,
    github_token: str | None = None,
) -> dict:
    """Process today's daily-domains.json into archive pages. Returns a
    status dict with counts. Raises on systemic failures (config error,
    breaker tripped, push failed)."""
    today = today or date.today()
    today_iso = today.isoformat()

    daily_payload = _load_json(daily_path, default={"domains": []})
    domains = daily_payload.get("domains") or []
    if not domains:
        logger.info("daily-domains.json has no entries; nothing to archive.")
        return {"status": "no_input", "new_count": 0}

    index_payload = _load_json(index_path, default={"generated_at": None, "entries": []})
    archived_entries: list[dict] = index_payload.get("entries") or []
    already = {e.get("name") for e in archived_entries if e.get("name")}

    qualifying = _filter_qualifying(domains, already)
    if not qualifying:
        logger.info(
            "No new qualifying domains (%d in payload, %d already archived).",
            len(domains), len(already),
        )
        return {"status": "no_new", "new_count": 0}

    logger.info(
        "Processing %d new qualifying domains (of %d in payload, %d already archived).",
        len(qualifying), len(domains), len(already),
    )

    if client is None:
        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY missing — required for Haiku calls.")
        client = HaikuClient(api_key)

    content_dir.mkdir(parents=True, exist_ok=True)

    # Sidecar-first excerpt resolution (Phase 4, 2026-05-20): pipeline's
    # Stage 4b populates src/data/wayback_excerpts.json so this loop hits
    # the sidecar instead of archive.org for every classified domain.
    # Per-domain fetch_excerpt is still the fallback for entries the
    # classifier didn't see (e.g., backfilled carryover from before Phase
    # 4 wire-in, or a day where Stage 4b's sidecar write failed).
    sidecar_excerpts = _load_sidecar_excerpts()

    new_entries: list[dict] = []
    consecutive_failures = 0
    for record in qualifying:
        name = record["name"]

        excerpt, source = _resolve_excerpt(record, sidecar_excerpts)
        # Only pause AFTER a real archive.org hit (not after a sidecar
        # lookup). On full-sidecar-coverage days the archive run drops
        # from ~50s/run of pacing waits to ~0s.
        if source in ("fetch", "fetch-error"):
            time.sleep(1.0)

        enriched_record = {**record, "wayback_excerpt": excerpt}

        try:
            body = client.generate(
                system=HAIKU_SYSTEM_PROMPT,
                user=_build_haiku_user_message(enriched_record),
            )
        except Exception as exc:
            consecutive_failures += 1
            logger.warning(
                "Haiku call failed for %s (%d/%d consecutive): %s",
                name, consecutive_failures, CONSECUTIVE_FAILURES_ABORT, exc,
            )
            if consecutive_failures >= CONSECUTIVE_FAILURES_ABORT:
                raise RuntimeError(
                    f"{CONSECUTIVE_FAILURES_ABORT} consecutive Haiku failures; "
                    f"circuit breaker open. Inspect API status and retry."
                ) from exc
            continue

        if not body or "##" not in body:
            consecutive_failures += 1
            logger.warning(
                "Haiku returned empty / malformed body for %s; skipping.", name,
            )
            if consecutive_failures >= CONSECUTIVE_FAILURES_ABORT:
                raise RuntimeError(
                    f"{CONSECUTIVE_FAILURES_ABORT} consecutive malformed responses; "
                    f"circuit breaker open."
                )
            continue
        consecutive_failures = 0

        slug = _slug_for(name)
        md_path = content_dir / f"{slug}.md"
        _atomic_write_text(md_path, _build_markdown_file(record, body, today_iso))
        entry = {
            "name": name,
            "tld": record.get("tld") or name.rsplit(".", 1)[-1],
            "verdict": record["verdict"],
            "score": int(record.get("score") or 0),
            "dropped_date": record.get("dropped_date"),
            "archived_date": today_iso,
            "wayback_snapshots": record.get("wayback_snapshots"),
            "cc_source_domain_count": record.get("cc_source_domain_count"),
            "open_page_rank": record.get("open_page_rank"),
        }
        new_entries.append(entry)
        already.add(name)
        logger.info("Archived %s → %s", name, md_path.name)

    if new_entries:
        archived_entries.extend(new_entries)
        index_payload = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entries": archived_entries,
        }
        _atomic_write_json(index_path, index_payload)
        logger.info(
            "Updated archive-index: +%d new, %d total entries.",
            len(new_entries), len(archived_entries),
        )

    if git_push and new_entries:
        token = github_token or (os.environ.get("GITHUB_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("GITHUB_TOKEN missing — required for push.")
        _git_commit_and_push(len(new_entries), today, token)

    return {
        "status": "ok",
        "new_count": len(new_entries),
        "total_archived": len(archived_entries),
        "attempted": len(qualifying),
    }


# --- Dry-run helper for pre-merge review (2026-05-18) -----------------------


def _render_one_page(
    record: dict,
    *,
    excerpt: dict | None,
    client: HaikuClient,
    output_dir: Path,
    today_iso: str,
) -> tuple[bool, str | None]:
    """Per-domain work: Haiku call → write .md. Excerpt is provided by the
    caller (so this helper doesn't decide whether to fetch vs read sidecar
    vs use None).

    Returns (success, reason). reason is None on success, a short string
    on failure ('haiku-error', 'haiku-malformed'). Caller decides whether
    a failure aborts the run or just logs.

    Phase 4 (2026-05-20): excerpt-fetching responsibility moved out to
    the callers (`generate_archive` and `generate_for_domains`) so the
    sidecar-first lookup pattern works uniformly for both production and
    dry-run paths."""
    name = record["name"]

    enriched_record = {**record, "wayback_excerpt": excerpt}

    try:
        body = client.generate(
            system=HAIKU_SYSTEM_PROMPT,
            user=_build_haiku_user_message(enriched_record),
        )
    except Exception as exc:
        logger.warning("Haiku call failed for %s: %s", name, exc)
        return False, "haiku-error"

    if not body or "##" not in body:
        logger.warning("Haiku returned empty / malformed body for %s; skipping.", name)
        return False, "haiku-malformed"

    slug = _slug_for(name)
    md_path = output_dir / f"{slug}.md"
    _atomic_write_text(md_path, _build_markdown_file(record, body, today_iso))
    logger.info("Rendered %s → %s", name, md_path)
    return True, None


def generate_for_domains(
    domain_names: list[str],
    output_dir: Path,
    *,
    daily_path: Path = DAILY_DOMAINS_PATH,
    client: HaikuClient | None = None,
    today: date | None = None,
) -> dict:
    """Dry-run: process ONLY the named domains, write to `output_dir`,
    do NOT update archive-index.json, do NOT push.

    Used for pre-merge review — picks the named records out of the
    current daily-domains.json (so the input data is real, just the
    output destination is a scratch dir). Domains not present in
    daily-domains.json are reported as missing. Domains present but
    without a qualifying verdict are reported as skipped (we still
    only generate /d/{name}-style content for Clean + Promising, even
    in dry-run; protects against accidentally rendering a page for a
    Caution domain).

    Per-domain failures isolate but never abort — for 1-2 review
    samples the circuit-breaker semantics are noise, not signal.

    Returns: {"rendered": [name, ...], "missing": [...], "skipped_verdict": [...], "failed": [...]}.
    """
    today = today or date.today()
    today_iso = today.isoformat()

    daily_payload = _load_json(daily_path, default={"domains": []})
    by_name = {d["name"]: d for d in (daily_payload.get("domains") or []) if d.get("name")}

    requested = [n.strip() for n in domain_names if n.strip()]
    missing = [n for n in requested if n not in by_name]
    found = [by_name[n] for n in requested if n in by_name]
    skipped_verdict = [d["name"] for d in found if d.get("verdict") not in QUALIFYING_VERDICTS]
    eligible = [d for d in found if d.get("verdict") in QUALIFYING_VERDICTS]

    if missing:
        logger.warning(
            "Names not present in daily-domains.json (skipping): %s", missing,
        )
    if skipped_verdict:
        logger.warning(
            "Names present but verdict not in {Clean, Promising} (skipping): %s",
            skipped_verdict,
        )
    if not eligible:
        logger.error("No eligible domains to render. Stopping.")
        return {
            "rendered": [], "missing": missing,
            "skipped_verdict": skipped_verdict, "failed": [],
        }

    if client is None:
        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY missing — required for Haiku calls.")
        client = HaikuClient(api_key)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Sidecar-first excerpt resolution, same as generate_archive (Phase 4).
    # Dry-run path benefits more from sidecar hits than the production
    # path (typical dry-run targets are 1-2 specific named domains that
    # were classified earlier today / yesterday).
    sidecar_excerpts = _load_sidecar_excerpts()

    rendered: list[str] = []
    failed: list[str] = []
    for record in eligible:
        excerpt, source = _resolve_excerpt(record, sidecar_excerpts)
        if source in ("fetch", "fetch-error"):
            time.sleep(1.0)
        ok, _reason = _render_one_page(
            record,
            excerpt=excerpt,
            client=client,
            output_dir=output_dir,
            today_iso=today_iso,
        )
        if ok:
            rendered.append(record["name"])
        else:
            failed.append(record["name"])

    logger.info(
        "Dry-run complete: %d rendered, %d failed, %d missing, %d skipped (verdict)",
        len(rendered), len(failed), len(missing), len(skipped_verdict),
    )
    return {
        "rendered": rendered,
        "missing": missing,
        "skipped_verdict": skipped_verdict,
        "failed": failed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.archive_generator",
        description="Generate archive pages for Clean and Promising domains.",
    )
    parser.add_argument(
        "--no-push", action="store_true",
        help="Skip the git commit + push at the end. Files still written.",
    )
    parser.add_argument(
        "--daily-path", default=str(DAILY_DOMAINS_PATH),
        help="Override daily-domains.json path (default: src/data/daily-domains.json)",
    )
    parser.add_argument(
        "--index-path", default=str(ARCHIVE_INDEX_PATH),
        help="Override archive-index.json path.",
    )
    parser.add_argument(
        "--only", default=None,
        help=(
            "Dry-run: comma-separated domain names. Processes ONLY these "
            "domains (pulled from daily-domains.json), writes .md files to "
            "--output-dir, does NOT update archive-index.json, does NOT "
            "git-push. Intended for pre-merge review."
        ),
    )
    parser.add_argument(
        "--output-dir", default=None,
        help=(
            "Required with --only. Destination directory for the dry-run "
            ".md files. src/content/archive/ is left untouched."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    # Dry-run branch: --only + --output-dir bypass the production path
    # entirely. No index update, no git push, no archive-index gate.
    if args.only or args.output_dir:
        if not (args.only and args.output_dir):
            logger.error("--only and --output-dir must be passed together.")
            return 1
        names = [n.strip() for n in args.only.split(",") if n.strip()]
        if not names:
            logger.error("--only was empty after splitting on commas.")
            return 1
        try:
            result = generate_for_domains(
                names,
                Path(args.output_dir),
                daily_path=Path(args.daily_path),
            )
        except RuntimeError as exc:
            logger.error("Dry-run aborted: %s", exc)
            return 2
        except Exception:  # pragma: no cover — defence-in-depth
            logger.exception("Dry-run crashed.")
            return 3
        logger.info("Dry-run summary: %s", result)
        return 0

    try:
        result = generate_archive(
            daily_path=Path(args.daily_path),
            index_path=Path(args.index_path),
            git_push=not args.no_push,
        )
    except RuntimeError as exc:
        logger.error("Archive run aborted: %s", exc)
        return 2
    except Exception:  # pragma: no cover — defence-in-depth
        logger.exception("Archive run crashed.")
        return 3

    logger.info("Run summary: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
