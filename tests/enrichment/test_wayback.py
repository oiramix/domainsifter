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


# --- timeout retry behaviour -------------------------------------------------


def test_enrich_retries_on_read_timeout_and_succeeds(monkeypatch):
    """Two ReadTimeouts in a row, then success on third attempt — Wayback
    should return the parsed snapshot count, not empty dict."""
    import requests as _requests
    from unittest.mock import MagicMock

    timeouts_remaining = [2]

    def fake_get(*_a, **_kw):
        if timeouts_remaining[0] > 0:
            timeouts_remaining[0] -= 1
            raise _requests.exceptions.ReadTimeout("simulated")
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json = lambda: [["timestamp"], ["20240101120000"]]
        return resp

    monkeypatch.setattr(wayback.requests, "get", fake_get)
    # Don't actually wait 5+15 seconds in tests.
    from scripts.enrichment import _circuit_breaker as cb
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)

    result = wayback.enrich("example.com", CONFIG)
    assert result == {"wayback_snapshots": 1, "wayback_last_snapshot": "2024-01-01"}
    assert timeouts_remaining[0] == 0  # exactly two timeouts consumed


def test_enrich_returns_empty_after_three_timeouts(monkeypatch):
    """All three attempts time out — Wayback returns empty dict and records
    one breaker failure (not three)."""
    import requests as _requests

    call_count = [0]

    def fake_get(*_a, **_kw):
        call_count[0] += 1
        raise _requests.exceptions.ReadTimeout("always")

    monkeypatch.setattr(wayback.requests, "get", fake_get)
    from scripts.enrichment import _circuit_breaker as cb
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)

    failures_before = wayback._BREAKER.consecutive_failures
    result = wayback.enrich("example.com", CONFIG)
    assert result == {}
    assert call_count[0] == 3  # 3 attempts total
    assert wayback._BREAKER.consecutive_failures == failures_before + 1


def test_enrich_does_not_retry_on_non_timeout_error(monkeypatch):
    """ConnectionError, HTTPError, JSON errors are NOT retried — they're
    final-failure signals. Only Timeout-family exceptions trigger retry."""
    import requests as _requests

    call_count = [0]

    def fake_get(*_a, **_kw):
        call_count[0] += 1
        raise _requests.ConnectionError("network down")

    monkeypatch.setattr(wayback.requests, "get", fake_get)
    from scripts.enrichment import _circuit_breaker as cb
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)

    result = wayback.enrich("example.com", CONFIG)
    assert result == {}
    assert call_count[0] == 1  # exactly one attempt, no retry


def test_enrich_uses_per_enricher_timeout_override(monkeypatch):
    """api_request_timeout_seconds.wayback overrides global request_timeout_seconds."""
    captured: dict = {}

    def fake_get(_url, *_a, **kw):
        captured["timeout"] = kw.get("timeout")
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json = lambda: []
        return resp

    monkeypatch.setattr(wayback.requests, "get", fake_get)
    cfg = {
        **CONFIG,
        "request_timeout_seconds": 5,
        "api_request_timeout_seconds": {"wayback": 60},
    }
    wayback.enrich("example.com", cfg)
    assert captured["timeout"] == 60
