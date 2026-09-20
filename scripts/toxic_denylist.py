"""Durable memory of every domain ever classified toxic.

Why this exists (2026-09-20)
----------------------------
`snapshot_classifier` labels a domain legitimate / parked / toxic / empty /
unknown by reading its most-recent Wayback snapshot. `unknown` is ALSO the
failure value: it is what a candidate gets when the archive.org fetch fails,
and that happens constantly (184 of 289 fetches failed on one sweep on
2026-09-19).

Verdicts were recomputed from scratch on every run and nothing was
remembered, so a transient network failure silently ERASED a known-correct
abuse verdict:

    2026-09-19  ridgemotorsports.net fetched OK  → classified TOXIC, evicted.
    2026-09-19  (later) + 2026-09-20  fetch FAILED → came back `unknown`,
                passed the toxic gate, stayed on the published list, and was
                given a permanent archive page. Its archived content is an
                Indonesian online-slot gambling site. Two more domains
                (fithealthyplanet.com, wyevalleyhistory.net) took the same
                path.

This module is the fix: an append-only record of every name ever judged
toxic. The filter consults it in addition to this run's category, so a
verdict once earned can never be forgotten by a flaky fetch.

Storage: R2, NEVER the repo
---------------------------
The file lives at `state/toxic_denylist.jsonl` in the same R2 bucket the
zone snapshots and `state/phase2_overflow.jsonl` use, and it must NOT be
committed to this repository, which is public. This is a list of specific
named domains we have judged to be abusive. Publishing accusations about
identifiable third parties is a liability we have no reason to take on, and
a classifier false positive published under our name is a defamation risk,
not just a data-quality one. Keep it private; it is an operational input,
not content.

No expiry
---------
Unlike `state/phase2_overflow.jsonl` (14-day rolling window), a toxic
verdict never ages out. The window on overflow exists because a
pick-pool entry goes stale; a toxic verdict does not, because the
ARCHIVED content that earned it does not become clean later. The record
is append-only and append-forever.

Failure behaviour (hard rule 17)
--------------------------------
Both entry points fail soft and never raise:

  - `load_denylist` on any R2 error logs LOUDLY and returns an empty set.
    The gate then degrades to exactly today's behaviour (live category
    only). Losing the memory must not block a daily run.
  - `record_toxic` on any R2 error logs and returns 0. The verdict is lost
    for that run and will be re-learned the next time the fetch succeeds.

Both take an injected client/bucket so the filter and tests never touch a
live endpoint (hard rules 13 and 16 — this module holds no module-level
state; the loaded set is passed IN to `filter.keep_post_enrichment`).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Mirrors phase2_ranker.OVERFLOW_R2_KEY: same bucket, same `state/` prefix,
# same JSONL-append shape. Deliberately a module constant rather than a
# config knob — it is an identifier for a specific stored object, not a
# tunable threshold (hard rule 9 governs thresholds and magic numbers).
DENYLIST_R2_KEY = "state/toxic_denylist.jsonl"

_NOT_FOUND_CODES = {"NoSuchKey", "404", "NotFound"}


# ---------------------------------------------------------------------------
# R2 plumbing (mirrors phase2_ranker.record_overflow)
# ---------------------------------------------------------------------------


def _resolve_r2(r2_client: Any | None, r2_bucket: str | None) -> tuple[Any, str]:
    """Return (client, bucket), constructing defaults from env only if needed.

    Imported lazily so this module stays importable (and testable) without
    boto3 credentials in the environment.
    """
    if r2_client is not None and r2_bucket is not None:
        return r2_client, r2_bucket
    from scripts import diff

    return (
        r2_client if r2_client is not None else diff._r2_client(),
        r2_bucket if r2_bucket is not None else diff._bucket(),
    )


def _get_object_or_empty(s3: Any, bucket: str, key: str) -> bytes:
    """Read the denylist object; return b'' when it does not exist yet.

    A missing object is a cold start, not an error. Every other ClientError
    propagates to the caller, which decides how to fail soft.
    """
    from botocore.exceptions import ClientError

    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            logger.info(
                "Toxic denylist: no r2://%s/%s yet — cold start, empty denylist",
                bucket, key,
            )
            return b""
        raise
    return resp["Body"].read()


def _parse_jsonl(raw: bytes) -> list[dict]:
    """Parse JSONL into dict records, skipping anything malformed.

    A single corrupt line (truncated write, partial upload) must not cost us
    the whole memory — every other line is still a valid verdict. Skipped
    lines are logged so corruption is visible in the run log.
    """
    out: list[dict] = []
    if not raw:
        return out
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.error("Toxic denylist: object is not valid UTF-8 (%s)", exc)
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Toxic denylist: skipping corrupt JSONL line (%s)", exc)
            continue
        if not isinstance(obj, dict):
            logger.warning("Toxic denylist: skipping non-object JSONL line")
            continue
        out.append(obj)
    return out


def _serialize_jsonl(records: list[dict]) -> bytes:
    if not records:
        return b""
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    return (body + "\n").encode("utf-8")


def _names_of(records: Iterable[dict]) -> set[str]:
    """Lowercase name set from parsed records; records without a usable
    `name` are ignored (a verdict with no subject is not a verdict)."""
    names: set[str] = set()
    for r in records:
        name = str(r.get("name") or "").strip().lower()
        if name:
            names.add(name)
    return names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_denylist(
    *,
    r2_client: Any | None = None,
    r2_bucket: str | None = None,
) -> set[str]:
    """Return the set of lowercase domain names ever classified toxic.

    Fails soft (hard rule 17): on ANY R2 or parse failure this logs at ERROR
    and returns an empty set. An empty set makes the filter's memory gate a
    no-op, which is precisely the pre-2026-09-20 behaviour — degraded, but
    never a blocked run.

    Pass the set to `filter.keep_post_enrichment(..., toxic_denylist=...)`.
    """
    try:
        s3, bucket = _resolve_r2(r2_client, r2_bucket)
        raw = _get_object_or_empty(s3, bucket, DENYLIST_R2_KEY)
        names = _names_of(_parse_jsonl(raw))
        logger.info(
            "Toxic denylist: loaded %d remembered toxic domain(s) from r2://%s/%s",
            len(names), bucket, DENYLIST_R2_KEY,
        )
        return names
    except Exception as exc:
        logger.error(
            "Toxic denylist: LOAD FAILED (%s: %s) — proceeding with an EMPTY "
            "denylist. The toxic gate is degraded to this run's live "
            "classifier verdicts only, so a domain previously judged toxic "
            "can slip back onto the published list if today's fetch fails.",
            type(exc).__name__, exc,
        )
        return set()


def record_toxic(
    names: Iterable[str],
    *,
    today: date,
    classifier_version: str,
    r2_client: Any | None = None,
    r2_bucket: str | None = None,
) -> int:
    """Append newly-toxic domains to the durable denylist. Returns how many
    NEW records were written (0 when there is nothing new, or on failure).

    Idempotent: a name already present in the stored file is not written
    again, so re-running a day's classification cannot duplicate entries.
    Existing records are never rewritten or aged out — the file is
    append-only, and the first `classified_date` for a name is the one that
    survives.

    Record shape (one JSON object per line):

        {"name": "marketglow.com",
         "classified_date": "2026-09-20",
         "classifier_version": "v2"}

    Fails soft (hard rule 17): R2 errors are logged and swallowed. A lost
    append means the verdict is re-learned on the next successful fetch;
    it must never crash the daily run.
    """
    incoming: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name or "").strip().lower()
        if name and name not in seen:
            seen.add(name)
            incoming.append(name)

    if not incoming:
        return 0

    try:
        s3, bucket = _resolve_r2(r2_client, r2_bucket)
        existing_records = _parse_jsonl(
            _get_object_or_empty(s3, bucket, DENYLIST_R2_KEY)
        )
        already = _names_of(existing_records)

        new_records = [
            {
                "name": name,
                "classified_date": today.isoformat(),
                "classifier_version": classifier_version,
            }
            for name in incoming
            if name not in already
        ]
        if not new_records:
            logger.info(
                "Toxic denylist: 0 new records (all %d already remembered)",
                len(incoming),
            )
            return 0

        s3.put_object(
            Bucket=bucket,
            Key=DENYLIST_R2_KEY,
            Body=_serialize_jsonl(existing_records + new_records),
            ContentType="application/json; charset=utf-8",
        )
        logger.info(
            "Toxic denylist: appended %d new toxic domain(s) to r2://%s/%s "
            "(%d already remembered; %d total)",
            len(new_records), bucket, DENYLIST_R2_KEY,
            len(incoming) - len(new_records),
            len(existing_records) + len(new_records),
        )
        return len(new_records)
    except Exception as exc:
        logger.error(
            "Toxic denylist: APPEND FAILED (%s: %s) — %d toxic verdict(s) "
            "were NOT persisted and will have to be re-earned by a future "
            "successful classification: %s",
            type(exc).__name__, exc, len(incoming), ", ".join(incoming[:10]),
        )
        return 0


def is_enabled(config: dict) -> bool:
    """Kill switch for the whole memory gate, read from
    `config["toxic_denylist"]["enabled"]`. Defaults to True so a config
    without the section behaves as designed rather than silently off.

    Exists so a bad classifier run that poisons the denylist can be
    neutralised by a config edit instead of a code change. Call sites check
    this before calling `load_denylist` / `record_toxic`; the filter itself
    just receives whatever set it is given.
    """
    section = (config or {}).get("toxic_denylist") or {}
    return bool(section.get("enabled", True))
