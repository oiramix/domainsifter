"""Per-TLD set diff between yesterday's zone snapshot and today's, backed by Cloudflare R2.

State layout in R2 (S3-compatible):
    bucket: $R2_BUCKET_NAME (e.g., domainsifter-state)
    key:    state/{tld}_yesterday.txt
            — one apex name per line, sorted, lowercase, no trailing dots,
              UTF-8, LF newlines.

`compute_drops` returns the set of names that were in yesterday but are gone
today (i.e. dropped from the zone — candidates for our list). The first time
a TLD is seen there's no R2 object, so `load_yesterday` returns an empty set
and `commit_today` writes today's snapshot for tomorrow's run.

`commit_today` overwrites the prior snapshot. The pipeline calls this AFTER
the parsed-zone set is in hand, so a crashed enrichment leaves yesterday
intact (R2 keeps the previous version on cold-start; on warm runs the new
snapshot is the canonical baseline for the next day).

WHY R2 INSTEAD OF A REPO-COMMITTED FILE:
    v1 originally committed `scripts/state/{tld}_yesterday.txt` to the repo
    (PLAN.md Principle 4). On the first manual run the .org snapshot was
    228 MB and .xyz was 136 MB — both blew past GitHub's 100 MB per-file
    hard limit and the daily commit was rejected. R2 was already planned
    for v2; we brought it forward to unblock v1.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_R2_REGION = "auto"
_NOT_FOUND_CODES = {"NoSuchKey", "404", "NotFound"}


def _object_key(tld: str) -> str:
    """R2 object key for a TLD's yesterday snapshot."""
    return f"state/{tld.lower()}_yesterday.txt"


def _r2_client() -> Any:
    """Construct an S3-compatible boto3 client pointed at Cloudflare R2.

    Reads R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY from the
    environment. Callers should pass the result via `client=` to avoid
    re-authenticating per call.
    """
    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name=_R2_REGION,
    )


def _bucket() -> str:
    return os.environ["R2_BUCKET_NAME"]


def load_yesterday(tld: str, *, client: Any = None, bucket: str | None = None) -> set[str]:
    """Return the set of names recorded for this TLD on the previous run.
    Returns an empty set if no R2 object exists yet (cold start)."""
    s3 = client if client is not None else _r2_client()
    bucket_name = bucket if bucket is not None else _bucket()
    key = _object_key(tld)
    try:
        resp = s3.get_object(Bucket=bucket_name, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            logger.info("No prior R2 snapshot for .%s — cold start", tld)
            return set()
        raise
    body = resp["Body"].read().decode("utf-8")
    return {line.strip() for line in body.splitlines() if line.strip()}


def compute_drops(yesterday: set[str], today: set[str]) -> set[str]:
    """Names present yesterday but missing today — these were dropped."""
    return yesterday - today


def commit_today(
    tld: str,
    today: set[str],
    *,
    client: Any = None,
    bucket: str | None = None,
) -> str:
    """Write today's snapshot to R2 (sorted, one per line). Returns the key."""
    s3 = client if client is not None else _r2_client()
    bucket_name = bucket if bucket is not None else _bucket()
    key = _object_key(tld)
    sorted_names = sorted(today)
    body = "\n".join(sorted_names)
    if sorted_names:
        body += "\n"
    s3.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    logger.info("Wrote %d names to r2://%s/%s", len(sorted_names), bucket_name, key)
    return key


def diff_and_commit(
    tld: str,
    today: set[str],
    *,
    client: Any = None,
    bucket: str | None = None,
) -> set[str]:
    """Convenience: load yesterday, compute drops, write today, return drops.

    NOTE: this commits BEFORE downstream enrichment runs. If you want
    crash-safety where a failed enrichment doesn't lose the diff, call
    `load_yesterday` + `compute_drops` first, run enrichment, then call
    `commit_today` only after success. The pipeline orchestrator decides.
    """
    s3 = client if client is not None else _r2_client()
    bucket_name = bucket if bucket is not None else _bucket()
    yesterday = load_yesterday(tld, client=s3, bucket=bucket_name)
    drops = compute_drops(yesterday, today)
    commit_today(tld, today, client=s3, bucket=bucket_name)
    logger.info(".%s: %d in zone today, %d dropped since yesterday", tld, len(today), len(drops))
    return drops
