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


# --- total_drops_scanned (added 2026-09-19) ----------------------------------


def test_total_drops_scanned_in_payload_when_passed():
    payload = output.build_payload(
        [_cand("marketglow.com", 80)], CONFIG, total_drops_scanned=222155,
    )
    assert payload["total_drops_scanned"] == 222155


def test_total_drops_scanned_is_independent_of_total_evaluated():
    """The two counters measure different ends of the funnel and must not be
    derived from, or confused with, each other."""
    payload = output.build_payload(
        [_cand("marketglow.com", 80)],
        CONFIG,
        total_evaluated=2741,
        total_drops_scanned=222155,
    )
    assert payload["total_drops_scanned"] == 222155
    assert payload["total_candidates_evaluated"] == 2741


@pytest.mark.parametrize("kwargs", [{}, {"total_drops_scanned": None}])
def test_total_drops_scanned_absent_not_zero_when_unknown(kwargs):
    """Hard rule 2: "0 drops scanned" is a lie. An unknown count is an
    ABSENT key, never a zero default."""
    payload = output.build_payload([_cand("marketglow.com", 80)], CONFIG, **kwargs)
    assert "total_drops_scanned" not in payload


def test_total_drops_scanned_zero_is_published_when_explicitly_passed():
    """An explicit 0 is data (a day with no new drops), not a default."""
    payload = output.build_payload(
        [_cand("marketglow.com", 80)], CONFIG, total_drops_scanned=0,
    )
    assert payload["total_drops_scanned"] == 0


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"total_drops_scanned": 222155}, 222155),
        ({"total_drops_scanned": None}, None),
        ({}, None),
    ],
)
def test_total_drops_scanned_round_trips_through_write_output(
    tmp_path, kwargs, expected,
):
    """End-to-end through the atomic file write: present, explicit-None and
    omitted all behave the same on disk as they do in build_payload."""
    target = tmp_path / "daily.json"
    output.write_output([_cand("tideblock.io", 80)], CONFIG, output_path=target, **kwargs)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if expected is None:
        assert "total_drops_scanned" not in payload
    else:
        assert payload["total_drops_scanned"] == expected


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


# --- wayback_unknown (added 2026-05-17) --------------------------------------


def test_wayback_unknown_field_in_contract():
    """Persisted to JSON so tomorrow's carryover.age_out_wayback_unknown
    can see and increment the counter."""
    assert "wayback_unknown" in output.CONTRACT_FIELDS
    assert "wayback_unknown_attempts" in output.CONTRACT_FIELDS


def test_wayback_unknown_exempts_completeness_penalty():
    """When wayback enrichment couldn't reach the server (breaker open / per-
    call failure), the candidate carries wayback_unknown=True. The two
    wayback fields are null. Without an exemption the candidate would lose
    2/5 completeness points and very likely fail the publish_min_enrichment_
    completeness=0.50 gate. Silent drop. The exemption fixes that."""
    cfg = {
        **CONFIG,
        "publish_min_score": 0,
        "publish_min_enrichment_completeness": 0.80,
    }
    cand = _cand(
        "wb-down.com", 60,
        wayback_snapshots=None, wayback_last_snapshot=None,
        open_page_rank=3.0, cert_history=True, previous_registrar="X",
    )
    cand["wayback_unknown"] = True
    # Without exemption: completeness = 3/5 = 0.60 < 0.80 → dropped.
    # With exemption: wayback fields count as populated → 5/5 = 1.0 → passes.
    payload = output.build_payload([cand], cfg)
    assert payload["domain_count"] == 1
    assert payload["domains"][0]["wayback_unknown"] is True


def test_wayback_unknown_zero_attempts_serializes_as_null():
    """First-day wayback_unknown candidates have attempts=0 (carryover will
    tick to 1 tomorrow). Serialize as null, not 0, so the JSON file stays
    minimal for the >99% of entries that never hit this state."""
    cand = _cand("first.com", 60, wayback_snapshots=None)
    cand["wayback_unknown"] = True
    payload = output.build_payload([cand], CONFIG)
    d = payload["domains"][0]
    assert d["wayback_unknown"] is True
    assert d["wayback_unknown_attempts"] is None


def test_wayback_unknown_existing_attempts_pass_through():
    """A carryover entry with attempts=2 must round-trip the integer."""
    cand = _cand("dayN.com", 60, wayback_snapshots=None)
    cand["wayback_unknown"] = True
    cand["wayback_unknown_attempts"] = 2
    payload = output.build_payload([cand], CONFIG)
    d = payload["domains"][0]
    assert d["wayback_unknown_attempts"] == 2


def test_wayback_unknown_false_serializes_as_null():
    """Most candidates never see wayback_unknown. They should NOT carry
    `wayback_unknown: false` in the JSON — keeps the payload compact."""
    cand = _cand("normal.com", 80, wayback_snapshots=42)
    payload = output.build_payload([cand], CONFIG)
    assert payload["domains"][0]["wayback_unknown"] is None


# --- snapshot_category verdict integration (Phase 4, 2026-05-20) -----------


def test_verdict_parked_forces_caution_even_at_clean_score():
    """Parked snapshot category overrides a high score. The domain may have
    great Wayback / OPR / CC numbers, but if today's snapshot is a parking
    page, we don't want a buyer mistaking it for an active site."""
    cand = _cand(
        "parked.com", 90,
        wayback_snapshots=5000, open_page_rank=8.0, cc_source_domain_count=2000,
    )
    cand["snapshot_category"] = "parked"
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_empty_forces_caution():
    """Same as parked — empty/blank snapshots downgrade to Caution."""
    cand = _cand(
        "empty.com", 85,
        wayback_snapshots=3000, open_page_rank=2.5, cc_source_domain_count=500,
    )
    cand["snapshot_category"] = "empty"
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_legitimate_passes_to_normal_scoring():
    """`legitimate` category does NOT short-circuit verdict — normal score
    rules apply. A 90-scoring legitimate-snapshot entry is still Clean."""
    cand = _cand(
        "real.com", 90,
        wayback_snapshots=5000, open_page_rank=4.0, cc_source_domain_count=1000,
    )
    cand["snapshot_category"] = "legitimate"
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Clean"


def test_verdict_unknown_passes_to_normal_scoring():
    """`unknown` category does NOT short-circuit — it means we don't know,
    not we know it's bad. Treat as if the classifier didn't run."""
    cand = _cand(
        "huh.com", 75,
        wayback_snapshots=2000, open_page_rank=3.0, cc_source_domain_count=200,
    )
    cand["snapshot_category"] = "unknown"
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Clean"


def test_verdict_soft_signal_short_circuits_before_snapshot_check():
    """When both a soft_signal_keyword AND a parked snapshot are present,
    the soft_signal check fires first (it's named in the rule order). Both
    paths lead to Caution, so the outcome is the same — this test pins
    the rule precedence so a future reorder doesn't silently change it."""
    cand = _cand(
        "datingsite.com", 85,
        wayback_snapshots=5000, open_page_rank=8.0,
    )
    cand["snapshot_category"] = "parked"
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Caution"


def test_verdict_no_snapshot_category_falls_through_to_score():
    """Pre-Phase-4 entries with no snapshot_category field at all must
    behave identically to today's pre-Phase-4 logic — Clean if score>=70."""
    cand = _cand(
        "legacy.com", 75,
        wayback_snapshots=2000, open_page_rank=3.0, cc_source_domain_count=200,
    )
    # explicitly no snapshot_category
    payload = output.build_payload([cand], VERDICT_CFG)
    assert _verdict_of(payload) == "Clean"


# --- snapshot_category contract surface (Phase 4, 2026-05-20) --------------


def test_contract_fields_includes_snapshot_category():
    assert "snapshot_category" in output.CONTRACT_FIELDS


def test_contract_fields_includes_snapshot_classifier_version():
    assert "snapshot_classifier_version" in output.CONTRACT_FIELDS


def test_contract_fields_excludes_wayback_excerpt():
    """Sidecar architecture (design (h)): wayback_excerpt lives in
    src/data/wayback_excerpts.json, NOT inline in daily-domains.json.
    Pin this so a future refactor doesn't accidentally re-add it."""
    assert "wayback_excerpt" not in output.CONTRACT_FIELDS


def test_project_emits_snapshot_fields_when_present():
    cand = _cand("x.com", 80, wayback_snapshots=2000)
    cand["snapshot_category"] = "legitimate"
    cand["snapshot_classifier_version"] = "v1"
    payload = output.build_payload([cand], VERDICT_CFG)
    d = payload["domains"][0]
    assert d["snapshot_category"] == "legitimate"
    assert d["snapshot_classifier_version"] == "v1"


def test_project_emits_none_for_missing_snapshot_fields():
    """Legacy / sample entries without classifier-touched fields project
    as None — JSON shape stays uniform across migration."""
    cand = _cand("legacy.com", 80, wayback_snapshots=2000)
    payload = output.build_payload([cand], VERDICT_CFG)
    d = payload["domains"][0]
    assert d["snapshot_category"] is None
    assert d["snapshot_classifier_version"] is None


def test_project_does_not_leak_inline_wayback_excerpt():
    """Even if a candidate dict has wayback_excerpt set (e.g., the pipeline
    forgot to strip it before write), _project must not emit it — sidecar
    is the only canonical location."""
    cand = _cand("x.com", 80, wayback_snapshots=2000)
    cand["wayback_excerpt"] = {"title": "Should not appear in JSON"}
    cand["snapshot_category"] = "legitimate"
    payload = output.build_payload([cand], VERDICT_CFG)
    assert "wayback_excerpt" not in payload["domains"][0]


# --- phase2_reason (added 2026-09-19) ----------------------------------------


def payload_reason(payload: dict) -> str | None:
    return payload["domains"][0]["phase2_reason"]


def test_phase2_reason_in_contract_fields():
    """Architectural assertion: the ranker's justification is part of the
    published contract now, not private R2-only data."""
    assert "phase2_reason" in output.CONTRACT_FIELDS


def test_phase2_reason_projected_when_present():
    cand = _cand("marketglow.com", 80,
                 phase2_reason="explicit service intent, clear and trustworthy")
    payload = output.build_payload([cand], CONFIG)
    assert payload["domains"][0]["phase2_reason"] == (
        "explicit service intent, clear and trustworthy"
    )


def test_phase2_reason_null_when_absent():
    """Fallback days (mechanical selection) and pre-migration carryover have
    no reason at all. Publish null — never "" and never a missing key, so
    the frontend's single null check covers every case."""
    cand = _cand("tideblock.io", 70)
    assert "phase2_reason" not in cand
    assert payload_reason(output.build_payload([cand], CONFIG)) is None


def test_phase2_reason_placeholder_is_published_as_null():
    """phase2_ranker writes "missing from response" when a candidate was sent
    to the model but came back absent. That's a pipeline marker, not a
    justification — it must not surface on the site."""
    cand = _cand("coppernest.org", 65, phase2_reason="missing from response")
    payload = output.build_payload([cand], CONFIG)
    assert payload_reason(payload) is None


def test_phase2_reason_blank_and_non_string_are_published_as_null():
    for junk in ("", "   ", None, 42, {"reason": "nope"}):
        cand = _cand("blankreason.com", 65, phase2_reason=junk)
        payload = output.build_payload([cand], CONFIG)
        assert payload_reason(payload) is None, f"junk value {junk!r} leaked"


def test_phase2_reason_is_whitespace_stripped():
    cand = _cand("trimmed.com", 65, phase2_reason="  aged brand, clean history \n")
    payload = output.build_payload([cand], CONFIG)
    assert payload_reason(payload) == "aged brand, clean history"


def test_phase2_reason_absence_does_not_disturb_other_projection():
    """Backward compatibility: a candidate with no phase2_reason projects
    exactly the same contract keys and values as before the migration, plus
    the new null field."""
    cand = _cand("amberkite.org", 75, days_listed=4,
                 cc_source_domain_count=12, first_seen_date="2026-09-15")
    d = output.build_payload([cand], CONFIG)["domains"][0]
    assert set(d.keys()) == set(output.CONTRACT_FIELDS)
    assert d["name"] == "amberkite.org"
    assert d["score"] == 75
    assert d["days_listed"] == 4
    assert d["cc_source_domain_count"] == 12
    assert d["first_seen_date"] == "2026-09-15"
    assert len(d["registrars"]) == 2
    assert d["phase2_reason"] is None


def test_phase2_reason_does_not_affect_quality_floor_or_verdict():
    """The reason is presentation data only — it must not change which
    candidates publish, nor their verdict."""
    cfg = {**VERDICT_CFG, "publish_min_score": 30,
           "publish_min_enrichment_completeness": 0.50}
    without = _cand("samecase.com", 75, wayback_snapshots=2000,
                    open_page_rank=3.0, cc_source_domain_count=200)
    with_reason = {**without, "phase2_reason": "strong niche authority"}
    a = output.build_payload([without], cfg)["domains"][0]
    b = output.build_payload([with_reason], cfg)["domains"][0]
    assert a["verdict"] == b["verdict"] == "Clean"
    assert {k: v for k, v in a.items() if k != "phase2_reason"} == {
        k: v for k, v in b.items() if k != "phase2_reason"
    }


def test_phase2_reason_round_trips_through_write_output(tmp_path):
    """Carryover depends on this: tomorrow's run reads yesterday's published
    JSON back in, so the reason must survive the file write."""
    target = tmp_path / "daily.json"
    output.write_output(
        [_cand("marketglow.com", 80, phase2_reason="clear commercial intent")],
        CONFIG,
        output_path=target,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["domains"][0]["phase2_reason"] == "clear commercial intent"


# --- cc_backlink_history (added 2026-09-20, DISPLAY ONLY) --------------------
#
# The enricher returns, newest release first:
#   [{"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 247}, ...]
# with source_domain_count=None meaning "apex absent from that release's
# graph" — distinct from 0 ("in the graph, no inbound source domains").
# Everything below also pins the load-bearing safety property: this field
# cannot influence which domains publish, their score, or their verdict.

HISTORY = [
    {"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 247},
    {"release": "cc-main-2026-may-jun-jul", "source_domain_count": 310},
    {"release": "cc-main-2026-apr-may-jun", "source_domain_count": None},
]


def _history_of(payload: dict) -> list[dict] | None:
    return payload["domains"][0]["cc_backlink_history"]


def test_cc_backlink_history_in_contract_fields():
    """Architectural assertion: the field is part of the locked schema."""
    assert "cc_backlink_history" in output.CONTRACT_FIELDS


def test_cc_backlink_history_passes_through_newest_first():
    """Order is meaningful (newest release first) and must be preserved
    exactly as the enricher produced it — the site renders a trend."""
    cand = _cand("marketglow.com", 80, cc_backlink_history=HISTORY)
    assert _history_of(output.build_payload([cand], CONFIG)) == HISTORY


def test_cc_backlink_history_preserves_none_counts_distinctly_from_zero():
    """The three-state design: None = not in that release's graph,
    0 = in the graph with no inbound edges. Coercing None to 0 would
    fabricate data (hard rule 2)."""
    history = [
        {"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 0},
        {"release": "cc-main-2026-may-jun-jul", "source_domain_count": None},
    ]
    got = _history_of(
        output.build_payload(
            [_cand("tideblock.io", 70, cc_backlink_history=history)], CONFIG
        )
    )
    assert got[0]["source_domain_count"] == 0
    assert got[1]["source_domain_count"] is None


def test_cc_backlink_history_null_when_key_absent_on_candidate():
    """Feature disabled, enricher failed, or pre-2026-09-20 carryover: the
    key is simply not on the candidate. Publish null, never a crash and
    never a fabricated history."""
    cand = _cand("coppernest.org", 65)
    assert "cc_backlink_history" not in cand
    payload = output.build_payload([cand], CONFIG)
    assert _history_of(payload) is None
    # Shape stays uniform — the key is present (as null), like every other
    # optional contract field.
    assert set(payload["domains"][0].keys()) == set(output.CONTRACT_FIELDS)


def test_cc_backlink_history_null_when_explicitly_none():
    cand = _cand("amberkite.org", 65, cc_backlink_history=None)
    assert _history_of(output.build_payload([cand], CONFIG)) is None


def test_cc_backlink_history_empty_list_normalises_to_null():
    """An empty history carries no more information than no history at all,
    and an empty array would make the frontend draw an empty chart."""
    cand = _cand("emptyhist.com", 65, cc_backlink_history=[])
    assert _history_of(output.build_payload([cand], CONFIG)) is None


MALFORMED_HISTORIES = [
    "cc-main-2026-jun-jul-aug",                        # string, not a list
    ["cc-main-2026-jun-jul-aug", "cc-main-2026-may"],  # list of strings
    [{"source_domain_count": 247}],                    # entry missing 'release'
    [{"release": "", "source_domain_count": 247}],      # blank release
    [{"release": None, "source_domain_count": 247}],    # non-string release
    [{"release": "cc-main-2026-jun-jul-aug", "source_domain_count": "247"}],
    [{"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 1.5}],
    [{"release": "cc-main-2026-jun-jul-aug", "source_domain_count": True}],
    [{"release": "cc-main-2026-jun-jul-aug", "source_domain_count": -1}],
    [{"release": "cc-main-2026-jun-jul-aug",
      "source_domain_count": {"nested": {"deeply": [1, 2, {"x": "y"}]}}}],
    {"cc-main-2026-jun-jul-aug": 247},                 # dict, not a list
    42,
    # One good entry plus one bad one — all-or-nothing, so the whole field
    # goes. A partial history would read as a genuine gap in the archive.
    [{"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 247},
     {"release": "cc-main-2026-may-jun-jul", "source_domain_count": []}],
]


@pytest.mark.parametrize("junk", MALFORMED_HISTORIES)
def test_cc_backlink_history_malformed_is_dropped_without_raising(junk):
    """The enricher fails soft, so junk can arrive. _project must never
    raise for a cosmetic field — that would take down the whole run."""
    cand = _cand("junkhist.com", 70, cc_backlink_history=junk)
    payload = output.build_payload([cand], CONFIG)
    assert _history_of(payload) is None, f"junk {junk!r} leaked into the payload"
    assert payload["domain_count"] == 1, "the row itself must still publish"


@pytest.mark.parametrize("junk", MALFORMED_HISTORIES)
def test_cc_backlink_history_malformed_still_writes_valid_json(junk, tmp_path):
    """Hard rule 17: invalid JSON output IS crash-worthy, so the correct
    behaviour is valid JSON with the field dropped — never broken JSON."""
    target = tmp_path / "daily.json"
    output.write_output(
        [_cand("junkhist.com", 70, cc_backlink_history=junk)],
        CONFIG,
        output_path=target,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))  # raises if invalid
    assert payload["domains"][0]["cc_backlink_history"] is None


def test_cc_backlink_history_malformed_logs_once_per_candidate(caplog):
    """A broken enricher must not flood the Actions log: one warning per
    candidate, not one per entry."""
    bad = [
        {"release": "cc-main-2026-jun-jul-aug", "source_domain_count": "247"},
        {"release": "cc-main-2026-may-jun-jul", "source_domain_count": "310"},
        {"no_release": True},
    ]
    with caplog.at_level("WARNING", logger=output.logger.name):
        output.build_payload([_cand("noisy.com", 70, cc_backlink_history=bad)], CONFIG)
    warnings = [r for r in caplog.records if "cc_backlink_history" in r.getMessage()]
    assert len(warnings) == 1
    assert "noisy.com" in warnings[0].getMessage()


def test_cc_backlink_history_releases_are_whitespace_stripped():
    history = [{"release": "  cc-main-2026-jun-jul-aug \n", "source_domain_count": 5}]
    got = _history_of(
        output.build_payload(
            [_cand("trimhist.com", 70, cc_backlink_history=history)], CONFIG
        )
    )
    assert got == [{"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 5}]


def test_cc_backlink_history_entries_projected_to_exactly_two_keys():
    """Extra keys from a future enricher version are not passed through —
    the site is handed the documented shape and nothing else."""
    history = [{
        "release": "cc-main-2026-jun-jul-aug",
        "source_domain_count": 247,
        "internal_debug_blob": {"sqlite_path": "/tmp/whatever.db"},
    }]
    got = _history_of(
        output.build_payload(
            [_cand("extrakeys.com", 70, cc_backlink_history=history)], CONFIG
        )
    )
    assert got == [{"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 247}]


def test_cc_backlink_history_not_in_completeness_fields():
    """Same stance as cc_source_domain_count: absence of CC history is
    informational, not a quality deficit."""
    assert "cc_backlink_history" not in output._ENRICHMENT_FIELDS_FOR_COMPLETENESS


def test_cc_backlink_history_absence_does_not_fail_completeness_gate():
    cfg = {**CONFIG, "publish_min_enrichment_completeness": 0.99}
    cand = _cand("fullenrich.com", 80)  # 5/5 traditional fields, no CC history
    assert output.build_payload([cand], cfg)["domain_count"] == 1


def test_cc_backlink_history_presence_does_not_change_completeness():
    """Completeness is computed over a fixed field tuple, so adding history
    must not move the ratio in either direction."""
    without = _cand("samecase.com", 70, wayback_last_snapshot=None)
    with_hist = {**without, "cc_backlink_history": HISTORY}
    assert output._enrichment_completeness(without) == (
        output._enrichment_completeness(with_hist)
    )


# The load-bearing safety property: display-only means structurally
# incapable of altering which domains publish, or in what order.

@pytest.mark.parametrize("history", [HISTORY, [], None] + MALFORMED_HISTORIES)
def test_cc_backlink_history_leaves_verdict_and_score_byte_identical(history):
    """Regression guard for the whole change: the projected row with a
    history must equal the row without it in EVERY other key — including
    score and verdict — for good, empty and malformed histories alike."""
    cfg = {**VERDICT_CFG, "publish_min_score": 30,
           "publish_min_enrichment_completeness": 0.50}
    without = _cand("samecase.com", 75, wayback_snapshots=2000,
                    open_page_rank=3.0, cc_source_domain_count=200)
    with_hist = {**without, "cc_backlink_history": history}
    a = output.build_payload([without], cfg)["domains"][0]
    b = output.build_payload([with_hist], cfg)["domains"][0]
    assert a["verdict"] == b["verdict"] == "Clean"
    assert a["score"] == b["score"] == 75
    assert {k: v for k, v in a.items() if k != "cc_backlink_history"} == {
        k: v for k, v in b.items() if k != "cc_backlink_history"
    }


def test_cc_backlink_history_does_not_rescue_a_promising_boundary_candidate():
    """A candidate one notch below the Promising gate (OPR and CC both too
    low) must stay Caution no matter how strong its history is. This is the
    test that would fail if history ever leaked into _compute_verdict."""
    base = _cand("boundary.com", 50, wayback_snapshots=2000,
                 open_page_rank=0.1, cc_source_domain_count=1)
    rich_history = [
        {"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 99999},
        {"release": "cc-main-2026-may-jun-jul", "source_domain_count": 99999},
    ]
    assert _verdict_of(output.build_payload([base], VERDICT_CFG)) == "Caution"
    with_hist = {**base, "cc_backlink_history": rich_history}
    assert _verdict_of(output.build_payload([with_hist], VERDICT_CFG)) == "Caution"


def test_cc_backlink_history_does_not_change_publication_order():
    """Sorting is by score only. Give the LOWEST-scoring candidate the
    richest history and confirm the published order is untouched."""
    cfg = {**CONFIG, "max_candidates_for_publication": 3}
    plain = [_cand("highest.com", 90), _cand("middle.com", 70),
             _cand("lowest.com", 50)]
    salted = [
        {**plain[0]},
        {**plain[1]},
        {**plain[2], "cc_backlink_history": [
            {"release": "cc-main-2026-jun-jul-aug", "source_domain_count": 99999}]},
    ]
    names_plain = [d["name"] for d in output.build_payload(plain, cfg)["domains"]]
    names_salted = [d["name"] for d in output.build_payload(salted, cfg)["domains"]]
    assert names_plain == names_salted == ["highest.com", "middle.com", "lowest.com"]


def test_cc_backlink_history_round_trips_through_write_output(tmp_path):
    """Carryover correctness depends on this: tomorrow's run reads this file
    back in via carryover.load_existing, keeps whole rows, and re-projects
    them — so the history survives all 14 days without any carryover change."""
    target = tmp_path / "daily.json"
    output.write_output(
        [_cand("marketglow.com", 80, cc_backlink_history=HISTORY,
               first_seen_date="2026-09-15", days_listed=5)],
        CONFIG,
        output_path=target,
    )
    reloaded = json.loads(target.read_text(encoding="utf-8"))["domains"][0]
    assert reloaded["cc_backlink_history"] == HISTORY
    # Second pass: the reloaded row is what carryover feeds back in.
    again = output.build_payload([reloaded], CONFIG)["domains"][0]
    assert again["cc_backlink_history"] == HISTORY
