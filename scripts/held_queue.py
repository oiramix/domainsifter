"""Held queue: domains whose screening did not finish are held, not published.

Why this exists (2026-10-01 plan, step 1)
-----------------------------------------
The pipeline needs answers from free external services (archive.org,
OpenPageRank, crt.sh, registry RDAP) inside one ~3h morning window. Each of
them fails on its own schedule, so most days at least one is down. Before
this module a failed check made a domain `unknown`, and `unknown` was
PUBLISHED UNSCREENED. That is how gambling and adult sites kept reaching the
list, and two of them reached subscribers by email (2026-09-20).

The fix is to stop treating "we could not check" as "nothing found". A
domain that is RDAP-confirmed available but whose evidence is incomplete is
neither published nor rejected: it goes into this queue. Every daily run
re-checks the queue first (RDAP again, then full enrichment and content
screening) and publishes only the ones whose screening completed. After
`held_queue.max_held_days` days a still-incomplete domain is dropped,
never published unscreened.

A dropped domain available today is almost always still available
tomorrow, so a 1-3 day delay costs little, while "archive.org fails 50% of
the time today" becomes "archive.org answers once within 3 days", which is
nearly always true.

What counts as incomplete (`incomplete_reasons`)
------------------------------------------------
  content_unscreened  snapshot_category is not one of the classifier's real
                      verdicts. `unknown` is the classifier's FAILURE value
                      (fetch failed, model failed, or no usable excerpt).
  wayback_unknown     the Wayback enricher could not answer.
  missing:<field>     a field in `held_queue.required_fields` is absent or
                      null. Defaults to the alarm sources
                      (wayback_snapshots, open_page_rank). cert_history is
                      deliberately NOT required: crt.sh is dead for whole
                      runs at a time, and requiring it would hold everything.
  not_enriched        RDAP said available but the enrichment time budget ran
                      out before this domain was enriched.

Storage: R2, never the repo
---------------------------
`state/held_queue.jsonl` in the same private R2 bucket as
`state/toxic_denylist.jsonl` and `state/phase2_overflow.jsonl`. The repo is
public and held domains are unscreened by definition, so the list must not
be committed. The whole object is rewritten each run (released and expired
records leave it).

Failure behaviour (hard rule 17)
--------------------------------
`load_held` returns None on any R2 failure (distinct from [] = empty queue)
and `save_held` returns False; neither raises. When the load fails the
pipeline still holds back incomplete domains, it just does not write the
queue, so nothing already in R2 is overwritten with a partial view. Every
failure direction ends in "not published", never "published unscreened".

This module holds no state (hard rule 16): the R2 client is injected or
built per call, and every decision function is pure.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Same bucket, same `state/` prefix, same JSONL shape as
# toxic_denylist.DENYLIST_R2_KEY. An identifier, not a tunable (hard rule 9).
HELD_R2_KEY = "state/held_queue.jsonl"

# Real classifier verdicts. `unknown` is missing on purpose: it is the
# classifier's failure value. `toxic` never gets this far (the post-enrichment
# filter rejects it) but it IS a completed screening, so it is listed.
SCREENED_CATEGORIES = frozenset({"legitimate", "parked", "empty", "toxic"})

DEFAULT_MAX_HELD_DAYS = 3
DEFAULT_REQUIRED_FIELDS = ("wayback_snapshots", "open_page_rank")

# In-memory marker on a candidate re-entering the pipeline from the queue.
# Never published: output.py and domain_archive.py project explicit field
# allowlists.
HELD_SINCE_FIELD = "held_since"

# Candidate fields copied into a held record so a released domain publishes
# with the same ranker context it had on its first day.
_CARRIED_FIELDS = ("tld", "dropped_date", "phase2_score", "phase2_reason")

_NOT_FOUND_CODES = {"NoSuchKey", "404", "NotFound"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _section(config: dict | None) -> dict:
    section = (config or {}).get("held_queue")
    return section if isinstance(section, dict) else {}


def is_enabled(config: dict | None) -> bool:
    """Kill switch, `held_queue.enabled`. Defaults to True: a config without
    the section must not quietly go back to publishing unscreened domains.
    Setting it false restores the pre-2026-10-01 behaviour exactly."""
    return bool(_section(config).get("enabled", True))


def max_held_days(config: dict | None) -> int:
    """Days a domain may stay held before it is dropped. 0 means "never
    hold": incomplete domains are dropped the same day."""
    raw = _section(config).get("max_held_days", DEFAULT_MAX_HELD_DAYS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "held_queue.max_held_days=%r is not an integer; using %d",
            raw, DEFAULT_MAX_HELD_DAYS,
        )
        return DEFAULT_MAX_HELD_DAYS


def required_fields(config: dict | None) -> tuple[str, ...]:
    raw = _section(config).get("required_fields")
    if not isinstance(raw, list):
        return DEFAULT_REQUIRED_FIELDS
    return tuple(f for f in raw if isinstance(f, str) and f)


# ---------------------------------------------------------------------------
# Decisions (pure)
# ---------------------------------------------------------------------------


def incomplete_reasons(candidate: dict, config: dict | None) -> list[str]:
    """Why this enriched candidate is not fully screened. [] = screened.

    Only meaningful for a candidate that went through enrichment and the
    classifier; a candidate that was never enriched is `not_enriched` and is
    tagged by `settle`, not here.
    """
    reasons: list[str] = []
    if candidate.get("snapshot_category") not in SCREENED_CATEGORIES:
        reasons.append("content_unscreened")
    if candidate.get("wayback_unknown") is True:
        reasons.append("wayback_unknown")
    for field in required_fields(config):
        if candidate.get(field) is None:
            reasons.append(f"missing:{field}")
    return reasons


def _key(name: object) -> str:
    return str(name or "").strip().lower()


def _first_held_date(raw: object, today: date) -> date:
    """A record's first_held_date. A missing or corrupt value becomes today,
    and is written back that way, so the record gets one full retry window
    instead of never expiring."""
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return today


def to_candidates(records: Iterable[dict]) -> list[dict]:
    """Fresh candidate dicts for held records, ready for the RDAP check.

    Only identity and ranker context are carried over. Enrichment is redone
    from scratch, so a held domain is judged on today's evidence plus
    whatever the excerpt sidecar already remembers.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        name = _key(rec.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        cand: dict = {"name": name}
        for field in _CARRIED_FIELDS:
            if rec.get(field) is not None:
                cand[field] = rec[field]
        cand.setdefault("tld", name.rsplit(".", 1)[-1])
        cand[HELD_SINCE_FIELD] = rec.get("first_held_date")
        out.append(cand)
    return out


def _held_record(
    cand: dict,
    *,
    first_held: date,
    today: date,
    checks: int,
    reasons: list[str],
) -> dict:
    record: dict = {"name": _key(cand.get("name"))}
    for field in _CARRIED_FIELDS:
        if cand.get(field) is not None:
            record[field] = cand[field]
    record["first_held_date"] = first_held.isoformat()
    record["last_checked_date"] = today.isoformat()
    record["checks"] = checks
    record["reasons"] = reasons
    return record


def settle(
    *,
    held_records: list[dict],
    held_candidates: list[dict],
    new_available: list[dict],
    enriched: list[dict],
    survivors: list[dict],
    config: dict,
    today: date,
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Decide, for every held and newly available domain, what happens today.

    Inputs are the same dict objects the pipeline passed through its stages
    (matched by name):
      held_records     the queue as loaded from R2
      held_candidates  their candidate dicts, after the RDAP re-check
      new_available    today's NEW candidates RDAP confirmed available
      enriched         every candidate that went through enrichment
      survivors        enriched candidates that passed the post-enrichment
                       filter (toxic / spam / no-Wayback rejects removed)

    Returns (publishable, next_records, outcome):
      publishable   survivors whose screening is complete (new + released)
      next_records  the queue to save back to R2
      outcome       counts and names for the log line and the daily report

    Per held domain:
      RDAP says registered           → dropped ("reregistered")
      enriched but filtered out      → dropped ("rejected"; e.g. toxic)
      survived and complete          → published ("released")
      anything else, within window   → stays held
      anything else, window used up  → dropped ("expired"), never published
    """
    window = max_held_days(config)
    enriched_names = {_key(c.get("name")) for c in enriched}
    survivor_by_name = {_key(c.get("name")): c for c in survivors}
    held_cand_by_name = {_key(c.get("name")): c for c in held_candidates}

    publishable: list[dict] = []
    next_records: list[dict] = []
    outcome: dict[str, Any] = {
        "new": [], "kept": [], "released": [], "expired": [],
        "reregistered": [], "rejected": [], "reasons": {},
    }

    def _hold(cand: dict, reasons: list[str], first_held: date, checks: int) -> bool:
        """Keep `cand` in the queue unless its window is used up. True if kept."""
        name = _key(cand.get("name"))
        if (today - first_held).days >= window:
            outcome["expired"].append(name)
            return False
        next_records.append(_held_record(
            cand, first_held=first_held, today=today, checks=checks, reasons=reasons,
        ))
        for reason in reasons:
            outcome["reasons"][reason] = outcome["reasons"].get(reason, 0) + 1
        return True

    # Previously held domains first, in queue order.
    settled: set[str] = set()
    for rec in held_records:
        name = _key(rec.get("name"))
        cand = held_cand_by_name.get(name)
        if not name or cand is None or name in settled:
            continue
        settled.add(name)
        first_held = _first_held_date(rec.get("first_held_date"), today)
        try:
            checks = int(rec.get("checks") or 0) + 1
        except (TypeError, ValueError):
            checks = 1
        if cand.get("is_available") is False:
            outcome["reregistered"].append(name)
            continue
        if name in survivor_by_name:
            reasons = incomplete_reasons(survivor_by_name[name], config)
            if not reasons:
                publishable.append(survivor_by_name[name])
                outcome["released"].append(name)
                continue
        elif name in enriched_names:
            outcome["rejected"].append(name)
            continue
        elif cand.get("is_available") is True:
            reasons = ["not_enriched"]
        else:
            reasons = ["rdap_unknown"]
        if _hold(cand, reasons, first_held, checks):
            outcome["kept"].append(name)

    # Today's new candidates.
    held_names = set(held_cand_by_name)
    for cand in new_available:
        name = _key(cand.get("name"))
        if not name or name in held_names:
            continue
        if name in survivor_by_name:
            reasons = incomplete_reasons(survivor_by_name[name], config)
            if not reasons:
                publishable.append(survivor_by_name[name])
                continue
        elif name in enriched_names:
            continue  # rejected by the post-enrichment filter as usual
        else:
            reasons = ["not_enriched"]
        if _hold(cand, reasons, today, 0):
            outcome["new"].append(name)

    outcome["held"] = len(next_records)
    return publishable, next_records, outcome


def log_outcome(outcome: dict[str, Any], *, saved: bool | None) -> None:
    """Log the run's held-queue result.

    The first line is a CONTRACT parsed by send_report.py:

        Held queue: held=12 new=5 released=3 expired=2 reregistered=1 rejected=0

    `saved` is True/False for the R2 write, None when the write was skipped
    because the load had failed. Never raises.
    """
    try:
        logger.info(
            "Held queue: held=%d new=%d released=%d expired=%d reregistered=%d rejected=%d",
            outcome.get("held", 0), len(outcome.get("new", [])),
            len(outcome.get("released", [])), len(outcome.get("expired", [])),
            len(outcome.get("reregistered", [])), len(outcome.get("rejected", [])),
        )
        if outcome.get("reasons"):
            logger.info(
                "Held queue reasons (domains still held, per reason): %s",
                dict(sorted(outcome["reasons"].items())),
            )
        for label in ("released", "expired", "reregistered", "rejected"):
            names = outcome.get(label) or []
            if names:
                logger.info("Held queue %s: %s", label, ", ".join(sorted(names)))
        if saved is False:
            logger.error(
                "Held queue: SAVE FAILED — %d held domain(s) were not persisted. "
                "They are NOT published; they are simply lost from the retry queue.",
                outcome.get("held", 0),
            )
        elif saved is None:
            logger.error(
                "Held queue: SAVE SKIPPED because the load failed — %d domain(s) "
                "held back today were not persisted (not published either).",
                outcome.get("held", 0),
            )
    except Exception as exc:  # pragma: no cover — reporting must never break a run
        logger.warning("Held queue summary unavailable (%s).", exc)


# ---------------------------------------------------------------------------
# R2 plumbing (mirrors toxic_denylist)
# ---------------------------------------------------------------------------


def _resolve_r2(r2_client: Any | None, r2_bucket: str | None) -> tuple[Any, str]:
    if r2_client is not None and r2_bucket is not None:
        return r2_client, r2_bucket
    from scripts import diff

    return (
        r2_client if r2_client is not None else diff._r2_client(),
        r2_bucket if r2_bucket is not None else diff._bucket(),
    )


def _get_object_or_empty(s3: Any, bucket: str, key: str) -> bytes:
    from botocore.exceptions import ClientError

    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            logger.info("Held queue: no r2://%s/%s yet — cold start, empty queue", bucket, key)
            return b""
        raise
    return resp["Body"].read()


def _parse_jsonl(raw: bytes) -> list[dict]:
    """Parse JSONL records, skipping corrupt lines so one bad line does not
    cost the whole queue."""
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
            logger.warning("Held queue: skipping corrupt JSONL line (%s)", exc)
            continue
        if isinstance(obj, dict) and _key(obj.get("name")):
            out.append(obj)
        else:
            logger.warning("Held queue: skipping JSONL line without a name")
    return out


def _serialize_jsonl(records: list[dict]) -> bytes:
    if not records:
        return b""
    return ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode("utf-8")


def load_held(
    *,
    r2_client: Any | None = None,
    r2_bucket: str | None = None,
) -> list[dict] | None:
    """The held queue from R2. [] on a cold start, None if the load FAILED.

    The None/[] distinction matters: on None the pipeline must not save,
    or it would overwrite the stored queue with only today's additions.
    """
    try:
        s3, bucket = _resolve_r2(r2_client, r2_bucket)
        records = _parse_jsonl(_get_object_or_empty(s3, bucket, HELD_R2_KEY))
        logger.info(
            "Held queue: loaded %d held domain(s) from r2://%s/%s",
            len(records), bucket, HELD_R2_KEY,
        )
        return records
    except Exception as exc:
        logger.error(
            "Held queue: LOAD FAILED (%s: %s) — previously held domains are "
            "not re-checked this run. Incomplete domains are still held back "
            "from publication.",
            type(exc).__name__, exc,
        )
        return None


def save_held(
    records: list[dict],
    *,
    r2_client: Any | None = None,
    r2_bucket: str | None = None,
) -> bool:
    """Overwrite the held queue in R2. Returns False on failure, never raises."""
    try:
        s3, bucket = _resolve_r2(r2_client, r2_bucket)
        s3.put_object(
            Bucket=bucket,
            Key=HELD_R2_KEY,
            Body=_serialize_jsonl(records),
            ContentType="application/json; charset=utf-8",
        )
        logger.info(
            "Held queue: wrote %d held domain(s) to r2://%s/%s",
            len(records), bucket, HELD_R2_KEY,
        )
        return True
    except Exception as exc:
        logger.error("Held queue: R2 write failed (%s: %s)", type(exc).__name__, exc)
        return False
