"""Tests for scripts/rebuild_referrers.py.

R2 is a MagicMock whose download_file copies small gzipped fixture graphs
into place; the derived SQLite is a tiny real file; DuckDB runs for real.
RDAP and the toxic denylist are injected. All domains are invented.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import sqlite3
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
    (0, "com.marketglow", 3),       # candidate, 4 referrers
    (1, "net.tideblock", 1),        # candidate, 1 referrer
    (2, "org.coppernest", 50),      # referrer, biggest
    (3, "io.lanternfield", 20),     # referrer
    (4, "com.quietharbor", 5),      # referrer
    (5, "net.saltmeadow", 5),       # referrer, ties quietharbor on num_hosts
    (6, "com.brightkettle", 2),     # links only to a non-candidate
    (7, "com.oldmill", 1),          # registered again, never a target
]
# from_id, to_id
EDGES = [
    (2, 0), (3, 0), (4, 0), (5, 0), (5, 0),  # duplicate edge counted once
    (0, 0),                                  # self-link ignored
    (2, 1),
    (6, 7),                                  # into a non-candidate
]

# Counts in the derived SQLite of the same release.
SQLITE_COUNTS = {
    "oldmill.com": 50,          # passes everything, RDAP says registered
    "marketglow.com": 40,
    "poker-hall.com": 30,       # rejected keyword (token)
    "gacor-zone.net": 30,       # spam signature on the name
    "harbourlight.org": 30,     # spam signature on its stored excerpt
    "ridgecanyon.net": 30,      # toxic denylist
    "tideblock.net": 25,
    "smallgarden.org": 3,       # below the gate
}
DROPS = list(SQLITE_COUNTS) + ["ghostorchard.com"]  # last one not in the graph

CONFIG_LISTS = {
    "rejected_keywords": ["poker"],
    "rejected_keyword_prefixes": [],
    "rejected_keyword_substrings": ["casino"],
    "snapshot_classifier": {"signature_terms_latin": ["gacor"], "signature_terms_cjk": []},
}


def _gz(path: Path, rows: list[tuple]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write("\t".join(str(x) for x in row) + "\n")
    return path


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    db = src / "derived.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cc_apex (apex_domain TEXT PRIMARY KEY, source_domain_count INTEGER)")
    conn.executemany("INSERT INTO cc_apex VALUES (?, ?)", list(SQLITE_COUNTS.items()))
    conn.commit()
    conn.close()
    sidecar = src / "excerpts.json"
    sidecar.write_text(json.dumps({
        "harbourlight.org": {"title": "Slot gacor terbaru"},
        "marketglow.com": {"title": "Marketglow candles"},
    }), encoding="utf-8")
    return {
        "vertices": _gz(src / "vertices.txt.gz", VERTICES),
        "edges": _gz(src / "edges.txt.gz", EDGES),
        "sqlite": db,
        "sidecar": sidecar,
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


def _config(tmp_path: Path, files: dict[str, Path] | None = None, **rebuild) -> dict:
    cfg = {
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
            "max_rdap_checks": 300,
            "rdap_budget_seconds": 600,
            "distribution_buckets": [1, 5, 10, 20, 50, 100],
            "workdir": str(tmp_path / "work"),
            "disk_headroom_multiplier": 1.5,
            **rebuild,
        },
        **CONFIG_LISTS,
    }
    if files:
        cfg["sidecar_excerpts_path"] = str(files["sidecar"])
    return cfg


class _FakeRdap:
    def __init__(self, registered: set[str] = frozenset({"oldmill.com"})):
        self.registered = registered
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, names, config, budget):
        self.calls.append((list(names), budget))
        avail = [n for n in names if n not in self.registered]
        return {"available": avail, "not_available": len(names) - len(avail), "unknown": 0}


def _run(tmp_path, files, s3=None, *, drops=DROPS, min_cc=20, release=None,
         rdap=None, denylist=frozenset({"ridgecanyon.net"}), **rebuild):
    input_path = tmp_path / "drops.jsonl"
    input_path.write_text(
        "".join(json.dumps({"name": n, "tld": n.rsplit(".", 1)[-1]}) + "\n" for n in drops),
        encoding="utf-8",
    )
    output_path = tmp_path / "state" / "out.jsonl"
    s3 = s3 or _fake_s3(files)
    rdap = rdap or _FakeRdap()
    sqlite_calls: list[str] = []

    def _sqlite(rel, _client, _bucket):
        sqlite_calls.append(rel)
        return files["sqlite"]

    rc = rr.run(
        _config(tmp_path, files, **rebuild),
        input_path=input_path,
        output_path=output_path,
        min_cc_domains=min_cc,
        release=release,
        s3_factory=lambda: (s3, "test-bucket"),
        sqlite_path_fn=_sqlite,
        denylist_fn=lambda _c, _b: set(denylist),
        rdap_fn=rdap,
    )
    lines = output_path.read_text(encoding="utf-8").splitlines() if output_path.exists() else []
    return rc, [json.loads(line) for line in lines], s3, rdap, sqlite_calls


def _summary(caplog) -> dict:
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Rebuild summary: ")][-1]
    return json.loads(line[len("Rebuild summary: "):])


# ---------------------------------------------------------------------------
# Input, counts, abuse filters
# ---------------------------------------------------------------------------


def test_load_drop_names_dedupes_lowercases_and_skips_corrupt(tmp_path):
    path = tmp_path / "drops.jsonl"
    path.write_text(
        '{"name": "MarketGlow.com"}\nnot json\n{"name": "marketglow.com"}\n\n{"name": "tideblock.net"}\n',
        encoding="utf-8",
    )
    assert rr.load_drop_names(path) == ["marketglow.com", "tideblock.net"]


def test_cc_counts_reads_only_present_names_across_chunks(files, monkeypatch):
    monkeypatch.setattr(rr, "_SQLITE_CHUNK", 2)
    counts = rr.cc_counts(files["sqlite"], DROPS)
    assert counts == SQLITE_COUNTS
    assert "ghostorchard.com" not in counts


def test_abuse_reason_covers_each_filter(tmp_path, files):
    cfg = _config(tmp_path, files)
    excerpts = {"harbourlight.org": {"title": "Slot gacor terbaru"}}
    deny = {"ridgecanyon.net"}

    def reason(name):
        return rr.abuse_reason(name, cfg, denylist=deny, excerpts=excerpts)

    assert reason("ridgecanyon.net") == "toxic_denylist"
    assert reason("poker-hall.com") == "rejected_keyword"
    assert reason("bestcasinohub.com") == "rejected_keyword"   # substring list
    assert reason("harbourlight.org") == "spam_signature_excerpt"
    assert reason("gacor-zone.net") == "spam_signature_name"
    assert reason("gacorzone.net") is None   # concatenated: left to keyword substrings
    # long, plain keyword names are NOT filtered: no length or brandability rules
    assert reason("smithandsonsplumbingandheatingservices.com") is None


def test_name_as_excerpt_splits_on_non_letters():
    assert rr._name_as_excerpt("Casino-Royale77.net") == {"title": "casino royale net"}


# ---------------------------------------------------------------------------
# Release selection
# ---------------------------------------------------------------------------


def test_retained_releases_requires_both_files_and_canonical_names(files):
    s3 = _fake_s3(files, keys=[
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


def test_run_end_to_end(tmp_path, files, caplog):
    caplog.set_level(logging.INFO, logger="scripts.rebuild_referrers")
    rc, out, s3, rdap, sqlite_calls = _run(tmp_path, files)

    assert rc == 0
    # counts come from the derived SQLite of the SAME release as the raw graph
    assert sqlite_calls == [LATEST]
    keys = {c.kwargs["Key"] for c in s3.download_file.call_args_list}
    assert keys == {f"cc/raw/{LATEST}/vertices.txt.gz", f"cc/raw/{LATEST}/edges.txt.gz"}
    # RDAP sees only abuse-clean candidates at or above the gate, highest count first
    assert rdap.calls == [(["oldmill.com", "marketglow.com", "tideblock.net"], 600.0)]
    assert out == [
        {
            "name": "marketglow.com",
            "cc_source_domain_count": 40,
            "release": LATEST,
            "referrers_total": 4,
            # num_hosts desc, then name; self-link and duplicate edge dropped
            "referrers": ["coppernest.org", "lanternfield.io", "quietharbor.com", "saltmeadow.net"],
        },
        {
            "name": "tideblock.net",
            "cc_source_domain_count": 25,
            "release": LATEST,
            "referrers_total": 1,
            "referrers": ["coppernest.org"],
        },
    ]
    summary = _summary(caplog)
    assert summary["drops"] == 9
    assert summary["in_graph"] == 8
    assert summary["drops_at_least"] == {"1": 8, "5": 7, "10": 7, "20": 7, "50": 1, "100": 0}
    assert summary["abuse_rejections"] == {
        "rejected_keyword": 1, "spam_signature_excerpt": 1,
        "spam_signature_name": 1, "toxic_denylist": 1,
    }
    assert summary["abuse_pass_at_least"] == {"1": 4, "5": 3, "10": 3, "20": 3, "50": 1, "100": 0}
    assert summary["rdap_checked"] == 3
    assert summary["rdap_available"] == 2
    assert summary["rdap_not_available"] == 1
    assert summary["written"] == 2


def test_run_caps_referrers_but_reports_total(tmp_path, files):
    rc, out, *_ = _run(tmp_path, files, max_referrers=2)
    assert rc == 0
    assert out[0]["referrers"] == ["coppernest.org", "lanternfield.io"]
    assert out[0]["referrers_total"] == 4


def test_run_caps_rdap_checks_highest_count_first(tmp_path, files, caplog):
    caplog.set_level(logging.INFO, logger="scripts.rebuild_referrers")
    rc, out, s3, rdap, _ = _run(tmp_path, files, max_rdap_checks=1)
    assert rc == 0
    assert rdap.calls[0][0] == ["oldmill.com"]
    # the one checked is registered: nothing available, nothing downloaded
    assert out == []
    s3.download_file.assert_not_called()
    summary = _summary(caplog)
    assert summary["rdap_not_checked_cap"] == 2


def test_run_with_nothing_over_gate_skips_rdap_and_download(tmp_path, files):
    rc, out, s3, rdap, _ = _run(tmp_path, files, min_cc=1000)
    assert rc == 0
    assert out == []
    assert rdap.calls == []
    s3.download_file.assert_not_called()
    assert (tmp_path / "state" / "out.jsonl").read_text(encoding="utf-8") == ""


def test_run_falls_back_to_newest_retained_release(tmp_path, files):
    s3 = _fake_s3(files, keys=[
        f"cc/raw/{OLDER}/vertices.txt.gz",
        f"cc/raw/{OLDER}/edges.txt.gz",
    ])
    rc, out, _, _, sqlite_calls = _run(tmp_path, files, s3)
    assert rc == 0
    assert sqlite_calls == [OLDER]
    assert {o["release"] for o in out} == {OLDER}


def test_run_rejects_release_without_raw(tmp_path, files):
    rc, *_ = _run(tmp_path, files, release="cc-main-2026-may-jun-jul")
    assert rc == 1


def test_run_fails_when_no_release_retained(tmp_path, files):
    rc, *_ = _run(tmp_path, files, _fake_s3(files, keys=[]))
    assert rc == 1


def test_run_refuses_when_disk_too_small(tmp_path, files, monkeypatch):
    monkeypatch.setattr(
        rr.cc_refresh, "_ensure_disk_space",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("Insufficient disk space")),
    )
    s3 = _fake_s3(files)
    with pytest.raises(RuntimeError):
        _run(tmp_path, files, s3)
    s3.download_file.assert_not_called()


def test_r2_listing_error_propagates_as_failure(tmp_path, files):
    s3 = _fake_s3(files)
    s3.list_objects_v2.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2",
    )
    with pytest.raises(ClientError):
        _run(tmp_path, files, s3)


def test_check_available_uses_daily_rdap_path_with_its_own_budget(monkeypatch, clean_breaker_state):
    from scripts import pipeline

    seen = {}

    def fake_validate(candidates, config):
        seen["budget"] = config["availability_budget_seconds"]
        seen["names"] = [c["name"] for c in candidates]
        candidates[0]["is_available"] = True
        candidates[1]["is_available"] = False
        candidates[2]["is_available"] = None
        return [candidates[0]]

    monkeypatch.setattr(pipeline, "validate_availability", fake_validate)
    config = {"availability_budget_seconds": 7200, "api_min_interval_seconds": {"rdap": 2.0}}
    result = rr.check_available(["marketglow.com", "tideblock.net", "oldmill.com"], config, 900)
    assert result == {
        "available": ["marketglow.com"], "not_available": 1, "unknown": 1,
        "rate_limited": [],
    }
    assert seen == {"budget": 900, "names": ["marketglow.com", "tideblock.net", "oldmill.com"]}
    assert config["availability_budget_seconds"] == 7200  # caller's config untouched


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


def test_main_passes_cli_overrides_and_defaults_to_drops_input(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_config(tmp_path)), encoding="utf-8")
    seen = {}

    def fake_run(config, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(rr, "run", fake_run)
    assert rr.main(["--config", str(cfg_path), "--force", "--min-cc-domains", "5"]) == 0
    assert seen["min_cc_domains"] == 5
    assert seen["input_path"].name == "drops_latest.jsonl"
    assert seen["output_path"].name == "rebuild_referrers_latest.jsonl"


def test_main_returns_1_on_missing_input(tmp_path, files):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_config(tmp_path)), encoding="utf-8")
    rc = rr.main(
        ["--config", str(cfg_path), "--input", str(tmp_path / "missing.jsonl"), "--force"],
        s3_factory=lambda: (_fake_s3(files), "b"),
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# Protecting the morning run (2026-10-15 review)
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_breaker_state():
    from scripts.enrichment._circuit_breaker import (
        GLOBAL_HOST_COOLDOWN,
        GLOBAL_HOST_STOP,
        GLOBAL_HOST_STRIKES,
    )

    def _reset():
        GLOBAL_HOST_COOLDOWN.reset()
        GLOBAL_HOST_STOP.reset()
        GLOBAL_HOST_STRIKES.reset()

    _reset()
    yield
    _reset()


def test_check_available_stops_a_host_on_its_first_429(monkeypatch, clean_breaker_state):
    """Change 1: the rebuild pass runs with rdap_429_strike_limit = 1. Driven
    through the real 429 handler, one 429 stops the host."""
    from scripts import pipeline
    from scripts.enrichment import _circuit_breaker as cb

    seen = {}

    def fake_validate(candidates, config):
        seen["strike_limit"] = config["rdap_429_strike_limit"]
        resp = MagicMock(status_code=429, headers={"Retry-After": "86397"})
        cb.request_with_429_backoff(
            lambda: resp,
            host="rdap.identitydigital.example",
            retry_after_floor=5.0,
            strike_limit=config["rdap_429_strike_limit"],
            sleep=lambda _s: None,
        )
        for c in candidates:
            c["is_available"] = None
        return []

    monkeypatch.setattr(pipeline, "validate_availability", fake_validate)
    config = {"rdap_429_strike_limit": 3}
    rr.check_available(["marketglow.live"], config, 900)

    assert seen["strike_limit"] == 1
    assert config["rdap_429_strike_limit"] == 3  # the caller's config is untouched
    assert cb.GLOBAL_HOST_STOP.is_stopped("rdap.identitydigital.example")


def test_calibration_rdap_cap_is_50():
    """Change 2: at most 50 RDAP checks per calibration run, in config and as
    the code default."""
    config = json.loads((Path(rr.__file__).parent / "config.json").read_text(encoding="utf-8"))
    assert config["rebuild"]["max_rdap_checks"] == 50
    assert config["rebuild"]["daily_run_utc_hour"] == 9


def test_run_defaults_to_50_rdap_checks_when_config_omits_the_cap(tmp_path, files):
    many = {f"plainname{i:03d}.com": 30 for i in range(60)}
    conn = sqlite3.connect(files["sqlite"])
    conn.executemany("INSERT INTO cc_apex VALUES (?, ?)", list(many.items()))
    conn.commit()
    conn.close()
    cfg_rebuild = {k: v for k, v in _config(tmp_path)["rebuild"].items() if k != "max_rdap_checks"}
    rdap = _FakeRdap(registered=set(many) | {"oldmill.com", "marketglow.com", "tideblock.net"})
    input_path = tmp_path / "drops.jsonl"
    input_path.write_text("".join(json.dumps({"name": n}) + "\n" for n in [*DROPS, *many]), encoding="utf-8")
    config = {**_config(tmp_path, files), "rebuild": cfg_rebuild}
    rr.run(
        config, input_path=input_path, output_path=tmp_path / "out.jsonl",
        min_cc_domains=20, release=None,
        s3_factory=lambda: (_fake_s3(files), "b"),
        sqlite_path_fn=lambda *_a: files["sqlite"],
        denylist_fn=lambda *_a: set(), rdap_fn=rdap,
    )
    assert len(rdap.calls[0][0]) == 50


def test_check_available_reports_429_with_retry_after_and_403(monkeypatch, clean_breaker_state):
    """Change 3, capture: every pushing-back host comes back with its status
    and Retry-After, read from the real 429 handler's log record."""
    from scripts import pipeline
    from scripts.enrichment import _circuit_breaker as cb

    def fake_validate(candidates, config):
        resp = MagicMock(status_code=429, headers={"Retry-After": "86397"})
        cb.request_with_429_backoff(
            lambda: resp, host="rdap.identitydigital.example", retry_after_floor=5.0,
            strike_limit=config["rdap_429_strike_limit"], sleep=lambda _s: None,
        )
        cb.GLOBAL_HOST_STOP.stop("rdap.blocked.example", reason="403")
        return []

    monkeypatch.setattr(pipeline, "validate_availability", fake_validate)
    result = rr.check_available(["marketglow.live", "tideblock.shop"], {}, 900)
    assert result["rate_limited"] == [
        {"host": "rdap.identitydigital.example", "status": 429,
         "retry_after_header": "86397", "honored_cooldown_seconds": 86397},
        {"host": "rdap.blocked.example", "status": 403,
         "retry_after_header": None, "honored_cooldown_seconds": None},
    ]
    # the capture handler is removed again afterwards
    assert not any(
        isinstance(h, rr._RateLimitCapture)
        for h in logging.getLogger("scripts.enrichment._circuit_breaker").handlers
    )


def test_run_puts_pushback_in_summary_and_warns(tmp_path, files, caplog):
    """Change 3, reporting: the summary lists the host and Retry-After, and a
    WARNING says the cooldown outlasts the next daily run."""
    caplog.set_level(logging.INFO, logger="scripts.rebuild_referrers")
    event = {"host": "rdap.identitydigital.example", "status": 429,
             "retry_after_header": "86397", "honored_cooldown_seconds": 86397}

    def rdap(names, config, budget):
        return {"available": [], "not_available": 0, "unknown": len(names), "rate_limited": [event]}

    rc, out, *_ = _run(tmp_path, files, rdap=rdap)
    assert rc == 0 and out == []
    assert _summary(caplog)["rdap_rate_limited"] == [event]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "RDAP REBUILD PUSHBACK" in text
    assert "rdap.identitydigital.example" in text and "'86397'" in text
    assert "STILL ACTIVE at the next daily run" in text


def test_clean_rdap_pass_has_empty_pushback_and_no_warning(tmp_path, files, caplog):
    caplog.set_level(logging.INFO, logger="scripts.rebuild_referrers")
    rc, *_ = _run(tmp_path, files)
    assert rc == 0
    assert _summary(caplog)["rdap_rate_limited"] == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_pushback_warning_says_when_cooldown_ends_before_next_run(caplog):
    caplog.set_level(logging.WARNING, logger="scripts.rebuild_referrers")
    now = datetime(2026, 10, 16, 16, 30, tzinfo=timezone.utc)
    rr._warn_rate_limited(
        [{"host": "rdap.radix.example", "status": 429,
          "retry_after_header": "60", "honored_cooldown_seconds": 60},
         {"host": "rdap.blocked.example", "status": 403,
          "retry_after_header": None, "honored_cooldown_seconds": None}],
        {"rebuild": {"daily_run_utc_hour": 9}}, now=now,
    )
    messages = [r.getMessage() for r in caplog.records]
    assert "Ends before the next daily run (2026-10-17 09:00 UTC)" in messages[0]
    assert "HTTP 403" in messages[1] and "2026-10-17 09:00" in messages[1]
