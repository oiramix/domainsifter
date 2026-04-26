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
