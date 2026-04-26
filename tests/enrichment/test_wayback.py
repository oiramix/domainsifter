"""Unit tests for scripts/enrichment/wayback.py."""

from __future__ import annotations

import responses

from scripts.enrichment import wayback

CONFIG = {
    "api_endpoints": {"wayback_cdx": "https://web.archive.org/cdx/search/cdx"},
    "request_timeout_seconds": 5,
}


@responses.activate
def test_enrich_returns_count_and_latest_date():
    payload = [
        ["timestamp"],
        ["20200101000000"],
        ["20240815120000"],
        ["20180510080000"],
    ]
    responses.add(responses.GET, "https://web.archive.org/cdx/search/cdx", json=payload, status=200)
    result = wayback.enrich("example.com", CONFIG)
    assert result == {"wayback_snapshots": 3, "wayback_last_snapshot": "2024-08-15"}


@responses.activate
def test_enrich_returns_zero_when_no_snapshots():
    responses.add(responses.GET, "https://web.archive.org/cdx/search/cdx", json=[], status=200)
    assert wayback.enrich("example.com", CONFIG) == {
        "wayback_snapshots": 0,
        "wayback_last_snapshot": None,
    }


@responses.activate
def test_enrich_returns_zero_when_only_header_row():
    responses.add(
        responses.GET,
        "https://web.archive.org/cdx/search/cdx",
        json=[["timestamp"]],
        status=200,
    )
    assert wayback.enrich("example.com", CONFIG) == {
        "wayback_snapshots": 0,
        "wayback_last_snapshot": None,
    }


@responses.activate
def test_enrich_returns_empty_dict_on_5xx():
    responses.add(responses.GET, "https://web.archive.org/cdx/search/cdx", status=503)
    assert wayback.enrich("example.com", CONFIG) == {}


@responses.activate
def test_enrich_returns_empty_dict_on_invalid_json():
    responses.add(
        responses.GET,
        "https://web.archive.org/cdx/search/cdx",
        body="<html>nope</html>",
        status=200,
        content_type="text/html",
    )
    assert wayback.enrich("example.com", CONFIG) == {}


def test_enrich_returns_empty_dict_on_connection_error(monkeypatch):
    import requests as _requests

    def boom(*_a, **_k):
        raise _requests.ConnectionError("nope")

    monkeypatch.setattr(wayback.requests, "get", boom)
    assert wayback.enrich("example.com", CONFIG) == {}


def test_enrich_uses_default_endpoint_when_config_missing():
    config = {}
    import requests as _requests

    def boom(*_a, **_k):
        raise _requests.ConnectionError("nope")

    import scripts.enrichment.wayback as w
    original_get = w.requests.get
    try:
        w.requests.get = boom
        assert wayback.enrich("example.com", config) == {}
    finally:
        w.requests.get = original_get
