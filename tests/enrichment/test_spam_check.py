"""Unit tests for scripts/enrichment/spam_check.py."""

from __future__ import annotations

import json

import pytest
import responses

from scripts.enrichment import spam_check
from scripts.enrichment.spam_check import SpamCheckConfigError

CONFIG = {
    "api_endpoints": {"safe_browsing": "https://safebrowsing.googleapis.com/v4/threatMatches:find"},
    "request_timeout_seconds": 5,
}


@responses.activate
def test_enrich_returns_not_flagged_when_no_matches(monkeypatch):
    monkeypatch.setenv("SAFE_BROWSING_KEY", "sekret")
    responses.add(
        responses.POST,
        "https://safebrowsing.googleapis.com/v4/threatMatches:find",
        json={},
        status=200,
    )
    assert spam_check.enrich("example.com", CONFIG) == {
        "spam_flagged": False,
        "spam_threat_types": [],
    }


@responses.activate
def test_enrich_returns_flagged_with_threat_types(monkeypatch):
    monkeypatch.setenv("SAFE_BROWSING_KEY", "sekret")
    responses.add(
        responses.POST,
        "https://safebrowsing.googleapis.com/v4/threatMatches:find",
        json={
            "matches": [
                {"threatType": "MALWARE"},
                {"threatType": "SOCIAL_ENGINEERING"},
                {"threatType": "MALWARE"},
            ]
        },
        status=200,
    )
    result = spam_check.enrich("evil.com", CONFIG)
    assert result["spam_flagged"] is True
    assert result["spam_threat_types"] == ["MALWARE", "SOCIAL_ENGINEERING"]


def test_enrich_raises_when_key_missing(monkeypatch):
    monkeypatch.delenv("SAFE_BROWSING_KEY", raising=False)
    with pytest.raises(SpamCheckConfigError):
        spam_check.enrich("example.com", CONFIG)


@responses.activate
def test_enrich_sends_key_as_query_param_and_url_in_body(monkeypatch):
    monkeypatch.setenv("SAFE_BROWSING_KEY", "sekret")
    responses.add(
        responses.POST,
        "https://safebrowsing.googleapis.com/v4/threatMatches:find",
        json={},
        status=200,
    )
    spam_check.enrich("example.com", CONFIG)
    call = responses.calls[0]
    assert "key=sekret" in call.request.url
    body = json.loads(call.request.body)
    entries = body["threatInfo"]["threatEntries"]
    assert entries == [{"url": "http://example.com/"}]


@responses.activate
def test_enrich_returns_empty_on_5xx(monkeypatch):
    monkeypatch.setenv("SAFE_BROWSING_KEY", "sekret")
    responses.add(
        responses.POST,
        "https://safebrowsing.googleapis.com/v4/threatMatches:find",
        status=503,
    )
    assert spam_check.enrich("example.com", CONFIG) == {}


@responses.activate
def test_enrich_returns_empty_on_invalid_json(monkeypatch):
    monkeypatch.setenv("SAFE_BROWSING_KEY", "sekret")
    responses.add(
        responses.POST,
        "https://safebrowsing.googleapis.com/v4/threatMatches:find",
        body="not json",
        status=200,
        content_type="text/plain",
    )
    assert spam_check.enrich("example.com", CONFIG) == {}


def test_enrich_returns_empty_on_connection_error(monkeypatch):
    monkeypatch.setenv("SAFE_BROWSING_KEY", "sekret")
    import requests as _requests

    def boom(*_a, **_k):
        raise _requests.ConnectionError("nope")

    monkeypatch.setattr(spam_check.requests, "post", boom)
    assert spam_check.enrich("example.com", CONFIG) == {}
