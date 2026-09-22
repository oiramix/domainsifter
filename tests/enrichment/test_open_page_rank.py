"""Unit tests for scripts/enrichment/open_page_rank.py.

Rewritten 2026-09-22 for the Keywords Everywhere API. The service moved
from DomCop and the old endpoint was switched off, not deprecated:
`openpagerank.com` 301s to `openpagerank.keywordseverywhere.com`, which
404s the old path. Because `requests` follows redirects, the pipeline saw
a bare "404 Not Found" and kept going with an empty result — costing two
days of publishing, since a missing `open_page_rank` drops a candidate
below the enrichment-completeness floor at the publish gate.

Fixtures use invented domains only (CLAUDE.md hard rule 1); the previous
version of this file used `example.com`.
"""

from __future__ import annotations

import pytest
import responses

from scripts.enrichment import open_page_rank as opr

ENDPOINT = "https://openpagerank.keywordseverywhere.com/v1/domains/bulk"

CONFIG = {
    "api_endpoints": {"open_page_rank": ENDPOINT},
    "request_timeout_seconds": 5,
}


@pytest.fixture(autouse=True)
def _reset_breaker():
    """The breaker is module-level and opens after 5 consecutive failures.

    Without a reset the failure tests below would trip it and every later
    test would get a short-circuited {} for the wrong reason — passing for
    a reason the test never intended to check.
    """
    opr._BREAKER.reset()
    yield
    opr._BREAKER.reset()


def _payload(score, *, found=True, domain="marketglow.com"):
    return {
        "as_of": "2026-09-01",
        "count": 1,
        "results": [
            {
                "domain": domain,
                "found": found,
                "open_page_rank": score,
                "rank": 12345,
                "referring_domains": 271,
            }
        ],
    }


@responses.activate
def test_enrich_returns_score_when_key_set(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, json=_payload(4.21), status=200)
    assert opr.enrich("marketglow.com", CONFIG) == {"open_page_rank": 4.21}


def test_enrich_returns_empty_when_key_missing(monkeypatch):
    monkeypatch.delenv("OPENPAGERANK_KEY", raising=False)
    assert opr.enrich("marketglow.com", CONFIG) == {}


@responses.activate
def test_enrich_sends_bearer_auth_header(monkeypatch):
    """Auth moved from `API-OPR: <key>` to `Authorization: Bearer <key>`."""
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, json=_payload(0), status=200)
    opr.enrich("marketglow.com", CONFIG)
    assert responses.calls[0].request.headers["Authorization"] == "Bearer sekret"
    assert "API-OPR" not in responses.calls[0].request.headers


@responses.activate
def test_enrich_posts_domain_in_json_body(monkeypatch):
    """Domains moved from a `domains[]` query param to a JSON body."""
    import json as _json

    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, json=_payload(1.5), status=200)
    opr.enrich("tideblock.io", CONFIG)
    sent = _json.loads(responses.calls[0].request.body)
    assert sent["domains"] == ["tideblock.io"]
    assert sent["include_history"] is False
    assert responses.calls[0].request.method == "POST"


@responses.activate
def test_enrich_handles_zero_rank(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, json=_payload(0), status=200)
    assert opr.enrich("coppernest.org", CONFIG) == {"open_page_rank": 0.0}


@responses.activate
def test_not_found_returns_zero_not_empty(monkeypatch):
    """`found: false` must be 0.0, NOT {}.

    This is the load-bearing case. An empty dict leaves `open_page_rank`
    ABSENT, which costs the candidate a point of enrichment completeness
    at the publish gate and rejects it — precisely the obscure dropped
    domains this project exists to surface. The old API expressed the same
    thing as `page_rank_decimal: 0`.
    """
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(
        responses.POST, ENDPOINT,
        json=_payload(None, found=False, domain="driftlantern.net"), status=200,
    )
    assert opr.enrich("driftlantern.net", CONFIG) == {"open_page_rank": 0.0}


@responses.activate
def test_enrich_returns_empty_on_5xx(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, status=502)
    assert opr.enrich("marketglow.com", CONFIG) == {}


@responses.activate
def test_enrich_returns_empty_on_404(monkeypatch):
    """The exact production failure of 2026-09-21: the migrated host 404s
    the old path. Must fail soft (rule 17), never raise."""
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, status=404)
    assert opr.enrich("marketglow.com", CONFIG) == {}


@responses.activate
@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_logs_an_error_not_a_warning(monkeypatch, caplog, status):
    """A rejected key silently zeroes the publish gate, so it must be loud
    and must name the legacy-key cause — no retry can fix it."""
    monkeypatch.setenv("OPENPAGERANK_KEY", "legacy-domcop-key")
    responses.add(responses.POST, ENDPOINT, status=status)
    with caplog.at_level("ERROR"):
        assert opr.enrich("marketglow.com", CONFIG) == {}
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert "legacy" in caplog.text.lower()


@responses.activate
@pytest.mark.parametrize(
    "body",
    [
        {"unexpected": "shape"},
        {"results": []},
        {"results": "notalist"},
        {"results": [None]},
        [],
        "a string",
    ],
)
def test_enrich_returns_empty_on_malformed_response(monkeypatch, body):
    """A malformed payload is a failure to MEASURE, so the field stays
    absent — the opposite of `found: false`, which is a measurement."""
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, json=body, status=200)
    assert opr.enrich("marketglow.com", CONFIG) == {}


def test_enrich_returns_empty_on_connection_error(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    import requests as _requests

    def boom(*_a, **_k):
        raise _requests.ConnectionError("nope")

    monkeypatch.setattr(opr.requests, "post", boom)
    assert opr.enrich("marketglow.com", CONFIG) == {}


@responses.activate
def test_enrich_handles_non_numeric_score(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, json=_payload(None), status=200)
    assert opr.enrich("marketglow.com", CONFIG) == {"open_page_rank": 0.0}


@responses.activate
def test_enrich_accepts_numeric_string_score(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    responses.add(responses.POST, ENDPOINT, json=_payload("3.75"), status=200)
    assert opr.enrich("marketglow.com", CONFIG) == {"open_page_rank": 3.75}


@responses.activate
def test_open_breaker_short_circuits_without_a_request(monkeypatch):
    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    for _ in range(5):
        opr._BREAKER.record_failure()
    assert opr._BREAKER.is_open()
    assert opr.enrich("marketglow.com", CONFIG) == {}
    assert len(responses.calls) == 0


def test_default_endpoint_points_at_the_migrated_host():
    """Guard against a revert to the dead DomCop URL: it 301s to the new
    host, which 404s, and `requests` follows redirects — so the failure
    looks like an outage rather than a misconfiguration."""
    assert "keywordseverywhere.com" in opr._DEFAULT_ENDPOINT
    assert "domcop" not in opr._DEFAULT_ENDPOINT
    assert opr._DEFAULT_ENDPOINT.endswith("/v1/domains/bulk")
    assert opr._HOST == "openpagerank.keywordseverywhere.com"


@responses.activate
def test_per_source_timeout_override_is_used(monkeypatch):
    """The migrated API is slow and variable (median 6.6s, max 29.9s measured
    2026-09-22), so a 10s global timeout failed ~30% of calls and would open
    the breaker mid-run. A per-source override must win over the global."""
    captured = {}
    real_post = opr.requests.post

    def spy(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return real_post(*args, **kwargs)

    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    monkeypatch.setattr(opr.requests, "post", spy)
    responses.add(responses.POST, ENDPOINT, json=_payload(1.0), status=200)
    cfg = dict(CONFIG)
    cfg["request_timeout_seconds"] = 10
    cfg["api_timeout_seconds"] = {"open_page_rank": 45}
    opr.enrich("marketglow.com", cfg)
    assert captured["timeout"] == 45


@responses.activate
def test_falls_back_to_global_timeout_when_no_override(monkeypatch):
    captured = {}
    real_post = opr.requests.post

    def spy(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return real_post(*args, **kwargs)

    monkeypatch.setenv("OPENPAGERANK_KEY", "sekret")
    monkeypatch.setattr(opr.requests, "post", spy)
    responses.add(responses.POST, ENDPOINT, json=_payload(1.0), status=200)
    opr.enrich("marketglow.com", CONFIG)
    assert captured["timeout"] == CONFIG["request_timeout_seconds"]
