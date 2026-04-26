"""Wayback Machine CDX enrichment.

Queries the Wayback CDX API for snapshot history of the domain. We hit the
host alone (no path) with `matchType=domain` to count snapshots across all
URLs ever archived under that domain.

Returned fields:
    {
        "wayback_snapshots": int,                # total snapshot count
        "wayback_last_snapshot": "YYYY-MM-DD",   # date of most recent snapshot, or None
    }

Endpoint: https://web.archive.org/cdx/search/cdx
Docs: https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server

We use `output=json&fl=timestamp&limit=-1` so the response is small (just the
last snapshot's timestamp) plus we ask for total via `showNumPages`. Simpler
approach: request `output=json&fl=timestamp` and let the result list size be
the count, then take the max timestamp. Counts are bounded (CZDS-eligible
domains rarely exceed 100k snapshots) but we cap with `limit=10000` defensively.

Returns empty dict on any failure.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://web.archive.org/cdx/search/cdx"


def enrich(domain: str, config: dict) -> dict:
    endpoint = config.get("api_endpoints", {}).get("wayback_cdx", _DEFAULT_ENDPOINT)
    timeout = config.get("request_timeout_seconds", 10)
    params = {
        "url": domain,
        "matchType": "domain",
        "output": "json",
        "fl": "timestamp",
        "limit": 10000,
    }
    try:
        response = requests.get(endpoint, params=params, timeout=timeout)
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Wayback enrich failed for %s: %s", domain, exc)
        return {}

    if not isinstance(rows, list) or len(rows) <= 1:
        return {"wayback_snapshots": 0, "wayback_last_snapshot": None}

    data_rows = rows[1:]
    snapshot_count = len(data_rows)
    timestamps = [row[0] for row in data_rows if row and isinstance(row[0], str)]
    last_snapshot = None
    if timestamps:
        latest = max(timestamps)
        if len(latest) >= 8:
            last_snapshot = f"{latest[0:4]}-{latest[4:6]}-{latest[6:8]}"

    return {
        "wayback_snapshots": snapshot_count,
        "wayback_last_snapshot": last_snapshot,
    }
