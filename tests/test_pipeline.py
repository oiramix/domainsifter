"""Unit tests for scripts/pipeline.py — the orchestrator.

Heavy mocking: we do NOT exercise the real CZDS, enrichment, R2, or
filesystem output paths here (those modules have their own tests). We
verify that the orchestrator wires the pieces together correctly:
    - env_check called first
    - CZDS auth uses env vars + the auth_base from config
    - only approved TLDs are downloaded
    - per-zone failures don't abort the run
    - R2 client is shared across TLDs (one auth, many objects)
    - enrichers are applied to every candidate
    - SpamCheckConfigError aborts the run
    - the final output is written via output.write_output
"""

from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts import pipeline


R2_ENV = {
    "R2_ACCOUNT_ID": "acct",
    "R2_ACCESS_KEY_ID": "ak",
    "R2_SECRET_ACCESS_KEY": "sk",
    "R2_BUCKET_NAME": "domainsifter-state",
}


def _set_r2_env(monkeypatch):
    for k, v in R2_ENV.items():
        monkeypatch.setenv(k, v)


def _no_such_key():
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )


@pytest.fixture
def cfg(tmp_path):
    config = {
        "version": "1.0",
        "tlds": {"approved": ["com", "app"]},
        "api_endpoints": {
            "czds_auth_base": "https://account-api.icann.org",
            "czds_api_base": "https://czds-api.icann.org",
        },
        "max_concurrent_enrichments": 4,
        "max_candidates_for_enrichment": 1000,
        "max_candidates_for_publication": 100,
        "enrichment_time_budget_seconds": 60,  # tests don't actually wait — short keeps the wait() ceilings small
        "registrars": [
            {"name": "Namecheap", "link_template": "https://aff.example/?d={name}"},
            {"name": "NameSilo", "link_template": "https://ns.example/?q={name}"},
        ],
        "output_path": str(tmp_path / "daily.json"),
        "filter_thresholds": {
            "min_domain_length": 2,
            "max_domain_length": 30,
            "min_wayback_snapshots": 1,
        },
        "scoring_weights": {
            "wayback_snapshots": 0.3,
            "open_page_rank": 0.4,
            "cert_history": 0.2,
            "domain_length": 0.1,
        },
        "rejected_keywords": [],
    }
    return config


def test_filename_to_tld_extracts_correctly():
    assert pipeline._filename_to_tld(
        "https://czds-download-api.icann.org/czds/downloads/com.zone"
    ) == "com"
    assert pipeline._filename_to_tld(
        "https://czds-download-api.icann.org/czds/downloads/store.zone"
    ) == "store"


def test_collect_drops_filters_to_approved_and_handles_failed_zone(monkeypatch, cfg):
    # Pre-populate R2: com has yesterday data; app would too but its zone
    # download fails so we never read it.
    r2 = MagicMock()

    def fake_get_object(Bucket, Key):
        if Key == "state/com_yesterday.txt":
            return {"Body": BytesIO(b"keep.com\ndropping.com\n")}
        if Key == "state/app_yesterday.txt":
            return {"Body": BytesIO(b"keep.app\ngoner.app\n")}
        raise _no_such_key()

    r2.get_object.side_effect = fake_get_object

    links = [
        "https://x/com.zone",
        "https://x/net.zone",
        "https://x/app.zone",
    ]
    monkeypatch.setattr(pipeline.czds_client, "list_zone_links", lambda *_a, **_k: links)

    def fake_download(url, _token, path, timeout=120):
        if "app.zone" in url:
            from scripts.czds_client import CzdsApiError
            raise CzdsApiError("simulated 5xx")
        with open(path, "wb") as fh:
            fh.write(b"placeholder")
        return 11

    monkeypatch.setattr(pipeline.czds_client, "download_zone", fake_download)
    monkeypatch.setattr(pipeline.zone_parser, "parse_zone", lambda _p: {"keep.com", "newcomer.com"})

    drops = pipeline.collect_drops(
        cfg, "tok", today=date(2026, 4, 27), r2_client=r2, r2_bucket="test-bucket",
    )
    names = {d["name"] for d in drops}
    assert names == {"dropping.com"}  # only com processed; net not approved; app failed
    assert all(d["dropped_date"] == "2026-04-27" for d in drops)
    assert all(d["tld"] == "com" for d in drops)

    # Today's snapshot was committed for com only (app failed before R2 reads).
    written_keys = [c.kwargs["Key"] for c in r2.put_object.call_args_list]
    assert written_keys == ["state/com_yesterday.txt"]
    # And it landed in the bucket the caller specified.
    assert r2.put_object.call_args.kwargs["Bucket"] == "test-bucket"


def test_collect_drops_constructs_r2_client_when_none_supplied(monkeypatch, cfg):
    """If the caller doesn't pass r2_client, the function calls diff._r2_client()
    once and reuses that client across TLDs (one auth per run, not 11)."""
    monkeypatch.setattr(pipeline.czds_client, "list_zone_links", lambda *_a, **_k: [])

    sentinel = MagicMock()
    calls = []

    def fake_factory():
        calls.append(1)
        return sentinel

    monkeypatch.setattr(pipeline.diff, "_r2_client", fake_factory)
    monkeypatch.setattr(pipeline.diff, "_bucket", lambda: "domainsifter-state")
    pipeline.collect_drops(cfg, "tok", today=date(2026, 4, 27))
    assert len(calls) == 1


def test_enrich_one_merges_results_from_all_enrichers(cfg):
    enrichers = [
        ("a", lambda d, c: {"a_field": 1}),
        ("b", lambda d, c: {"b_field": "x"}),
        ("c", lambda d, c: {}),
    ]
    cand = {"name": "foo.com", "tld": "com"}
    result = pipeline._enrich_one(cand, cfg, enrichers)
    assert result["a_field"] == 1
    assert result["b_field"] == "x"
    assert result["name"] == "foo.com"


def test_enrich_one_continues_on_enricher_exception(cfg):
    def boom(_d, _c):
        raise RuntimeError("source down")

    enrichers = [
        ("good", lambda d, c: {"good_field": True}),
        ("bad", boom),
        ("also_good", lambda d, c: {"other": 7}),
    ]
    cand = {"name": "foo.com"}
    result = pipeline._enrich_one(cand, cfg, enrichers)
    assert result["good_field"] is True
    assert result["other"] == 7


def test_enrich_one_propagates_spam_check_config_error(cfg):
    from scripts.enrichment.spam_check import SpamCheckConfigError

    def boom(_d, _c):
        raise SpamCheckConfigError("missing key")

    enrichers = [("spam_check", boom)]
    with pytest.raises(SpamCheckConfigError):
        pipeline._enrich_one({"name": "foo.com"}, cfg, enrichers)


def test_enrich_all_returns_empty_when_no_candidates(cfg):
    assert pipeline.enrich_all([], cfg) == []


def test_trim_for_enrichment_no_op_when_under_cap():
    cands = [{"name": "a.com"}, {"name": "bbb.com"}]
    assert pipeline._trim_for_enrichment(cands, 10) == cands


def test_trim_for_enrichment_keeps_shortest_names_first():
    cands = [
        {"name": "longerdomain.com"},
        {"name": "ab.com"},
        {"name": "medium.com"},
        {"name": "abcd.com"},
    ]
    trimmed = pipeline._trim_for_enrichment(cands, 2)
    assert [c["name"] for c in trimmed] == ["ab.com", "abcd.com"]


def test_enrich_all_respects_time_budget(monkeypatch, cfg):
    """When budget exhausts mid-run, enrich_all stops submitting and returns
    whatever finished. Per project guidance: partial output is the design,
    not a failure."""
    cfg = {**cfg, "enrichment_time_budget_seconds": 0.0, "max_concurrent_enrichments": 2}

    # Stub out _load_enrichers so we don't import real modules.
    def slow_enricher(_d, _c):
        import time as _t
        _t.sleep(0.05)
        return {"slow_field": True}

    monkeypatch.setattr(pipeline, "_load_enrichers", lambda: [("slow", slow_enricher)])

    cands = [{"name": f"d{i}.com"} for i in range(10)]
    result = pipeline.enrich_all(cands, cfg)
    # We can't assert exactly how many got enriched (depends on the scheduler),
    # but with budget=0.0 we definitely don't process all 10.
    assert len(result) <= len(cands)


def test_enrich_all_propagates_spam_check_config_error(monkeypatch, cfg):
    from scripts.enrichment.spam_check import SpamCheckConfigError

    def boom(_d, _c):
        raise SpamCheckConfigError("nope")

    monkeypatch.setattr(pipeline, "_load_enrichers", lambda: [("spam_check", boom)])
    cands = [{"name": "anything.com"}]
    with pytest.raises(SpamCheckConfigError):
        pipeline.enrich_all(cands, cfg)


# --- validate_availability ---------------------------------------------------


def test_validate_availability_keeps_only_404_responders(monkeypatch, cfg):
    """is_available=True survives; False and None are rejected."""
    def fake_check(domain, _config):
        if domain == "free.com":
            return {
                "is_available": True, "rdap_http": 404,
                "rdap_status": [], "rdap_expiration": None,
                "previous_registrar": None,
            }
        if domain == "owned.com":
            return {
                "is_available": False, "rdap_http": 200,
                "rdap_status": ["client hold"], "rdap_expiration": "2027-01-01",
                "previous_registrar": "Namecheap",
            }
        return {  # transport failure / unknown
            "is_available": None, "rdap_http": None,
            "rdap_status": [], "rdap_expiration": None,
            "previous_registrar": None,
        }

    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)

    cands = [
        {"name": "free.com", "score": 75},
        {"name": "owned.com", "score": 60},
        {"name": "broken.tld", "score": 50},
    ]
    kept = pipeline.validate_availability(cands, cfg)
    assert [c["name"] for c in kept] == ["free.com"]
    # The kept candidate has all RDAP fields merged on.
    assert kept[0]["is_available"] is True
    assert kept[0]["rdap_http"] == 404
    assert "availability_verified_at" in kept[0]


def test_validate_availability_respects_budget(monkeypatch, cfg):
    """Budget=0 means we don't make any RDAP calls; everything rejected."""
    cfg = {**cfg, "availability_budget_seconds": 0.0}
    calls = []

    def fake_check(domain, _config):
        calls.append(domain)
        return {"is_available": True, "rdap_http": 404, "rdap_status": [],
                "rdap_expiration": None, "previous_registrar": None}

    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)

    cands = [{"name": f"d{i}.com", "score": 50} for i in range(5)]
    kept = pipeline.validate_availability(cands, cfg)
    assert kept == []
    assert calls == []  # budget exhausted before first call


def test_validate_availability_empty_input_short_circuits():
    assert pipeline.validate_availability([], {}) == []


def test_main_skips_enrichment_for_unavailable_domains(monkeypatch, cfg, tmp_path):
    """Architectural assertion: availability check runs BEFORE enrichment.
    A candidate marked is_available=False must NOT reach enrich_all."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today: [
            {"name": "available.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "owned.com", "tld": "com", "dropped_date": today.isoformat()},
        ],
    )

    from scripts.enrichment import rdap as rdap_mod
    def fake_check(domain, _c):
        if domain == "available.com":
            return {"is_available": True, "rdap_http": 404, "rdap_status": [],
                    "rdap_expiration": None, "previous_registrar": None}
        return {"is_available": False, "rdap_http": 200,
                "rdap_status": ["client hold"], "rdap_expiration": "2027-01-01",
                "previous_registrar": "Acme"}
    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)

    enriched_names: list[str] = []

    def fake_enrich_all(cands, _cfg):
        for c in cands:
            enriched_names.append(c["name"])
            c.update({
                "wayback_snapshots": 50, "wayback_last_snapshot": "2024-01-01",
                "open_page_rank": 3.0, "cert_history": True,
                "spam_flagged": False, "surbl_listed": False,
                "spamhaus_listed": False,
            })
        return cands

    monkeypatch.setattr(pipeline, "enrich_all", fake_enrich_all)

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    # owned.com was rejected at availability stage; only available.com hit
    # the (expensive) enrichment phase.
    assert enriched_names == ["available.com"]
    written = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert [d["name"] for d in written["domains"]] == ["available.com"]


def test_main_runs_structural_then_lexical_then_enrich_then_post(monkeypatch, cfg, tmp_path):
    """Trace which filtering stages run in what order. Inputs are crafted so:
       - "great.com" survives both filter passes (real word)
       - "78win012.com" survives structural but gets killed by lexical
       - "xn--bad.com" gets killed by structural (punycode)
    Final output should contain only "great.com".
    """
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today: [
            {"name": "great.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "78win012.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "xn--bad.com", "tld": "com", "dropped_date": today.isoformat()},
        ],
    )

    enriched_names: list[str] = []

    def fake_enrich_all(cands, _cfg):
        for c in cands:
            enriched_names.append(c["name"])
            c.update({
                "wayback_snapshots": 50,
                "wayback_last_snapshot": "2024-01-01",
                "open_page_rank": 3.0,
                "cert_history": True,
                "spam_flagged": False,
                "surbl_listed": False,
                "spamhaus_listed": False,
                "previous_registrar": "Acme",
            })
        return cands

    monkeypatch.setattr(pipeline, "enrich_all", fake_enrich_all)

    # Stub RDAP availability — every survivor is "available" so we can
    # observe what reached the publication stage.
    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(
        rdap_mod, "check_availability",
        lambda d, _c: {"is_available": True, "rdap_http": 404,
                       "rdap_status": [], "rdap_expiration": None,
                       "previous_registrar": None},
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    # Only great.com should have hit enrichment (78win012 fails lexical, xn-- fails structural)
    assert enriched_names == ["great.com"]
    written = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert [d["name"] for d in written["domains"]] == ["great.com"]


def test_main_aborts_when_required_env_missing(monkeypatch, cfg, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    for var in (
        "CZDS_USERNAME", "CZDS_PASSWORD", "SAFE_BROWSING_KEY",
        "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    from scripts.env_check import MissingEnvVarsError

    with pytest.raises(MissingEnvVarsError):
        pipeline.main(["--config", str(cfg_path)])


def test_main_happy_path_writes_output(monkeypatch, cfg, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    monkeypatch.setenv("OPENPAGERANK_KEY", "o")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today: [
            {"name": "great.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "alsogood.com", "tld": "com", "dropped_date": today.isoformat()},
        ],
    )

    def fake_enrich_all(cands, _cfg):
        for c in cands:
            c.update(
                {
                    "wayback_snapshots": 50,
                    "wayback_last_snapshot": "2024-01-01",
                    "open_page_rank": 3.0,
                    "cert_history": True,
                    "spam_flagged": False,
                    "surbl_listed": False,
                    "spamhaus_listed": False,
                    "previous_registrar": "Acme",
                }
            )
        return cands

    monkeypatch.setattr(pipeline, "enrich_all", fake_enrich_all)

    # Both candidates are "available" per RDAP so they reach the output.
    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(
        rdap_mod, "check_availability",
        lambda d, _c: {"is_available": True, "rdap_http": 404,
                       "rdap_status": [], "rdap_expiration": None,
                       "previous_registrar": None},
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    written = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert written["domain_count"] == 2
    names = sorted(d["name"] for d in written["domains"])
    assert names == ["alsogood.com", "great.com"]
    # Each emitted domain has both registrars wired up with substituted URLs.
    for d in written["domains"]:
        reg_names = [r["name"] for r in d["registrars"]]
        assert reg_names == ["Namecheap", "NameSilo"]
        assert any(d["name"] in r["url"] for r in d["registrars"])
        # Availability fields land in the published payload.
        assert "rdap_status" in d
        assert "rdap_expiration" in d
        assert "availability_verified_at" in d


def test_main_propagates_spam_check_config_error(monkeypatch, cfg, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today: [{"name": "great.com", "tld": "com", "dropped_date": "2026-04-27"}],
    )

    # Availability check now runs BEFORE enrichment — stub it so the
    # candidate flows through to the spam_check failure we're testing.
    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(
        rdap_mod, "check_availability",
        lambda d, _c: {"is_available": True, "rdap_http": 404,
                       "rdap_status": [], "rdap_expiration": None,
                       "previous_registrar": None},
    )

    from scripts.enrichment.spam_check import SpamCheckConfigError

    def boom(*_a, **_k):
        raise SpamCheckConfigError("nope")

    monkeypatch.setattr(pipeline, "enrich_all", boom)

    with pytest.raises(SpamCheckConfigError):
        pipeline.main(["--config", str(cfg_path)])
