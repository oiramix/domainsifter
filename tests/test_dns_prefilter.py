"""Unit tests for scripts/dns_prefilter.py.

Codifies the three-state contract established 2026-05-12:
    NXDOMAIN     → dns_available=True  (registry removed delegation; proceed to RDAP)
    NS records   → dns_available=False (still delegated; reject before RDAP)
    error states → dns_available=None  (fail open to RDAP)

DNS resolution is monkeypatched in every test — no live queries to Quad9.
"""

from __future__ import annotations

import logging

import dns.exception
import dns.resolver

from scripts import dns_prefilter


class _NSRecord:
    """Stand-in for dnspython's NS rdata. Production code reads `.target`
    and stringifies + rstrips it, so we mirror that contract — including
    the trailing dot that dns.name.Name renders by convention."""

    def __init__(self, target: str) -> None:
        self.target = f"{target}."


def _fake_ns_answer(*targets: str) -> list[_NSRecord]:
    """Iterable of NS records — matches what dns.resolver.Resolver.resolve
    returns on success."""
    return [_NSRecord(t) for t in targets]


# --- check_dns_availability: listing states -------------------------------


def test_check_returns_not_available_when_ns_records_exist(monkeypatch):
    """The whole point of the pre-filter: a domain whose registry still
    delegates it (NS records present in the response) is NOT available."""
    answer = _fake_ns_answer("ns1.cloudflare.com", "ns2.cloudflare.com")
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve",
        lambda self, qname, rdtype: answer,
    )

    result = dns_prefilter.check_dns_availability("registered.com")
    assert result == {
        "dns_available": False,
        "ns_records": ["ns1.cloudflare.com", "ns2.cloudflare.com"],
    }


def test_check_strips_trailing_dot_from_ns_targets(monkeypatch):
    """dnspython's dns.name.Name renders NS targets with a trailing dot.
    Our output should NOT include it — downstream consumers shouldn't have
    to know about DNS wire-format conventions."""
    answer = _fake_ns_answer("ns1.example.com")
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve",
        lambda self, qname, rdtype: answer,
    )

    result = dns_prefilter.check_dns_availability("foo.com")
    assert result["ns_records"] == ["ns1.example.com"]


def test_check_returns_available_on_nxdomain(monkeypatch):
    """NXDOMAIN at the registry level → genuinely dropped → proceed to RDAP."""
    def raise_nxdomain(self, qname, rdtype):
        raise dns.resolver.NXDOMAIN(qnames=[qname])

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", raise_nxdomain)
    assert dns_prefilter.check_dns_availability("dropped.com") == {
        "dns_available": True,
        "ns_records": [],
    }


# --- check_dns_availability: unknown states (fail-open to RDAP) -----------


def test_check_returns_unknown_on_no_answer(monkeypatch):
    """NoAnswer is the 'name exists but no NS records at the queried level'
    edge case. Per the three-state contract: fail open to RDAP rather than
    guess. (Mario specifically called this out — epistemic honesty matters
    here, same pattern as the DNSBL fix earlier today.)"""
    def raise_no_answer(self, qname, rdtype):
        raise dns.resolver.NoAnswer()

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", raise_no_answer)
    assert dns_prefilter.check_dns_availability("weird.com") == {
        "dns_available": None,
        "ns_records": [],
    }


def test_check_returns_unknown_on_no_nameservers(monkeypatch):
    """NoNameservers = no upstream resolver could be reached. Fail open."""
    def raise_no_ns(self, qname, rdtype):
        raise dns.resolver.NoNameservers()

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", raise_no_ns)
    assert dns_prefilter.check_dns_availability("anything.com") == {
        "dns_available": None,
        "ns_records": [],
    }


def test_check_returns_unknown_on_timeout(monkeypatch):
    """Query timed out before completion. The candidate gets the benefit of
    the doubt — RDAP makes the final call."""
    def raise_timeout(self, qname, rdtype):
        raise dns.exception.Timeout()

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", raise_timeout)
    result = dns_prefilter.check_dns_availability("slow.com", timeout_seconds=0.1)
    assert result == {"dns_available": None, "ns_records": []}


def test_check_returns_unknown_on_generic_dns_exception(monkeypatch):
    """Catch-all for the remaining dnspython exception hierarchy (FormError,
    BadResponse, etc.). All map to dns_available=None."""
    class _Synthetic(dns.exception.DNSException):
        pass

    def raise_other(self, qname, rdtype):
        raise _Synthetic("synthetic failure")

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", raise_other)
    assert dns_prefilter.check_dns_availability("foo.com") == {
        "dns_available": None,
        "ns_records": [],
    }


def test_check_returns_unknown_on_empty_answer(monkeypatch):
    """If dnspython returns successfully but the answer is empty (anomalous
    — NoAnswer should have triggered), refuse to interpret. Fail open."""
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve",
        lambda self, qname, rdtype: [],
    )
    assert dns_prefilter.check_dns_availability("foo.com") == {
        "dns_available": None,
        "ns_records": [],
    }


def test_check_passes_timeout_to_resolver(monkeypatch):
    """The timeout parameter is wired through to both Resolver.timeout AND
    Resolver.lifetime — dnspython uses both for different bounds (per-query
    vs. cumulative-resolution)."""
    captured: dict = {}

    original_init = dns.resolver.Resolver.__init__

    def fake_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured["instance"] = self

    monkeypatch.setattr(dns.resolver.Resolver, "__init__", fake_init)
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve",
        lambda self, qname, rdtype: _fake_ns_answer("ns1.foo.com"),
    )

    dns_prefilter.check_dns_availability("any.com", timeout_seconds=7.5)
    inst = captured["instance"]
    assert inst.timeout == 7.5
    assert inst.lifetime == 7.5


# --- filter_candidates: pipeline-stage behaviour --------------------------


def test_filter_passes_through_when_disabled(monkeypatch):
    """`dns_check.enabled=false` is the escape hatch — every candidate
    passes through unchanged, dns_available isn't set on them, and no
    DNS query happens."""
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    cands = [{"name": "a.com"}, {"name": "b.com"}]
    result = dns_prefilter.filter_candidates(
        cands, {"dns_check": {"enabled": False}},
    )
    assert result == cands
    assert "dns_available" not in cands[0]


def test_filter_handles_empty_input():
    """No candidates to check → no work, return empty list."""
    result = dns_prefilter.filter_candidates(
        [], {"dns_check": {"enabled": True, "workers": 4, "timeout_seconds": 1}},
    )
    assert result == []


def test_filter_keeps_available_and_unknown_rejects_not_available(monkeypatch):
    """End-to-end stage behaviour: True and None survive; False is dropped.
    Each candidate gets its dns_available + ns_records annotated."""
    def fake_resolve(self, qname, rdtype):
        if qname == "free.com":
            raise dns.resolver.NXDOMAIN(qnames=[qname])
        if qname == "owned.com":
            return _fake_ns_answer("ns1.parking.example", "ns2.parking.example")
        if qname == "broken.com":
            raise dns.exception.Timeout()
        raise AssertionError(f"unexpected qname {qname!r}")

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", fake_resolve)

    cands = [
        {"name": "free.com"},
        {"name": "owned.com"},
        {"name": "broken.com"},
    ]
    cfg = {"dns_check": {"enabled": True, "workers": 4, "timeout_seconds": 1}}
    kept = dns_prefilter.filter_candidates(cands, cfg)

    kept_names = sorted(c["name"] for c in kept)
    assert kept_names == ["broken.com", "free.com"]  # owned.com rejected
    # Annotations applied to every input candidate (even rejected ones).
    by_name = {c["name"]: c for c in cands}
    assert by_name["free.com"]["dns_available"] is True
    assert by_name["free.com"]["ns_records"] == []
    assert by_name["owned.com"]["dns_available"] is False
    assert by_name["owned.com"]["ns_records"] == ["ns1.parking.example", "ns2.parking.example"]
    assert by_name["broken.com"]["dns_available"] is None
    assert by_name["broken.com"]["ns_records"] == []


def test_filter_logs_signal_distribution(monkeypatch, caplog):
    """Daily run report relies on the summary log line. Verifies the exact
    pieces the operator scans for: input count, kept count, rejection
    percentage, unknown count."""
    def fake_resolve(self, qname, rdtype):
        if qname.startswith("f"):  # free*.com → NXDOMAIN
            raise dns.resolver.NXDOMAIN(qnames=[qname])
        if qname.startswith("o"):  # owned*.com → NS records
            return _fake_ns_answer("ns1.example.com")
        raise dns.exception.Timeout()  # broken*.com → unknown

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", fake_resolve)

    cands = (
        [{"name": f"free{i}.com"} for i in range(2)]
        + [{"name": f"owned{i}.com"} for i in range(7)]
        + [{"name": f"broken{i}.com"} for i in range(1)]
    )
    cfg = {"dns_check": {"enabled": True, "workers": 4, "timeout_seconds": 1}}
    with caplog.at_level(logging.INFO, logger="scripts.dns_prefilter"):
        kept = dns_prefilter.filter_candidates(cands, cfg)

    assert len(kept) == 3  # 2 free + 1 broken; 7 owned rejected
    log_messages = " ".join(rec.message for rec in caplog.records)
    assert "10 candidates → 3 kept" in log_messages
    assert "70% rejected" in log_messages
    assert "1 unknown" in log_messages


def test_filter_swallows_exceptions_from_check(monkeypatch, caplog):
    """check_dns_availability is contracted not to raise, but
    filter_candidates is defensive — if it somehow does raise, the worker
    logs a warning and treats the candidate as unknown (fail open)."""
    def boom(*_a, **_kw):
        raise RuntimeError("synthetic — shouldn't happen but must be handled")

    monkeypatch.setattr(dns_prefilter, "check_dns_availability", boom)

    cfg = {"dns_check": {"enabled": True, "workers": 1, "timeout_seconds": 1}}
    with caplog.at_level(logging.WARNING, logger="scripts.dns_prefilter"):
        kept = dns_prefilter.filter_candidates([{"name": "any.com"}], cfg)
    assert [c["name"] for c in kept] == ["any.com"]  # fail-open
    assert kept[0]["dns_available"] is None
    log_messages = " ".join(rec.message for rec in caplog.records)
    assert "any.com" in log_messages


def test_filter_applies_throttle_when_configured(monkeypatch):
    """`throttle_seconds > 0` routes through GLOBAL_HOST_THROTTLE so the
    aggregate query rate stays bounded even with many workers. The throttle
    bucket key is module-level — we verify by capturing acquire() calls."""
    from scripts.enrichment import _circuit_breaker

    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve",
        lambda self, qname, rdtype: _fake_ns_answer("ns1.foo.com"),
    )

    seen_acquires: list[tuple] = []
    real_throttle = _circuit_breaker.GLOBAL_HOST_THROTTLE
    real_throttle.reset()

    original_acquire = real_throttle.acquire

    def spy_acquire(host, interval, **kw):
        seen_acquires.append((host, interval))
        # Don't actually sleep — keep test fast.
        return None

    monkeypatch.setattr(real_throttle, "acquire", spy_acquire)

    cfg = {"dns_check": {"enabled": True, "workers": 4,
                         "timeout_seconds": 1, "throttle_seconds": 0.05}}
    cands = [{"name": f"d{i}.com"} for i in range(3)]
    dns_prefilter.filter_candidates(cands, cfg)

    # Every candidate acquired the throttle slot; all under the same bucket
    # key so the throttle paces them as one queue rather than one queue
    # per worker.
    assert len(seen_acquires) == 3
    assert all(host == "dns_prefilter" for host, _ in seen_acquires)
    assert all(interval == 0.05 for _, interval in seen_acquires)


def test_filter_skips_throttle_when_zero(monkeypatch):
    """throttle_seconds=0.0 is the default — no throttle.acquire() call at
    all. Production starting position; we don't want to artificially slow
    a healthy resolver."""
    from scripts.enrichment import _circuit_breaker

    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve",
        lambda self, qname, rdtype: _fake_ns_answer("ns1.foo.com"),
    )

    def fail_acquire(*_a, **_kw):
        raise AssertionError("throttle should be skipped at throttle_seconds=0.0")

    monkeypatch.setattr(_circuit_breaker.GLOBAL_HOST_THROTTLE, "acquire", fail_acquire)

    cfg = {"dns_check": {"enabled": True, "workers": 2,
                         "timeout_seconds": 1, "throttle_seconds": 0.0}}
    dns_prefilter.filter_candidates([{"name": "any.com"}], cfg)
