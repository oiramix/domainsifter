"""Unit tests for scripts/enrichment/crtsh.py."""

from __future__ import annotations

import responses

from scripts.enrichment import crtsh

CONFIG = {
    "api_endpoints": {"crtsh": "https://crt.sh"},
    "request_timeout_seconds": 5,
}


@responses.activate
def test_enrich_returns_history_true_with_count():
    payload = [
        {"id": 1, "name_value": "example.com"},
        {"id": 2, "name_value": "www.example.com"},
        {"id": 1, "name_value": "example.com"},
    ]
    responses.add(responses.GET, "https://crt.sh/", json=payload, status=200)
    assert crtsh.enrich("example.com", CONFIG) == {"cert_history": True, "cert_count": 2}


@responses.activate
def test_enrich_returns_history_false_when_empty_list():
    responses.add(responses.GET, "https://crt.sh/", json=[], status=200)
    assert crtsh.enrich("nocerts.com", CONFIG) == {"cert_history": False, "cert_count": 0}


@responses.activate
def test_enrich_uses_wildcard_query():
    responses.add(responses.GET, "https://crt.sh/", json=[], status=200)
    crtsh.enrich("foo.com", CONFIG)
    qs = responses.calls[0].request.url
    assert "q=%25.foo.com" in qs
    assert "output=json" in qs


@responses.activate
def test_enrich_returns_empty_on_5xx():
    responses.add(responses.GET, "https://crt.sh/", status=502)
    assert crtsh.enrich("example.com", CONFIG) == {}


@responses.activate
def test_enrich_returns_empty_on_html_response():
    responses.add(
        responses.GET,
        "https://crt.sh/",
        body="<html>overloaded</html>",
        status=200,
        content_type="text/html",
    )
    assert crtsh.enrich("example.com", CONFIG) == {}


@responses.activate
def test_enrich_returns_empty_on_non_list_payload():
    responses.add(responses.GET, "https://crt.sh/", json={"error": "bad"}, status=200)
    assert crtsh.enrich("example.com", CONFIG) == {}


def test_enrich_returns_empty_on_connection_error(monkeypatch):
    import requests as _requests

    def boom(*_a, **_k):
        raise _requests.ConnectionError("nope")

    monkeypatch.setattr(crtsh.requests, "get", boom)
    assert crtsh.enrich("example.com", CONFIG) == {}


# --- timeout retry behaviour -------------------------------------------------


def test_enrich_retries_on_read_timeout_and_succeeds(monkeypatch):
    """ReadTimeout, then success — crt.sh should return cert data, not empty."""
    import requests as _requests
    from unittest.mock import MagicMock

    timeouts_remaining = [1]

    def fake_get(*_a, **_kw):
        if timeouts_remaining[0] > 0:
            timeouts_remaining[0] -= 1
            raise _requests.exceptions.ReadTimeout("simulated")
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json = lambda: [{"id": 7}]
        return resp

    monkeypatch.setattr(crtsh.requests, "get", fake_get)
    from scripts.enrichment import _circuit_breaker as cb
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)

    result = crtsh.enrich("example.com", CONFIG)
    assert result == {"cert_history": True, "cert_count": 1}
    assert timeouts_remaining[0] == 0


def test_enrich_returns_empty_after_three_timeouts(monkeypatch):
    import requests as _requests

    call_count = [0]

    def fake_get(*_a, **_kw):
        call_count[0] += 1
        raise _requests.exceptions.ConnectTimeout("always")

    monkeypatch.setattr(crtsh.requests, "get", fake_get)
    from scripts.enrichment import _circuit_breaker as cb
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)

    failures_before = crtsh._BREAKER.consecutive_failures
    result = crtsh.enrich("example.com", CONFIG)
    assert result == {}
    assert call_count[0] == 3
    assert crtsh._BREAKER.consecutive_failures == failures_before + 1


def test_enrich_uses_per_enricher_timeout_override(monkeypatch):
    captured: dict = {}

    def fake_get(_url, *_a, **kw):
        captured["timeout"] = kw.get("timeout")
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json = lambda: []
        return resp

    monkeypatch.setattr(crtsh.requests, "get", fake_get)
    cfg = {
        **CONFIG,
        "request_timeout_seconds": 5,
        "api_request_timeout_seconds": {"crtsh": 60},
    }
    crtsh.enrich("example.com", cfg)
    assert captured["timeout"] == 60
