"""Unit tests for scripts/enrichment/rdap.py — bootstrap + per-domain queries mocked."""

from __future__ import annotations

import pytest
import responses

from scripts.enrichment import rdap
from scripts.enrichment._circuit_breaker import GLOBAL_HOST_COOLDOWN, GLOBAL_HOST_STOP


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


@pytest.fixture
def _no_sleep_backoff(monkeypatch):
    """Run the REAL request_with_429_backoff — real 429 handling, real
    Retry-After honoring, real GLOBAL_HOST_COOLDOWN arming — but with the
    backoff sleep no-op'd, so a 429 test doesn't actually wait out a 60s
    floor. Tests that exercise check_availability's live 429 path use this."""
    real = rdap.request_with_429_backoff

    def fast(call_fn, **kwargs):
        kwargs.setdefault("sleep", lambda *_a, **_k: None)
        return real(call_fn, **kwargs)

    monkeypatch.setattr(rdap, "request_with_429_backoff", fast)


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
def test_check_availability_single_429_strikes_does_not_stop(_no_sleep_backoff):
    """A single 429 → unknown for this domain AND one strike, but the host is
    NOT stopped (3-strike rule — a transient blip is survivable). The cooldown
    is armed so the host briefly pauses, then resumes. Exactly ONE request
    fires (no retry)."""
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/limited.com",
        status=429,
    )
    result = rdap.check_availability("limited.com", _config())
    assert result["is_available"] is None
    assert result["rdap_http"] == 429
    from scripts.enrichment._circuit_breaker import GLOBAL_HOST_STRIKES
    assert GLOBAL_HOST_STRIKES.count("rdap.verisign.example") == 1
    assert not GLOBAL_HOST_STOP.is_stopped("rdap.verisign.example")
    assert GLOBAL_HOST_COOLDOWN.is_cooling("rdap.verisign.example")


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
def test_check_availability_uses_per_host_throttle_override(monkeypatch):
    """rdap_per_host overrides the global rdap interval for the matching host;
    unlisted hosts fall through to the global rdap value, which itself falls
    through to the 0.2 final default. Verifies the lookup chain end-to-end by
    capturing the min_interval that ends up at request_with_429_backoff.

    Calibrated 2026-05-01 for rdap.gmoregistry.net (.shop and 46 other GMO
    Registry TLDs); other RDAP hosts are unaffected by this override.
    """
    captured: list[float] = []

    def fake_request_with_429_backoff(call_fn, *, host, min_interval, **_kw):
        captured.append(min_interval)
        # Return a synthetic 200 with empty body so the rest of check_availability
        # runs to completion without making a real network call.
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: {"status": [], "events": [], "entities": []}
        resp.headers = {}
        return resp

    monkeypatch.setattr(rdap, "request_with_429_backoff", fake_request_with_429_backoff)

    bootstrap = {
        "services": [
            [["shop"], ["https://rdap.gmoregistry.net/rdap/"]],
            [["com"], ["https://rdap.verisign-grs.com/com/v1/"]],
        ]
    }

    # Stub the bootstrap fetch so we don't hit the network for IANA either.
    monkeypatch.setattr(
        rdap, "_load_bootstrap",
        lambda _cfg, _to: {tld: tuple(urls) for entry in bootstrap["services"] for tld, urls in [(t, entry[1]) for t in entry[0]]},
    )

    cfg_with_override = {
        "api_min_interval_seconds": {
            "rdap": 0.4,
            "rdap_per_host": {"rdap.gmoregistry.net": 3.0},
        },
    }

    rdap.check_availability("anything.shop", cfg_with_override)
    rdap.check_availability("other.com", cfg_with_override)

    # GMO host gets the override; verisign host falls through to 0.4.
    assert captured == [3.0, 0.4]

    # Unlisted host with no rdap_per_host map at all → falls through to global
    # rdap, then to the 0.2 final fallback when global is also missing.
    captured.clear()
    rdap.check_availability("solo.com", {})
    assert captured == [0.2]


@responses.activate
def test_rdap_requests_send_named_user_agent():
    """Every outbound HTTP call in rdap.py — bootstrap fetch + the
    per-domain RDAP query — must carry our named, contactable User-Agent
    instead of the default `python-requests/X.Y.Z` (heavily-flagged WAF
    signal; see USER_AGENT docstring in rdap.py for the why)."""
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json", json=BOOTSTRAP, status=200
    )
    responses.add(
        responses.GET,
        "https://rdap.verisign.example/com/v1/domain/example.com",
        status=404,
    )

    rdap.check_availability("example.com", _config())

    # Two outbound calls expected: IANA bootstrap + the RDAP /domain query.
    assert len(responses.calls) == 2
    for call in responses.calls:
        ua = call.request.headers.get("User-Agent", "")
        assert ua == rdap.USER_AGENT, f"unexpected UA on {call.request.url}: {ua!r}"
        assert "python-requests" not in ua


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


# --- per-host 429 cooldown (added 2026-05-22) --------------------------------

COOLDOWN_BOOTSTRAP = {
    "services": [
        [["shop"], ["https://rdap.gmoregistry.net/rdap/"]],
        [["com"], ["https://rdap.verisign.example/com/v1/"]],
    ]
}


def _cooldown_config():
    """Config with an explicit rdap_429_backoff_floor_seconds block (60s for
    GMO, mirroring production config.json) and the steady-state RDAP throttle
    zeroed so tests don't incur throttle waits."""
    return {
        "api_endpoints": {"rdap_bootstrap": "https://data.iana.org/rdap/dns.json"},
        "request_timeout_seconds": 5,
        "api_min_interval_seconds": {"rdap": 0},
        "rdap_429_backoff_floor_seconds": {
            "default": 5,
            "per_host": {"rdap.gmoregistry.net": 60},
        },
    }


def test_retry_after_floor_seconds_lookup():
    """The per-host floor lookup chain: per_host[host] → default → 5.0."""
    cfg = _cooldown_config()
    assert rdap._retry_after_floor_seconds("rdap.gmoregistry.net", cfg) == 60.0
    assert rdap._retry_after_floor_seconds("rdap.verisign.example", cfg) == 5.0
    # No config block at all → hard default 5.0.
    assert rdap._retry_after_floor_seconds("anything", {}) == 5.0


@responses.activate
def test_check_availability_cooldown_skips_during_window_then_strike_not_stop(_no_sleep_backoff):
    """After ONE 429 the host is NOT stopped (strike 1/3). Its cooldown is
    armed, so the very next same-host domain is skipped during the window
    (cooldown-skip, NOT a stop-skip — no rdap_skipped_reason)."""
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json",
        json=COOLDOWN_BOOTSTRAP, status=200,
    )
    responses.add(
        responses.GET, "https://rdap.gmoregistry.net/rdap/domain/first.shop", status=429,
    )
    # second.shop NOT registered — must be skipped via the cooldown window.

    cfg = _cooldown_config()
    r1 = rdap.check_availability("first.shop", cfg)
    assert r1["rdap_http"] == 429
    assert not GLOBAL_HOST_STOP.is_stopped("rdap.gmoregistry.net")  # 1 strike only
    assert GLOBAL_HOST_COOLDOWN.is_cooling("rdap.gmoregistry.net")

    r2 = rdap.check_availability("second.shop", cfg)
    assert r2["rdap_http"] is None              # skipped (cooldown), no request
    assert r2.get("rdap_skipped_reason") is None  # NOT a stop — just cooling

    domain_calls = [c for c in responses.calls if "/domain/" in c.request.url]
    assert len(domain_calls) == 1
    assert all("first.shop" in c.request.url for c in domain_calls)


def _drive_three_strikes(host_domains, cfg):
    """Fire three real 429s against one host (resetting the time-based cooldown
    between each so the next query proceeds), driving it to the 3-strike stop.
    `host_domains` is three distinct domains on the same RDAP host."""
    for i, name in enumerate(host_domains, start=1):
        GLOBAL_HOST_COOLDOWN.reset()  # simulate the cooldown window elapsing
        r = rdap.check_availability(name, cfg)
        assert r["rdap_http"] == 429
        yield i, r


@responses.activate
def test_check_availability_three_strikes_stops_host(_no_sleep_backoff):
    """The 3rd cumulative 429 on a host stops it for the run; the 4th domain is
    then skipped via the run-stop (rdap_skipped_reason='host_stopped')."""
    from scripts.enrichment._circuit_breaker import GLOBAL_HOST_STRIKES

    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json",
        json=COOLDOWN_BOOTSTRAP, status=200,
    )
    for name in ("s1.shop", "s2.shop", "s3.shop"):
        responses.add(
            responses.GET, f"https://rdap.gmoregistry.net/rdap/domain/{name}", status=429,
        )
    # s4.shop NOT registered — must be skipped via the run-stop.

    cfg = _cooldown_config()
    for i, _r in _drive_three_strikes(["s1.shop", "s2.shop", "s3.shop"], cfg):
        if i < 3:
            assert not GLOBAL_HOST_STOP.is_stopped("rdap.gmoregistry.net")
    assert GLOBAL_HOST_STRIKES.count("rdap.gmoregistry.net") == 3
    assert GLOBAL_HOST_STOP.is_stopped("rdap.gmoregistry.net")

    GLOBAL_HOST_COOLDOWN.reset()
    r4 = rdap.check_availability("s4.shop", cfg)
    assert r4["rdap_http"] is None
    assert r4["rdap_skipped_reason"] == "host_stopped"

    domain_calls = [c for c in responses.calls if "/domain/" in c.request.url]
    assert len(domain_calls) == 3  # only s1/s2/s3 ever hit the network


@responses.activate
def test_check_availability_strikes_do_not_block_other_host(_no_sleep_backoff):
    """Three strikes stop one host; a different host is completely unaffected
    (its own independent strike counter is still 0)."""
    from scripts.enrichment._circuit_breaker import GLOBAL_HOST_STRIKES

    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json",
        json=COOLDOWN_BOOTSTRAP, status=200,
    )
    for name in ("a.shop", "b.shop", "c.shop"):
        responses.add(
            responses.GET, f"https://rdap.gmoregistry.net/rdap/domain/{name}", status=429,
        )
    responses.add(
        responses.GET, "https://rdap.verisign.example/com/v1/domain/free.com", status=404,
    )

    cfg = _cooldown_config()
    list(_drive_three_strikes(["a.shop", "b.shop", "c.shop"], cfg))
    assert GLOBAL_HOST_STOP.is_stopped("rdap.gmoregistry.net")
    assert not GLOBAL_HOST_STOP.is_stopped("rdap.verisign.example")
    assert GLOBAL_HOST_STRIKES.count("rdap.verisign.example") == 0

    GLOBAL_HOST_COOLDOWN.reset()
    r = rdap.check_availability("free.com", cfg)
    assert r["is_available"] is True
    assert r["rdap_http"] == 404


@responses.activate
def test_check_availability_does_not_resume_after_strike_stop(_no_sleep_backoff):
    """Once a host hits the strike limit and stops, clearing the time-based
    cooldown does NOT let it resume — the run-stop is permanent."""
    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json",
        json=COOLDOWN_BOOTSTRAP, status=200,
    )
    for name in ("o1.shop", "o2.shop", "o3.shop"):
        responses.add(
            responses.GET, f"https://rdap.gmoregistry.net/rdap/domain/{name}", status=429,
        )
    # later.shop NOT registered — must stay skipped even after a cooldown reset.

    cfg = _cooldown_config()
    list(_drive_three_strikes(["o1.shop", "o2.shop", "o3.shop"], cfg))
    assert GLOBAL_HOST_STOP.is_stopped("rdap.gmoregistry.net")

    GLOBAL_HOST_COOLDOWN.reset()
    assert not GLOBAL_HOST_COOLDOWN.is_cooling("rdap.gmoregistry.net")
    assert GLOBAL_HOST_STOP.is_stopped("rdap.gmoregistry.net")  # stop still holds

    r = rdap.check_availability("later.shop", cfg)
    assert r["rdap_http"] is None
    assert r["rdap_skipped_reason"] == "host_stopped"

    domain_calls = [c for c in responses.calls if "/domain/" in c.request.url]
    assert len(domain_calls) == 3  # only the three strike domains hit network


@responses.activate
def test_check_availability_403_alarms_and_stops_host(_no_sleep_backoff, caplog):
    """A 403 is the catastrophic block: unknown for this domain, host stopped,
    and a CRITICAL alarm logged. Subsequent domains on the host are skipped."""
    import logging

    responses.add(
        responses.GET, "https://data.iana.org/rdap/dns.json",
        json=COOLDOWN_BOOTSTRAP, status=200,
    )
    responses.add(
        responses.GET, "https://rdap.verisign.example/com/v1/domain/blocked.com", status=403,
    )
    # next.com NOT registered — must be skipped without a request.

    cfg = _cooldown_config()
    with caplog.at_level(logging.CRITICAL, logger="scripts.enrichment.rdap"):
        r1 = rdap.check_availability("blocked.com", cfg)
    assert r1["is_available"] is None
    assert r1["rdap_http"] == 403
    assert GLOBAL_HOST_STOP.is_stopped("rdap.verisign.example")
    assert any("403 FORBIDDEN" in rec.message and "CATASTROPHIC" in rec.message
               for rec in caplog.records)

    r2 = rdap.check_availability("next.com", cfg)
    assert r2["rdap_http"] is None
    assert r2["rdap_skipped_reason"] == "host_stopped"
