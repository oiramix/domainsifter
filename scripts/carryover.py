"""14-day persistent rolling list — carryover handling.

Each daily pipeline run produces three buckets:

  1. Today's new drops    — domains the pipeline found and verified
                            available today.
  2. Carryover available  — yesterday's published domains still absent
                            from today's zone files (re-validated by
                            zone-membership, NOT by another RDAP call).
  3. Removed              — domains that are now in today's zone (someone
                            registered them) OR are older than 14 days.

The functions here are pure (no I/O except `load_existing` which reads a
single JSON file). Pipeline orchestration calls them; the carryover
validation per-TLD happens inside `collect_drops` because it needs the
TLD's today_set in memory.

Schema additions per domain:
  - first_seen_date:        when the domain first appeared on our list
                            (set on first capture, NEVER modified after).
  - last_validated_date:    when we last confirmed it's still available.
                            Updated each day a successful zone validation
                            sees the domain absent from today's zone.
  - days_listed:            derived (today − first_seen_date), computed
                            at write time so the frontend doesn't need to.

Migration: pre-persistence entries (no first_seen_date) get treated as
if first seen today — they'll show as "0 days listed" on the migration
day and age naturally going forward. No data loss.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_MAX_WAYBACK_UNKNOWN_DAYS = 3


def load_existing(path: Path | str) -> list[dict]:
    """Read existing daily-domains.json. Returns [] on any unreadability —
    missing file, malformed JSON, unexpected shape. The pipeline must
    survive a deleted or corrupt prior output by simply starting fresh."""
    p = Path(path)
    if not p.exists():
        logger.info("No existing list at %s — starting fresh", p)
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("Existing list at %s unreadable: %s", p, exc)
        return []
    if not isinstance(payload, dict):
        return []
    domains = payload.get("domains")
    if not isinstance(domains, list):
        return []
    return [d for d in domains if isinstance(d, dict) and d.get("name")]


def _parse_iso(s: str | None) -> date | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None


def filter_by_age(
    entries: list[dict],
    today: date,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> tuple[list[dict], int]:
    """Drop entries whose first_seen_date is more than max_age_days ago.

    Returns (survivors, dropped_by_age).

    Migration behaviour: an entry without first_seen_date (pre-persistence
    schema) gets a synthetic first_seen_date=today injected — it survives
    this round and ages naturally going forward.
    """
    survivors: list[dict] = []
    today_iso = today.isoformat()
    dropped = 0
    for e in entries:
        fsd = _parse_iso(e.get("first_seen_date"))
        if fsd is None:
            survivors.append({**e, "first_seen_date": today_iso})
            continue
        if (today - fsd).days > max_age_days:
            dropped += 1
            continue
        survivors.append(e)
    return survivors, dropped


def age_out_wayback_unknown(
    entries: list[dict],
    max_unknown_days: int = DEFAULT_MAX_WAYBACK_UNKNOWN_DAYS,
) -> tuple[list[dict], int]:
    """Increment `wayback_unknown_attempts` for any entry still flagged
    wayback_unknown=True, then drop entries that have been flagged for
    `max_unknown_days` consecutive runs.

    Why this lives in the carryover module: it's a per-day stateful update
    that mutates the carry-forward list. Same shape as filter_by_age — drop
    after N days of an unwanted state. The counter starts at 1 on the day
    the entry is first added to carryover with the flag set, and ticks up
    each subsequent carryover pass while the flag persists. Drop when
    counter > max_unknown_days (so a max of 3 means: kept on days 1, 2, 3;
    dropped on the day it would tick to 4).

    Returns (survivors, dropped_count).

    Future: when wayback retry-on-carryover ships, successful retries should
    clear the flag here (entries with `wayback_unknown` not True pass through
    unchanged). Same pattern will apply for `cert_history_unknown` once
    crt.sh gets the same circuit-breaker-surfacing treatment.
    """
    survivors: list[dict] = []
    dropped = 0
    for e in entries:
        if not e.get("wayback_unknown"):
            survivors.append(e)
            continue
        attempts = int(e.get("wayback_unknown_attempts") or 0) + 1
        if attempts > max_unknown_days:
            dropped += 1
            continue
        survivors.append({**e, "wayback_unknown_attempts": attempts})
    return survivors, dropped


def validate_against_zone(
    entries: list[dict],
    today_set: set[str],
    today: date,
) -> tuple[list[dict], int]:
    """Filter entries to those NOT present in today's zone (= still available).

    Caller passes only the entries belonging to the matching TLD; today_set
    is that TLD's full apex set as parsed from today's zone download.

    Returns (retained, dropped_count_registered). Survivors get
    last_validated_date = today.
    """
    today_iso = today.isoformat()
    retained: list[dict] = []
    registered = 0
    for e in entries:
        if e.get("name") in today_set:
            registered += 1
            continue
        retained.append({**e, "last_validated_date": today_iso})
    return retained, registered


def annotate_today_drops(drops: list[dict], today: date) -> list[dict]:
    """In-place: tag fresh drops with first_seen_date / last_validated_date /
    days_listed=0. Returns the same list for chaining."""
    today_iso = today.isoformat()
    for d in drops:
        d["first_seen_date"] = today_iso
        d["last_validated_date"] = today_iso
        d["days_listed"] = 0
    return drops


def annotate_carryover_days_listed(carryover: list[dict], today: date) -> list[dict]:
    """In-place: compute days_listed = today − first_seen_date for each
    carryover entry. Entries with malformed/missing dates default to 0."""
    for e in carryover:
        fsd = _parse_iso(e.get("first_seen_date"))
        e["days_listed"] = (today - fsd).days if fsd is not None else 0
    return carryover


def merge(today_drops: list[dict], carryover: list[dict]) -> list[dict]:
    """Combine today's new drops with retained carryover.

    Edge case 4 from the spec: a domain present in BOTH lists (rare —
    dropped, registered yesterday, dropped again today) is a full
    REPLACEMENT by today's entry, not a merge. Today's data is fresher.
    """
    today_names = {d.get("name") for d in today_drops}
    deduped_carryover = [c for c in carryover if c.get("name") not in today_names]
    return list(today_drops) + deduped_carryover
