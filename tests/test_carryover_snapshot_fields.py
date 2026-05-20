"""Phase 4 carryover preservation tests (2026-05-20).

Verifies that snapshot_category and snapshot_classifier_version survive
the daily carryover cycle: filter_by_age, age_out_wayback_unknown, and
validate_against_zone. The carryover module wasn't modified by Phase 4
because its existing {**e, ...} preservation pattern already passes
unknown fields through, but these tests lock that behaviour in so a
future refactor (e.g., adding explicit field-list filtering) can't
silently strip the classifier output.
"""

from __future__ import annotations

from datetime import date

from scripts import carryover


def _classified(
    name: str,
    *,
    category: str = "legitimate",
    version: str = "v1",
    first_seen: str = "2026-05-15",
    last_validated: str = "2026-05-19",
    tld: str | None = None,
) -> dict:
    tld_val = tld or name.rsplit(".", 1)[-1]
    return {
        "name": name,
        "tld": tld_val,
        "first_seen_date": first_seen,
        "last_validated_date": last_validated,
        "snapshot_category": category,
        "snapshot_classifier_version": version,
    }


# ---------------------------------------------------------------------------
# filter_by_age
# ---------------------------------------------------------------------------


def test_filter_by_age_preserves_snapshot_fields_on_within_window_entry():
    entry = _classified("alpha.com", first_seen="2026-05-15")
    survivors, dropped = carryover.filter_by_age(
        [entry], date(2026, 5, 20), max_age_days=14,
    )
    assert dropped == 0
    assert len(survivors) == 1
    assert survivors[0]["snapshot_category"] == "legitimate"
    assert survivors[0]["snapshot_classifier_version"] == "v1"


def test_filter_by_age_preserves_snapshot_fields_when_synthesizing_first_seen():
    """Migration path: entry without first_seen_date gets one synthesized.
    The {**e, ...} spread must carry forward snapshot_category."""
    entry = {
        "name": "alpha.com",
        "tld": "com",
        "snapshot_category": "parked",
        "snapshot_classifier_version": "v1",
        # no first_seen_date
    }
    survivors, dropped = carryover.filter_by_age(
        [entry], date(2026, 5, 20), max_age_days=14,
    )
    assert dropped == 0
    assert len(survivors) == 1
    assert survivors[0]["snapshot_category"] == "parked"
    assert survivors[0]["snapshot_classifier_version"] == "v1"
    assert survivors[0]["first_seen_date"] == "2026-05-20"


# ---------------------------------------------------------------------------
# age_out_wayback_unknown
# ---------------------------------------------------------------------------


def test_age_out_wayback_unknown_preserves_snapshot_fields_on_normal_entry():
    """A non-wayback_unknown entry passes through unchanged. The function
    returns the entry by REFERENCE (`survivors.append(e)`), so all fields
    including snapshot_category come through verbatim."""
    entry = _classified("alpha.com")
    survivors, dropped = carryover.age_out_wayback_unknown([entry])
    assert dropped == 0
    assert survivors[0]["snapshot_category"] == "legitimate"
    assert survivors[0]["snapshot_classifier_version"] == "v1"


def test_age_out_wayback_unknown_preserves_snapshot_fields_when_incrementing():
    """A wayback_unknown=True entry gets its attempts counter bumped via
    `{**e, "wayback_unknown_attempts": ...}` — the spread must carry
    forward snapshot_category."""
    entry = _classified("alpha.com")
    entry["wayback_unknown"] = True
    entry["wayback_unknown_attempts"] = 1
    survivors, dropped = carryover.age_out_wayback_unknown([entry], max_unknown_days=3)
    assert dropped == 0
    assert len(survivors) == 1
    survivor = survivors[0]
    assert survivor["snapshot_category"] == "legitimate"
    assert survivor["snapshot_classifier_version"] == "v1"
    assert survivor["wayback_unknown_attempts"] == 2


# ---------------------------------------------------------------------------
# validate_against_zone
# ---------------------------------------------------------------------------


def test_validate_against_zone_preserves_snapshot_fields_on_retained_entry():
    entry = _classified("alpha.com")
    retained, registered = carryover.validate_against_zone(
        [entry], today_set=set(), today=date(2026, 5, 20),
    )
    assert registered == 0
    assert len(retained) == 1
    survivor = retained[0]
    assert survivor["snapshot_category"] == "legitimate"
    assert survivor["snapshot_classifier_version"] == "v1"
    # last_validated_date updated to today.
    assert survivor["last_validated_date"] == "2026-05-20"


def test_validate_against_zone_drops_registered_entries():
    """When a carryover entry shows up in today's zone (someone registered
    it), it's dropped — snapshot fields go with it (correct: no longer
    relevant). The other entries still have their fields preserved."""
    kept = _classified("kept.com")
    registered = _classified("registered.com", category="parked")
    retained, registered_count = carryover.validate_against_zone(
        [kept, registered],
        today_set={"registered.com"},
        today=date(2026, 5, 20),
    )
    assert registered_count == 1
    assert len(retained) == 1
    assert retained[0]["name"] == "kept.com"
    assert retained[0]["snapshot_category"] == "legitimate"


# ---------------------------------------------------------------------------
# Full carryover cycle integration
# ---------------------------------------------------------------------------


def test_full_carryover_cycle_preserves_snapshot_fields():
    """Run a candidate through all three carryover steps in sequence
    (filter_by_age → age_out_wayback_unknown → validate_against_zone)
    and verify snapshot_category + snapshot_classifier_version survive
    end-to-end. This is the actual order pipeline.main uses."""
    entry = _classified("survivor.com")
    today = date(2026, 5, 20)

    # Step 1: age filter
    survivors, _ = carryover.filter_by_age([entry], today, max_age_days=14)
    # Step 2: wayback_unknown age-out (no-op for this entry)
    survivors, _ = carryover.age_out_wayback_unknown(survivors)
    # Step 3: zone validation (entry NOT in zone → kept)
    retained, _ = carryover.validate_against_zone(survivors, set(), today)

    assert len(retained) == 1
    assert retained[0]["snapshot_category"] == "legitimate"
    assert retained[0]["snapshot_classifier_version"] == "v1"


def test_full_carryover_cycle_preserves_all_classifier_categories():
    """Same as above but with the full category matrix — proves none of
    the carryover steps treat one category differently."""
    today = date(2026, 5, 20)
    entries = [
        _classified(f"e-{cat}.com", category=cat)
        for cat in ("legitimate", "parked", "empty", "unknown")
    ]
    # toxic is intentionally NOT here — the filter rejects toxic upstream,
    # so it never enters carryover (a toxic entry on day N is evicted
    # before write, doesn't get serialized, doesn't reappear day N+1).

    survivors, _ = carryover.filter_by_age(entries, today, max_age_days=14)
    survivors, _ = carryover.age_out_wayback_unknown(survivors)
    retained, _ = carryover.validate_against_zone(survivors, set(), today)

    by_name = {e["name"]: e for e in retained}
    for cat in ("legitimate", "parked", "empty", "unknown"):
        name = f"e-{cat}.com"
        assert name in by_name
        assert by_name[name]["snapshot_category"] == cat
        assert by_name[name]["snapshot_classifier_version"] == "v1"


def test_load_existing_preserves_snapshot_fields(tmp_path):
    """The JSON round-trip must not strip snapshot_category /
    snapshot_classifier_version. carryover.load_existing reads the
    full entry dict — confirms the entries written by output.py's
    _project come back faithfully on the next day's pipeline start."""
    import json
    path = tmp_path / "daily.json"
    path.write_text(json.dumps({
        "domains": [{
            "name": "a.com", "tld": "com",
            "snapshot_category": "legitimate",
            "snapshot_classifier_version": "v1",
        }, {
            "name": "b.com", "tld": "com",
            "snapshot_category": "parked",
            "snapshot_classifier_version": "v1",
        }],
    }), encoding="utf-8")

    loaded = carryover.load_existing(path)
    assert len(loaded) == 2
    by_name = {e["name"]: e for e in loaded}
    assert by_name["a.com"]["snapshot_category"] == "legitimate"
    assert by_name["a.com"]["snapshot_classifier_version"] == "v1"
    assert by_name["b.com"]["snapshot_category"] == "parked"
