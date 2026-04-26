"""ICANN CZDS API client.

Handles authentication, zone-link enumeration, and zone-file streaming download.
Auth and download failures raise; the caller decides per-zone tolerance.

CZDS uses two hosts:
- account-api.icann.org for /api/authenticate (returns a JWT-style accessToken)
- czds-api.icann.org for /czds/downloads/links and the per-zone download URLs

Tokens are valid for ~24h. The pipeline obtains one token per run.
"""

from __future__ import annotations

import logging
from typing import Final

import requests

logger = logging.getLogger(__name__)

AUTH_PATH: Final = "/api/authenticate"
LINKS_PATH: Final = "/czds/downloads/links"

DEFAULT_AUTH_BASE: Final = "https://account-api.icann.org"
DEFAULT_API_BASE: Final = "https://czds-api.icann.org"


class CzdsAuthError(Exception):
    """CZDS authentication failed. Pipeline should abort — no zones can be fetched."""


class CzdsApiError(Exception):
    """A CZDS API call (links or download) failed at the protocol level."""


def authenticate(
    username: str,
    password: str,
    auth_base_url: str = DEFAULT_AUTH_BASE,
    timeout: int = 10,
) -> str:
    """Obtain a CZDS access token.

    Raises CzdsAuthError on transport failure, non-200 response, or missing token.
    """
    url = auth_base_url.rstrip("/") + AUTH_PATH
    try:
        response = requests.post(
            url,
            json={"username": username, "password": password},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise CzdsAuthError(f"CZDS auth request failed: {exc}") from exc

    if response.status_code != 200:
        raise CzdsAuthError(
            f"CZDS auth returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        token = response.json().get("accessToken")
    except ValueError as exc:
        raise CzdsAuthError(f"CZDS auth response was not valid JSON: {exc}") from exc

    if not token or not isinstance(token, str):
        raise CzdsAuthError("CZDS auth response missing accessToken")

    logger.info("CZDS authentication succeeded")
    return token


def list_zone_links(
    access_token: str,
    api_base_url: str = DEFAULT_API_BASE,
    timeout: int = 10,
) -> list[str]:
    """Return the list of zone download URLs the account is approved for."""
    url = api_base_url.rstrip("/") + LINKS_PATH
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise CzdsApiError(f"CZDS links request failed: {exc}") from exc

    if response.status_code != 200:
        raise CzdsApiError(
            f"CZDS links returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        links = response.json()
    except ValueError as exc:
        raise CzdsApiError(f"CZDS links response was not valid JSON: {exc}") from exc

    if not isinstance(links, list) or not all(isinstance(item, str) for item in links):
        raise CzdsApiError("CZDS links response was not a list of strings")

    logger.info("CZDS returned %d zone links", len(links))
    return links


def download_zone(
    url: str,
    access_token: str,
    output_path: str,
    timeout: int = 120,
    chunk_size: int = 65536,
) -> int:
    """Stream a gzipped zone file to output_path. Returns bytes written.

    The caller is responsible for placing output_path in a tempdir and deleting it
    after parsing — per project rule, raw zone files must never be committed.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    bytes_written = 0
    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout) as response:
            if response.status_code != 200:
                raise CzdsApiError(
                    f"CZDS download {url} returned HTTP {response.status_code}"
                )
            with open(output_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        fh.write(chunk)
                        bytes_written += len(chunk)
    except requests.RequestException as exc:
        raise CzdsApiError(f"CZDS download {url} failed: {exc}") from exc

    logger.info("Downloaded %d bytes from %s to %s", bytes_written, url, output_path)
    return bytes_written
