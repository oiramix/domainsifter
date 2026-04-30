"""Unit tests for scripts/carryover.py — the 14-day persistent rolling list.

The pipeline calls these in this order each day:

    existing = load_existing(path)
    fresh, dropped_age = filter_by_age(existing, today, max_age=14)
    # ... per-TLD inside collect_drops:
    retained, dropped_reg = validate_against_zone(tld_carryover, today_set, today)
    # ... after scoring:
    annotate_today_drops(today_drops, today)
    annotate_carryover_days_listed(retained, today)
    final = merge(today_drops, retained)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scripts import carryover


def _entry(name: str, **kw) -> dict:
    base = {
        "name": name,
        "tld": name.rsplit(".", 1)[-1],
        "score": 50,
    }
    base.update(kw)
    return base


# --- load_existing -----------------------------------------------------------


def test_load_existing_returns_empty_when_file_missing(tmp_path):
    assert carryover.load_existing(tmp_path / "nope.json") == []


def test_load_existing_parses_domains_from_payload(tmp_path):
    p = tmp_path / "out.json"
    p.write_text(json.dumps({
        "generated_at": "2026-04-30T07:00:00Z",
        "domains": [_entry("a.com"), _entry("b.org")],
    }), encoding="utf-8")
    result = carryover.load_existing(p)
    assert [d["name"] for d in result] == ["a.com", "b.org"]


def test_load_existing_returns_empty_on_malformed_json(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{ not json", encoding="utf-8")
    assert carryover.load_existing(p) == []


def test_load_existing_skips_entries_without_name(tmp_path):
    p = tmp_path / "out.json"
    p.write_text(json.dumps({
        "domains": [_entry("good.com"), {"tld": "com"}, "garbage", None],
    }), encoding="utf-8")
    result = carryover.load_existing(p)
    assert [d["name"] for d in result] == ["good.com"]


def test_load_existing_returns_empty_when_payload_lacks_domains(tmp_path):
    p = tmp_path / "out.json"
    p.write_text(json.dumps({"generated_at": "..."}), encoding="utf-8")
    assert carryover.load_existing(p) == []


# --- filter_by_age -----------------------------------------------------------


def test_filter_by_age_drops_entries_older_than_window():
    today = date(2026, 4, 30)
    entries = [
        _entry("recent.com", first_seen_date="2026-04-25"),    # 5 days ago
        _entry("old.com",    first_seen_date="2026-04-15"),    # 15 days ago
        _entry("ancient.org", first_seen_date="2025-12-01"),   # 150 days ago
    ]
    survivors, dropped = carryover.filter_by_age(entries, today, max_age_days=14)
    assert [d["name"] for d in survivors] == ["recent.com"]
    assert dropped == 2


def test_filter_by_age_keeps_entries_at_exact_boundary():
    """14 days ago = inclusive; 15 days = excluded."""
    today = date(2026, 4, 30)
    entries = [
        _entry("edge.com", first_seen_date="2026-04-16"),   # 14 days ago
        _entry("over.com", first_seen_date="2026-04-15"),   # 15 days ago
    ]
    survivors, dropped = carryover.filter_by_age(entries, today, max_age_days=14)
    assert [d["name"] for d in survivors] == ["edge.com"]
    assert dropped == 1


def test_filter_by_age_migrates_missing_first_seen_date():
    """Pre-persistence entries get first_seen_date=today injected so they
    survive this round and age naturally going forward."""
    today = date(2026, 4, 30)
    entries = [_entry("legacy.com")]  # no first_seen_date
    survivors, dropped = carryover.filter_by_age(entries, today)
    assert dropped == 0
    assert survivors[0]["first_seen_date"] == "2026-04-30"


def test_filter_by_age_handles_malformed_date_as_migration():
    today = date(2026, 4, 30)
    entries = [_entry("garbled.com", first_seen_date="not-a-date")]
    survivors, dropped = carryover.filter_by_age(entries, today)
    assert survivors[0]["first_seen_date"] == "2026-04-30"
    assert dropped == 0


# --- validate_against_zone ---------------------------------------------------


def test_validate_against_zone_keeps_absent_entries():
    today = date(2026, 4, 30)
    entries = [
        _entry("free.com", first_seen_date="2026-04-29"),
        _entry("taken.com", first_seen_date="2026-04-29"),
    ]
    today_set = {"taken.com", "other.com"}
    retained, registered = carryover.validate_against_zone(entries, today_set, today)
    assert [r["name"] for r in retained] == ["free.com"]
    assert registered == 1
    assert retained[0]["last_validated_date"] == "2026-04-30"


def test_validate_against_zone_empty_input_returns_empty():
    retained, registered = carryover.validate_against_zone([], set(), date(2026, 4, 30))
    assert retained == []
    assert registered == 0


def test_validate_against_zone_does_not_mutate_originals():
    """The function should return new dicts so callers' references stay
    pristine — important when we keep `existing` around for diagnostics."""
    today = date(2026, 4, 30)
    entry = _entry("free.com", last_validated_date="2026-04-29")
    retained, _ = carryover.validate_against_zone([entry], set(), today)
    assert entry["last_validated_date"] == "2026-04-29"
    assert retained[0]["last_validated_date"] == "2026-04-30"


# --- annotate_* --------------------------------------------------------------


def test_annotate_today_drops_sets_all_three_fields():
    today = date(2026, 4, 30)
    drops = [_entry("fresh.com")]
    carryover.annotate_today_drops(drops, today)
    assert drops[0]["first_seen_date"] == "2026-04-30"
    assert drops[0]["last_validated_date"] == "2026-04-30"
    assert drops[0]["days_listed"] == 0


def test_annotate_carryover_days_listed_computes_delta():
    today = date(2026, 4, 30)
    cands = [
        _entry("a.com", first_seen_date="2026-04-30"),  # today
        _entry("b.com", first_seen_date="2026-04-26"),  # 4 days ago
        _entry("c.com", first_seen_date="2026-04-16"),  # 14 days ago
    ]
    carryover.annotate_carryover_days_listed(cands, today)
    assert cands[0]["days_listed"] == 0
    assert cands[1]["days_listed"] == 4
    assert cands[2]["days_listed"] == 14


def test_annotate_carryover_days_listed_defaults_to_zero_for_missing_dates():
    today = date(2026, 4, 30)
    cands = [_entry("legacy.com")]  # no first_seen_date
    carryover.annotate_carryover_days_listed(cands, today)
    assert cands[0]["days_listed"] == 0


# --- merge -------------------------------------------------------------------


def test_merge_appends_carryover_after_today_drops():
    today_drops = [_entry("today.com")]
    cary = [_entry("yesterday.com", first_seen_date="2026-04-29")]
    merged = carryover.merge(today_drops, cary)
    assert [d["name"] for d in merged] == ["today.com", "yesterday.com"]


def test_merge_today_replaces_old_entry_for_same_domain():
    """Edge case 4 from the spec: a domain present in BOTH today's drops
    AND carryover (rare — registered then dropped again same week). Today's
    entry wins fully — no field merge."""
    today_drops = [_entry("dup.com", score=80)]
    cary = [_entry("dup.com", score=20, first_seen_date="2026-04-22")]
    merged = carryover.merge(today_drops, cary)
    assert len(merged) == 1
    assert merged[0]["score"] == 80
    # No first_seen_date carried over — today's entry is fresh.
    assert "first_seen_date" not in merged[0] or merged[0].get("first_seen_date") is None


def test_merge_empty_inputs_yield_empty_output():
    assert carryover.merge([], []) == []
