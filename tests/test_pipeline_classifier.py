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
    def fake_classify(cands, *, client=None, pause_seconds=1.0, config=None):
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
        pipeline.snapshot_classifier, "make_default_client", lambda *_a, **_k: None,
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
    def fake_classify(cands, *, client=None, pause_seconds=1.0, config=None):
        for c in cands:
            c["snapshot_category"] = category_map[c["name"]]
            c["snapshot_classifier_version"] = "v1"
            c["wayback_excerpt"] = {"title": "x"}
        return {"legitimate": 1, "parked": 1, "toxic": 1, "empty": 1, "unknown": 1}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda *_a, **_k: None,
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
    def fake_classify(cands, *, client=None, pause_seconds=1.0, config=None):
        for c in cands:
            c["snapshot_category"] = category_map[c["name"]]
            c["snapshot_classifier_version"] = "v1"
        return {"legitimate": 1, "parked": 1, "toxic": 1, "empty": 1, "unknown": 1}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda *_a, **_k: None,
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

    def fake_classify(cands, *, client=None, pause_seconds=1.0, config=None):
        for c in cands:
            c["snapshot_category"] = "legitimate"
            c["snapshot_classifier_version"] = "v1"
            c["wayback_excerpt"] = {"title": "leak-me"}
        return {"legitimate": len(cands), "parked": 0, "toxic": 0,
                "empty": 0, "unknown": 0}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda *_a, **_k: None,
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

    def fake_classify(cands, *, client=None, pause_seconds=1.0, config=None):
        for c in cands:
            c["snapshot_category"] = "legitimate"
            c["snapshot_classifier_version"] = "v1"
            c["wayback_excerpt"] = {"title": "today-" + c["name"]}
        return {"legitimate": len(cands), "parked": 0, "toxic": 0,
                "empty": 0, "unknown": 0}
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda *_a, **_k: None,
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
        pipeline.snapshot_classifier, "make_default_client", lambda *_a, **_k: None,
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0

    daily = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert len(daily["domains"]) == 5
    for entry in daily["domains"]:
        assert entry["snapshot_category"] == "unknown"
        # Assert against the constant, not a literal: the version is bumped
        # whenever the prompt/parser could move a label (v1 -> v2 on the
        # 2026-09-18 batching + backend switch), and this test is about the
        # pass-through behaviour, not the stamp's value.
        assert (
            entry["snapshot_classifier_version"]
            == pipeline.snapshot_classifier.CLASSIFIER_VERSION
        )


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

    def test_failed_refetch_does_not_null_out_stored_excerpt(self, tmp_path):
        """THE regression that would destroy the sidecar: 379 of 598 stored
        entries are null and archive.org failed 184 of 289 fetches on
        2026-09-19, so a run whose fetches fail must not overwrite the 214
        usable excerpts with null."""
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({
                "keepme.com": {"title": "Stored evidence"},
                "alsonull.com": None,
            }),
            encoding="utf-8",
        )
        records = [
            # Re-fetch failed this run.
            {"name": "keepme.com", "wayback_excerpt": None,
             "snapshot_classifier_version": "v1"},
            # Still null, still null.
            {"name": "alsonull.com", "wayback_excerpt": None,
             "snapshot_classifier_version": "v1"},
            # New failure with nothing stored — the None IS recorded.
            {"name": "brandnewfail.com", "wayback_excerpt": None,
             "snapshot_classifier_version": "v1"},
        ]
        pipeline._write_sidecar_excerpts(records, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["keepme.com"] == {"title": "Stored evidence"}
        assert data["alsonull.com"] is None
        assert data["brandnewfail.com"] is None

    def test_empty_dict_excerpt_also_does_not_clobber(self, tmp_path):
        """An empty-dict excerpt is no more evidence than None."""
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({"keepme.com": {"title": "Stored evidence"}}),
            encoding="utf-8",
        )
        records = [
            {"name": "keepme.com", "wayback_excerpt": {},
             "snapshot_classifier_version": "v1"},
        ]
        pipeline._write_sidecar_excerpts(records, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["keepme.com"] == {"title": "Stored evidence"}

    def test_successful_refetch_still_overwrites(self, tmp_path):
        """Reuse must not freeze a stale excerpt in place: when a fetch DOES
        return content it replaces what is stored."""
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({"movesite.com": {"title": "Old"}}), encoding="utf-8",
        )
        records = [
            {"name": "movesite.com", "wayback_excerpt": {"title": "Fresh"},
             "snapshot_classifier_version": "v1"},
        ]
        pipeline._write_sidecar_excerpts(records, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["movesite.com"] == {"title": "Fresh"}


# ---------------------------------------------------------------------------
# _load_sidecar_excerpts / _build_excerpt_cache unit tests
# ---------------------------------------------------------------------------


class TestLoadSidecarExcerpts:
    def test_loads_dict_and_null_entries(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({
                "coppernest.org": {"title": "Copper Nest"},
                "tideblock.io": None,
            }),
            encoding="utf-8",
        )
        cache = pipeline._load_sidecar_excerpts(path)
        assert cache == {
            "coppernest.org": {"title": "Copper Nest"},
            "tideblock.io": None,
        }

    def test_missing_file_returns_empty(self, tmp_path):
        assert pipeline._load_sidecar_excerpts(tmp_path / "nope.json") == {}

    def test_invalid_json_returns_empty(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text("{not json at all", encoding="utf-8")
        assert pipeline._load_sidecar_excerpts(path) == {}

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text("", encoding="utf-8")
        assert pipeline._load_sidecar_excerpts(path) == {}

    def test_wrong_toplevel_shape_returns_empty(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text(json.dumps(["marketglow.com"]), encoding="utf-8")
        assert pipeline._load_sidecar_excerpts(path) == {}

    def test_drops_entries_with_unexpected_value_shape(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({
                "marketglow.com": {"title": "Market Glow"},
                "bogus.com": "a bare string is not an excerpt",
                "alsobogus.com": 7,
            }),
            encoding="utf-8",
        )
        cache = pipeline._load_sidecar_excerpts(path)
        assert cache == {"marketglow.com": {"title": "Market Glow"}}

    def test_unreadable_file_returns_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "sidecar.json"
        path.write_text(json.dumps({"marketglow.com": None}), encoding="utf-8")

        def boom(*_a, **_k):
            raise OSError("permission denied")
        monkeypatch.setattr("builtins.open", boom)
        assert pipeline._load_sidecar_excerpts(path) == {}


class TestBuildExcerptCache:
    def test_returns_cache_when_reuse_enabled_by_default(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({"marketglow.com": {"title": "MG"}}), encoding="utf-8",
        )
        # No snapshot_classifier block at all -> reuse defaults to on.
        assert pipeline._build_excerpt_cache({}, path) == {
            "marketglow.com": {"title": "MG"},
        }

    def test_returns_none_when_reuse_disabled(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text(
            json.dumps({"marketglow.com": {"title": "MG"}}), encoding="utf-8",
        )
        config = {"snapshot_classifier": {"reuse_cached_excerpts": False}}
        assert pipeline._build_excerpt_cache(config, path) is None

    def test_non_dict_classifier_block_falls_back_to_enabled(self, tmp_path):
        path = tmp_path / "sidecar.json"
        path.write_text(json.dumps({"tideblock.io": None}), encoding="utf-8")
        cache = pipeline._build_excerpt_cache(
            {"snapshot_classifier": "not a dict"}, path,
        )
        assert cache == {"tideblock.io": None}

    def test_unexpected_exception_degrades_to_empty_cache(self, tmp_path, monkeypatch):
        def boom(_path):
            raise RuntimeError("something nobody predicted")
        monkeypatch.setattr(pipeline, "_load_sidecar_excerpts", boom)
        assert pipeline._build_excerpt_cache({}, tmp_path / "sidecar.json") == {}


class TestClassifierAcceptsExcerptCache:
    def test_true_for_keyword_only_param(self):
        def new_sig(cands, *, client=None, pause_seconds=1.0, config=None,
                    excerpt_cache=None):
            return {}
        assert pipeline._classifier_accepts_excerpt_cache(new_sig) is True

    def test_false_for_old_signature(self):
        def old_sig(cands, *, client=None, pause_seconds=1.0, config=None):
            return {}
        assert pipeline._classifier_accepts_excerpt_cache(old_sig) is False

    def test_true_for_var_keyword(self):
        def kwargs_sig(cands, **kwargs):
            return {}
        assert pipeline._classifier_accepts_excerpt_cache(kwargs_sig) is True

    def test_uninspectable_callable_is_treated_as_old(self):
        assert pipeline._classifier_accepts_excerpt_cache(object()) is False


# ---------------------------------------------------------------------------
# Excerpt reuse wired through main()
# ---------------------------------------------------------------------------


def _install_classifier(monkeypatch, fake_classify):
    monkeypatch.setattr(pipeline.snapshot_classifier, "classify_all", fake_classify)
    monkeypatch.setattr(
        pipeline.snapshot_classifier, "make_default_client", lambda *_a, **_k: None,
    )


def _recording_classifier(calls, excerpt_map=None):
    """A classify_all stand-in with the NEW signature that records what it
    was handed. excerpt_map lets a test control the per-domain excerpt the
    classifier ends up storing (None == fetch failed)."""
    def fake_classify(cands, *, client=None, pause_seconds=1.0, config=None,
                      excerpt_cache=None):
        calls.append({
            "names": [c["name"] for c in cands],
            "config": config,
            "excerpt_cache": excerpt_cache,
        })
        for c in cands:
            c["snapshot_category"] = "legitimate"
            c["snapshot_classifier_version"] = "v1"
            if excerpt_map is None:
                c["wayback_excerpt"] = {"title": "fresh-" + c["name"]}
            else:
                c["wayback_excerpt"] = excerpt_map.get(c["name"])
        return {"legitimate": len(cands), "parked": 0, "toxic": 0,
                "empty": 0, "unknown": 0}
    return fake_classify


def test_main_passes_sidecar_as_excerpt_cache(monkeypatch, cfg, tmp_path):
    """The stored sidecar reaches classify_all as excerpt_cache — this is
    what stops archive.org being re-asked the same question every day."""
    sidecar = tmp_path / "wayback_excerpts.json"
    sidecar.write_text(
        json.dumps({
            "alphasite.com": {"title": "Stored Alpha"},
            "mysteryco.com": None,
            "notintodays.com": {"title": "Historical"},
        }),
        encoding="utf-8",
    )
    cfg["sidecar_excerpts_path"] = str(sidecar)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    calls: list[dict] = []
    _install_classifier(monkeypatch, _recording_classifier(calls))

    assert pipeline.main(["--config", str(cfg_path)]) == 0
    assert len(calls) == 1
    cache = calls[0]["excerpt_cache"]
    assert cache is not None
    assert cache["alphasite.com"] == {"title": "Stored Alpha"}
    assert cache["mysteryco.com"] is None
    # The whole sidecar is handed over, not just today's names — the
    # classifier decides what to reuse.
    assert cache["notintodays.com"] == {"title": "Historical"}


def test_main_threads_config_into_classifier_stage(monkeypatch, cfg, tmp_path):
    """Regression guard for the 2026-09-18 defect: pipeline.py failed to
    pass `config` to classify_all, so every config.json knob for this stage
    (including reuse_cached_excerpts) silently had no effect."""
    cfg["snapshot_classifier"] = {
        "reuse_cached_excerpts": True,
        "marker_for_test": "reached-the-classifier",
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    calls: list[dict] = []
    _install_classifier(monkeypatch, _recording_classifier(calls))

    assert pipeline.main(["--config", str(cfg_path)]) == 0
    passed_config = calls[0]["config"]
    assert isinstance(passed_config, dict)
    assert (
        passed_config["snapshot_classifier"]["marker_for_test"]
        == "reached-the-classifier"
    )


def test_main_omits_excerpt_cache_when_reuse_disabled(monkeypatch, cfg, tmp_path):
    """reuse_cached_excerpts=false restores the pre-2026-09-21 behaviour
    exactly: the kwarg is not passed at all."""
    sidecar = tmp_path / "wayback_excerpts.json"
    sidecar.write_text(
        json.dumps({"alphasite.com": {"title": "Stored Alpha"}}),
        encoding="utf-8",
    )
    cfg["sidecar_excerpts_path"] = str(sidecar)
    cfg["snapshot_classifier"] = {"reuse_cached_excerpts": False}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    calls: list[dict] = []
    _install_classifier(monkeypatch, _recording_classifier(calls))

    assert pipeline.main(["--config", str(cfg_path)]) == 0
    assert calls[0]["excerpt_cache"] is None


def test_main_degrades_to_empty_cache_when_sidecar_corrupt(monkeypatch, cfg, tmp_path):
    """A corrupt sidecar must not abort the run — it degrades to no cache
    and the classifier re-fetches, i.e. the old behaviour."""
    sidecar = tmp_path / "wayback_excerpts.json"
    sidecar.write_text("{{{ truncated garbage", encoding="utf-8")
    cfg["sidecar_excerpts_path"] = str(sidecar)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    calls: list[dict] = []
    _install_classifier(monkeypatch, _recording_classifier(calls))

    assert pipeline.main(["--config", str(cfg_path)]) == 0
    assert calls[0]["excerpt_cache"] == {}
    # ...and the run still published.
    daily = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert len(daily["domains"]) == 5


def test_main_absent_sidecar_degrades_to_empty_cache(monkeypatch, cfg, tmp_path):
    """First-ever run: no sidecar on disk, empty cache, no crash."""
    sidecar = tmp_path / "does_not_exist.json"
    cfg["sidecar_excerpts_path"] = str(sidecar)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    calls: list[dict] = []
    _install_classifier(monkeypatch, _recording_classifier(calls))

    assert pipeline.main(["--config", str(cfg_path)]) == 0
    assert calls[0]["excerpt_cache"] == {}


def test_main_old_classifier_signature_still_runs(monkeypatch, cfg, tmp_path):
    """The excerpt_cache kwarg is being added by a parallel change. If this
    wiring lands first, classify_all still has the old signature and the
    daily run must be completely unaffected."""
    sidecar = tmp_path / "wayback_excerpts.json"
    sidecar.write_text(
        json.dumps({"alphasite.com": {"title": "Stored Alpha"}}),
        encoding="utf-8",
    )
    cfg["sidecar_excerpts_path"] = str(sidecar)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    calls: list[dict] = []

    def old_signature_classify(cands, *, client=None, pause_seconds=1.0,
                               config=None):
        calls.append({"config": config})
        for c in cands:
            c["snapshot_category"] = "legitimate"
            c["snapshot_classifier_version"] = "v1"
            c["wayback_excerpt"] = {"title": "fresh"}
        return {"legitimate": len(cands), "parked": 0, "toxic": 0,
                "empty": 0, "unknown": 0}
    _install_classifier(monkeypatch, old_signature_classify)

    assert pipeline.main(["--config", str(cfg_path)]) == 0
    assert len(calls) == 1
    daily = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert len(daily["domains"]) == 5


def test_main_keeps_stored_excerpt_when_this_runs_fetch_fails(monkeypatch, cfg, tmp_path):
    """End-to-end version of the write-back guard: a domain whose fetch
    failed this run keeps the excerpt stored by an earlier run."""
    sidecar = tmp_path / "wayback_excerpts.json"
    sidecar.write_text(
        json.dumps({
            "alphasite.com": {"title": "Stored Alpha"},
            "parkedhome.com": {"title": "Stored Parked"},
        }),
        encoding="utf-8",
    )
    cfg["sidecar_excerpts_path"] = str(sidecar)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    # Every fetch failed this run (the 2026-09-19 archive.org scenario).
    excerpt_map = {
        "alphasite.com": None,
        "parkedhome.com": None,
        "toxicpage.com": None,
        "emptypage.com": None,
        "mysteryco.com": None,
    }
    calls: list[dict] = []
    _install_classifier(monkeypatch, _recording_classifier(calls, excerpt_map))

    assert pipeline.main(["--config", str(cfg_path)]) == 0
    after = json.loads(sidecar.read_text(encoding="utf-8"))
    assert after["alphasite.com"] == {"title": "Stored Alpha"}
    assert after["parkedhome.com"] == {"title": "Stored Parked"}
    assert after["toxicpage.com"] is None


def test_main_logs_reuse_summary(monkeypatch, cfg, tmp_path, caplog):
    """One summary line so the archive.org load drop is visible in the
    daily report."""
    import logging
    sidecar = tmp_path / "wayback_excerpts.json"
    sidecar.write_text(
        json.dumps({
            "alphasite.com": {"title": "Stored Alpha"},
            "parkedhome.com": {"title": "Stored Parked"},
            "mysteryco.com": None,
        }),
        encoding="utf-8",
    )
    cfg["sidecar_excerpts_path"] = str(sidecar)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _wire_minimal_pipeline_for_classifier(monkeypatch, date.today())

    calls: list[dict] = []
    _install_classifier(monkeypatch, _recording_classifier(calls))

    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        assert pipeline.main(["--config", str(cfg_path)]) == 0

    reuse_lines = [
        rec.getMessage() for rec in caplog.records
        if "Excerpt reuse:" in rec.getMessage()
    ]
    assert len(reuse_lines) == 1
    # 2 of 5 candidates had a usable stored excerpt; the cached None still
    # counts as a fetch.
    assert "2 of 5" in reuse_lines[0]
    assert "3 needed an archive.org fetch" in reuse_lines[0]
    assert "eligible for reuse" in reuse_lines[0]


def test_reuse_summary_prefers_classifier_reported_counts(caplog):
    """If classify_all ever reports the split itself, the log uses its
    numbers rather than the pipeline's cache-hit estimate."""
    import logging
    candidates = [{"name": "marketglow.com"}, {"name": "tideblock.io"}]
    cache = {"marketglow.com": {"title": "MG"}, "tideblock.io": {"title": "TB"}}
    counts = {"legitimate": 2, "excerpts_reused": 1, "excerpts_fetched": 1}
    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        pipeline._log_excerpt_reuse_summary(counts, candidates, cache, True)
    line = [r.getMessage() for r in caplog.records if "Excerpt reuse:" in r.getMessage()]
    assert len(line) == 1
    assert "1 of 2" in line[0]


def test_reuse_summary_never_raises():
    """Not even on nonsense input — it is a log line, not a stage."""
    class Exploding(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")

    pipeline._log_excerpt_reuse_summary(None, [{"name": "x.com"}], Exploding(), True)
    pipeline._log_excerpt_reuse_summary({}, [], None, True)
    pipeline._log_excerpt_reuse_summary({}, [], None, False)


def test_main_survives_exception_anywhere_in_reuse_wiring(monkeypatch, cfg, tmp_path):
    """The overriding constraint: the 09:00 UTC run must publish. Nothing in
    the excerpt-reuse wiring may abort it, however it fails."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def boom(*_a, **_k):
        raise RuntimeError("this helper exploded")

    for target in [
        "_load_sidecar_excerpts",
        "_build_excerpt_cache",
        "_classifier_accepts_excerpt_cache",
        "_log_excerpt_reuse_summary",
    ]:
        with pytest.MonkeyPatch.context() as mp:
            _wire_minimal_pipeline_for_classifier(mp, date.today())
            calls: list[dict] = []
            mp.setattr(
                pipeline.snapshot_classifier, "classify_all",
                _recording_classifier(calls),
            )
            mp.setattr(
                pipeline.snapshot_classifier, "make_default_client",
                lambda *_a, **_k: None,
            )
            mp.setattr(pipeline, target, boom)
            assert pipeline.main(["--config", str(cfg_path)]) == 0, target
            # The classifier stage itself still ran and the day still published.
            assert len(calls) == 1, target
            daily = json.loads(
                (tmp_path / "daily.json").read_text(encoding="utf-8"),
            )
            assert len(daily["domains"]) == 5, target


# ---------------------------------------------------------------------------
# Enrichment coverage line (added 2026-09-23)
#
# The meta-fix for the failure shape every incident here shares: a source
# fails soft, returns empty, the next step treats empty as a legitimate value,
# and nobody learns until output visibly drops. The format asserted below is a
# CONTRACT — the daily report parses it out of the journal.
# ---------------------------------------------------------------------------

_REAL_ENRICH_ALL = pipeline.enrich_all


def _static_enricher(payload: dict):
    """An enrichment module's `enrich` that always returns `payload`."""
    return lambda _domain, _config: dict(payload)


def _wire_pipeline_with_real_enrichment(monkeypatch, today, enrichers):
    """Same five-candidate wiring, but the REAL enrich_all runs over fake
    enrichers, so the coverage line is produced by production code."""
    _wire_minimal_pipeline_for_classifier(monkeypatch, today)
    monkeypatch.setattr(pipeline, "enrich_all", _REAL_ENRICH_ALL)
    monkeypatch.setattr(pipeline, "_load_enrichers", lambda: list(enrichers))


def _coverage_lines(caplog) -> list[str]:
    return [
        rec.getMessage() for rec in caplog.records
        if rec.getMessage().startswith("Enrichment coverage (")
    ]


def _tokens(line: str) -> dict[str, str]:
    _prefix, _sep, rest = line.partition(": ")
    return dict(tok.split("=", 1) for tok in rest.split(" "))


def test_coverage_line_reports_mixed_counts_exactly(caplog):
    """The whole contract on one mixed set: prefix count, one token per field,
    identical denominators, exact text."""
    import logging
    candidates = [
        {
            "name": "marketglow.com",
            "wayback_snapshots": 12,
            "wayback_last_snapshot": "2024-03-01",
            "open_page_rank": 2.5,
            "cert_history": True,
            "cc_source_domain_count": 40,
        },
        {
            "name": "tideblock.io",
            "wayback_snapshots": 3,
            "wayback_last_snapshot": "2023-11-02",
            "open_page_rank": None,
            "cert_history": False,
            "cc_source_domain_count": None,
        },
        # No cert_history key at all — absent and explicit-None both read as
        # missing, while c2's False above still counts as present.
        {
            "name": "coppernest.org",
            "wayback_snapshots": 0,
            "wayback_last_snapshot": None,
        },
    ]
    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        pipeline._log_enrichment_coverage(candidates, set(), {})

    lines = _coverage_lines(caplog)
    assert len(lines) == 1
    assert lines[0] == (
        "Enrichment coverage (3 candidates): "
        "wayback_snapshots=3/3 wayback_last_snapshot=2/3 open_page_rank=1/3 "
        "cert_history=2/3 previous_registrar=0/3 cc_source_domain_count=1/3"
    )
    # Denominator identical in every token and equal to the prefix count.
    assert {v.split("/", 1)[1] for v in _tokens(lines[0]).values()} == {"3"}


def test_coverage_line_keeps_a_field_absent_everywhere_as_zero_of_n(caplog):
    """`0/N` is the entire point of this change. A field no candidate carries
    must still appear — this is literally the 2026-09-21/22 OpenPageRank
    outage, where nothing anywhere said open_page_rank was missing."""
    import logging
    candidates = [
        {"name": "marketglow.com", "wayback_snapshots": 9, "open_page_rank": None},
        {"name": "tideblock.io", "wayback_snapshots": 4},
    ]
    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        pipeline._log_enrichment_coverage(candidates, set(), {})

    line = _coverage_lines(caplog)[0]
    assert "open_page_rank=0/2" in line
    assert "cert_history=0/2" in line
    assert "previous_registrar=0/2" in line


def test_coverage_line_emitted_for_empty_candidate_set(caplog):
    """Absence of the line must always mean "the stage did not run", never
    "there was nothing to say"."""
    import logging
    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        pipeline._log_enrichment_coverage([], set(), {})

    line = _coverage_lines(caplog)[0]
    assert line.startswith("Enrichment coverage (0 candidates): ")
    # Every baseline field present at 0/0 — a stable line, not an empty one.
    assert set(_tokens(line)) == set(pipeline.ENRICHMENT_COVERAGE_FIELDS)
    assert set(_tokens(line).values()) == {"0/0"}


def test_coverage_line_appends_fields_discovered_from_enrichers(caplog):
    """The field list is derived from what the modules actually returned, so a
    newly wired source appears without editing a list. Discovered extras sort
    after the declared baseline, which keeps the order stable run to run."""
    import logging
    candidates = [
        {"name": "marketglow.com", "spam_flagged": False, "surbl_listed": None},
        {"name": "tideblock.io", "spam_flagged": False, "surbl_listed": False},
    ]
    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        pipeline._log_enrichment_coverage(
            candidates, {"spam_flagged", "surbl_listed", "wayback_snapshots"}, {},
        )

    line = _coverage_lines(caplog)[0]
    # Baseline first in declared order; discovered extras appended, sorted.
    assert line.endswith("spam_flagged=2/2 surbl_listed=1/2")
    # A discovered field that is also in the baseline is not duplicated.
    assert line.count("wayback_snapshots=") == 1


def test_coverage_line_suppressed_when_disabled(caplog):
    """enrichment_coverage.enabled: false silences it; a missing or malformed
    block does not, because the default is on."""
    import logging
    candidates = [{"name": "marketglow.com", "wayback_snapshots": 1}]
    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        pipeline._log_enrichment_coverage(
            candidates, set(), {"enrichment_coverage": {"enabled": False}},
        )
    assert _coverage_lines(caplog) == []

    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        pipeline._log_enrichment_coverage(candidates, set(), {"other": 1})
        pipeline._log_enrichment_coverage(
            candidates, set(), {"enrichment_coverage": {}},
        )
        pipeline._log_enrichment_coverage(
            candidates, set(), {"enrichment_coverage": "not a dict"},
        )
    assert len(_coverage_lines(caplog)) == 3


def test_coverage_line_never_raises():
    """Not even on nonsense input — it is a log line, not a stage."""
    class Exploding(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")

    pipeline._log_enrichment_coverage([Exploding()], {"open_page_rank"}, {})
    pipeline._log_enrichment_coverage(None, None, {})
    pipeline._log_enrichment_coverage([None, "nope", 7], set(), {})
    pipeline._log_enrichment_coverage([], set(), Exploding())
    pipeline._log_enrichment_coverage([], set(), None)


def test_enrich_all_emits_coverage_with_fields_from_the_enrichers(cfg, caplog):
    """End-to-end through the real enrich_all: counts come from the merged
    records and the field list from the modules' own return keys. One enricher
    returning {} for every domain is the 2026-09-21 outage shape."""
    import logging
    cands = [
        {"name": "marketglow.com"},
        {"name": "tideblock.io"},
        {"name": "coppernest.org"},
    ]
    enrichers = [
        ("wayback", _static_enricher(
            {"wayback_snapshots": 7, "wayback_last_snapshot": "2024-02-02"},
        )),
        ("open_page_rank", _static_enricher({})),
        ("surbl", _static_enricher({"surbl_listed": None})),
    ]
    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pipeline, "_load_enrichers", lambda: list(enrichers))
            enriched = pipeline.enrich_all(cands, cfg)

    assert len(enriched) == 3
    lines = _coverage_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("Enrichment coverage (3 candidates): ")
    assert "wayback_snapshots=3/3" in lines[0]
    # Declared-but-never-returned and returned-but-always-None both read 0/3.
    assert "open_page_rank=0/3" in lines[0]
    assert "surbl_listed=0/3" in lines[0]
    # Placed adjacent to the pre-existing enrichment summary, right after it.
    summary_idx = [
        i for i, rec in enumerate(caplog.records)
        if rec.getMessage().startswith("Enrichment summary:")
    ]
    coverage_idx = [
        i for i, rec in enumerate(caplog.records)
        if rec.getMessage().startswith("Enrichment coverage (")
    ]
    assert summary_idx and coverage_idx[0] == summary_idx[-1] + 1


def test_enrich_all_emits_coverage_for_zero_candidates(cfg, caplog):
    """No candidates is not a reason to go quiet."""
    import logging
    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        assert pipeline.enrich_all([], cfg) == []
    assert _coverage_lines(caplog)[0].startswith(
        "Enrichment coverage (0 candidates): ",
    )


def _publishable_enrichers() -> list[tuple[str, object]]:
    """Enough enrichment for all five candidates to survive Stage 5 and
    publish — notably spam_flagged, which the strict post-enrichment spam
    check requires."""
    return [
        ("wayback", _static_enricher({
            "wayback_snapshots": 5000, "wayback_last_snapshot": "2024-01-01",
        })),
        ("open_page_rank", _static_enricher({"open_page_rank": 3.0})),
        ("crtsh", _static_enricher({"cert_history": True, "cert_count": 4})),
        ("spam_check", _static_enricher({"spam_flagged": False})),
        ("cc_backlinks", _static_enricher({"cc_source_domain_count": 500})),
    ]


def test_main_emits_coverage_line_and_still_publishes(monkeypatch, cfg, tmp_path, caplog):
    """The line appears once in a full run, with that run's candidate count."""
    import logging
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    enrichers = _publishable_enrichers()
    _wire_pipeline_with_real_enrichment(monkeypatch, date.today(), enrichers)
    _install_classifier(monkeypatch, _recording_classifier([]))

    with caplog.at_level(logging.INFO, logger="scripts.pipeline"):
        assert pipeline.main(["--config", str(cfg_path)]) == 0

    lines = _coverage_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("Enrichment coverage (5 candidates): ")
    assert "open_page_rank=5/5" in lines[0]
    assert "cert_history=5/5" in lines[0]
    assert "previous_registrar=0/5" in lines[0]
    assert "cert_count=5/5" in lines[0]  # discovered at runtime, not declared
    daily = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert len(daily["domains"]) == 5


def test_main_survives_an_exploding_coverage_report(monkeypatch, cfg, tmp_path):
    """The overriding constraint: the 09:00 UTC run must publish. A reporting
    line may not be able to break a three-hour run, however it fails."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    enrichers = _publishable_enrichers()

    def boom(*_a, **_k):
        raise RuntimeError("coverage report exploded")

    for target in ["_log_enrichment_coverage", "_enrichment_coverage_enabled"]:
        with pytest.MonkeyPatch.context() as mp:
            _wire_pipeline_with_real_enrichment(mp, date.today(), enrichers)
            _install_classifier(mp, _recording_classifier([]))
            mp.setattr(pipeline, target, boom)
            assert pipeline.main(["--config", str(cfg_path)]) == 0, target
            daily = json.loads(
                (tmp_path / "daily.json").read_text(encoding="utf-8"),
            )
            assert len(daily["domains"]) == 5, target
