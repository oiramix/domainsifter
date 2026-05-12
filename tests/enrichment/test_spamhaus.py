"""Unit tests for scripts/enrichment/spamhaus.py — DNS is monkeypatched.

Covers the three-state contract (changed 2026-05-12):
    True   → genuine listing
    False  → confirmed clean (NXDOMAIN)
    None   → unknown (rate-limit error code, unexpected response, transport
             failure) — passed through as `{"spamhaus_listed": None}`, NOT
             collapsed into empty dict
    {}     → only when the circuit breaker is open
"""

from __future__ import annotations

import socket

from scripts.enrichment import _dnsbl, spamhaus

CONFIG: dict = {}


def test_enrich_returns_listed_true_for_127_response(monkeypatch):
    def fake(name):
        assert name == "bad.com.dbl.spamhaus.org"
        return ("bad.com.dbl.spamhaus.org", [], ["127.0.1.2"])

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert spamhaus.enrich("bad.com", CONFIG) == {"spamhaus_listed": True}


def test_enrich_returns_listed_false_on_nxdomain(monkeypatch):
    def fake(_name):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert spamhaus.enrich("good.com", CONFIG) == {"spamhaus_listed": False}


def test_enrich_returns_unknown_on_rate_limit_response(monkeypatch):
    """127.255.255.254 is Spamhaus's 'public resolver, refused' code. The
    enricher must surface this as `spamhaus_listed: None`, not as `True`
    (which mis-rejected every candidate on 2026-05-12) and not as missing
    (which would have collapsed it into 'not listed' downstream)."""
    def fake(_name):
        return ("any.com.dbl.spamhaus.org", [], ["127.255.255.254"])

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert spamhaus.enrich("any.com", CONFIG) == {"spamhaus_listed": None}


def test_enrich_returns_unknown_on_transient_dns_failure(monkeypatch):
    """Transport failure is also 'unknown' — surface explicitly so the
    filter and the daily report can distinguish it from 'not listed'."""
    def fake(_name):
        raise socket.gaierror(-3, "Temporary failure in name resolution")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert spamhaus.enrich("anything.com", CONFIG) == {"spamhaus_listed": None}


def test_enrich_returns_empty_when_circuit_breaker_open(monkeypatch):
    """Only an open circuit breaker yields the empty-dict (no-field) signal.
    The breaker opens after consecutive failures; tripping it manually
    here exercises the early-return path."""
    spamhaus._BREAKER.reset()
    for _ in range(spamhaus._BREAKER.failure_threshold):
        spamhaus._BREAKER.record_failure()
    assert spamhaus._BREAKER.is_open()

    def fake(_name):  # pragma: no cover — must not be called
        raise AssertionError("DNS resolved while breaker was open")
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)

    assert spamhaus.enrich("anything.com", CONFIG) == {}


def test_enrich_records_breaker_failure_on_unknown(monkeypatch):
    """Unknown responses count as failures for circuit-breaker purposes —
    if a DNSBL keeps refusing to answer, we want to stop calling it. A
    SUCCESSFUL non-listing (False) must reset the failure counter."""
    spamhaus._BREAKER.reset()

    def fake_rate_limit(_name):
        return ("x.com.dbl.spamhaus.org", [], ["127.255.255.254"])

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake_rate_limit)
    assert spamhaus.enrich("x.com", CONFIG) == {"spamhaus_listed": None}
    assert spamhaus._BREAKER.consecutive_failures == 1

    def fake_nxdomain(_name):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake_nxdomain)
    assert spamhaus.enrich("y.com", CONFIG) == {"spamhaus_listed": False}
    assert spamhaus._BREAKER.consecutive_failures == 0


def test_enrich_uses_zone_from_config(monkeypatch):
    seen = {}

    def fake(name):
        seen["name"] = name
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    spamhaus.enrich("foo.com", {"dnsbl": {"spamhaus_zone": "alt.dbl.example"}})
    assert seen["name"] == "foo.com.alt.dbl.example"
