"""Tests for scripts/api_server.py. Domains are invented; no network except a
local loopback round trip."""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import api_server, api_snapshot

REAL_CONFIG = json.loads((Path(api_server.__file__).parent / "config.json").read_text(encoding="utf-8"))
TOKENS = ["tok-current", "tok-next"]
AUTH = {"Authorization": "Bearer tok-current"}


def _ev(domain: str, day: str, **fields) -> dict:
    base = {"domain": domain, "tld": domain.rsplit(".", 1)[-1], "drop_date": day,
            "availability_status": "available", "availability_confirmed_date": day,
            "source": "live", "was_published": False}
    base.update(fields)
    return base


EVENTS = [
    _ev("marketglow.com", "2026-09-20", cc_source_domain_count=43, wayback_snapshots=120,
        wayback_last_snapshot="2025-03-04", snapshot_category="legitimate", snapshot_classifier_version="v3"),
    _ev("marketglow.com", "2026-10-02", cc_source_domain_count=44, wayback_snapshots=121),
    _ev("tideblock.net", "2026-10-05", wayback_snapshots=0),
    _ev("coppernest.org", "2026-10-10", cc_source_domain_count=7, wayback_snapshots=9),
    _ev("xn--bcher-kva.com", "2026-10-11", cc_source_domain_count=2),
]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def setup(tmp_path):
    snap = api_snapshot.build_snapshot(EVENTS, {"marketglow.com": {"title": "Marketglow candles"}}, REAL_CONFIG,
                                       now=datetime(2026, 10, 16, 15, 10, tzinfo=timezone.utc), git_sha="abc1234")
    path = tmp_path / "snapshot.json"
    api_snapshot.write_snapshot(snap, path)
    clock = Clock()
    store = api_server.SnapshotStore(path, clock=clock)
    api = api_server.Api(REAL_CONFIG, TOKENS, store, clock=clock)
    return api, store, path, clock


def call(api, target, headers=AUTH, method="GET"):
    resp = api.handle(method, target, dict(headers))
    body = json.loads(resp.body) if resp.body else None
    return resp.status, body, resp.headers


# ---------------------------------------------------------------------------
# Auth, errors, rate limit
# ---------------------------------------------------------------------------


def test_missing_or_wrong_token_is_401(setup):
    api, *_ = setup
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic tok-current"}):
        status, body, hdrs = call(api, "/v1/health", headers)
        assert status == 401
        assert body == {"error": {"code": "unauthorized", "message": "Missing or invalid bearer token."}}
        assert hdrs["WWW-Authenticate"].startswith("Bearer")


def test_both_rotation_tokens_work(setup):
    api, *_ = setup
    for token in TOKENS:
        assert call(api, "/v1/health", {"Authorization": f"Bearer {token}"})[0] == 200


def test_api_refuses_to_exist_without_tokens(setup):
    _, store, *_ = setup
    with pytest.raises(ValueError):
        api_server.Api(REAL_CONFIG, [], store)
    assert api_server.load_tokens(" a , ,b ") == ["a", "b"]


def test_only_get_is_allowed(setup):
    api, *_ = setup
    status, body, hdrs = call(api, "/v1/health", method="POST")
    assert status == 405 and body["error"]["code"] == "bad_request" and hdrs["Allow"] == "GET"


def test_rate_limit_returns_429_with_retry_after(setup):
    api, _, _, clock = setup
    burst = REAL_CONFIG["api"]["rate_limit_burst"]
    for _ in range(burst):
        assert call(api, "/v1/sources")[0] == 200
    status, body, hdrs = call(api, "/v1/sources")
    assert status == 429 and body["error"]["code"] == "rate_limited"
    assert int(hdrs["Retry-After"]) >= 1
    clock.t += 1.0
    assert call(api, "/v1/sources")[0] == 200


def test_unknown_path_is_404_and_errors_leak_nothing(setup):
    api, _, path, _ = setup
    status, body, _ = call(api, "/v1/nope")
    assert status == 404 and body["error"]["code"] == "not_found"
    assert str(path.parent) not in json.dumps(body)


# ---------------------------------------------------------------------------
# Health, sources, OpenAPI
# ---------------------------------------------------------------------------


def test_health_reports_freshness(setup):
    api, *_ = setup
    status, body, _ = call(api, "/v1/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["version"] == f"{api_server.API_VERSION}+abc1234"
    assert body["data"]["domains"] == 4
    assert body["data"]["last_ingest_at"] == "2026-10-16T15:10:00Z"
    by_id = {s["id"]: s["last_run_at"] for s in body["data"]["sources"]}
    assert by_id["registry_rdap"] == "2026-10-11T00:00:00Z"
    # drop dates come from the daily CZDS diff even for RDAP-confirmed domains
    assert by_id["icann_czds"] == "2026-10-11T00:00:00Z"


def test_health_degraded_and_data_503_without_snapshot(tmp_path):
    store = api_server.SnapshotStore(tmp_path / "absent.json")
    api = api_server.Api(REAL_CONFIG, TOKENS, store)
    assert call(api, "/v1/health")[1]["status"] == "degraded"
    status, body, hdrs = call(api, "/v1/domains/marketglow.com")
    assert status == 503 and body["error"]["code"] == "internal" and hdrs["Retry-After"]


def test_sources_registry_is_valid_and_czds_is_explicit(setup):
    api, *_ = setup
    items = call(api, "/v1/sources")[1]["items"]
    methods = {"scrape", "api", "own_crawl", "licensed_feed", "manual"}
    terms = {"permitted", "permitted_with_conditions", "prohibited", "unknown"}
    for s in items:
        assert s["method"] in methods and s["terms_status"] in terms
        assert isinstance(s["may_display"], bool) and s["notes"]
        if s["terms_status"] == "unknown":
            assert s["may_display"] is False
    czds = next(s for s in items if s["id"] == "icann_czds")
    assert czds["terms_status"] == "permitted_with_conditions"
    # Asset X-Ray's request, 2026-10-17: lists derived from zone diffs are not displayed
    assert czds["may_display"] is False
    assert "Appendix 3" in czds["notes"] and "Specification 4" in czds["notes"]
    used = {o["source"] for d in api_snapshot.build_snapshot(EVENTS, {}, REAL_CONFIG)["domains"].values()
            for o in d["observations"]}
    assert used <= {s["id"] for s in items}


def test_openapi_document(setup):
    api, *_ = setup
    doc = call(api, "/v1/openapi.json")[1]
    assert doc["openapi"] == "3.1.0"
    assert {"/v1/health", "/v1/sources", "/v1/domains/{domain}", "/v1/domains/{domain}/observations",
            "/v1/domains/{domain}/backlinks", "/v1/domains/{domain}/referring-domains",
            "/v1/candidates", "/v1/changes"} <= set(doc["paths"])


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------


def test_domain_lookup(setup):
    api, *_ = setup
    status, body, hdrs = call(api, "/v1/domains/MarketGlow.com")
    assert status == 200
    assert body["domain"] == "marketglow.com"
    assert "observations" not in body
    assert body["observations_count"] == 16
    assert body["wayback"]["sample_title"] == "Marketglow candles"
    rd = next(m for m in body["metrics"] if m["name"] == "referring_domains")
    assert rd["value"] == 44 and rd["observed_at"] == "2026-10-02T00:00:00Z"
    assert hdrs["Last-Modified"] == "Fri, 16 Oct 2026 15:10:00 GMT"


def test_unicode_domain_is_converted_to_punycode(setup):
    api, *_ = setup
    status, body, _ = call(api, "/v1/domains/b%C3%BCcher.com")
    assert status == 200 and body["domain"] == "xn--bcher-kva.com"


def test_unknown_and_invalid_domains(setup):
    api, *_ = setup
    assert call(api, "/v1/domains/ghostorchard.com")[0] == 404
    assert call(api, "/v1/domains/not_a_domain")[0] == 400


def test_backlinks_and_referring_domains_are_501(setup):
    api, *_ = setup
    for sub in ("backlinks", "referring-domains"):
        status, body, _ = call(api, f"/v1/domains/marketglow.com/{sub}")
        assert status == 501 and "Not implemented" in body["error"]["message"]


def test_observations_paginate_oldest_first(setup):
    api, *_ = setup
    status, page1, _ = call(api, "/v1/domains/marketglow.com/observations?limit=10")
    assert status == 200 and len(page1["items"]) == 10 and page1["next_cursor"]
    assert page1["items"][0]["observed_at"] == "2026-09-20T00:00:00Z"
    page2 = call(api, f"/v1/domains/marketglow.com/observations?limit=10&cursor={page1['next_cursor']}")[1]
    assert len(page2["items"]) == 6 and page2["next_cursor"] is None
    assert {o["kind"] for o in page1["items"] + page2["items"]} == {"availability", "metric", "wayback", "analysis"}


def test_bad_or_foreign_cursor_is_400(setup):
    api, *_ = setup
    assert call(api, "/v1/domains/marketglow.com/observations?cursor=garbage")[0] == 400
    cursor = call(api, "/v1/domains/marketglow.com/observations?limit=1")[1]["next_cursor"]
    assert call(api, f"/v1/candidates?cursor={cursor}")[0] == 400
    forged = base64.urlsafe_b64encode(b'{"o":-5,"s":"x"}').decode()
    assert call(api, f"/v1/domains/marketglow.com/observations?cursor={forged}")[0] == 400


def test_limit_is_capped_at_max(setup):
    api, *_ = setup
    assert call(api, "/v1/candidates?limit=5000")[0] == 200
    assert call(api, "/v1/candidates?limit=0")[0] == 400


# ---------------------------------------------------------------------------
# Candidates and changes
# ---------------------------------------------------------------------------


def names(body):
    return [i["domain"] for i in body["items"]]


def test_candidates_default_sort_and_compact_items(setup):
    api, *_ = setup
    body = call(api, "/v1/candidates")[1]
    assert names(body) == ["xn--bcher-kva.com", "coppernest.org", "tideblock.net", "marketglow.com"]
    item = body["items"][-1]
    assert set(item) == {"domain", "first_seen_at", "last_seen_at", "availability", "metrics", "wayback", "analysis"}
    assert item["analysis"] == {"niche": None, "verdict": "legitimate"}


def test_candidates_filters(setup):
    api, *_ = setup
    assert names(call(api, "/v1/candidates?tld=.net")[1]) == ["tideblock.net"]
    assert names(call(api, "/v1/candidates?min_referring_domains=5&sort=referring_domains_desc")[1]) == [
        "marketglow.com", "coppernest.org"]
    assert names(call(api, "/v1/candidates?has_wayback=false")[1]) == ["xn--bcher-kva.com", "tideblock.net"]
    assert names(call(api, "/v1/candidates?status=available&first_seen_after=2026-10-05")[1]) == [
        "xn--bcher-kva.com", "coppernest.org"]
    assert names(call(api, "/v1/candidates?updated_after=2026-10-01T12:00:00Z&tld=com")[1]) == [
        "xn--bcher-kva.com", "marketglow.com"]
    assert call(api, "/v1/candidates?status=registered")[1]["items"] == []


def test_candidates_rejects_niche_and_bad_params(setup):
    api, *_ = setup
    status, body, _ = call(api, "/v1/candidates?niche=crafts")
    assert status == 400 and "niche" in body["error"]["message"]
    for q in ("sort=bogus", "has_wayback=maybe", "min_referring_domains=x", "first_seen_after=yesterday"):
        assert call(api, f"/v1/candidates?{q}")[0] == 400


def test_changes_feed(setup):
    api, *_ = setup
    assert call(api, "/v1/changes")[0] == 400
    body = call(api, "/v1/changes?since=2026-09-25T00:00:00Z")[1]
    assert body["items"] == [
        {"domain": "marketglow.com", "changed_at": "2026-10-02T00:00:00Z", "change": "updated"},
        {"domain": "tideblock.net", "changed_at": "2026-10-05T00:00:00Z", "change": "added"},
        {"domain": "coppernest.org", "changed_at": "2026-10-10T00:00:00Z", "change": "added"},
        {"domain": "xn--bcher-kva.com", "changed_at": "2026-10-11T00:00:00Z", "change": "added"},
    ]
    assert all(i["change"] != "removed" for i in body["items"])
    page = call(api, "/v1/changes?since=2026-01-01&limit=3")[1]
    assert len(page["items"]) == 3 and page["next_cursor"]


# ---------------------------------------------------------------------------
# Caching and reload
# ---------------------------------------------------------------------------


def test_etag_and_if_none_match(setup):
    api, *_ = setup
    _, _, hdrs = call(api, "/v1/domains/marketglow.com")
    status, body, again = call(api, "/v1/domains/marketglow.com", {**AUTH, "If-None-Match": hdrs["ETag"]})
    assert status == 304 and body is None and again["ETag"] == hdrs["ETag"]
    assert call(api, "/v1/domains/marketglow.com", {**AUTH, "If-None-Match": '"other"'})[0] == 200


def test_snapshot_reloads_when_file_changes_and_survives_a_bad_file(setup):
    api, store, path, clock = setup
    assert call(api, "/v1/health")[1]["data"]["domains"] == 4
    api_snapshot.write_snapshot(api_snapshot.build_snapshot(EVENTS[:1], {}, REAL_CONFIG), path)
    os.utime(path, (2_000_000_000, 2_000_000_000))
    clock.t += 10
    assert call(api, "/v1/health")[1]["data"]["domains"] == 1
    path.write_text("{broken", encoding="utf-8")
    os.utime(path, (2_000_000_100, 2_000_000_100))
    clock.t += 10
    assert call(api, "/v1/health")[1]["data"]["domains"] == 1


# ---------------------------------------------------------------------------
# Real HTTP round trip on loopback
# ---------------------------------------------------------------------------


def test_http_round_trip(setup):
    api, *_ = setup
    server = api_server.ThreadingHTTPServer(("127.0.0.1", 0), api_server._make_handler(api))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/v1/domains/marketglow.com", headers=AUTH)
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
            assert resp.headers["Server"].startswith("domainsifter-api")
            assert json.loads(resp.read())["domain"] == "marketglow.com"
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/v1/health", timeout=5)
        assert err.value.code == 401
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Zone facts vs. zone lists (Asset X-Ray feedback, 2026-10-17)
# ---------------------------------------------------------------------------


def test_zone_sources_are_split_lists_hidden_single_facts_shown(setup):
    api, *_ = setup
    items = {s["id"]: s for s in call(api, "/v1/sources")[1]["items"]}
    assert items["icann_czds"]["may_display"] is False
    assert items["icann_czds_domain_fact"]["may_display"] is True
    assert items["icann_czds_domain_fact"]["terms_status"] == "permitted_with_conditions"


def test_domain_carries_zone_facts_with_their_own_source(setup):
    api, *_ = setup
    body = call(api, "/v1/domains/marketglow.com")[1]
    assert body["zone"] == {
        "status": "ok", "left_zone_at": "2026-10-02T00:00:00Z", "left_zone_count": 2,
        "first_seen_in_zone_at": None, "source": "icann_czds_domain_fact",
    }
    assert body["availability"]["drop_date"] == "2026-10-02"
    obs = call(api, "/v1/domains/marketglow.com/observations?limit=100")[1]["items"]
    left = [o for o in obs if o["name"] == "left_zone"]
    assert [o["value"] for o in left] == ["2026-09-20", "2026-10-02"]
    assert {o["source"] for o in left} == {"icann_czds_domain_fact"}
    # no per-domain observation cites the LIST source
    assert all(o["source"] != "icann_czds" for o in obs)


def test_list_endpoints_cite_the_list_source(setup):
    api, *_ = setup
    assert call(api, "/v1/candidates")[1]["list_source"] == "icann_czds"
    assert call(api, "/v1/changes?since=2026-01-01")[1]["list_source"] == "icann_czds"


def test_health_reports_both_zone_sources(setup):
    api, *_ = setup
    by_id = {s["id"]: s["last_run_at"] for s in call(api, "/v1/health")[1]["data"]["sources"]}
    assert by_id["icann_czds"] == by_id["icann_czds_domain_fact"] == "2026-10-11T00:00:00Z"


def test_openapi_names_every_field_asset_xray_asked_about(setup):
    api, *_ = setup
    doc = call(api, "/v1/openapi.json")[1]
    schemas = doc["components"]["schemas"]
    ids = [s["id"] for s in REAL_CONFIG["api"]["sources"]]
    assert schemas["Source"]["properties"]["id"]["enum"] == ids
    assert "open_page_rank" in schemas["Metric"]["properties"]["name"]["enum"]
    assert set(schemas["Zone"]["properties"]) == {
        "status", "left_zone_at", "left_zone_count", "first_seen_in_zone_at", "source"}
    assert schemas["Domain"]["properties"]["zone"] == {"$ref": "#/components/schemas/Zone"}
    assert schemas["Analysis"]["properties"]["name_quality"] == {"$ref": "#/components/schemas/NameQuality"}
    assert schemas["NameQuality"]["properties"]["score"]["maximum"] == 100
    assert "left_zone" in schemas["Observation"]["properties"]["name"]["enum"]


def test_committed_openapi_copy_matches_the_code():
    """docs/api/openapi.json is what gets sent to Asset X-Ray before the
    tunnel is live. Regenerate with:
        python -m scripts.api_server --openapi > docs/api/openapi.json"""
    committed = json.loads((Path(api_server.__file__).parent.parent / "docs" / "api" / "openapi.json")
                           .read_text(encoding="utf-8"))
    ids = [s["id"] for s in REAL_CONFIG["api"]["sources"]]
    assert committed == json.loads(json.dumps(api_server.openapi_document(ids)))
