"""Unit tests for scripts/output.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts import output

CONFIG = {
    "max_candidates_per_day": 3,
    "affiliate_link_template": "https://example.com/?domain={name}",
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


def test_build_payload_caps_at_max_candidates_per_day():
    cands = [_cand(f"d{i}.com", 100 - i) for i in range(10)]
    payload = output.build_payload(cands, CONFIG)
    assert payload["domain_count"] == 3
    assert [d["name"] for d in payload["domains"]] == ["d0.com", "d1.com", "d2.com"]


def test_build_payload_applies_affiliate_template():
    payload = output.build_payload([_cand("foo.com")], CONFIG)
    assert payload["domains"][0]["affiliate_link"] == "https://example.com/?domain=foo.com"


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
