"""Spam / malware / phishing check (v1: Google Safe Browsing).

Module is named generically — v2 swaps the internal call to Google Web Risk
without changing the function signature or returned keys.

Endpoint: https://safebrowsing.googleapis.com/v4/threatMatches:find
Auth: API key as `?key=<KEY>` query param.

Returned fields:
    {
        "spam_flagged": bool,
        "spam_threat_types": list[str]   # empty list when not flagged
    }

API key from `os.environ["SAFE_BROWSING_KEY"]`.

UNLIKE the other enrichment modules, a missing key here raises
`SpamCheckConfigError`. spam_check is a CORE filter rule — silently degraded
filtering would let malware domains slip through. The pipeline's startup
validation (`scripts.env_check`) catches the missing key before any
enrichment runs, so this raise is a defence-in-depth.

Per-domain API failures (network, 5xx, malformed JSON) still return an empty
dict — those are transient and do NOT warrant aborting the run, but the
filter module treats "no spam_flagged field" as conservative-reject when the
key IS configured (see filter.py).
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
_KEY_ENV_VAR = "SAFE_BROWSING_KEY"
_THREAT_TYPES = [
    "MALWARE",
    "SOCIAL_ENGINEERING",
    "UNWANTED_SOFTWARE",
    "POTENTIALLY_HARMFUL_APPLICATION",
]


class SpamCheckConfigError(RuntimeError):
    """Raised when SAFE_BROWSING_KEY is not configured. Pipeline must abort —
    we will not produce a daily list with degraded malware filtering."""


def enrich(domain: str, config: dict) -> dict:
    api_key = os.environ.get(_KEY_ENV_VAR)
    if not api_key:
        raise SpamCheckConfigError(
            "SAFE_BROWSING_KEY environment variable is not set. "
            "spam_check is a core filter rule and cannot be silently skipped."
        )

    endpoint = config.get("api_endpoints", {}).get("safe_browsing", _DEFAULT_ENDPOINT)
    timeout = config.get("request_timeout_seconds", 10)
    body = {
        "client": {"clientId": "domainsifter", "clientVersion": "1.0"},
        "threatInfo": {
            "threatTypes": _THREAT_TYPES,
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": f"http://{domain}/"}],
        },
    }

    try:
        response = requests.post(
            endpoint, params={"key": api_key}, json=body, timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("spam_check enrich failed for %s: %s", domain, exc)
        return {}

    matches = data.get("matches") if isinstance(data, dict) else None
    if not matches:
        return {"spam_flagged": False, "spam_threat_types": []}

    threat_types = sorted({m.get("threatType") for m in matches if m.get("threatType")})
    return {"spam_flagged": True, "spam_threat_types": threat_types}
