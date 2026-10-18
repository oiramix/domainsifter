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
    """RDAP stand-in. `registered` maps a name to (status list, events);
    anything not listed is available (HTTP 404)."""

    def __init__(self, registered=None):
        self.registered = registered if registered is not None else {"oldmill.com": (["active"], [])}
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, names, config, budget):
        self.calls.append((list(names), budget))
        results = {}
        for n in names:
            if n in self.registered:
                status, events = self.registered[n]
                results[n] = {"is_available": False, "rdap_status": status, "registrar": "Acme Registrar",
                              "rdap_expiration": "2027-01-01", "events": events}
            else:
                results[n] = {"is_available": True, "rdap_status": [], "registrar": None,
                              "rdap_expiration": None, "events": []}
        avail = [n for n in names if n not in self.registered]
        return {"available": avail, "not_available": len(names) - len(avail), "unknown": 0,
                "results": results}


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
    # RDAP sees only abuse-clean candidates at or above the gate, by band
    # (20-49 first, highest count first within it), then 50-99
    assert rdap.calls == [(["marketglow.com", "tideblock.net", "oldmill.com"], 600.0)]
    today = rr.datetime.now(rr.timezone.utc).date().isoformat()
    assert out == [
        {
            "name": "marketglow.com",
            "cc_source_domain_count": 40,
            "release": LATEST,
            "bucket": "available",
            "registrar": None,
            "rdap_status": [],
            "expiration_date": None,
            "last_changed": None,
            "expected_release_date": today,
            "expected_release_basis": "rdap_404",
            "referrers_total": 4,
            # num_hosts desc, then name; self-link and duplicate edge dropped
            "referrers": ["coppernest.org", "lanternfield.io", "quietharbor.com", "saltmeadow.net"],
        },
        {
            "name": "tideblock.net",
            "cc_source_domain_count": 25,
            "release": LATEST,
            "bucket": "available",
            "registrar": None,
            "rdap_status": [],
            "expiration_date": None,
            "last_changed": None,
            "expected_release_date": today,
            "expected_release_basis": "rdap_404",
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
    assert summary["rdap_buckets"] == {"active": 1, "available": 2}
    assert summary["rdap_bands"]["20-49"] == {"eligible": 2, "selected": 2}
    assert summary["candidates"] == summary["written"] == 2


def test_run_caps_referrers_but_reports_total(tmp_path, files):
    rc, out, *_ = _run(tmp_path, files, max_referrers=2)
    assert rc == 0
    assert out[0]["referrers"] == ["coppernest.org", "lanternfield.io"]
    assert out[0]["referrers_total"] == 4


def test_run_caps_rdap_checks_first_band_highest_first(tmp_path, files, caplog):
    caplog.set_level(logging.INFO, logger="scripts.rebuild_referrers")
    rdap = _FakeRdap(registered={"marketglow.com": (["active"], [])})
    rc, out, s3, rdap, _ = _run(tmp_path, files, max_rdap_checks=1, rdap=rdap)
    assert rc == 0
    assert rdap.calls[0][0] == ["marketglow.com"]
    # the one checked is active: no candidate, nothing downloaded
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
        "results": {
            n: {"is_available": a, "rdap_http": None, "rdap_status": [], "registrar": None,
                "rdap_expiration": None, "events": []}
            for n, a in (("marketglow.com", True), ("tideblock.net", False), ("oldmill.com", None))
        },
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
    many = {f"plainname{i:03d}.com": 20 + i for i in range(60)}  # distinct counts: no cluster
    conn = sqlite3.connect(files["sqlite"])
    conn.executemany("INSERT INTO cc_apex VALUES (?, ?)", list(many.items()))
    conn.commit()
    conn.close()
    cfg_rebuild = {k: v for k, v in _config(tmp_path)["rebuild"].items() if k != "max_rdap_checks"}
    rdap = _FakeRdap(registered={n: (["active"], []) for n in [*many, "oldmill.com", "marketglow.com", "tideblock.net"]})
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


# ---------------------------------------------------------------------------
# 2026-10-18 revision: lifecycle buckets, clusters, gambling terms, bands
# ---------------------------------------------------------------------------

TODAY = rr.date(2026, 10, 19)
REAL_CONFIG = json.loads((Path(rr.__file__).parent / "config.json").read_text(encoding="utf-8"))


def _life(status, events=(), *, available=False, expiration=None):
    result = {"is_available": None if available is None else available,
              "rdap_status": list(status), "rdap_expiration": expiration, "events": list(events)}
    return rr.classify_lifecycle(result, TODAY, {"rebuild": {"lifecycle_days": {"pending_delete": 5, "redemption": 30}}})


# (a) lifecycle -------------------------------------------------------------


def test_lifecycle_available_releases_today():
    life = _life([], available=True)
    assert life["bucket"] == "available"
    assert life["expected_release_date"] == "2026-10-19"
    assert life["expected_release_basis"] == "rdap_404"


def test_lifecycle_redemption_estimates_35_days_from_last_change():
    # RDAP shows both flags during the redemption grace period
    life = _life(["redemption period", "pending delete"],
                 [{"eventAction": "last changed", "eventDate": "2026-10-10T08:00:00Z"}])
    assert life["bucket"] == "redemption"
    assert life["expected_release_date"] == "2026-11-14"
    assert life["expected_release_basis"] == "last_changed+35d"
    assert life["last_changed"] == "2026-10-10"
    assert _life(["pendingRestore"])["bucket"] == "redemption"


def test_lifecycle_pending_delete_uses_deletion_event_when_published():
    with_event = _life(["pending delete"], [{"action": "deletion", "date": "2026-10-21"},
                                            {"action": "last changed", "date": "2026-10-16"}])
    assert with_event["bucket"] == "pending_delete"
    assert (with_event["expected_release_date"], with_event["expected_release_basis"]) == ("2026-10-21", "deletion_event")
    estimated = _life(["pendingDelete"], [{"action": "last changed", "date": "2026-10-16"}])
    assert (estimated["expected_release_date"], estimated["expected_release_basis"]) == ("2026-10-21", "last_changed+5d")
    assert _life(["pending delete"])["expected_release_basis"] == "no_dated_event"


def test_lifecycle_hold_grace_active_unknown():
    assert _life(["client hold"])["bucket"] == "registrar_hold_or_grace"
    assert _life(["auto renew period", "client transfer prohibited"])["bucket"] == "registrar_hold_or_grace"
    assert _life(["client transfer prohibited"], expiration="2026-09-30")["bucket"] == "registrar_hold_or_grace"
    active = _life(["client transfer prohibited"], expiration="2027-09-30")
    assert active["bucket"] == "active" and active["expected_release_date"] is None
    assert _life([], available=None)["bucket"] == "unknown"


def test_capture_rdap_records_keeps_200_bodies_and_restores(monkeypatch):
    from scripts.enrichment import rdap

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    original = lambda url, host, timeout, config: (  # noqa: E731
        Resp(200, {"events": [{"eventAction": "last changed", "eventDate": "2026-10-10"}]})
        if url.lower().endswith("marketglow.com") else Resp(404, None))
    monkeypatch.setattr(rdap, "_rdap_get", original)
    with rr._capture_rdap_records() as records:
        assert rdap._rdap_get is not original
        rdap._rdap_get("https://rdap.example/domain/MarketGlow.com", "rdap.example", 10, {})
        rdap._rdap_get("https://rdap.example/domain/tideblock.net", "rdap.example", 10, {})
    assert rdap._rdap_get is original
    assert list(records) == ["marketglow.com"]


def test_check_available_returns_events_from_raw_records(monkeypatch, clean_breaker_state):
    from scripts import pipeline
    from scripts.enrichment import rdap

    class Resp:
        status_code = 200

        def json(self):
            return {"events": [{"eventAction": "last changed", "eventDate": "2026-10-10T00:00:00Z"}]}

    monkeypatch.setattr(rdap, "_rdap_get", lambda *a: Resp())

    def fake_validate(candidates, config):
        for c in candidates:
            rdap._rdap_get(f"https://rdap.example/domain/{c['name']}", "rdap.example", 10, config)
            c.update(is_available=False, rdap_status=["redemption period"], previous_registrar="Acme Registrar",
                     rdap_expiration="2026-09-01", rdap_http=200)
        return []

    monkeypatch.setattr(pipeline, "validate_availability", fake_validate)
    result = rr.check_available(["tideblock.net"], {}, 900)
    assert result["results"]["tideblock.net"] == {
        "is_available": False, "rdap_http": 200, "rdap_status": ["redemption period"],
        "registrar": "Acme Registrar", "rdap_expiration": "2026-09-01",
        "events": [{"action": "last changed", "date": "2026-10-10"}],
    }


def test_run_keeps_redemption_and_pending_delete_as_candidates(tmp_path, files, caplog):
    caplog.set_level(logging.INFO, logger="scripts.rebuild_referrers")
    rdap = _FakeRdap(registered={
        "marketglow.com": (["redemption period", "pending delete"],
                           [{"action": "last changed", "date": "2026-10-10"}]),
        "tideblock.net": (["pending delete"], [{"action": "deletion", "date": "2026-10-23"}]),
        "oldmill.com": (["client hold"], []),
    })
    rc, out, s3, _, _ = _run(tmp_path, files, rdap=rdap)
    assert rc == 0
    assert [(o["name"], o["bucket"], o["expected_release_date"], o["registrar"]) for o in out] == [
        ("tideblock.net", "pending_delete", "2026-10-23", "Acme Registrar"),
        ("marketglow.com", "redemption", "2026-11-14", "Acme Registrar"),
    ]
    assert out[1]["referrers"][:2] == ["coppernest.org", "lanternfield.io"]
    s3.download_file.assert_called()
    rdap_rows = [json.loads(line) for line in
                 (tmp_path / "state" / "rebuild_rdap_latest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["name"]: r["bucket"] for r in rdap_rows} == {
        "marketglow.com": "redemption", "tideblock.net": "pending_delete", "oldmill.com": "registrar_hold_or_grace",
    }
    assert {r["name"]: r["band"] for r in rdap_rows}["oldmill.com"] == "50-99"
    summary = _summary(caplog)
    assert summary["rdap_buckets"] == {"pending_delete": 1, "redemption": 1, "registrar_hold_or_grace": 1}
    assert summary["candidates"] == 2


# (b) clusters --------------------------------------------------------------


def test_collapse_clusters_keeps_one_of_three_or_more_identical_counts():
    cands = {"kadinb.org": 1549, "kadina.org": 1549, "kadinc.org": 1549,
             "pairone.com": 77, "pairtwo.com": 77, "marketglow.com": 40}
    kept, clusters = rr.collapse_clusters(cands, 3)
    assert kept == {"kadina.org": 1549, "pairone.com": 77, "pairtwo.com": 77, "marketglow.com": 40}
    assert clusters == [{"cc_source_domain_count": 1549, "size": 3, "kept": "kadina.org",
                         "sample": ["kadina.org", "kadinb.org", "kadinc.org"]}]
    assert rr.collapse_clusters(cands, 0)[0] == cands


def test_run_collapses_clusters_and_logs_them(tmp_path, files, caplog):
    caplog.set_level(logging.INFO, logger="scripts.rebuild_referrers")
    net = {f"kadinnetwork{i}.org": 1549 for i in range(4)}
    conn = sqlite3.connect(files["sqlite"])
    conn.executemany("INSERT INTO cc_apex VALUES (?, ?)", list(net.items()))
    conn.commit()
    conn.close()
    rc, *_ = _run(tmp_path, files, drops=[*DROPS, *net])
    assert rc == 0
    summary = _summary(caplog)
    assert summary["clusters"] == [{"cc_source_domain_count": 1549, "size": 4, "kept": "kadinnetwork0.org"}]
    assert summary["cluster_members_dropped"] == 3
    assert any("Cluster: 4 candidates share cc_source_domain_count=1549" in r.getMessage() for r in caplog.records)


# (c) rebuild-only gambling terms -------------------------------------------


@pytest.mark.parametrize("name,term", [
    ("bingoireland.org", "bingo"), ("merkurslots.org", "slots"), ("royalcasinohall.com", "casino"),
    ("megajackpot.net", "jackpot"), ("pokerpalace.com", "poker"), ("sportbetting.info", "betting"),
    ("slot-machine.com", "slot"), ("bet-365.com", "bet"), ("spin79-top.com", "spin"),
    ("voodoo-wins.org", "wins"), ("kasinoparadise.com", "kasino"),
])
def test_extra_terms_reject_gambling_names(tmp_path, name, term):
    assert rr.matched_extra_term(name, REAL_CONFIG) == term


@pytest.mark.parametrize("name", [
    "twins.org", "voodoowins.org", "alphabet.com", "better-homes.com", "spinach.com",
    "slothrescue.org", "kevin-de-bruyne-az.com",
])
def test_extra_terms_leave_innocent_names_alone(name):
    assert rr.matched_extra_term(name, REAL_CONFIG) is None


def test_extra_terms_are_rebuild_only_and_reported(tmp_path, files, caplog):
    caplog.set_level(logging.INFO, logger="scripts.rebuild_referrers")
    conn = sqlite3.connect(files["sqlite"])
    conn.execute("INSERT INTO cc_apex VALUES ('bingoireland.org', 1413)")
    conn.commit()
    conn.close()
    rc, *_ = _run(tmp_path, files, drops=[*DROPS, "bingoireland.org"],
                  extra_rejected_terms=REAL_CONFIG["rebuild"]["extra_rejected_terms"])
    assert rc == 0
    assert _summary(caplog)["abuse_rejections"]["extra_rejected_term"] == 1
    # the daily keyword lists are untouched
    assert "bingo" not in REAL_CONFIG["rejected_keywords"]
    assert "bingo" not in REAL_CONFIG["rejected_keyword_substrings"]


# (d) bands -----------------------------------------------------------------


BANDS = [[20, 49], [50, 99], [100, 499], [500, None]]


def test_select_by_band_equal_shares_highest_first():
    cands = {f"b1n{i}.com": 20 + i for i in range(20)}
    cands.update({f"b2n{i}.com": 50 + i for i in range(20)})
    cands.update({f"b3n{i}.com": 100 + i for i in range(20)})
    cands.update({f"b4n{i}.com": 500 + i for i in range(20)})
    picked, stats = rr.select_by_band(cands, BANDS, 50)
    assert stats == {"20-49": {"eligible": 20, "selected": 13}, "50-99": {"eligible": 20, "selected": 13},
                     "100-499": {"eligible": 20, "selected": 12}, "500+": {"eligible": 20, "selected": 12}}
    assert picked[:2] == ["b1n19.com", "b1n18.com"]
    assert len(picked) == 50


def test_select_by_band_gives_unused_share_to_other_bands():
    cands = {"onlyhigh.com": 900, **{f"low{i}.com": 20 + i for i in range(30)}}
    picked, stats = rr.select_by_band(cands, BANDS, 10)
    assert stats["500+"] == {"eligible": 1, "selected": 1}
    assert stats["20-49"] == {"eligible": 30, "selected": 9}
    assert len(picked) == 10
    assert rr.select_by_band({}, BANDS, 50) == ([], {
        "20-49": {"eligible": 0, "selected": 0}, "50-99": {"eligible": 0, "selected": 0},
        "100-499": {"eligible": 0, "selected": 0}, "500+": {"eligible": 0, "selected": 0}})


def test_real_config_has_the_revision_settings():
    rb = REAL_CONFIG["rebuild"]
    assert rb["max_rdap_checks"] == 50 and rb["cluster_min_size"] == 3
    assert rb["rdap_bands"] == BANDS
    assert rb["lifecycle_days"] == {"pending_delete": 5, "redemption": 30}
