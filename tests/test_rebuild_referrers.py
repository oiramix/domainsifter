"""Tests for scripts/rebuild_referrers.py.

R2 is a MagicMock whose download_file copies small gzipped fixture graphs
into place; DuckDB runs for real against them. All domains are invented.
"""

from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts import rebuild_referrers as rr

LATEST = "cc-main-2026-jul-aug-sep"
OLDER = "cc-main-2026-jun-jul-aug"

# id, reversed domain, num_hosts
VERTICES = [
    (0, "com.marketglow", 3),       # gated, 4 referrers
    (1, "net.tideblock", 1),        # gated, 1 referrer
    (2, "org.coppernest", 50),      # referrer, biggest
    (3, "io.lanternfield", 20),     # referrer
    (4, "com.quietharbor", 5),      # referrer
    (5, "net.saltmeadow", 5),       # referrer, ties quietharbor on num_hosts
    (6, "com.brightkettle", 2),     # links only to a NON-gated domain
    (7, "com.mossyledger", 1),      # not gated (parked), has a referrer
]
# from_id, to_id
EDGES = [
    (2, 0), (3, 0), (4, 0), (5, 0), (5, 0),  # duplicate edge counted once
    (0, 0),                                  # self-link ignored
    (2, 1),
    (6, 7),                                  # to a non-gated target
]


def _gz(path: Path, rows: list[tuple]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write("\t".join(str(x) for x in row) + "\n")
    return path


def _record(name: str, category: str, cc: int | None) -> dict:
    rec = {"domain": name, "snapshot_category": category, "source": "live"}
    if cc is not None:
        rec["cc_source_domain_count"] = cc
    return rec


INPUT_RECORDS = [
    _record("marketglow.com", "legitimate", 40),
    _record("tideblock.net", "legitimate", 25),
    _record("smallgarden.org", "legitimate", 3),          # below gate
    _record("mossyledger.com", "parked", 90),              # wrong category
    _record("ghostorchard.com", "legitimate", None),       # not in graph
    _record("absentquarry.com", "legitimate", 30),         # gated, missing from fixture graph
]


@pytest.fixture
def fixture_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return {
        "vertices": _gz(src / "vertices.txt.gz", VERTICES),
        "edges": _gz(src / "edges.txt.gz", EDGES),
    }


def _fake_s3(files: dict[str, Path], keys: list[str] | None = None) -> MagicMock:
    keys = keys if keys is not None else [
        f"cc/raw/{LATEST}/vertices.txt.gz",
        f"cc/raw/{LATEST}/edges.txt.gz",
        f"cc/raw/{OLDER}/vertices.txt.gz",
        f"cc/raw/{OLDER}/edges.txt.gz",
    ]
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": [{"Key": k} for k in keys]}
    s3.head_object.side_effect = lambda Bucket, Key: {
        "ContentLength": files[Key.rsplit("/", 1)[-1].split(".")[0]].stat().st_size,
    }

    def _download(Bucket, Key, Filename, Config=None):
        shutil.copyfile(files[Key.rsplit("/", 1)[-1].split(".")[0]], Filename)

    s3.download_file.side_effect = _download
    return s3


def _config(tmp_path: Path, **rebuild) -> dict:
    return {
        "cc_backlinks": {
            "latest_release": LATEST,
            "refresh": {
                "blackout_start_utc_hour": 7,
                "blackout_end_utc_hour": 16,
                "pipeline_unit": "domainsifter.service",
            },
        },
        "rebuild": {
            "min_cc_domains": 20,
            "max_referrers": 100,
            "distribution_buckets": [1, 5, 10, 20, 50, 100],
            "workdir": str(tmp_path / "work"),
            "disk_headroom_multiplier": 1.5,
            **rebuild,
        },
    }


def _run(tmp_path, s3, *, records=INPUT_RECORDS, min_cc=20, release=None, **rebuild):
    input_path = tmp_path / "available.jsonl"
    input_path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    output_path = tmp_path / "state" / "out.jsonl"
    rc = rr.run(
        _config(tmp_path, **rebuild),
        input_path=input_path,
        output_path=output_path,
        min_cc_domains=min_cc,
        release=release,
        s3_factory=lambda: (s3, "test-bucket"),
    )
    lines = output_path.read_text(encoding="utf-8").splitlines() if output_path.exists() else []
    return rc, [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Gate and distribution
# ---------------------------------------------------------------------------


def test_gate_keeps_legitimate_at_or_above_threshold_sorted_by_count():
    assert rr.gate(INPUT_RECORDS, 20) == [
        {"name": "marketglow.com", "cc_source_domain_count": 40},
        {"name": "absentquarry.com", "cc_source_domain_count": 30},
        {"name": "tideblock.net", "cc_source_domain_count": 25},
    ]


def test_gate_dedupes_names_and_accepts_name_field():
    records = [
        {"name": "MarketGlow.com", "snapshot_category": "legitimate", "cc_source_domain_count": 21},
        _record("marketglow.com", "legitimate", 33),
        _record("tideblock.net", "legitimate", True),  # bool is not a count
    ]
    assert rr.gate(records, 20) == [{"name": "marketglow.com", "cc_source_domain_count": 33}]


def test_distribution_counts_buckets_over_legitimate_only():
    dist = rr.distribution(INPUT_RECORDS, [1, 5, 20, 50])
    assert dist["records"] == 6
    assert dist["legitimate"] == 5
    assert dist["legitimate_in_graph"] == 4
    assert dist["legitimate_at_least"] == {"1": 4, "5": 3, "20": 3, "50": 0}
    assert dist["categories"] == {"legitimate": 5, "parked": 1}


# ---------------------------------------------------------------------------
# Release selection
# ---------------------------------------------------------------------------


def test_retained_releases_requires_both_files_and_canonical_names(fixture_files):
    s3 = _fake_s3(fixture_files, keys=[
        f"cc/raw/{LATEST}/vertices.txt.gz",
        f"cc/raw/{LATEST}/edges.txt.gz",
        f"cc/raw/{OLDER}/vertices.txt.gz",           # edges missing
        "cc/raw/not-a-release/vertices.txt.gz",
        "cc/raw/not-a-release/edges.txt.gz",
    ])
    assert rr.retained_releases(s3, "b") == [LATEST]


def test_choose_release_prefers_configured_latest_then_newest():
    cfg = {"cc_backlinks": {"latest_release": OLDER}}
    assert rr.choose_release([OLDER, LATEST], cfg) == OLDER
    assert rr.choose_release([OLDER, LATEST], {"cc_backlinks": {}}) == LATEST
    assert rr.choose_release([], cfg) is None


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_run_lists_referrers_biggest_first_only_for_gated_apexes(tmp_path, fixture_files):
    s3 = _fake_s3(fixture_files)
    rc, out = _run(tmp_path, s3)

    assert rc == 0
    assert [o["name"] for o in out] == ["marketglow.com", "absentquarry.com", "tideblock.net"]
    by_name = {o["name"]: o for o in out}
    assert by_name["marketglow.com"] == {
        "name": "marketglow.com",
        "cc_source_domain_count": 40,
        "release": LATEST,
        "referrers_total": 4,
        # num_hosts desc, then name; self-link and duplicate edge dropped
        "referrers": ["coppernest.org", "lanternfield.io", "quietharbor.com", "saltmeadow.net"],
    }
    assert by_name["tideblock.net"]["referrers"] == ["coppernest.org"]
    assert by_name["absentquarry.com"]["referrers_total"] == 0
    assert by_name["absentquarry.com"]["referrers"] == []
    # mossyledger.com is parked, so brightkettle.com never appears
    assert all("brightkettle.com" not in o["referrers"] for o in out)
    # raw comes from the configured latest release
    keys = {c.kwargs["Key"] for c in s3.download_file.call_args_list}
    assert keys == {f"cc/raw/{LATEST}/vertices.txt.gz", f"cc/raw/{LATEST}/edges.txt.gz"}


def test_run_caps_referrers_but_reports_total(tmp_path, fixture_files):
    rc, out = _run(tmp_path, _fake_s3(fixture_files), max_referrers=2)
    assert rc == 0
    first = out[0]
    assert first["referrers"] == ["coppernest.org", "lanternfield.io"]
    assert first["referrers_total"] == 4


def test_run_with_nothing_gated_writes_empty_file_and_downloads_nothing(tmp_path, fixture_files):
    s3 = _fake_s3(fixture_files)
    rc, out = _run(tmp_path, s3, min_cc=1000)
    assert rc == 0
    assert out == []
    assert (tmp_path / "state" / "out.jsonl").read_text(encoding="utf-8") == ""
    s3.download_file.assert_not_called()


def test_run_falls_back_to_newest_retained_release(tmp_path, fixture_files):
    s3 = _fake_s3(fixture_files, keys=[
        f"cc/raw/{OLDER}/vertices.txt.gz",
        f"cc/raw/{OLDER}/edges.txt.gz",
    ])
    rc, out = _run(tmp_path, s3)
    assert rc == 0
    assert {o["release"] for o in out} == {OLDER}


def test_run_rejects_release_without_raw(tmp_path, fixture_files):
    rc, _ = _run(tmp_path, _fake_s3(fixture_files), release="cc-main-2026-may-jun-jul")
    assert rc == 1


def test_run_fails_when_no_release_retained(tmp_path, fixture_files):
    rc, _ = _run(tmp_path, _fake_s3(fixture_files, keys=[]))
    assert rc == 1


def test_run_refuses_when_disk_too_small(tmp_path, fixture_files, monkeypatch):
    monkeypatch.setattr(
        rr.cc_refresh, "_ensure_disk_space",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("Insufficient disk space")),
    )
    s3 = _fake_s3(fixture_files)
    with pytest.raises(RuntimeError):
        _run(tmp_path, s3)
    s3.download_file.assert_not_called()


# ---------------------------------------------------------------------------
# Guard and CLI
# ---------------------------------------------------------------------------


def test_blocked_inside_blackout_and_while_pipeline_active(tmp_path):
    cfg = _config(tmp_path)
    noon = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    evening = datetime(2026, 10, 15, 18, tzinfo=timezone.utc)
    assert "blackout" in rr._blocked_reason(cfg, now=noon, unit_active_fn=lambda _u: False)
    assert "active" in rr._blocked_reason(cfg, now=evening, unit_active_fn=lambda _u: True)
    assert rr._blocked_reason(cfg, now=evening, unit_active_fn=lambda _u: False) is None


def test_main_refuses_without_force_when_blocked(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_config(tmp_path)), encoding="utf-8")
    monkeypatch.setattr(rr, "_blocked_reason", lambda _c: "inside the blackout window")
    s3_factory = MagicMock()
    assert rr.main(["--config", str(cfg_path)], s3_factory=s3_factory) == 2
    s3_factory.assert_not_called()


def test_main_runs_with_force_and_cli_overrides(tmp_path, fixture_files, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_config(tmp_path)), encoding="utf-8")
    input_path = tmp_path / "in.jsonl"
    input_path.write_text(
        "".join(json.dumps(r) + "\n" for r in INPUT_RECORDS) + "not json\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "out.jsonl"
    monkeypatch.setattr(rr, "_blocked_reason", lambda _c: "inside the blackout window")
    s3 = _fake_s3(fixture_files)

    rc = rr.main(
        ["--config", str(cfg_path), "--input", str(input_path), "--output",
         str(output_path), "--min-cc-domains", "26", "--force"],
        s3_factory=lambda: (s3, "test-bucket"),
    )
    assert rc == 0
    names = [json.loads(line)["name"] for line in output_path.read_text().splitlines()]
    assert names == ["marketglow.com", "absentquarry.com"]


def test_main_returns_1_on_missing_input(tmp_path, fixture_files):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_config(tmp_path)), encoding="utf-8")
    rc = rr.main(
        ["--config", str(cfg_path), "--input", str(tmp_path / "missing.jsonl"), "--force"],
        s3_factory=lambda: (_fake_s3(fixture_files), "b"),
    )
    assert rc == 1


def test_r2_listing_error_propagates_as_failure(tmp_path, fixture_files):
    s3 = _fake_s3(fixture_files)
    s3.list_objects_v2.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2",
    )
    with pytest.raises(ClientError):
        _run(tmp_path, s3)
