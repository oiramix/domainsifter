"""Unit tests for scripts/enrichment/surbl.py — DNS is monkeypatched."""

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


def test_enrich_returns_empty_on_transient_failure(monkeypatch):
    def fake(_name):
        raise socket.gaierror(-3, "Temporary failure in name resolution")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert surbl.enrich("anything.com", CONFIG) == {}


def test_enrich_returns_empty_on_os_error(monkeypatch):
    def fake(_name):
        raise OSError("network down")

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
