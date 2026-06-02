"""Unit tests for scripts/domain_archive.py — the permanent dropped-domain
lifecycle archive (private R2). Pure: R2 is faked in-memory, git backfill is
exercised via fake snapshots, no network or subprocess."""

from __future__ import annotations

import json
from datetime import date
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from scripts import domain_archive


# Minimal config sufficient for score.score_candidate + output._compute_verdict.
CFG = {
    "scoring_weights": {
        "wayback_snapshots": 0.3,
        "open_page_rank": 0.4,
        "cert_history": 0.2,
        "domain_length": 0.1,
        "cc_source_domain_count": 0.3,
    },
    "verdict_thresholds": {
        "clean_min_score": 70,
        "promising_min_score": 40,
        "promising_min_wayback_snapshots": 1000,
        "promising_min_open_page_rank": 1.5,
        "promising_min_cc_source_domain_count": 10,
    },
    "soft_signal_keywords": [],
    "rejected_keywords": [],
    "rejected_keyword_prefixes": [],
    "rejected_keyword_substrings": [],
}

TODAY = date(2026, 6, 2)


class FakeR2:
    """In-memory S3-ish store so append/read round-trips (for idempotency and
    partitioning assertions). Mirrors the botocore surface domain_archive uses."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.put_calls = 0

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(self.store[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put_calls += 1
        self.store[Key] = Body

    def records(self, key):
        return [json.loads(l) for l in self.store[key].decode().splitlines() if l]


# --- build_live_record -------------------------------------------------------


def test_live_record_uses_existing_score_and_computes_verdict():
    cand = {
        "name": "marketglow.com", "tld": "com", "dropped_date": "2026-06-01",
        "availability_verified_at": "2026-06-02T10:30:00Z",
        "score": 80, "phase2_score": 75,
        "wayback_snapshots": 1500, "open_page_rank": 3.0,
        "cc_source_domain_count": 40, "cert_history": True,
        "snapshot_category": "legitimate",
    }
    rec = domain_archive.build_live_record(cand, was_published=True, config=CFG, today=TODAY)
    assert rec["domain"] == "marketglow.com"
    assert rec["tld"] == "com"
    assert rec["drop_date"] == "2026-06-01"
    assert rec["availability_status"] == "available"
    assert rec["availability_confirmed_date"] == "2026-06-02"  # trimmed from verified_at
    assert rec["score"] == 80  # existing score reused, not recomputed
    assert rec["phase2_score"] == 75
    assert rec["verdict"] == "Clean"  # score >= 70
    assert rec["was_published"] is True
    assert rec["source"] == "live"
    # Signals carried verbatim.
    assert rec["wayback_snapshots"] == 1500
    assert rec["snapshot_category"] == "legitimate"


def test_live_record_computes_score_for_unscored_rejected_tail():
    """The available-but-rejected tail (never scored in the pipeline) still gets
    a score + verdict so the archive schema is uniform."""
    cand = {
        "name": "tideblock.io", "tld": "io", "dropped_date": "2026-06-02",
        "availability_verified_at": "2026-06-02T10:31:00Z",
        # No 'score' key — this domain was filtered before Stage 6 scoring.
        "wayback_snapshots": 3, "open_page_rank": 0.2, "cert_history": False,
    }
    rec = domain_archive.build_live_record(cand, was_published=False, config=CFG, today=TODAY)
    assert rec["was_published"] is False
    assert rec["score"] is not None  # computed via score.score_candidate
    assert rec["verdict"] in ("Clean", "Promising", "Caution")
    assert rec["source"] == "live"


def test_live_record_confirmed_date_falls_back_to_today():
    cand = {"name": "coppernest.org", "tld": "org", "dropped_date": "2026-06-02"}
    rec = domain_archive.build_live_record(cand, was_published=False, config=CFG, today=TODAY)
    assert rec["availability_confirmed_date"] == "2026-06-02"  # today


def test_live_record_omits_absent_signals():
    cand = {"name": "x.dev", "tld": "dev", "dropped_date": "2026-06-02", "score": 10}
    rec = domain_archive.build_live_record(cand, was_published=False, config=CFG, today=TODAY)
    assert "wayback_snapshots" not in rec  # absent → omitted, not null
    assert "open_page_rank" not in rec


# --- build_backfill_record(s) ------------------------------------------------


def test_backfill_record_marks_source_and_published():
    entry = {
        "name": "amberkite.com", "tld": "com", "dropped_date": "2026-05-01",
        "score": 62, "verdict": "Caution", "wayback_snapshots": 5,
        "open_page_rank": 1.1, "cc_source_domain_count": 2,
    }
    rec = domain_archive.build_backfill_record(entry, confirmed_date="2026-05-01")
    assert rec["source"] == "backfill"
    assert rec["was_published"] is True
    assert rec["availability_confirmed_date"] == "2026-05-01"
    assert rec["score"] == 62
    assert rec["verdict"] == "Caution"
    assert rec["wayback_snapshots"] == 5
    assert rec["phase2_score"] is None  # older payloads lack it


def test_backfill_dedup_carryover_one_event_per_drop_cycle():
    """A carryover domain appearing across many daily snapshots is ONE event
    (its drop), anchored on first_seen_date. A re-drop with a NEW first_seen_date
    is a SECOND event."""
    snapshots = [
        ("2026-05-01", {"domains": [
            {"name": "a.com", "tld": "com", "first_seen_date": "2026-05-01"},
        ]}),
        ("2026-05-02", {"domains": [
            {"name": "a.com", "tld": "com", "first_seen_date": "2026-05-01"},  # carryover
            {"name": "b.org", "tld": "org", "first_seen_date": "2026-05-02"},
        ]}),
        ("2026-06-10", {"domains": [
            {"name": "a.com", "tld": "com", "first_seen_date": "2026-06-10"},  # re-dropped
        ]}),
    ]
    records = domain_archive.build_backfill_records(snapshots)
    keys = sorted((r["domain"], r["availability_confirmed_date"]) for r in records)
    assert keys == [
        ("a.com", "2026-05-01"),
        ("a.com", "2026-06-10"),
        ("b.org", "2026-05-02"),
    ]


def test_backfill_missing_first_seen_falls_back_to_commit_date():
    snapshots = [
        ("2026-04-26", {"domains": [{"name": "old.com", "tld": "com"}]}),  # no first_seen_date
    ]
    records = domain_archive.build_backfill_records(snapshots)
    assert records[0]["availability_confirmed_date"] == "2026-04-26"


# --- _month_key / _event_key -------------------------------------------------


def test_month_key_partitions_by_year_month():
    assert domain_archive._month_key("2026-06-02") == "state/domain_archive/2026-06.jsonl"
    assert domain_archive._month_key("2026-04-26") == "state/domain_archive/2026-04.jsonl"


def test_month_key_unparseable_goes_to_unknown_partition():
    assert domain_archive._month_key("") == "state/domain_archive/unknown.jsonl"


def test_event_key_includes_domain_date_source():
    rec = {"domain": "a.com", "availability_confirmed_date": "2026-06-02", "source": "live"}
    assert domain_archive._event_key(rec) == ("a.com", "2026-06-02", "live")


# --- append_records (R2 append-only, event-deduped, monthly) -----------------


def test_append_writes_new_records_to_empty_r2():
    r2 = FakeR2()
    recs = [
        {"domain": "a.com", "availability_confirmed_date": "2026-06-02", "source": "live"},
        {"domain": "b.org", "availability_confirmed_date": "2026-06-02", "source": "live"},
    ]
    n = domain_archive.append_records(recs, r2_client=r2, r2_bucket="b")
    assert n == 2
    assert r2.records("state/domain_archive/2026-06.jsonl")[0]["domain"] == "a.com"


def test_append_is_idempotent_on_rerun():
    """Re-running the same day appends nothing (event keys already present)."""
    r2 = FakeR2()
    recs = [{"domain": "a.com", "availability_confirmed_date": "2026-06-02", "source": "live"}]
    assert domain_archive.append_records(recs, r2_client=r2, r2_bucket="b") == 1
    assert domain_archive.append_records(recs, r2_client=r2, r2_bucket="b") == 0
    assert len(r2.records("state/domain_archive/2026-06.jsonl")) == 1


def test_append_preserves_existing_and_adds_new_event():
    r2 = FakeR2()
    domain_archive.append_records(
        [{"domain": "a.com", "availability_confirmed_date": "2026-06-02", "source": "live"}],
        r2_client=r2, r2_bucket="b",
    )
    # Same domain, LATER date = a genuinely new event, must append.
    n = domain_archive.append_records(
        [{"domain": "a.com", "availability_confirmed_date": "2026-06-20", "source": "live"}],
        r2_client=r2, r2_bucket="b",
    )
    assert n == 1
    recs = r2.records("state/domain_archive/2026-06.jsonl")
    assert sorted(r["availability_confirmed_date"] for r in recs) == ["2026-06-02", "2026-06-20"]


def test_append_partitions_across_months():
    r2 = FakeR2()
    recs = [
        {"domain": "a.com", "availability_confirmed_date": "2026-05-31", "source": "backfill"},
        {"domain": "b.org", "availability_confirmed_date": "2026-06-01", "source": "backfill"},
    ]
    n = domain_archive.append_records(recs, r2_client=r2, r2_bucket="b")
    assert n == 2
    assert "state/domain_archive/2026-05.jsonl" in r2.store
    assert "state/domain_archive/2026-06.jsonl" in r2.store


def test_append_collapses_intra_batch_duplicates():
    r2 = FakeR2()
    dup = {"domain": "a.com", "availability_confirmed_date": "2026-06-02", "source": "live"}
    n = domain_archive.append_records([dup, dict(dup)], r2_client=r2, r2_bucket="b")
    assert n == 1


def test_append_live_and_backfill_same_domain_date_coexist():
    """Source is part of the event key, so a live and a backfill row for the
    same domain+date are both kept (honest provenance, no masking)."""
    r2 = FakeR2()
    recs = [
        {"domain": "a.com", "availability_confirmed_date": "2026-06-02", "source": "live"},
        {"domain": "a.com", "availability_confirmed_date": "2026-06-02", "source": "backfill"},
    ]
    assert domain_archive.append_records(recs, r2_client=r2, r2_bucket="b") == 2


# --- emit_available_set (pipeline-side handoff) ------------------------------


def test_emit_available_set_writes_handoff(tmp_path):
    available = [
        {"name": "pub.com", "tld": "com", "dropped_date": "2026-06-02",
         "availability_verified_at": "2026-06-02T10:00:00Z", "score": 85,
         "wayback_snapshots": 2000, "open_page_rank": 3.0},
        {"name": "rejected.com", "tld": "com", "dropped_date": "2026-06-02",
         "availability_verified_at": "2026-06-02T10:00:05Z",
         "wayback_snapshots": 0},  # below wayback floor, unpublished
    ]
    handoff = tmp_path / "available_set_latest.jsonl"
    n = domain_archive.emit_available_set(
        available, published_names={"pub.com"}, config=CFG, today=TODAY, path=handoff,
    )
    assert n == 2
    recs = [json.loads(l) for l in handoff.read_text().splitlines() if l]
    by_name = {r["domain"]: r for r in recs}
    assert by_name["pub.com"]["was_published"] is True
    assert by_name["rejected.com"]["was_published"] is False  # captured anyway
    assert all(r["source"] == "live" for r in recs)


def test_emit_empty_available_set_writes_empty_handoff(tmp_path):
    handoff = tmp_path / "av.jsonl"
    n = domain_archive.emit_available_set([], published_names=set(), config=CFG, today=TODAY, path=handoff)
    assert n == 0
    assert handoff.read_text() == ""


# --- run_live (read handoff → append) ----------------------------------------


def test_run_live_reads_handoff_and_appends(tmp_path):
    available = [
        {"name": "x.com", "tld": "com", "dropped_date": "2026-06-02",
         "availability_verified_at": "2026-06-02T10:00:00Z", "score": 50},
    ]
    handoff = tmp_path / "av.jsonl"
    domain_archive.emit_available_set(available, {"x.com"}, CFG, TODAY, path=handoff)

    r2 = FakeR2()
    n = domain_archive.run_live(handoff_path=handoff, r2_client=r2, r2_bucket="b")
    assert n == 1
    assert r2.records("state/domain_archive/2026-06.jsonl")[0]["domain"] == "x.com"


def test_run_live_missing_handoff_is_noop(tmp_path):
    r2 = FakeR2()
    n = domain_archive.run_live(handoff_path=tmp_path / "nope.jsonl", r2_client=r2, r2_bucket="b")
    assert n == 0
    assert r2.put_calls == 0


# --- run_backfill (via fake snapshots) ---------------------------------------


def test_run_backfill_appends_published_events(monkeypatch):
    snapshots = [
        ("2026-05-01", {"domains": [
            {"name": "a.com", "tld": "com", "first_seen_date": "2026-05-01", "score": 60, "verdict": "Caution"},
        ]}),
        ("2026-06-01", {"domains": [
            {"name": "b.org", "tld": "org", "first_seen_date": "2026-06-01", "score": 72, "verdict": "Clean"},
        ]}),
    ]
    monkeypatch.setattr(domain_archive, "iter_git_daily_snapshots", lambda *_a, **_k: iter(snapshots))
    r2 = FakeR2()
    n = domain_archive.run_backfill(repo_root=".", r2_client=r2, r2_bucket="b")
    assert n == 2
    assert "state/domain_archive/2026-05.jsonl" in r2.store
    assert "state/domain_archive/2026-06.jsonl" in r2.store
    assert all(r["source"] == "backfill" for r in r2.records("state/domain_archive/2026-05.jsonl"))
