"""Unit tests for scripts/czds_client.py — all HTTP is mocked via `responses`."""

from __future__ import annotations

import pytest
import responses

from scripts import czds_client
from scripts.czds_client import CzdsApiError, CzdsAuthError

AUTH_BASE = "https://account-api.icann.org"
API_BASE = "https://czds-api.icann.org"
ZONE_URL = "https://czds-download-api.icann.org/czds/downloads/com.zone"


@responses.activate
def test_authenticate_returns_token_on_200():
    responses.add(
        responses.POST,
        AUTH_BASE + "/api/authenticate",
        json={"accessToken": "tok-xyz"},
        status=200,
    )
    assert czds_client.authenticate("user", "pw", AUTH_BASE) == "tok-xyz"


@responses.activate
def test_authenticate_raises_on_401():
    responses.add(
        responses.POST,
        AUTH_BASE + "/api/authenticate",
        json={"message": "bad credentials"},
        status=401,
    )
    with pytest.raises(CzdsAuthError):
        czds_client.authenticate("user", "wrong", AUTH_BASE)


@responses.activate
def test_authenticate_raises_when_token_missing():
    responses.add(
        responses.POST,
        AUTH_BASE + "/api/authenticate",
        json={"message": "ok but no token"},
        status=200,
    )
    with pytest.raises(CzdsAuthError):
        czds_client.authenticate("user", "pw", AUTH_BASE)


@responses.activate
def test_authenticate_raises_on_non_json_body():
    responses.add(
        responses.POST,
        AUTH_BASE + "/api/authenticate",
        body="<html>500</html>",
        status=200,
        content_type="text/html",
    )
    with pytest.raises(CzdsAuthError):
        czds_client.authenticate("user", "pw", AUTH_BASE)


def test_authenticate_raises_on_connection_error(monkeypatch):
    import requests as _requests

    def boom(*_args, **_kwargs):
        raise _requests.ConnectionError("DNS exploded")

    monkeypatch.setattr(czds_client.requests, "post", boom)
    with pytest.raises(CzdsAuthError):
        czds_client.authenticate("user", "pw", AUTH_BASE)


@responses.activate
def test_list_zone_links_returns_string_list():
    links = [
        ZONE_URL,
        "https://czds-download-api.icann.org/czds/downloads/net.zone",
    ]
    responses.add(
        responses.GET,
        API_BASE + "/czds/downloads/links",
        json=links,
        status=200,
    )
    assert czds_client.list_zone_links("tok", API_BASE) == links


@responses.activate
def test_list_zone_links_sends_bearer_header():
    responses.add(
        responses.GET,
        API_BASE + "/czds/downloads/links",
        json=[],
        status=200,
    )
    czds_client.list_zone_links("tok-abc", API_BASE)
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok-abc"


@responses.activate
def test_list_zone_links_raises_on_non_list_payload():
    responses.add(
        responses.GET,
        API_BASE + "/czds/downloads/links",
        json={"not": "a list"},
        status=200,
    )
    with pytest.raises(CzdsApiError):
        czds_client.list_zone_links("tok", API_BASE)


@responses.activate
def test_list_zone_links_raises_on_403():
    responses.add(
        responses.GET,
        API_BASE + "/czds/downloads/links",
        json={"message": "forbidden"},
        status=403,
    )
    with pytest.raises(CzdsApiError):
        czds_client.list_zone_links("tok", API_BASE)


@responses.activate
def test_download_zone_streams_to_disk(tmp_path):
    payload = b"\x1f\x8bfake-gzip-bytes" * 256
    responses.add(responses.GET, ZONE_URL, body=payload, status=200)
    out = tmp_path / "com.zone.gz"
    written = czds_client.download_zone(ZONE_URL, "tok", str(out))
    assert written == len(payload)
    assert out.read_bytes() == payload


@responses.activate
def test_download_zone_raises_on_404(tmp_path):
    responses.add(responses.GET, ZONE_URL, body="missing", status=404)
    out = tmp_path / "com.zone.gz"
    with pytest.raises(CzdsApiError):
        czds_client.download_zone(ZONE_URL, "tok", str(out))


@responses.activate
def test_download_zone_sends_bearer_header(tmp_path):
    responses.add(responses.GET, ZONE_URL, body=b"abc", status=200)
    czds_client.download_zone(ZONE_URL, "tok-xyz", str(tmp_path / "z.gz"))
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok-xyz"
