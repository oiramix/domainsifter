"""Unit tests for scripts/enrichment/_dnsbl.py — three-state DNSBL classifier.

Codifies the contract established 2026-05-12: only 127.0.0.x and 127.0.1.x
count as a listing. The 127.255.255.x error band and any unexpected
response collapse to `None` (unknown), as does any DNS transport failure.
NXDOMAIN is the only signal for "confirmed not listed".

DNS resolution is monkeypatched in every test — no live blocklist queries.
"""

from __future__ import annotations

import socket

from scripts.enrichment import _dnsbl


def _fake_addrs(addrs: list[str]):
    """Return a fake gethostbyname_ex callable that yields `addrs`."""
    def fake(name: str):
        return (name, [], addrs)
    return fake


def _fake_nxdomain():
    def fake(_name: str):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return fake


# -- listed ------------------------------------------------------------------


def test_listed_true_for_surbl_127_0_0_2(monkeypatch):
    """127.0.0.2 is SURBL's 'SC' (spam) category flag — a legitimate listing."""
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", _fake_addrs(["127.0.0.2"]))
    assert _dnsbl.is_listed("evil.com", "multi.surbl.org") is True


def test_listed_true_for_spamhaus_127_0_1_2(monkeypatch):
    """127.0.1.2 is Spamhaus DBL's 'spam domain' category — a legitimate listing."""
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", _fake_addrs(["127.0.1.2"]))
    assert _dnsbl.is_listed("evil.com", "dbl.spamhaus.org") is True


def test_listed_true_when_listing_address_appears_with_others(monkeypatch):
    """If the resolver returns a listing code AND an error-band code, the
    listing wins. A single legitimate listing is dispositive regardless of
    any noise alongside it."""
    monkeypatch.setattr(
        _dnsbl.socket, "gethostbyname_ex",
        _fake_addrs(["127.0.0.2", "127.255.255.254"]),
    )
    assert _dnsbl.is_listed("evil.com", "multi.surbl.org") is True


# -- not listed --------------------------------------------------------------


def test_listed_false_on_nxdomain(monkeypatch):
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", _fake_nxdomain())
    assert _dnsbl.is_listed("clean.com", "dbl.spamhaus.org") is False


def test_listed_false_on_nxdomain_via_no_such_host_message(monkeypatch):
    """Some resolvers raise gaierror without EAI_NONAME — message-based
    fallback must still classify these as NXDOMAIN (not listed)."""
    def fake(_name):
        raise socket.gaierror(0, "No such host")
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert _dnsbl.is_listed("clean.com", "dbl.spamhaus.org") is False


# -- unknown (the bug class fixed on 2026-05-12) -----------------------------


def test_listed_unknown_for_127_255_255_254(monkeypatch):
    """127.255.255.254 is Spamhaus's 'query via public/open resolver, refused'
    error code. The 2026-05-12 OVH run hit this for every query because
    the shared resolver was rate-limited. Previously misclassified as
    listed=True, mis-rejecting 100% of post-enrichment candidates.
    """
    monkeypatch.setattr(
        _dnsbl.socket, "gethostbyname_ex", _fake_addrs(["127.255.255.254"]),
    )
    assert _dnsbl.is_listed("anyhost.com", "dbl.spamhaus.org") is None


def test_listed_unknown_for_127_255_255_255(monkeypatch):
    """127.255.255.255 is Spamhaus's 'excessive number of queries' error code."""
    monkeypatch.setattr(
        _dnsbl.socket, "gethostbyname_ex", _fake_addrs(["127.255.255.255"]),
    )
    assert _dnsbl.is_listed("anyhost.com", "dbl.spamhaus.org") is None


def test_listed_unknown_for_unexpected_127_address(monkeypatch):
    """A 127.x.x.x address outside the listing bands (e.g. 127.0.2.x or
    other future error codes) is unexpected — refuse to interpret as
    a listing."""
    monkeypatch.setattr(
        _dnsbl.socket, "gethostbyname_ex", _fake_addrs(["127.0.2.99"]),
    )
    assert _dnsbl.is_listed("anyhost.com", "dbl.spamhaus.org") is None


def test_listed_unknown_for_non_127_address(monkeypatch):
    """A DNSBL response outside 127/8 entirely is unexpected — treat as
    unknown, not as a listing."""
    monkeypatch.setattr(
        _dnsbl.socket, "gethostbyname_ex", _fake_addrs(["10.0.0.1"]),
    )
    assert _dnsbl.is_listed("anyhost.com", "dbl.spamhaus.org") is None


def test_listed_unknown_on_empty_address_list(monkeypatch):
    """gethostbyname_ex normally raises gaierror on NXDOMAIN, so an empty
    address list is anomalous — refuse to interpret."""
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", _fake_addrs([]))
    assert _dnsbl.is_listed("anyhost.com", "dbl.spamhaus.org") is None


def test_listed_unknown_on_transient_resolver_failure(monkeypatch):
    """Temporary DNS failure (errno != EAI_NONAME, no NXDOMAIN message) is
    unknown, not 'not listed'."""
    def fake(_name):
        raise socket.gaierror(-3, "Temporary failure in name resolution")
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert _dnsbl.is_listed("anyhost.com", "dbl.spamhaus.org") is None


def test_listed_unknown_on_os_error(monkeypatch):
    def fake(_name):
        raise OSError("network down")
    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    assert _dnsbl.is_listed("anyhost.com", "dbl.spamhaus.org") is None


# -- query construction ------------------------------------------------------


def test_query_name_is_domain_dot_zone(monkeypatch):
    seen = {}

    def fake(name):
        seen["name"] = name
        raise socket.gaierror(socket.EAI_NONAME, "no such host")

    monkeypatch.setattr(_dnsbl.socket, "gethostbyname_ex", fake)
    _dnsbl.is_listed("foo.com", "test.zone")
    assert seen["name"] == "foo.com.test.zone"
