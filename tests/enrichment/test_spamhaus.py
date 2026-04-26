"""Unit tests for scripts/enrichment/spamhaus.py — DNS is monkeypatched."""

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


def test_enrich_returns_empty_on_transient_failure(monkeypatch):
    def fake(_name):
        raise socket.gaierror(-3, "Temporary failure in name resolution")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert spamhaus.enrich("anything.com", CONFIG) == {}


def test_enrich_uses_zone_from_config(monkeypatch):
    seen = {}

    def fake(name):
        seen["name"] = name
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    spamhaus.enrich("foo.com", {"dnsbl": {"spamhaus_zone": "alt.dbl.example"}})
    assert seen["name"] == "foo.com.alt.dbl.example"
