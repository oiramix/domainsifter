"""Tests for scripts/api_snapshot.py. R2 is a MagicMock; domains are invented."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from scripts import api_snapshot

CONFIG = {"api": {"rdap_verified_since": "2026-04-30", "classifier_model": "claude-haiku-4-5",
                  "name_ranker_model": "claude-haiku-4-5"}}


def _ev(day: str, source: str = "live", **fields) -> dict:
    base = {"domain": "marketglow.com", "tld": "com", "drop_date": day,
            "availability_status": "available", "availability_confirmed_date": day,
            "source": source, "was_published": False}
    base.update(fields)
    return base


def _metric(doc: dict, name: str) -> dict:
    return next(m for m in doc["metrics"] if m["name"] == name)


def test_full_live_record_shapes_every_block():
    doc = api_snapshot.build_domain("marketglow.com", [_ev(
        "2026-09-22", wayback_snapshots=120, wayback_last_snapshot="2025-03-04",
        open_page_rank=2.5, cc_source_domain_count=43, cert_history=True,
        snapshot_category="legitimate", snapshot_classifier_version="v3", phase2_score=81,
        was_published=True,
    )], {"title": "Marketglow candles"}, CONFIG)

    assert doc["first_seen_at"] == doc["last_seen_at"] == "2026-09-22T00:00:00Z"
    assert doc["availability"] == {"status": "available", "observed_at": "2026-09-22T00:00:00Z",
                                   "source": "registry_rdap", "drop_date": "2026-09-22"}
    assert _metric(doc, "referring_domains") == {"name": "referring_domains", "value": 43, "status": "ok",
                                                 "source": "common_crawl_webgraph",
                                                 "observed_at": "2026-09-22T00:00:00Z"}
    assert doc["wayback"] == {"status": "ok", "snapshots": 120, "first_year": None, "last_year": 2025,
                              "sample_title": "Marketglow candles", "observed_at": "2026-09-22T00:00:00Z"}
    assert doc["analysis"] == {
        "model": "claude-haiku-4-5", "prompt_version": "v3", "niche": None, "score": None,
        "spam_signal": "low", "verdict": "legitimate", "analyzed_at": "2026-09-22T00:00:00Z",
        "name_quality": {"model": "claude-haiku-4-5", "score": 81, "analyzed_at": "2026-09-22T00:00:00Z"},
    }
    assert doc["published_on_domainsifter"] is True
    assert doc["observations_count"] == len(doc["observations"]) == 7


def test_unknown_is_null_with_status_and_zero_stays_zero():
    doc = api_snapshot.build_domain("marketglow.com", [_ev(
        "2026-09-22", cc_source_domain_count=None, open_page_rank=0.0,
    )], None, CONFIG)
    assert _metric(doc, "referring_domains")["value"] is None
    assert _metric(doc, "referring_domains")["status"] == "not_checked"
    assert _metric(doc, "open_page_rank") == {"name": "open_page_rank", "value": 0.0, "status": "ok",
                                              "source": "open_page_rank", "observed_at": "2026-09-22T00:00:00Z"}
    assert _metric(doc, "cert_history")["status"] == "not_checked"
    assert doc["wayback"]["status"] == "not_checked"
    assert doc["wayback"]["sample_title"] is None


@pytest.mark.parametrize("fields,status,snapshots", [
    ({"wayback_snapshots": 5}, "ok", 5),
    ({"wayback_snapshots": 0}, "none_found", 0),
    ({"wayback_unknown": True}, "lookup_failed", None),
    ({}, "not_checked", None),
])
def test_wayback_status(fields, status, snapshots):
    doc = api_snapshot.build_domain("marketglow.com", [_ev("2026-09-22", **fields)], None, CONFIG)
    assert doc["wayback"]["status"] == status
    assert doc["wayback"]["snapshots"] == snapshots


def test_metric_keeps_last_good_value_with_its_own_date():
    doc = api_snapshot.build_domain("marketglow.com", [
        _ev("2026-09-20", cc_source_domain_count=12),
        _ev("2026-09-22"),
    ], None, CONFIG)
    rd = _metric(doc, "referring_domains")
    assert rd["value"] == 12 and rd["observed_at"] == "2026-09-20T00:00:00Z"
    assert doc["last_seen_at"] == "2026-09-22T00:00:00Z"


def test_pre_rdap_backfill_is_dropped_from_zone_diff_only():
    doc = api_snapshot.build_domain("marketglow.com", [_ev("2026-04-28", source="backfill")], None, CONFIG)
    assert doc["availability"]["status"] == "dropped"
    assert doc["availability"]["source"] == "icann_czds"
    later = api_snapshot.build_domain("marketglow.com", [_ev("2026-05-02", source="backfill")], None, CONFIG)
    assert later["availability"]["source"] == "registry_rdap"


def test_live_beats_backfill_on_the_same_day():
    doc = api_snapshot.build_domain("marketglow.com", [
        _ev("2026-06-02", source="backfill", cc_source_domain_count=3),
        _ev("2026-06-02", source="live", cc_source_domain_count=9),
    ], None, CONFIG)
    assert _metric(doc, "referring_domains")["value"] == 9
    assert doc["observations_count"] == 7


def test_unknown_category_gives_no_verdict_and_toxic_is_high_spam():
    unknown = api_snapshot.build_domain("marketglow.com", [_ev("2026-09-22", snapshot_category="unknown")], None, CONFIG)
    assert unknown["analysis"] is None
    toxic = api_snapshot.build_domain("marketglow.com", [_ev(
        "2026-09-22", snapshot_category="toxic", snapshot_classifier_version="v2")], None, CONFIG)
    assert toxic["analysis"]["verdict"] == "toxic"
    assert toxic["analysis"]["spam_signal"] == "high"
    assert toxic["analysis"]["prompt_version"] == "v2"
    assert toxic["analysis"]["name_quality"] is None


def test_events_without_a_valid_date_are_ignored():
    assert api_snapshot.build_domain("marketglow.com", [_ev("not-a-date")], None, CONFIG) is None


def test_build_snapshot_groups_and_normalises_names():
    events = [_ev("2026-09-22"), dict(_ev("2026-09-23"), domain="TideBlock.NET."), {"domain": "", "availability_confirmed_date": "2026-09-22"}]
    snap = api_snapshot.build_snapshot(events, {}, CONFIG, now=datetime(2026, 10, 16, 15, 10, tzinfo=timezone.utc), git_sha="abc1234")
    assert sorted(snap["domains"]) == ["marketglow.com", "tideblock.net"]
    assert snap["built_at"] == "2026-10-16T15:10:00Z"
    assert snap["git_sha"] == "abc1234"
    assert snap["schema_version"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_write_snapshot_is_atomic_and_world_readable(tmp_path):
    path = tmp_path / "api" / "snapshot.json"
    api_snapshot.write_snapshot({"domains": {}}, path)
    assert json.loads(path.read_text()) == {"domains": {}}
    assert oct(os.stat(path).st_mode & 0o777) == "0o644"


def test_main_reads_only_the_archive_and_never_the_denylist(tmp_path):
    lines = "".join(json.dumps(e) + "\n" for e in [
        _ev("2026-09-22", wayback_snapshots=3), dict(_ev("2026-09-23"), domain="tideblock.net"),
    ]).encode()
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": [{"Key": "state/domain_archive/2026-09.jsonl"}]}
    s3.get_object.side_effect = lambda Bucket, Key: {"Body": BytesIO(lines)}
    cfg = {**CONFIG, "sidecar_excerpts_path": str(tmp_path / "missing.json")}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    out = tmp_path / "snap.json"

    rc = api_snapshot.main(["--config", str(cfg_path), "--output", str(out)], s3_factory=lambda: (s3, "b"))

    assert rc == 0
    assert sorted(json.loads(out.read_text(encoding="utf-8"))["domains"]) == ["marketglow.com", "tideblock.net"]
    read_keys = {c.kwargs["Key"] for c in s3.get_object.call_args_list}
    assert read_keys == {"state/domain_archive/2026-09.jsonl"}
    s3.list_objects_v2.assert_called_with(Bucket="b", Prefix="state/domain_archive/")


def test_main_failure_keeps_previous_snapshot(tmp_path):
    out = tmp_path / "snap.json"
    out.write_text('{"domains": {"old.com": {}}}', encoding="utf-8")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    s3 = MagicMock()
    s3.list_objects_v2.side_effect = RuntimeError("r2 down")
    assert api_snapshot.main(["--config", str(cfg_path), "--output", str(out)], s3_factory=lambda: (s3, "b")) == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {"domains": {"old.com": {}}}
