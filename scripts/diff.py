"""Per-TLD set diff between yesterday's zone snapshot and today's.

State layout under `state_dir`:
    {tld}_yesterday.txt — one apex name per line, sorted, no trailing newline
                          on the final line, lowercase, no trailing dots.

`compute_drops` returns the set of names that were in yesterday but are gone
today (i.e. dropped from the zone — candidates for our list). The first time
a TLD is seen there's no yesterday file, so the function returns an empty set
and writes today's snapshot for tomorrow's run.

`commit_today` replaces yesterday's file with today's set. The pipeline calls
this AFTER enrichment + filtering succeeds, so a crashed run leaves yesterday
intact and the next run will re-detect the same drops.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _state_path(state_dir: str | Path, tld: str) -> Path:
    return Path(state_dir) / f"{tld.lower()}_yesterday.txt"


def load_yesterday(state_dir: str | Path, tld: str) -> set[str]:
    """Return the set of names recorded for this TLD on the previous run.
    Returns an empty set if no snapshot exists yet (cold start)."""
    path = _state_path(state_dir, tld)
    if not path.exists():
        logger.info("No prior snapshot for .%s — cold start", tld)
        return set()
    with path.open("r", encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def compute_drops(yesterday: set[str], today: set[str]) -> set[str]:
    """Names present yesterday but missing today — these were dropped."""
    return yesterday - today


def commit_today(state_dir: str | Path, tld: str, today: set[str]) -> Path:
    """Write today's snapshot to {tld}_yesterday.txt (sorted, one per line).
    Creates the state directory if missing. Returns the written path."""
    state_path = _state_path(state_dir, tld)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_names = sorted(today)
    with state_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(sorted_names))
        if sorted_names:
            fh.write("\n")
    logger.info("Wrote %d names to %s", len(sorted_names), state_path)
    return state_path


def diff_and_commit(
    state_dir: str | Path,
    tld: str,
    today: set[str],
) -> set[str]:
    """Convenience: load yesterday, compute drops, write today, return drops.

    NOTE: this commits BEFORE downstream enrichment runs. If you want
    crash-safety where a failed enrichment doesn't lose the diff, call
    `load_yesterday` + `compute_drops` first, run enrichment, then call
    `commit_today` only after success. The pipeline orchestrator decides.
    """
    yesterday = load_yesterday(state_dir, tld)
    drops = compute_drops(yesterday, today)
    commit_today(state_dir, tld, today)
    logger.info(".%s: %d in zone today, %d dropped since yesterday", tld, len(today), len(drops))
    return drops
