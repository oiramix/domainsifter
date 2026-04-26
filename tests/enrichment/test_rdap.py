"""Unit tests for scripts/enrichment/rdap.py — bootstrap + per-domain queries mocked."""

from __future__ import annotations

import pytest
import responses

from scripts.enrichment import rdap


@pytest.fixture(autouse=True)
def _clear_bootstrap_cache():
    rdap._fetch_bootstrap.cache_clear()
    yield
    rdap._fetch_bootstrap.cache_clear()

BOOTSTRAP = {
    "services": [
        [["com", "net"], ["https://rdap.verisign.example/com/v1/"]],
        [["org"], ["https://rdap.publicinterest.example/org/v1/"]],
    ]
}


def _config():
    return {
        "api_endpoints": {"rdap_bootstrap": "https://data.iana.org/rdap/dns.json"},
        "request_timeout_seconds": 5,
    }


@responses.activate
def test_enrich_returns_registrar_and_status_on_success():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/example.com",
        json={
            "status": ["pending delete", "redemption period"],
            "entities": [
                {
                    "roles": ["registrar"],
                    "vcardArray": [
                        "vcard",
                        [
                            ["version", {}, "text", "4.0"],
                            ["fn", {}, "text", "GoDaddy.com, LLC"],
                        ],
                    ],
                }
            ],
        },
        status=200,
    )
    result = rdap.enrich("example.com", _config())
    assert result == {
        "previous_registrar": "GoDaddy.com, LLC",
        "rdap_status": ["pending delete", "redemption period"],
    }


@responses.activate
def test_enrich_caches_bootstrap_across_calls_via_lru_cache():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/a.com",
        json={"status": [], "entities": []},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/b.com",
        json={"status": [], "entities": []},
        status=200,
    )
    rdap.enrich("a.com", _config())
    rdap.enrich("b.com", _config())
    bootstrap_hits = [c for c in responses.calls if "iana.org" in c.request.url]
    assert len(bootstrap_hits) == 1


@responses.activate
def test_enrich_returns_empty_when_bootstrap_fails():
    responses.add(responses.GET, "https://data.iana.org/rdap/dns.json", status=503)
    assert rdap.enrich("example.com", _config()) == {}


@responses.activate
def test_enrich_returns_empty_when_tld_unknown():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    assert rdap.enrich("example.unknowntld", _config()) == {}


@responses.activate
def test_enrich_returns_null_registrar_on_404():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/dropped.com",
        status=404,
    )
    assert rdap.enrich("dropped.com", _config()) == {
        "previous_registrar": None,
        "rdap_status": [],
    }


@responses.activate
def test_enrich_returns_empty_on_5xx():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET, "https://rdap.verisign.example/com/v1/domain/x.com", status=500
    )
    assert rdap.enrich("x.com", _config()) == {}


@responses.activate
def test_enrich_handles_record_with_no_registrar_entity():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/orphan.com",
        json={"status": ["active"], "entities": [{"roles": ["technical"]}]},
        status=200,
    )
    result = rdap.enrich("orphan.com", _config())
    assert result == {"previous_registrar": None, "rdap_status": ["active"]}


@responses.activate
def test_enrich_handles_missing_status_field():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/y.com",
        json={"entities": []},
        status=200,
    )
    assert rdap.enrich("y.com", _config()) == {
        "previous_registrar": None,
        "rdap_status": [],
    }


def test_enrich_returns_empty_on_bootstrap_connection_error(monkeypatch):
    import requests as _requests

    def boom(*_a, **_k):
        raise _requests.ConnectionError("nope")

    monkeypatch.setattr(rdap.requests, "get", boom)
    assert rdap.enrich("example.com", _config()) == {}
