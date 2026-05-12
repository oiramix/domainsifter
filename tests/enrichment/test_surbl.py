"""Unit tests for scripts/enrichment/surbl.py — DNS is monkeypatched.

Covers the three-state contract (changed 2026-05-12):
    True   → genuine listing
    False  → confirmed clean (NXDOMAIN)
    None   → unknown (rate-limit error code, unexpected response, transport
             failure) — passed through as `{"surbl_listed": None}`, NOT
             collapsed into empty dict
    {}     → only when the circuit breaker is open
"""

from __future__ import annotations

import socket

from scripts.enrichment import _dnsbl, surbl

CONFIG: dict = {}


def test_enrich_returns_listed_true_for_127_response(monkeypatch):
    def fake(name):
        assert name == "evil.com.multi.surbl.org"
        return ("evil.com.multi.surbl.org", [], ["127.0.0.2"])

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert surbl.enrich("evil.com", CONFIG) == {"surbl_listed": True}


def test_enrich_returns_listed_false_on_nxdomain(monkeypatch):
    def fake(_name):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert surbl.enrich("clean.com", CONFIG) == {"surbl_listed": False}


def test_enrich_returns_unknown_on_rate_limit_response(monkeypatch):
    """SURBL's behavior on rate-limit varies — on 2026-05-12 it returned
    NXDOMAIN, but other DNSBL operators use 127.255.255.x error codes.
    Either way, an unexpected 127.x response must surface as None."""
    def fake(_name):
        return ("any.com.multi.surbl.org", [], ["127.255.255.254"])

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert surbl.enrich("any.com", CONFIG) == {"surbl_listed": None}


def test_enrich_returns_unknown_on_transient_dns_failure(monkeypatch):
    def fake(_name):
        raise socket.gaierror(-3, "Temporary failure in name resolution")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert surbl.enrich("anything.com", CONFIG) == {"surbl_listed": None}


def test_enrich_returns_unknown_on_os_error(monkeypatch):
    def fake(_name):
        raise OSError("network down")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert surbl.enrich("anything.com", CONFIG) == {"surbl_listed": None}


def test_enrich_returns_empty_when_circuit_breaker_open(monkeypatch):
    """Only an open circuit breaker yields the empty-dict signal."""
    surbl._BREAKER.reset()
    for _ in range(surbl._BREAKER.failure_threshold):
        surbl._BREAKER.record_failure()
    assert surbl._BREAKER.is_open()

    def fake(_name):  # pragma: no cover — must not be called
        raise AssertionError("DNS resolved while breaker was open")
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)

    assert surbl.enrich("anything.com", CONFIG) == {}


def test_enrich_uses_zone_from_config(monkeypatch):
    seen = {}

    def fake(name):
        seen["name"] = name
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    surbl.enrich("foo.com", {"dnsbl": {"surbl_zone": "custom.zone.example"}})
    assert seen["name"] == "foo.com.custom.zone.example"
