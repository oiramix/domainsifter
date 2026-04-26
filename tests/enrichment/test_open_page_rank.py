"""Unit tests for scripts/enrichment/open_page_rank.py."""

from __future__ import annotations

import responses

from scripts.enrichment import open_page_rank as opr

CONFIG = {
    "api_endpoints": {"open_page_rank": "https://openpagerank.com/api/v1.0/getPageRank"},
    "request_timeout_seconds": 5,
}


def _payload(decimal):
    return {
        "status_code": 200,
        "response": [
            {
                "status_code": 200,
                "error": "",
                "page_rank_integer": int(float(decimal)),
                "page_rank_decimal": decimal,
                "rank": "12345",
                "domain": "example.com",
            }
        ],
    }


@responses.activate
def test_enrich_returns_score_when_key_set(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(
        responses.GET,
        "https://openpagerank.com/api/v1.0/getPageRank",
        json=_payload("4.21"),
        status=200,
    )
    assert opr.enrich("example.com", CONFIG) == {"open_page_rank": 4.21}


def test_enrich_returns_empty_when_key_missing(monkeypatch):
    monkeypatch.delenv("OPENPAGERANK_KEY", raising=False)
    assert opr.enrich("example.com", CONFIG) == {}


@responses.activate
def test_enrich_sends_api_key_header(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(
        responses.GET,
        "https://openpagerank.com/api/v1.0/getPageRank",
        json=_payload("0"),
        status=200,
    )
    opr.enrich("example.com", CONFIG)
    assert responses.calls[0].request.headers["API-OPR"] == "sekret"


@responses.activate
def test_enrich_handles_zero_rank(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(
        responses.GET,
        "https://openpagerank.com/api/v1.0/getPageRank",
        json=_payload("0"),
        status=200,
    )
    assert opr.enrich("unranked.com", CONFIG) == {"open_page_rank": 0.0}


@responses.activate
def test_enrich_returns_empty_on_5xx(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(
        responses.GET, "https://openpagerank.com/api/v1.0/getPageRank", status=502
    )
    assert opr.enrich("example.com", CONFIG) == {}


@responses.activate
def test_enrich_returns_empty_on_malformed_response(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(
        responses.GET,
        "https://openpagerank.com/api/v1.0/getPageRank",
        json={"unexpected": "shape"},
        status=200,
    )
    assert opr.enrich("example.com", CONFIG) == {}


def test_enrich_returns_empty_on_connection_error(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    import requests as _requests

    def boom(*_a, **_k):
        raise _requests.ConnectionError("nope")

    monkeypatch.setattr(opr.requests, "get", boom)
    assert opr.enrich("example.com", CONFIG) == {}


@responses.activate
def test_enrich_handles_non_numeric_decimal(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    payload = _payload("0")
    payload["response"][0]["page_rank_decimal"] = None
    responses.add(
        responses.GET,
        "https://openpagerank.com/api/v1.0/getPageRank",
        json=payload,
        status=200,
    )
    assert opr.enrich("example.com", CONFIG) == {"open_page_rank": 0.0}
