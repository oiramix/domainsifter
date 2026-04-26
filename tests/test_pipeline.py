"""Unit tests for scripts/pipeline.py — the orchestrator.

Heavy mocking: we do NOT exercise the real CZDS, enrichment, or filesystem
output paths here (those modules have their own tests). We verify that the
orchestrator wires the pieces together correctly:
    - env_check called first
    - CZDS auth uses env vars + the auth_base from config
    - only approved TLDs are downloaded
    - per-zone failures don't abort the run
    - enrichers are applied to every candidate
    - SpamCheckConfigError aborts the run
    - the final output is written via output.write_output
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from scripts import pipeline


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
        "max_candidates_per_day": 100,
        "affiliate_link_template": "https://aff.example/?d={name}",
        "state_dir": str(tmp_path / "state"),
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


def test_collect_drops_filters_to_approved_and_handles_failed_zone(monkeypatch, cfg, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "com_yesterday.txt").write_text(
        "keep.com\ndropping.com\n", encoding="utf-8"
    )
    (state_dir / "app_yesterday.txt").write_text(
        "keep.app\ngoner.app\n", encoding="utf-8"
    )

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

    drops = pipeline.collect_drops(cfg, "tok", today=date(2026, 4, 27))
    names = {d["name"] for d in drops}
    assert names == {"dropping.com"}  # only com processed; net not approved; app failed
    assert all(d["dropped_date"] == "2026-04-27" for d in drops)
    assert all(d["tld"] == "com" for d in drops)


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


def test_main_aborts_when_required_env_missing(monkeypatch, cfg, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.delenv("CZDS_USERNAME", raising=False)
    monkeypatch.delenv("CZDS_PASSWORD", raising=False)
    monkeypatch.delenv("SAFE_BROWSING_KEY", raising=False)
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

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    written = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert written["domain_count"] == 2
    names = sorted(d["name"] for d in written["domains"])
    assert names == ["alsogood.com", "great.com"]
    assert all(d["affiliate_link"].startswith("https://aff.example/?d=") for d in written["domains"])


def test_main_propagates_spam_check_config_error(monkeypatch, cfg, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today: [{"name": "x.com", "tld": "com", "dropped_date": "2026-04-27"}],
    )

    from scripts.enrichment.spam_check import SpamCheckConfigError

    def boom(*_a, **_k):
        raise SpamCheckConfigError("nope")

    monkeypatch.setattr(pipeline, "enrich_all", boom)

    with pytest.raises(SpamCheckConfigError):
        pipeline.main(["--config", str(cfg_path)])
