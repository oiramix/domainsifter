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
from datetime import date, datetime, timezone
from pathlib import Path
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

GITHUB_REPO_URL_TEMPLATE = "https://x-access-token:{token}@github.com/oiramix/domainsifter.git"
GIT_USER_NAME = "domainsifter-archive"
GIT_USER_EMAIL = "99090280+oiramix@users.noreply.github.com"


# --- Haiku system prompt (verbatim per spec) --------------------------------

HAIKU_SYSTEM_PROMPT = """You are writing SEO-optimized reference pages for DomainSifter, a domain-research service that publishes daily evaluations of recently-dropped (expired) domains. Each page documents one specific domain at the moment it became available for re-registration, including the historical web presence, authority signals, and DomainSifter's verdict.

YOUR TASK
Given a JSON record describing one domain, write a 250-400 word reference page in clean Markdown format. The page must rank in Google for long-tail searches about this specific domain (e.g., "[domain] expired", "[domain] history", "is [domain] available", "[domain] backlinks").

CONTENT REQUIREMENTS

The page must include, in this order:

1. ## Heading: The domain name plain text (no formatting tricks). This is the H2.

2. Lead paragraph (40-60 words): State plainly what this domain is, when it became available, and DomainSifter's verdict in one sentence. The first sentence must contain the full domain name exactly as a buyer would search it.

3. Authority signals subsection (### Authority and historical presence):
   2-3 short paragraphs interpreting the Wayback snapshot count, OpenPageRank score, and Common Crawl backlinks count in plain language. Tie each number to what it means for someone considering registering this domain. Avoid bullet-point-only sections — mix prose with the numbers.

4. Historical use subsection (### Historical use):
   Based on the wayback_last_snapshot date and what's reasonable to infer from the domain name itself (linguistic structure, TLD, length), describe in 1-2 sentences what kind of website this likely was. If the data doesn't support specific claims, stay general and honest ("the domain previously resolved to a website indexed by the Wayback Machine [N] times") — never invent specific content or business types.

5. Verdict subsection (### Why we labeled this {verdict}):
   2-3 sentences explaining the verdict in terms a domain buyer cares about. Reference the actual signals. For Clean: explain what positive signals justified the highest confidence. For Promising: name the signal strengths that matter and any limitations.

6. Closing line: A brief honest disclaimer that the evaluation reflects the state on {dropped_date} and that domain availability changes quickly. Encourage verification at the registrar.

TONE
Neutral, factual, mildly informative. Not salesy. Not overly technical. Reads like Wikipedia or a quality SEO blog, not like AI fluff. No exclamation marks. No "imagine the possibilities" type filler. Every sentence must contain specific information.

CRITICAL CONSTRAINTS
- Never invent facts. If a field is missing or zero, say so honestly or omit.
- Never use generic AI-ese ("In today's digital landscape...", "Discover the potential of...", "Unlock the power of...").
- The exact domain name must appear at least 4 times naturally across the page.
- Use the TLD naturally ("[domain].net" not just "[domain]").
- Never claim a domain "is" something based on its name alone. Use language like "the name suggests" or "based on linguistic analysis."

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

    new_entries: list[dict] = []
    consecutive_failures = 0
    for record in qualifying:
        name = record["name"]
        try:
            body = client.generate(
                system=HAIKU_SYSTEM_PROMPT,
                user=_build_haiku_user_message(record),
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
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

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
