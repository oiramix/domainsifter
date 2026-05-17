"""Unit tests for scripts/output.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts import output

REGISTRARS = [
    {
        "name": "Namecheap",
        "link_template": "https://namecheap.pxf.io/WO655J?u=https%3A%2F%2Fwww.namecheap.com%2Fdomains%2Fregistration%2Fresults%2F%3Fdomain%3D{name}",
    },
    {
        "name": "NameSilo",
        "link_template": "https://www.namesilo.com/domain/search-domains?query={name}&rid=36a0644du",
    },
]

CONFIG = {
    "max_candidates_for_publication": 3,
    "registrars": REGISTRARS,
}


def _cand(name: str, score: int = 50, **extra) -> dict:
    base = {
        "name": name,
        "tld": name.rsplit(".", 1)[-1],
        "dropped_date": "2026-04-26",
        "wayback_snapshots": 10,
        "wayback_last_snapshot": "2024-08-15",
        "open_page_rank": 2.0,
        "cert_history": True,
        "previous_registrar": "Acme Registrar",
        "score": score,
    }
    base.update(extra)
    return base


def test_build_payload_returns_contract_shape():
    payload = output.build_payload(
        [_cand("good.com", 90)],
        CONFIG,
        generated_at=datetime(2026, 4, 27, 6, 0, 0, tzinfo=timezone.utc),
    )
    assert payload["generated_at"] == "2026-04-27T06:00:00Z"
    assert payload["domain_count"] == 1
    assert len(payload["domains"]) == 1
    d = payload["domains"][0]
    assert set(d.keys()) == set(output.CONTRACT_FIELDS)


def test_build_payload_caps_at_max_candidates_for_publication():
    cands = [_cand(f"d{i}.com", 100 - i) for i in range(10)]
    payload = output.build_payload(cands, CONFIG)
    assert payload["domain_count"] == 3
    assert [d["name"] for d in payload["domains"]] == ["d0.com", "d1.com", "d2.com"]


def test_build_payload_falls_back_to_legacy_per_day_key():
    """Configs predating the rename keep working — legacy key still parsed."""
    legacy = {"max_candidates_per_day": 2, "registrars": REGISTRARS}
    payload = output.build_payload([_cand(f"d{i}.com", 90 - i) for i in range(5)], legacy)
    assert payload["domain_count"] == 2


def test_build_payload_publishes_all_when_below_cap():
    """Cap is a CEILING, not a quota — fewer candidates → fewer published."""
    cands = [_cand(f"d{i}.com", 100 - i) for i in range(2)]  # 2 candidates, cap=3
    payload = output.build_payload(cands, CONFIG)
    assert payload["domain_count"] == 2


# --- Quality floor (added 2026-04-28 in response to day-3 publishing junk) ---


def _cand_with_completeness(name: str, score: int, populated_fields: int) -> dict:
    """Build a candidate where exactly `populated_fields` of the 5 enrichment
    fields (wayback_snapshots, wayback_last_snapshot, open_page_rank,
    cert_history, previous_registrar) are populated; the rest are None."""
    enrichment = [
        ("wayback_snapshots", 100),
        ("wayback_last_snapshot", "2024-01-01"),
        ("open_page_rank", 4.2),
        ("cert_history", True),
        ("previous_registrar", "GoDaddy"),
    ]
    cand = {
        "name": name, "tld": name.rsplit(".", 1)[-1],
        "dropped_date": "2026-04-26", "score": score,
    }
    for i, (k, v) in enumerate(enrichment):
        cand[k] = v if i < populated_fields else None
    return cand


def test_quality_floor_drops_low_score():
    cfg = {**CONFIG, "publish_min_score": 30}
    cands = [
        _cand_with_completeness("strong.com", score=80, populated_fields=5),
        _cand_with_completeness("weak.com", score=12, populated_fields=5),
    ]
    payload = output.build_payload(cands, cfg)
    assert [d["name"] for d in payload["domains"]] == ["strong.com"]


def test_quality_floor_drops_low_completeness():
    cfg = {**CONFIG, "publish_min_enrichment_completeness": 0.50}
    cands = [
        _cand_with_completeness("rich.com", score=50, populated_fields=4),  # 0.80
        _cand_with_completeness("sparse.com", score=50, populated_fields=2), # 0.40
    ]
    payload = output.build_payload(cands, cfg)
    assert [d["name"] for d in payload["domains"]] == ["rich.com"]


def test_quality_floor_zero_thresholds_keeps_everything():
    """Both thresholds default to 0 — no floor when omitted from config."""
    cands = [_cand_with_completeness("any.com", score=0, populated_fields=0)]
    payload = output.build_payload(cands, CONFIG)
    assert payload["domain_count"] == 1


def test_quality_floor_both_must_pass():
    """Either threshold failing rejects the candidate. Both pass → keep."""
    cfg = {**CONFIG, "publish_min_score": 30, "publish_min_enrichment_completeness": 0.50}
    cands = [
        _cand_with_completeness("ok.com", score=50, populated_fields=4),     # both ok
        _cand_with_completeness("low_score.com", score=10, populated_fields=5),     # score fails
        _cand_with_completeness("low_cmpl.com", score=80, populated_fields=1),      # completeness fails
        _cand_with_completeness("both_fail.com", score=10, populated_fields=1),     # both fail
    ]
    payload = output.build_payload(cands, cfg)
    assert [d["name"] for d in payload["domains"]] == ["ok.com"]


def test_quality_floor_runs_before_publication_cap():
    """Quality floor + cap compose: floor first, then cap. If 5 pass the
    floor and cap=3, you publish 3. If 2 pass the floor and cap=3, you
    publish 2 (cap is a ceiling, never pads)."""
    cfg = {**CONFIG, "max_candidates_for_publication": 3, "publish_min_score": 30}
    cands = [
        _cand_with_completeness(f"d{i}.com", score=80 - i, populated_fields=5)
        for i in range(5)
    ] + [
        _cand_with_completeness("junk.com", score=10, populated_fields=5),
    ]
    payload = output.build_payload(cands, cfg)
    assert payload["domain_count"] == 3
    assert all(d["score"] >= 30 for d in payload["domains"])


# --- total_candidates_evaluated ---------------------------------------------


def test_total_evaluated_in_payload_when_passed():
    payload = output.build_payload([_cand("a.com", 80)], CONFIG, total_evaluated=1500)
    assert payload["total_candidates_evaluated"] == 1500


def test_total_evaluated_omitted_when_not_passed():
    """Backward compatibility — sample-domains.json and old fixtures don't
    pass this kwarg; the field is omitted entirely from the payload."""
    payload = output.build_payload([_cand("a.com", 80)], CONFIG)
    assert "total_candidates_evaluated" not in payload


# --- persistent-list bucket counts -------------------------------------------


def test_payload_includes_today_and_carryover_counts():
    """today_count = entries with days_listed == 0; carryover_count = the rest.
    Both are top-level fields the frontend reads to drive the two-card layout."""
    cfg = {**CONFIG, "max_candidates_for_publication": 10}  # don't truncate
    cands = [
        _cand("today1.com", 80, days_listed=0),
        _cand("today2.com", 70, days_listed=0),
        _cand("yesterday.com", 60, days_listed=1),
        _cand("week.com", 50, days_listed=7),
    ]
    payload = output.build_payload(cands, cfg)
    assert payload["today_count"] == 2
    assert payload["carryover_count"] == 2
    assert payload["domain_count"] == 4


def test_payload_today_count_zero_when_only_carryover():
    cands = [
        _cand("a.com", 80, days_listed=3),
        _cand("b.com", 70, days_listed=10),
    ]
    payload = output.build_payload(cands, CONFIG)
    assert payload["today_count"] == 0
    assert payload["carryover_count"] == 2


def test_payload_carryover_count_zero_when_only_today():
    """First-ever run or zero-carryover state — all entries days_listed=0."""
    cands = [_cand("fresh.com", 80, days_listed=0)]
    payload = output.build_payload(cands, CONFIG)
    assert payload["today_count"] == 1
    assert payload["carryover_count"] == 0


def test_payload_treats_missing_days_listed_as_today():
    """Sample data and migration-state entries may omit days_listed; treat
    those as today (they'll show up in Card 1)."""
    cands = [_cand("legacy.com", 80)]  # no days_listed field
    payload = output.build_payload(cands, CONFIG)
    assert payload["today_count"] == 1
    assert payload["carryover_count"] == 0
    assert payload["domains"][0]["days_listed"] == 0


def test_project_preserves_persistence_fields():
    """first_seen_date, last_validated_date, days_listed must survive
    projection so they're available to the frontend."""
    cand = _cand("d.com", 75,
                 first_seen_date="2026-04-25",
                 last_validated_date="2026-04-30",
                 days_listed=5)
    payload = output.build_payload([cand], CONFIG)
    d = payload["domains"][0]
    assert d["first_seen_date"] == "2026-04-25"
    assert d["last_validated_date"] == "2026-04-30"
    assert d["days_listed"] == 5


def test_build_payload_drops_internal_fields():
    cand = _cand("foo.com")
    cand["spam_flagged"] = False
    cand["surbl_listed"] = False
    cand["_internal_debug"] = "secret"
    payload = output.build_payload([cand], CONFIG)
    keys = set(payload["domains"][0].keys())
    assert "spam_flagged" not in keys
    assert "surbl_listed" not in keys
    assert "_internal_debug" not in keys


def test_build_payload_handles_empty_list():
    payload = output.build_payload([], CONFIG)
    assert payload["domain_count"] == 0
    assert payload["domains"] == []


def test_build_payload_falls_back_to_apex_tld():
    cand = _cand("foo.com")
    del cand["tld"]
    payload = output.build_payload([cand], CONFIG)
    assert payload["domains"][0]["tld"] == "com"


# --- registrars[] schema -----------------------------------------------------


def test_registrars_array_preserves_config_order():
    """Popover render order = config order. Reverse the config to verify
    we're not silently sorting alphabetically or by some implicit rule."""
    reversed_cfg = {**CONFIG, "registrars": list(reversed(REGISTRARS))}
    payload = output.build_payload([_cand("foo.com")], reversed_cfg)
    names = [r["name"] for r in payload["domains"][0]["registrars"]]
    assert names == ["NameSilo", "Namecheap"]


def test_registrars_substitutes_name_in_both_templates():
    """Each template gets {name} replaced with the apex domain literally
    (no URL-encoding; the percent-encoded scaffolding around {name} is
    already correct in the templates)."""
    payload = output.build_payload([_cand("amberkite.org")], CONFIG)
    regs = payload["domains"][0]["registrars"]
    by_name = {r["name"]: r["url"] for r in regs}
    assert by_name["Namecheap"] == (
        "https://namecheap.pxf.io/WO655J?u=https%3A%2F%2Fwww.namecheap.com"
        "%2Fdomains%2Fregistration%2Fresults%2F%3Fdomain%3Damberkite.org"
    )
    assert by_name["NameSilo"] == (
        "https://www.namesilo.com/domain/search-domains?query=amberkite.org&rid=36a0644du"
    )


def test_registrars_skips_malformed_config_entries():
    """Bad entries (missing name or link_template) are silently dropped
    rather than crashing the whole payload build. Defence-in-depth."""
    cfg = {
        "max_candidates_for_publication": 10,
        "registrars": [
            {"name": "Namecheap", "link_template": "https://nc/{name}"},
            {"name": "MissingTemplate"},                # missing link_template
            {"link_template": "https://x/{name}"},      # missing name
            "not a dict",                                # not even a dict
            {"name": "NameSilo", "link_template": "https://ns/{name}"},
        ],
    }
    payload = output.build_payload([_cand("foo.com")], cfg)
    names = [r["name"] for r in payload["domains"][0]["registrars"]]
    assert names == ["Namecheap", "NameSilo"]


def test_registrars_empty_when_config_omits_them():
    """If config has no registrars list, every domain gets registrars=[].
    The frontend can then render a graceful empty state."""
    cfg = {"max_candidates_for_publication": 10}
    payload = output.build_payload([_cand("foo.com")], cfg)
    assert payload["domains"][0]["registrars"] == []


def test_registrars_does_not_url_encode_dots_in_name():
    """Plain str.replace — must not treat `{name}` as a format string and
    must not URL-encode the dot in the substituted apex."""
    payload = output.build_payload([_cand("foo.bar.org")], CONFIG)
    ns_url = next(r["url"] for r in payload["domains"][0]["registrars"] if r["name"] == "NameSilo")
    assert "query=foo.bar.org" in ns_url


# --- write_output (filesystem) -----------------------------------------------


def test_write_output_writes_valid_json_to_disk(tmp_path):
    target = tmp_path / "out" / "daily.json"
    written = output.write_output(
        [_cand("a.com", 80), _cand("b.com", 70)],
        CONFIG,
        output_path=target,
        generated_at=datetime(2026, 4, 27, 6, 0, 0, tzinfo=timezone.utc),
    )
    assert written == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["generated_at"] == "2026-04-27T06:00:00Z"
    assert payload["domain_count"] == 2
    assert payload["domains"][0]["name"] == "a.com"
    assert len(payload["domains"][0]["registrars"]) == 2


def test_write_output_creates_parent_directory(tmp_path):
    target = tmp_path / "deeply" / "nested" / "daily.json"
    output.write_output([_cand("a.com")], CONFIG, output_path=target)
    assert target.exists()


def test_write_output_overwrites_existing_file(tmp_path):
    target = tmp_path / "daily.json"
    target.write_text('{"old": true}', encoding="utf-8")
    output.write_output([_cand("a.com")], CONFIG, output_path=target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "old" not in payload


def test_write_output_atomic_no_tmp_left_behind(tmp_path):
    target = tmp_path / "daily.json"
    output.write_output([_cand("a.com")], CONFIG, output_path=target)
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith("daily.json.") and p.suffix == ".tmp"]
    assert leftover == []


def test_write_output_atomic_no_partial_file_on_error(tmp_path, monkeypatch):
    target = tmp_path / "daily.json"

    real_dump = output.json.dump

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(output.json, "dump", boom)
    with pytest.raises(RuntimeError):
        output.write_output([_cand("a.com")], CONFIG, output_path=target)
    assert not target.exists()
    leftover = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftover == []
    monkeypatch.setattr(output.json, "dump", real_dump)


def test_write_output_uses_config_path_when_omitted(tmp_path):
    cfg = {**CONFIG, "output_path": str(tmp_path / "from_config.json")}
    written = output.write_output([_cand("a.com")], cfg)
    assert written == tmp_path / "from_config.json"
    assert written.exists()


# --- cc_source_domain_count (added 2026-05-14, wire-in commit) ---------------


def test_cc_source_domain_count_passes_through_to_payload():
    """The new contract field is projected from the candidate into the
    emitted JSON. Integer counts flow through as-is."""
    cand = _cand("amber.org", 80, cc_source_domain_count=247)
    payload = output.build_payload([cand], CONFIG)
    assert payload["domains"][0]["cc_source_domain_count"] == 247


def test_cc_source_domain_count_null_passes_through_to_payload():
    """null means 'not in CC graph for the latest release'. The contract
    preserves null distinctly from missing — the frontend renders a dash
    for null, not a zero."""
    cand = _cand("nograph.com", 60, cc_source_domain_count=None)
    payload = output.build_payload([cand], CONFIG)
    assert payload["domains"][0]["cc_source_domain_count"] is None


def test_cc_source_domain_count_defaults_to_null_when_missing_on_candidate():
    """Older candidate dicts produced before the wire-in (or by tests
    constructed via the legacy _cand() helper) get null in the projected
    payload. Backward-compatible."""
    cand = _cand("legacy.com", 60)
    assert "cc_source_domain_count" not in cand  # confirm the helper omits it
    payload = output.build_payload([cand], CONFIG)
    assert payload["domains"][0]["cc_source_domain_count"] is None


def test_cc_source_domain_count_not_in_completeness_calculation():
    """Hard guarantee: a candidate with null cc must not be rejected by
    the publish_min_enrichment_completeness gate due to missing CC alone.
    Absence from the CC graph is informational, not a quality deficit."""
    cfg = {**CONFIG, "publish_min_enrichment_completeness": 0.99}
    # 5/5 of _ENRICHMENT_FIELDS_FOR_COMPLETENESS populated, cc null.
    cand = _cand("rich.com", 80, cc_source_domain_count=None)
    payload = output.build_payload([cand], cfg)
    assert payload["domain_count"] == 1, (
        "candidate with full traditional enrichment but null CC must still "
        "pass the completeness gate"
    )


def test_cc_source_domain_count_in_contract_fields():
    """Architectural assertion: the field is part of the locked schema."""
    assert "cc_source_domain_count" in output.CONTRACT_FIELDS


# --- verdict (added 2026-05-17) ----------------------------------------------

VERDICT_CFG = {
    **CONFIG,
    "verdict_thresholds": {
        "clean_min_score": 70,
        "promising_min_score": 40,
        "promising_min_wayback_snapshots": 1000,
        "promising_min_open_page_rank": 1.5,
        "promising_min_cc_source_domain_count": 10,
    },
    "soft_signal_keywords": ["dating", "pump"],
}


def _verdict_of(payload: dict) -> str:
    return payload["domains"][0]["verdict"]


def test_verdict_clean_for_high_score():
    """Clean is unchanged — score >= 70 wins regardless of wayback / OPR / CC."""
    cand = _cand("clean.com", 75, wayback_snapshots=0, open_page_rank=0,
                 cc_source_domain_count=None)
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Clean"


def test_verdict_promising_requires_wayback_gate():
    """score >= promising_min but wayback < 1000 → demote to Caution."""
    cand = _cand("light.com", 55, wayback_snapshots=500, open_page_rank=3.0,
                 cc_source_domain_count=100)
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_promising_requires_authority_signal():
    """score >= promising_min, wayback >= 1000, but OPR=0 and CC=0 → Caution."""
    cand = _cand("traces.com", 55, wayback_snapshots=5000, open_page_rank=0,
                 cc_source_domain_count=0)
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_promising_satisfied_via_opr():
    cand = _cand("ok-opr.com", 55, wayback_snapshots=1500, open_page_rank=1.5,
                 cc_source_domain_count=0)
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Promising"


def test_verdict_promising_satisfied_via_cc_backlinks():
    cand = _cand("ok-cc.com", 55, wayback_snapshots=1500, open_page_rank=0,
                 cc_source_domain_count=10)
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Promising"


def test_verdict_below_promising_min_is_caution():
    cand = _cand("low.com", 35, wayback_snapshots=99999, open_page_rank=9.0,
                 cc_source_domain_count=99999)
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_soft_signal_forces_caution_even_at_clean_score():
    """A high-scoring domain whose name carries a soft-signal token still
    warns the reader. The forced-Caution check short-circuits before
    Clean."""
    cand = _cand(
        "datingmegahub.com", 90,
        wayback_snapshots=5000, open_page_rank=8.0, cc_source_domain_count=2000,
    )
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_soft_signal_forces_caution_on_passing_candidate():
    cand = _cand(
        "pump99.io", 55,
        wayback_snapshots=2000, open_page_rank=3.0, cc_source_domain_count=50,
    )
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_tolerates_missing_wayback_field():
    """Coerces None to 0 for the Promising gate — conservative: a missing
    enrichment field shouldn't earn the higher tier."""
    cand = _cand("nowb.com", 55, wayback_snapshots=None, open_page_rank=3.0,
                 cc_source_domain_count=100)
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_field_in_contract():
    assert "verdict" in output.CONTRACT_FIELDS
