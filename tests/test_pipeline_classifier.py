"""Integration + unit tests for Stage 4b (snapshot content classifier)
wired into the pipeline, plus the new _write_sidecar_excerpts helper.

Kept in a separate file from test_pipeline.py because the classifier
wire-in concerns are clustered (sidecar I/O, classifier mocking, the
filter+verdict downstream effects). All tests reuse the cfg fixture
and minimal-pipeline helpers from test_pipeline.py via direct import.

Phase 4 wire-in: 2026-05-20.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from scripts import pipeline
from tests.test_pipeline import cfg, _set_r2_env  # reuse the fixture machinery


def _wire_minimal_pipeline_for_classifier(monkeypatch, today):
    """Minimal mocking for main() exercising Stage 4b. Five-candidate
    fixture covers the full category matrix.

    Bypasses the lexical filter (which has its own dedicated tests) by
    passing every candidate through — this test file's concern is the
    Stage 4b classifier and its downstream filter/verdict effects, not
    the pre-existing lexical filter behaviour.
    """
    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    # Pass-through the lexical filter — tested independently in
    # tests/test_lexical_filter.py; here we want all 5 candidates to
    # reach Stage 4b regardless of their letter-pattern entropy.
    monkeypatch.setattr(
        pipeline.lexical_filter, "filter_candidates",
        lambda cands, _cfg, rejections_out=None: list(cands),
    )
    monkeypatch.setattr(
        pipeline, "collect_drops",
        lambda _cfg, _tok, today, **_kw: ([
            {"name": "alphasite.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "parkedhome.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "toxicpage.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "emptypage.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "mysteryco.com", "tld": "com", "dropped_date": today.isoformat()},
        ], []),
    )

    def fake_enrich_all(cands, _cfg):
        for c in cands:
            c.update({
                "wayback_snapshots": 5000,
                "wayback_last_snapshot": "2024-01-01",
                "open_page_rank": 3.0,
                "cert_history": True,
                "spam_flagged": False,
                "surbl_listed": False,
                "spamhaus_listed": False,
                "cc_source_domain_count": 500,
                "previous_registrar": "Acme",
            })
        return cands
    monkeypatch.setattr(pipeline, "enrich_all", fake_enrich_all)

    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(
        rdap_mod, "check_availability",
        lambda d, _c: {
            "is_available": True, "rdap_http": 404,
            "rdap_status": [], "rdap_expiration": None,
            "previous_registrar": None,
        },
    )


# ---------------------------------------------------------------------------
# Stage 4b integration via main()
# ---------------------------------------------------------------------------


def test_main_invokes_snapshot_classifier_with_enriched_list(monkeypatch, cfg, tmp_path):
    """classify_all is called with the enriched candidates between Stage 4
    (enrichment) and Stage 5 (post-enrichment filter)."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    classify_calls = []
    def fake_classify(cands, *, client=None, pause_seconds=1.0):
        classify_calls.append({
            "count": len(cands),
            "names": [c["name"] for c in cands],
            "client_is_none": client is None,
            "pause_seconds": pause_seconds,
        })
        for c in cands:
            c["snapshot_category"] = "legitimate"
            c["snapshot_classifier_version"] = "v1"
            c["wayback_excerpt"] = {"title": "x"}
        return {"legitimate": len(cands), "parked": 0, "toxic": 0,
                "empty": 0, "unknown": 0}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda: None,
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    assert len(classify_calls) == 1
    call = classify_calls[0]
    assert call["count"] == 5
    assert set(call["names"]) == {
        "alphasite.com", "parkedhome.com", "toxicpage.com", "emptypage.com", "mysteryco.com",
    }
    assert call["pause_seconds"] == 1.0


def test_main_rejects_toxic_at_post_enrichment_filter(monkeypatch, cfg, tmp_path):
    """Toxic labeled by Stage 4b → removed at Stage 5, absent from JSON."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    category_map = {
        "alphasite.com": "legitimate",
        "parkedhome.com": "parked",
        "toxicpage.com": "toxic",
        "emptypage.com": "empty",
        "mysteryco.com": "unknown",
    }
    def fake_classify(cands, *, client=None, pause_seconds=1.0):
        for c in cands:
            c["snapshot_category"] = category_map[c["name"]]
            c["snapshot_classifier_version"] = "v1"
            c["wayback_excerpt"] = {"title": "x"}
        return {"legitimate": 1, "parked": 1, "toxic": 1, "empty": 1, "unknown": 1}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda: None,
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0

    daily = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    names = {d["name"] for d in daily["domains"]}
    assert "toxicpage.com" not in names
    assert names == {"alphasite.com", "parkedhome.com", "emptypage.com", "mysteryco.com"}


def test_main_downgrades_parked_and_empty_to_caution(monkeypatch, cfg, tmp_path):
    """parked/empty labels force verdict=Caution regardless of score."""
    # Loosen thresholds so legitimate/unknown would otherwise be Clean.
    cfg["verdict_thresholds"] = {
        "clean_min_score": 30,
        "promising_min_score": 10,
        "promising_min_wayback_snapshots": 1,
        "promising_min_open_page_rank": 0.0,
        "promising_min_cc_source_domain_count": 0,
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    category_map = {
        "alphasite.com": "legitimate",
        "parkedhome.com": "parked",
        "toxicpage.com": "toxic",
        "emptypage.com": "empty",
        "mysteryco.com": "unknown",
    }
    def fake_classify(cands, *, client=None, pause_seconds=1.0):
        for c in cands:
            c["snapshot_category"] = category_map[c["name"]]
            c["snapshot_classifier_version"] = "v1"
        return {"legitimate": 1, "parked": 1, "toxic": 1, "empty": 1, "unknown": 1}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda: None,
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0

    daily = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    by_name = {d["name"]: d for d in daily["domains"]}

    assert by_name["parkedhome.com"]["verdict"] == "Caution"
    assert by_name["emptypage.com"]["verdict"] == "Caution"
    assert by_name["alphasite.com"]["verdict"] == "Clean"
    assert by_name["mysteryco.com"]["verdict"] == "Clean"


def test_main_strips_inline_wayback_excerpt_before_filter(monkeypatch, cfg, tmp_path):
    """Inline wayback_excerpt must not propagate to the published JSON.
    Sidecar is the canonical location (design (h))."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    def fake_classify(cands, *, client=None, pause_seconds=1.0):
        for c in cands:
            c["snapshot_category"] = "legitimate"
            c["snapshot_classifier_version"] = "v1"
            c["wayback_excerpt"] = {"title": "leak-me"}
        return {"legitimate": len(cands), "parked": 0, "toxic": 0,
                "empty": 0, "unknown": 0}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda: None,
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0

    daily = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    for entry in daily["domains"]:
        assert "wayback_excerpt" not in entry


def test_main_writes_sidecar_with_classified_excerpts(monkeypatch, cfg, tmp_path):
    """Sidecar gets today's classified excerpts written, merged with
    pre-existing entries."""
    sidecar = tmp_path / "wayback_excerpts.json"
    sidecar.write_text(
        json.dumps({"legacy.com": {"title": "From a prior run"}}),
        encoding="utf-8",
    )
    cfg["sidecar_excerpts_path"] = str(sidecar)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    def fake_classify(cands, *, client=None, pause_seconds=1.0):
        for c in cands:
            c["snapshot_category"] = "legitimate"
            c["snapshot_classifier_version"] = "v1"
            c["wayback_excerpt"] = {"title": "today-" + c["name"]}
        return {"legitimate": len(cands), "parked": 0, "toxic": 0,
                "empty": 0, "unknown": 0}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda: None,
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0

    sidecar_after = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_after["legacy.com"] == {"title": "From a prior run"}
    for name in ["alphasite.com", "parkedhome.com", "toxicpage.com", "emptypage.com", "mysteryco.com"]:
        assert sidecar_after[name] == {"title": "today-" + name}


def test_main_no_api_key_passes_through_as_unknown(monkeypatch, cfg, tmp_path):
    """Soft-fail (design (k)): no ANTHROPIC_API_KEY → all entries unknown,
    pipeline still publishes."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda: None,
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0

    daily = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert len(daily["domains"]) == 5
    for entry in daily["domains"]:
        assert entry["snapshot_category"] == "unknown"
        assert entry["snapshot_classifier_version"] == "v1"


# ---------------------------------------------------------------------------
# _write_sidecar_excerpts unit tests
# ---------------------------------------------------------------------------


class TestWriteSidecarExcerpts:
    def test_writes_only_classifier_touched_records(self, tmp_path):
        path = tmp_path / "sidecar.json"
        records = [
            {"name": "a.com", "wayback_excerpt": {"title": "A"},
             "snapshot_classifier_version": "v1"},
            {"name": "b.com", "wayback_excerpt": None,
             "snapshot_classifier_version": "v1"},
            {"name": "untouched.com", "wayback_excerpt": {"title": "ignore"}},
        ]
        total = pipeline._write_sidecar_excerpts(records, path)
        assert total == 2
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data.keys()) == {"a.com", "b.com"}
        assert data["a.com"] == {"title": "A"}
        assert data["b.com"] is None

    def test_strips_inline_wayback_excerpt_after_write(self, tmp_path):
        path = tmp_path / "sidecar.json"
        records = [
            {"name": "a.com", "wayback_excerpt": {"title": "A"},
             "snapshot_classifier_version": "v1"},
        ]
        pipeline._write_sidecar_excerpts(records, path)
        assert "wayback_excerpt" not in records[0]
        assert records[0]["name"] == "a.com"
        assert records[0]["snapshot_classifier_version"] == "v1"

    def test_merges_with_existing_sidecar(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({
                "legacy.com": {"title": "Old"},
                "shared.com": {"title": "Old shared"},
            }),
            encoding="utf-8",
        )
        records = [
            {"name": "shared.com", "wayback_excerpt": {"title": "New shared"},
             "snapshot_classifier_version": "v1"},
            {"name": "new.com", "wayback_excerpt": {"title": "Brand new"},
             "snapshot_classifier_version": "v1"},
        ]
        pipeline._write_sidecar_excerpts(records, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["legacy.com"] == {"title": "Old"}
        assert data["shared.com"] == {"title": "New shared"}
        assert data["new.com"] == {"title": "Brand new"}

    def test_no_classifier_touched_records_skips_write(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({"legacy.com": {"title": "Preserved"}}),
            encoding="utf-8",
        )
        records = [
            {"name": "a.com", "wayback_excerpt": {"title": "A"}},
        ]
        total = pipeline._write_sidecar_excerpts(records, path)
        assert total == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"legacy.com": {"title": "Preserved"}}

    def test_corrupt_sidecar_resets_with_warning(self, tmp_path, caplog):
        import logging
        path = tmp_path / "sidecar.json"
        path.write_text(json.dumps(["unexpected", "shape"]), encoding="utf-8")
        records = [
            {"name": "a.com", "wayback_excerpt": {"title": "A"},
             "snapshot_classifier_version": "v1"},
        ]
        with caplog.at_level(logging.WARNING, logger="scripts.pipeline"):
            pipeline._write_sidecar_excerpts(records, path)
        assert any("not a dict" in rec.message for rec in caplog.records)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"a.com": {"title": "A"}}

    def test_missing_sidecar_creates_new(self, tmp_path):
        path = tmp_path / "nonexistent.json"
        assert not path.exists()
        records = [
            {"name": "a.com", "wayback_excerpt": {"title": "A"},
             "snapshot_classifier_version": "v1"},
        ]
        pipeline._write_sidecar_excerpts(records, path)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"a.com": {"title": "A"}}
