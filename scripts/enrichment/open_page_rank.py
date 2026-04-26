"""OpenPageRank enrichment.

Authority score for the domain. API is free up to 10k requests/day with an
API key registered at https://www.domcop.com/openpagerank/.

Endpoint: https://openpagerank.com/api/v1.0/getPageRank
Auth: header `API-OPR: <key>`
Request: GET with `domains[]=example.com` (supports up to 100 per call; we
send one at a time to keep the plugin contract simple — pipeline orchestrator
can batch later if rate becomes a concern).

Response:
    {
      "status_code": 200,
      "response": [
        {
          "status_code": 200,
          "error": "",
          "page_rank_integer": 4,
          "page_rank_decimal": "4.21",
          "rank": "1234567",
          "domain": "example.com"
        }
      ]
    }

Returned fields:
    {"open_page_rank": float}   — page_rank_decimal as float
                                  Returns 0.0 if API says domain has no rank.

API key comes from `os.environ["OPENPAGERANK_KEY"]`. If unset, returns empty
dict (this enrichment is silently skipped — never crashes the pipeline).
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://openpagerank.com/api/v1.0/getPageRank"
_KEY_ENV_VAR = "OPENPAGERANK_KEY"


def enrich(domain: str, config: dict) -> dict:
    api_key = os.environ.get(_KEY_ENV_VAR)
    if not api_key:
        logger.debug("OPENPAGERANK_KEY unset; skipping OPR for %s", domain)
        return {}

    endpoint = config.get("api_endpoints", {}).get("open_page_rank", _DEFAULT_ENDPOINT)
    timeout = config.get("request_timeout_seconds", 10)
    headers = {"API-OPR": api_key}
    params = [("domains[]", domain)]

    try:
        response = requests.get(endpoint, headers=headers, params=params, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OPR enrich failed for %s: %s", domain, exc)
        return {}

    entries = body.get("response") if isinstance(body, dict) else None
    if not isinstance(entries, list) or not entries:
        return {}

    entry = entries[0]
    if not isinstance(entry, dict):
        return {}

    raw = entry.get("page_rank_decimal", 0)
    try:
        score = float(raw)
    except (TypeError, ValueError):
        score = 0.0

    return {"open_page_rank": score}
