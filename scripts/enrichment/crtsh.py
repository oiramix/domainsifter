"""crt.sh certificate transparency lookup.

Asks crt.sh for the cert history of `%.{domain}` (covers the apex and any
subdomain leaves that ever had a publicly logged cert). The presence of any
historical cert is a weak-but-useful signal that the domain was used for
a real service at some point.

Endpoint: https://crt.sh/?q=%25.example.com&output=json
The `%25` is URL-encoded `%`; we let `requests` encode the `%.` prefix for us.

Returned fields:
    {
        "cert_history": bool,      # True if any cert ever logged
        "cert_count": int,         # number of unique cert IDs returned
    }

Empty dict on transport failure or non-JSON response. crt.sh occasionally
returns HTML error pages instead of JSON when overloaded — those count as
failures, not "no certs".
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://crt.sh"


def enrich(domain: str, config: dict) -> dict:
    base = config.get("api_endpoints", {}).get("crtsh", _DEFAULT_BASE).rstrip("/")
    timeout = config.get("request_timeout_seconds", 10)
    params = {"q": f"%.{domain}", "output": "json"}

    try:
        response = requests.get(base + "/", params=params, timeout=timeout)
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("crt.sh enrich failed for %s: %s", domain, exc)
        return {}

    if not isinstance(rows, list):
        return {}

    cert_ids = {row.get("id") for row in rows if isinstance(row, dict) and row.get("id") is not None}
    count = len(cert_ids)
    return {"cert_history": count > 0, "cert_count": count}
