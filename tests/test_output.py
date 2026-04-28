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
