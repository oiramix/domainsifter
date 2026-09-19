"""One-off diagnostic: probe the freshness of ICANN's .com zone file.

Authenticates to CZDS (reusing scripts/czds_client.py's auth helper), then
issues a single header-only request against the com.zone download URL to read
Last-Modified / ETag / Content-Length / Date WITHOUT downloading the ~4.6 GB
body. Prints the host's current UTC time alongside for clock comparison.

This script is intentionally inert with respect to the production pipeline:
  - It does NOT import or invoke scripts/pipeline.py.
  - It does NOT touch R2 (no boto3, no diff state).
  - It does NOT read or write scripts/state/ or any sentinel file.
  - It does NOT interact with systemd.
  - It only READS scripts/czds_client.py's authenticate() helper.

Safe to run any time, any number of times, without affecting the cron.

Usage (from repo root):
    python -m scripts.probe_com_freshness
    # or
    python scripts/probe_com_freshness.py

Requires CZDS_USERNAME / CZDS_PASSWORD in the environment (same as the
pipeline). Exit 0 on success; non-zero on auth or HTTP failure.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# Allow running as a bare script (python scripts/probe_com_freshness.py) by
# putting the repo root on sys.path so `from scripts import czds_client` works
# the same way it does under `python -m`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import czds_client  # noqa: E402  (after sys.path shim)

COM_ZONE_URL = "https://czds-download-api.icann.org/czds/downloads/com.zone"
HEADERS_OF_INTEREST = ("Last-Modified", "ETag", "Content-Length", "Content-Range", "Date")
REQUEST_TIMEOUT = 30


def _print_headers(resp: requests.Response, *, how: str) -> None:
    print(f"--- response headers (via {how}, HTTP {resp.status_code}) ---")
    for name in HEADERS_OF_INTEREST:
        value = resp.headers.get(name)
        if value is not None:
            print(f"{name}: {value}")
        elif name != "Content-Range":  # Content-Range only exists on a 206
            print(f"{name}: <absent>")


def main() -> int:
    username = os.environ.get("CZDS_USERNAME")
    password = os.environ.get("CZDS_PASSWORD")
    if not username or not password:
        print("ERROR: CZDS_USERNAME / CZDS_PASSWORD must be set in the environment.", file=sys.stderr)
        return 2

    try:
        token = czds_client.authenticate(username, password)
    except czds_client.CzdsAuthError as exc:
        print(f"ERROR: CZDS authentication failed: {exc}", file=sys.stderr)
        return 3

    auth_headers = {"Authorization": f"Bearer {token}"}

    # Step 1: try HEAD — cheapest possible way to read headers, no body at all.
    head_resp: requests.Response | None = None
    try:
        head_resp = requests.head(
            COM_ZONE_URL, headers=auth_headers, timeout=REQUEST_TIMEOUT, allow_redirects=True,
        )
    except requests.RequestException as exc:
        print(f"WARNING: HEAD request raised ({exc}); will try a Range GET fallback.", file=sys.stderr)

    if head_resp is not None and head_resp.status_code == 200:
        _print_headers(head_resp, how="HEAD")
        print(f"\nHost UTC now: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
        return 0

    if head_resp is not None:
        print(
            f"NOTE: HEAD returned HTTP {head_resp.status_code} (unsupported?); "
            f"falling back to a single Range: bytes=0-0 GET.",
            file=sys.stderr,
        )

    # Step 2: fall back to a 1-byte ranged GET. stream=True + reading only
    # headers means the 4.6 GB body is never pulled down. Range bytes=0-0
    # asks for a single byte, so even a server that ignores Range and sends
    # 200 would stream — hence we explicitly close without iterating content.
    range_headers = {**auth_headers, "Range": "bytes=0-0"}
    try:
        with requests.get(
            COM_ZONE_URL, headers=range_headers, timeout=REQUEST_TIMEOUT,
            stream=True, allow_redirects=True,
        ) as get_resp:
            if get_resp.status_code not in (200, 206):
                print(
                    f"ERROR: Range GET returned HTTP {get_resp.status_code}: "
                    f"{get_resp.text[:200]}",
                    file=sys.stderr,
                )
                return 4
            _print_headers(get_resp, how="GET Range: bytes=0-0")
    except requests.RequestException as exc:
        print(f"ERROR: Range GET failed: {exc}", file=sys.stderr)
        return 4

    print(f"\nHost UTC now: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
