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


# --- check_availability ------------------------------------------------------


@responses.activate
def test_check_availability_404_means_available():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/free.com",
        status=404,
    )
    result = rdap.check_availability("free.com", _config())
    assert result["is_available"] is True
    assert result["rdap_http"] == 404
    assert result["rdap_status"] == []
    assert result["rdap_expiration"] is None
    assert result["previous_registrar"] is None


@responses.activate
def test_check_availability_200_with_owned_record_returns_false():
    """Owned domain with future expiration — owned, NOT available."""
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/owned.com",
        json={
            "status": ["client transfer prohibited", "auto renew period"],
            "events": [
                {"eventAction": "registration", "eventDate": "2023-04-25T00:00:00Z"},
                {"eventAction": "expiration", "eventDate": "2027-04-25T00:00:00Z"},
                {"eventAction": "last changed", "eventDate": "2026-04-29T00:00:00Z"},
            ],
            "entities": [
                {
                    "roles": ["registrar"],
                    "vcardArray": [
                        "vcard",
                        [
                            ["version", {}, "text", "4.0"],
                            ["fn", {}, "text", "Namecheap"],
                        ],
                    ],
                }
            ],
        },
        status=200,
    )
    result = rdap.check_availability("owned.com", _config())
    assert result["is_available"] is False
    assert result["rdap_http"] == 200
    assert result["rdap_expiration"] == "2027-04-25"
    assert result["previous_registrar"] == "Namecheap"
    assert "auto renew period" in result["rdap_status"]


@responses.activate
def test_check_availability_200_redemption_period_returns_false():
    """The most common case from real audit data: lapsed but in 30-day
    redemption window. Owner can still recover. NOT available."""
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/redempt.com",
        json={
            "status": ["redemption period", "pending delete"],
            "events": [
                {"eventAction": "expiration", "eventDate": "2026-03-15"},
            ],
            "entities": [],
        },
        status=200,
    )
    result = rdap.check_availability("redempt.com", _config())
    assert result["is_available"] is False
    assert result["rdap_status"] == ["redemption period", "pending delete"]
    assert result["rdap_expiration"] == "2026-03-15"


@responses.activate
def test_check_availability_bootstrap_failure_returns_unknown():
    responses.add(responses.GET, "https://data.iana.org/rdap/dns.json", status=503)
    result = rdap.check_availability("anything.com", _config())
    assert result["is_available"] is None
    assert result["rdap_http"] is None


@responses.activate
def test_check_availability_unknown_tld_returns_unknown():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    result = rdap.check_availability("example.weird-tld", _config())
    assert result["is_available"] is None


@responses.activate
def test_check_availability_5xx_returns_unknown():
    """Transient registry failure — treat as unknown so caller can REJECT."""
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/down.com",
        status=503,
    )
    result = rdap.check_availability("down.com", _config())
    assert result["is_available"] is None
    assert result["rdap_http"] == 503


@responses.activate
def test_check_availability_persistent_429_returns_unknown():
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    # Three back-to-back 429s — request_with_429_backoff returns the last one.
    for _ in range(3):
        responses.add(
            responses.GET,
            "https://rdap.verisign.example/com/v1/domain/limited.com",
            status=429,
        )
    result = rdap.check_availability("limited.com", _config())
    assert result["is_available"] is None
    assert result["rdap_http"] == 429


def test_check_availability_connection_error_returns_unknown(monkeypatch):
    import requests as _requests

    def boom(*_a, **_k):
        raise _requests.ConnectionError("network down")

    monkeypatch.setattr(rdap.requests, "get", boom)
    result = rdap.check_availability("example.com", _config())
    assert result["is_available"] is None


@responses.activate
def test_check_availability_extracts_registrar_expiration_when_no_registry_expiration():
    """Some registries only emit 'registrar expiration' — fall back to that."""
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/regonly.com",
        json={
            "status": ["client hold"],
            "events": [
                {"eventAction": "registrar expiration", "eventDate": "2027-01-01"},
                {"eventAction": "registration", "eventDate": "2024-01-01"},
            ],
            "entities": [],
        },
        status=200,
    )
    result = rdap.check_availability("regonly.com", _config())
    assert result["rdap_expiration"] == "2027-01-01"


@responses.activate
def test_check_availability_breaker_open_returns_unknown_immediately(monkeypatch):
    """When the module breaker is open, no network call is made and the
    response is unknown. The autouse conftest fixture resets the breaker
    around tests, so we only need to trip it inside this one."""
    # Trip the breaker manually.
    rdap._BREAKER.record_failure()
    rdap._BREAKER.record_failure()
    rdap._BREAKER.record_failure()
    rdap._BREAKER.record_failure()
    rdap._BREAKER.record_failure()
    assert rdap._BREAKER.is_open()

    # No HTTP mock registered — if a request escapes, this test will explode.
    result = rdap.check_availability("anything.com", _config())
    assert result["is_available"] is None
