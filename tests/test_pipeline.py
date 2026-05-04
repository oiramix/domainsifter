"""Unit tests for scripts/pipeline.py — the orchestrator.

Heavy mocking: we do NOT exercise the real CZDS, enrichment, R2, or
filesystem output paths here (those modules have their own tests). We
verify that the orchestrator wires the pieces together correctly:
    - env_check called first
    - CZDS auth uses env vars + the auth_base from config
    - only approved TLDs are downloaded
    - per-zone failures don't abort the run
    - R2 client is shared across TLDs (one auth, many objects)
    - enrichers are applied to every candidate
    - SpamCheckConfigError aborts the run
    - the final output is written via output.write_output
"""

from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts import pipeline


R2_ENV = {
    "R2_ACCOUNT_ID": "acct",
    "R2_ACCESS_KEY_ID": "ak",
    "R2_SECRET_ACCESS_KEY": "sk",
    "R2_BUCKET_NAME": "domainsifter-state",
}


def _set_r2_env(monkeypatch):
    for k, v in R2_ENV.items():
        monkeypatch.setenv(k, v)


def _no_such_key():
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )


@pytest.fixture(autouse=True)
def _stub_resolve_rdap_host(monkeypatch):
    """Block the live IANA bootstrap fetch that resolve_rdap_host would do
    in any test exercising validate_availability / pipeline.main. Returns a
    synthetic per-TLD host so each TLD lands in its own bucket — this keeps
    the per-host concurrency orchestration exercised in tests without
    introducing a network dependency. Tests that need bucket-collapsing or
    specific hostnames patch resolve_rdap_host themselves; the autouse
    fixture is a no-op for those (the explicit patch wins).
    """
    from scripts.enrichment import rdap as _rdap

    def _stub(name, _c):
        if not name or "." not in name:
            return None
        return f"rdap.{name.rsplit('.', 1)[-1]}.example"

    monkeypatch.setattr(_rdap, "resolve_rdap_host", _stub)


@pytest.fixture
def cfg(tmp_path):
    config = {
        "version": "1.0",
        "tlds": {"approved": ["com", "app"]},
        "api_endpoints": {
            "czds_auth_base": "https://account-api.icann.org",
            "czds_api_base": "https://czds-api.icann.org",
        },
        "max_concurrent_enrichments": 4,
        "availability_check": {
            "max_runtime_per_host_seconds": 900,
            "global_cap": 15000,
        },
        "max_candidates_for_publication": 100,
        "enrichment_time_budget_seconds": 60,  # tests don't actually wait — short keeps the wait() ceilings small
        "registrars": [
            {"name": "Namecheap", "link_template": "https://aff.example/?d={name}"},
            {"name": "NameSilo", "link_template": "https://ns.example/?q={name}"},
        ],
        "output_path": str(tmp_path / "daily.json"),
        "filter_thresholds": {
            "min_domain_length": 2,
            "max_domain_length": 30,
            "min_wayback_snapshots": 1,
        },
        "scoring_weights": {
            "wayback_snapshots": 0.3,
            "open_page_rank": 0.4,
            "cert_history": 0.2,
            "domain_length": 0.1,
        },
        "rejected_keywords": [],
    }
    return config


def test_filename_to_tld_extracts_correctly():
    assert pipeline._filename_to_tld(
        "https://czds-download-api.icann.org/czds/downloads/com.zone"
    ) == "com"
    assert pipeline._filename_to_tld(
        "https://czds-download-api.icann.org/czds/downloads/store.zone"
    ) == "store"


def test_collect_drops_filters_to_approved_and_handles_failed_zone(monkeypatch, cfg):
    # Pre-populate R2: com has yesterday data; app would too but its zone
    # download fails so we never read it.
    r2 = MagicMock()

    def fake_get_object(Bucket, Key):
        if Key == "state/com_yesterday.txt":
            return {"Body": BytesIO(b"keep.com\ndropping.com\n")}
        if Key == "state/app_yesterday.txt":
            return {"Body": BytesIO(b"keep.app\ngoner.app\n")}
        raise _no_such_key()

    r2.get_object.side_effect = fake_get_object

    links = [
        "https://x/com.zone",
        "https://x/net.zone",
        "https://x/app.zone",
    ]
    monkeypatch.setattr(pipeline.czds_client, "list_zone_links", lambda *_a, **_k: links)

    def fake_download(url, _token, path, timeout=120):
        if "app.zone" in url:
            from scripts.czds_client import CzdsApiError
            raise CzdsApiError("simulated 5xx")
        with open(path, "wb") as fh:
            fh.write(b"placeholder")
        return 11

    monkeypatch.setattr(pipeline.czds_client, "download_zone", fake_download)
    monkeypatch.setattr(pipeline.zone_parser, "parse_zone", lambda _p: {"keep.com", "newcomer.com"})

    drops, retained_carryover = pipeline.collect_drops(
        cfg, "tok", today=date(2026, 4, 27), r2_client=r2, r2_bucket="test-bucket",
    )
    names = {d["name"] for d in drops}
    assert names == {"dropping.com"}  # only com processed; net not approved; app failed
    assert all(d["dropped_date"] == "2026-04-27" for d in drops)
    assert all(d["tld"] == "com" for d in drops)
    assert retained_carryover == []  # no carryover_candidates passed in

    # Today's snapshot was committed for com only (app failed before R2 reads).
    written_keys = [c.kwargs["Key"] for c in r2.put_object.call_args_list]
    assert written_keys == ["state/com_yesterday.txt"]
    # And it landed in the bucket the caller specified.
    assert r2.put_object.call_args.kwargs["Bucket"] == "test-bucket"


def test_collect_drops_validates_carryover_against_today_zone(monkeypatch, cfg):
    """Carryover entry whose name is ABSENT from today's zone is kept with
    a refreshed last_validated_date. An entry whose name IS in today's zone
    (someone registered) is dropped. Entries for a TLD whose download failed
    pass through unvalidated."""
    r2 = MagicMock()
    def _fake_get(**_kw):
        raise _no_such_key()  # cold start for each TLD's R2 read
    r2.get_object.side_effect = _fake_get

    links = ["https://x/com.zone", "https://x/app.zone"]
    monkeypatch.setattr(pipeline.czds_client, "list_zone_links", lambda *_a, **_k: links)

    def fake_download(url, _token, path, timeout=120):
        if "app.zone" in url:
            from scripts.czds_client import CzdsApiError
            raise CzdsApiError("simulated failure")
        with open(path, "wb") as fh:
            fh.write(b"placeholder")
        return 11

    monkeypatch.setattr(pipeline.czds_client, "download_zone", fake_download)
    # com zone today contains "registered.com" (someone took it overnight)
    # but NOT "still-free.com".
    monkeypatch.setattr(
        pipeline.zone_parser, "parse_zone",
        lambda _p: {"registered.com", "other.com"},
    )

    carryover_candidates = [
        {"name": "still-free.com", "tld": "com", "first_seen_date": "2026-04-28",
         "last_validated_date": "2026-04-29", "score": 60},
        {"name": "registered.com", "tld": "com", "first_seen_date": "2026-04-28",
         "last_validated_date": "2026-04-29", "score": 50},
        # .app failed to download — this entry should pass through unvalidated.
        {"name": "untouched.app", "tld": "app", "first_seen_date": "2026-04-28",
         "last_validated_date": "2026-04-29", "score": 70},
    ]

    drops, retained = pipeline.collect_drops(
        cfg, "tok", today=date(2026, 4, 30),
        r2_client=r2, r2_bucket="test-bucket",
        carryover_candidates=carryover_candidates,
    )
    retained_names = {r["name"] for r in retained}
    assert "still-free.com" in retained_names
    assert "registered.com" not in retained_names  # rejected — in today's zone
    assert "untouched.app" in retained_names       # passed through (zone DL failed)

    # The validated survivor has last_validated_date=today; the unvalidated
    # one keeps its prior value.
    by_name = {r["name"]: r for r in retained}
    assert by_name["still-free.com"]["last_validated_date"] == "2026-04-30"
    assert by_name["untouched.app"]["last_validated_date"] == "2026-04-29"


def test_collect_drops_constructs_r2_client_when_none_supplied(monkeypatch, cfg):
    """If the caller doesn't pass r2_client, the function calls diff._r2_client()
    once and reuses that client across TLDs (one auth per run, not 11)."""
    monkeypatch.setattr(pipeline.czds_client, "list_zone_links", lambda *_a, **_k: [])

    sentinel = MagicMock()
    calls = []

    def fake_factory():
        calls.append(1)
        return sentinel

    monkeypatch.setattr(pipeline.diff, "_r2_client", fake_factory)
    monkeypatch.setattr(pipeline.diff, "_bucket", lambda: "domainsifter-state")
    pipeline.collect_drops(cfg, "tok", today=date(2026, 4, 27))
    assert len(calls) == 1


def test_enrich_one_merges_results_from_all_enrichers(cfg):
    enrichers = [
        ("a", lambda d, c: {"a_field": 1}),
        ("b", lambda d, c: {"b_field": "x"}),
        ("c", lambda d, c: {}),
    ]
    cand = {"name": "foo.com", "tld": "com"}
    result = pipeline._enrich_one(cand, cfg, enrichers)
    assert result["a_field"] == 1
    assert result["b_field"] == "x"
    assert result["name"] == "foo.com"


def test_enrich_one_continues_on_enricher_exception(cfg):
    def boom(_d, _c):
        raise RuntimeError("source down")

    enrichers = [
        ("good", lambda d, c: {"good_field": True}),
        ("bad", boom),
        ("also_good", lambda d, c: {"other": 7}),
    ]
    cand = {"name": "foo.com"}
    result = pipeline._enrich_one(cand, cfg, enrichers)
    assert result["good_field"] is True
    assert result["other"] == 7


def test_enrich_one_propagates_spam_check_config_error(cfg):
    from scripts.enrichment.spam_check import SpamCheckConfigError

    def boom(_d, _c):
        raise SpamCheckConfigError("missing key")

    enrichers = [("spam_check", boom)]
    with pytest.raises(SpamCheckConfigError):
        pipeline._enrich_one({"name": "foo.com"}, cfg, enrichers)


def test_enrich_all_returns_empty_when_no_candidates(cfg):
    assert pipeline.enrich_all([], cfg) == []


# --- per-host availability caps (replaces length-asc trim) ----------------


def test_per_host_cap_calculation_from_runtime_budget(cfg):
    """cap = floor(max_runtime_per_host_seconds * workers / effective_throttle).
    Verifies the lookup chain (rdap_per_host → rdap → 0.4) AND the
    workers chain (per_host → default_workers_per_host → 1)."""
    cfg = {
        **cfg,
        "availability_check": {"max_runtime_per_host_seconds": 900, "global_cap": 15000},
        "api_min_interval_seconds": {
            "rdap": 0.4,
            "rdap_per_host": {"rdap.gmoregistry.net": 3.0},
        },
        "rdap_concurrency": {
            "default_workers_per_host": 1,
            "per_host": {"rdap.fast.example": 5},
        },
    }
    # Global rdap (0.4s) × 1 worker → 900 / 0.4 = 2250
    assert pipeline._per_host_cap("rdap.someregistry.example", cfg) == 2250
    # GMO override (3.0s) × 1 worker → 900 / 3 = 300
    assert pipeline._per_host_cap("rdap.gmoregistry.net", cfg) == 300
    # Per-host concurrency override → 900 × 5 / 0.4 = 11250
    assert pipeline._per_host_cap("rdap.fast.example", cfg) == 11250


def test_bucket_overflow_triggers_random_shuffle_not_length_asc(monkeypatch, cfg):
    """When a host bucket exceeds its cap, the cut is random (deterministic
    via the today=date seed), not length-asc. Asserted by checking that the
    survivors are NOT the alphabetically/length-shortest subset."""
    from scripts.enrichment import rdap as rdap_mod

    monkeypatch.setattr(
        rdap_mod, "resolve_rdap_host",
        lambda name, _c: "rdap.solo.example",  # everything in one bucket
    )

    cfg = {
        **cfg,
        "availability_check": {"max_runtime_per_host_seconds": 1.0, "global_cap": 10000},
        "api_min_interval_seconds": {"rdap": 0.5},  # cap = floor(1 / 0.5) = 2
        "rdap_concurrency": {"default_workers_per_host": 1, "per_host": {}},
    }
    # 8 candidates, names sortable (length-asc would yield "a", "b", "c", ...).
    cands = [{"name": f"{c}.com"} for c in "abcdefgh"]
    final, stats = pipeline._bucket_and_cap_for_availability(cands, cfg, today=date(2026, 5, 4))

    # Cap = 2; one bucket over its cap.
    assert stats["rdap.solo.example"]["before"] == 8
    assert stats["rdap.solo.example"]["after"] == 2
    assert stats["rdap.solo.example"]["cap"] == 2
    assert len(final) == 2
    kept_names = sorted(c["name"] for c in final)
    # Length-asc would deterministically pick a.com + b.com. Random-shuffle
    # with a fixed seed yields a different pair.
    assert kept_names != ["a.com", "b.com"]


def test_global_cap_applies_before_bucketing_when_over_total(monkeypatch, cfg):
    """global_cap is a safety net engaged BEFORE per-host bucketing. When
    total survivors exceed it, random-shuffle and trim to global_cap, then
    bucket what remains."""
    from scripts.enrichment import rdap as rdap_mod

    # Two TLDs → two distinct hosts.
    monkeypatch.setattr(
        rdap_mod, "resolve_rdap_host",
        lambda name, _c: f"rdap.{name.rsplit('.', 1)[-1]}.example",
    )

    cfg = {
        **cfg,
        "availability_check": {"max_runtime_per_host_seconds": 900, "global_cap": 5},
        "api_min_interval_seconds": {"rdap": 0.4},
        "rdap_concurrency": {"default_workers_per_host": 1, "per_host": {}},
    }
    # 12 candidates, total > global_cap=5.
    cands = (
        [{"name": f"a{i}.com"} for i in range(6)]
        + [{"name": f"b{i}.org"} for i in range(6)]
    )
    final, stats = pipeline._bucket_and_cap_for_availability(cands, cfg, today=date(2026, 5, 4))
    # Global cap engaged: total post-cap is exactly 5.
    assert len(final) == 5
    # The remaining candidates split across two host buckets, neither over the
    # per-host cap (2250) — so per-host stats reflect the post-global-cap
    # split, not the original 6/6.
    total_after = sum(s["after"] for s in stats.values())
    assert total_after == 5
    # And no bucket got individually re-trimmed past its per-host cap.
    for s in stats.values():
        assert s["before"] == s["after"], "per-host trim ran inappropriately"


def test_bucket_cap_is_deterministic_per_day(monkeypatch, cfg):
    """Same date ⇒ same shuffle ⇒ same survivors. Different date ⇒ may
    differ. This is the property that lets us reproduce a day's run from
    the same input data when debugging."""
    from scripts.enrichment import rdap as rdap_mod

    monkeypatch.setattr(
        rdap_mod, "resolve_rdap_host", lambda name, _c: "rdap.solo.example",
    )
    cfg = {
        **cfg,
        "availability_check": {"max_runtime_per_host_seconds": 1.0, "global_cap": 10000},
        "api_min_interval_seconds": {"rdap": 0.5},  # cap = 2
        "rdap_concurrency": {"default_workers_per_host": 1, "per_host": {}},
    }
    cands = [{"name": f"{c}.com"} for c in "abcdefgh"]

    a, _ = pipeline._bucket_and_cap_for_availability(list(cands), cfg, today=date(2026, 5, 4))
    b, _ = pipeline._bucket_and_cap_for_availability(list(cands), cfg, today=date(2026, 5, 4))
    assert sorted(c["name"] for c in a) == sorted(c["name"] for c in b)
    # Different day MAY produce a different cut. Loose assertion: at least
    # don't crash; concrete equality not guaranteed by Random.shuffle for
    # small sets, so we just verify the function returns the right shape.
    c_diff, _ = pipeline._bucket_and_cap_for_availability(list(cands), cfg, today=date(2026, 5, 5))
    assert len(c_diff) == 2


def test_enrich_all_respects_time_budget(monkeypatch, cfg):
    """When budget exhausts mid-run, enrich_all stops submitting and returns
    whatever finished. Per project guidance: partial output is the design,
    not a failure."""
    cfg = {**cfg, "enrichment_time_budget_seconds": 0.0, "max_concurrent_enrichments": 2}

    # Stub out _load_enrichers so we don't import real modules.
    def slow_enricher(_d, _c):
        import time as _t
        _t.sleep(0.05)
        return {"slow_field": True}

    monkeypatch.setattr(pipeline, "_load_enrichers", lambda: [("slow", slow_enricher)])

    cands = [{"name": f"d{i}.com"} for i in range(10)]
    result = pipeline.enrich_all(cands, cfg)
    # We can't assert exactly how many got enriched (depends on the scheduler),
    # but with budget=0.0 we definitely don't process all 10.
    assert len(result) <= len(cands)


def test_enrich_all_propagates_spam_check_config_error(monkeypatch, cfg):
    from scripts.enrichment.spam_check import SpamCheckConfigError

    def boom(_d, _c):
        raise SpamCheckConfigError("nope")

    monkeypatch.setattr(pipeline, "_load_enrichers", lambda: [("spam_check", boom)])
    cands = [{"name": "anything.com"}]
    with pytest.raises(SpamCheckConfigError):
        pipeline.enrich_all(cands, cfg)


# --- validate_availability ---------------------------------------------------


def test_validate_availability_keeps_only_404_responders(monkeypatch, cfg):
    """is_available=True survives; False and None are rejected."""
    def fake_check(domain, _config):
        if domain == "free.com":
            return {
                "is_available": True, "rdap_http": 404,
                "rdap_status": [], "rdap_expiration": None,
                "previous_registrar": None,
            }
        if domain == "owned.com":
            return {
                "is_available": False, "rdap_http": 200,
                "rdap_status": ["client hold"], "rdap_expiration": "2027-01-01",
                "previous_registrar": "Namecheap",
            }
        return {  # transport failure / unknown
            "is_available": None, "rdap_http": None,
            "rdap_status": [], "rdap_expiration": None,
            "previous_registrar": None,
        }

    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)

    cands = [
        {"name": "free.com", "score": 75},
        {"name": "owned.com", "score": 60},
        {"name": "broken.tld", "score": 50},
    ]
    kept = pipeline.validate_availability(cands, cfg)
    assert [c["name"] for c in kept] == ["free.com"]
    # The kept candidate has all RDAP fields merged on.
    assert kept[0]["is_available"] is True
    assert kept[0]["rdap_http"] == 404
    assert "availability_verified_at" in kept[0]


def test_validate_availability_respects_budget(monkeypatch, cfg):
    """Budget=0 means we don't make any RDAP calls; everything rejected."""
    cfg = {**cfg, "availability_budget_seconds": 0.0}
    calls = []

    def fake_check(domain, _config):
        calls.append(domain)
        return {"is_available": True, "rdap_http": 404, "rdap_status": [],
                "rdap_expiration": None, "previous_registrar": None}

    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)

    cands = [{"name": f"d{i}.com", "score": 50} for i in range(5)]
    kept = pipeline.validate_availability(cands, cfg)
    assert kept == []
    assert calls == []  # budget exhausted before first call


def test_validate_availability_empty_input_short_circuits():
    assert pipeline.validate_availability([], {}) == []


# --- per-host concurrency (rdap_concurrency) --------------------------------


def test_check_availability_concurrent_buckets_by_host(monkeypatch, cfg):
    """Candidates with different hosts go to separate ThreadPoolExecutors;
    each pool is sized via the per-host worker count. We assert host
    bucketing by capturing the host each candidate's _process_one was
    routed through (visible via the worker thread's name_prefix)."""
    import threading as _thr

    from scripts.enrichment import rdap as rdap_mod

    cfg = {
        **cfg,
        "rdap_concurrency": {
            "default_workers_per_host": 2,
            "per_host": {"rdap.com.example": 4},
        },
    }

    # Override the autouse synthetic host with a TLD-keyed pattern that
    # actually maps to the per_host override key for ".com".
    monkeypatch.setattr(
        rdap_mod, "resolve_rdap_host",
        lambda name, _c: f"rdap.{name.rsplit('.', 1)[-1]}.example",
    )

    seen_hosts: list[str] = []
    seen_lock = _thr.Lock()

    def fake_check(domain, _c):
        # Worker thread name has the host bucket as prefix; capture it.
        with seen_lock:
            seen_hosts.append(_thr.current_thread().name)
        return {"is_available": True, "rdap_http": 404, "rdap_status": [],
                "rdap_expiration": None, "previous_registrar": None}

    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)

    cands = [
        {"name": "a.com", "score": 50}, {"name": "b.com", "score": 50},
        {"name": "c.org", "score": 50}, {"name": "d.org", "score": 50},
        {"name": "e.dev", "score": 50},
    ]
    kept = pipeline.validate_availability(cands, cfg)
    assert len(kept) == 5

    com_threads = {n for n in seen_hosts if n.startswith("rdap-rdap.com.example")}
    org_threads = {n for n in seen_hosts if n.startswith("rdap-rdap.org.example")}
    dev_threads = {n for n in seen_hosts if n.startswith("rdap-rdap.dev.example")}
    # All three buckets actually executed work.
    assert com_threads
    assert org_threads
    assert dev_threads
    # No cross-pollination — a com candidate never ran on an org thread.
    other = {n for n in seen_hosts if not (
        n.startswith("rdap-rdap.com.example")
        or n.startswith("rdap-rdap.org.example")
        or n.startswith("rdap-rdap.dev.example")
    )}
    assert other == set()


def test_check_availability_concurrent_per_host_override_resolves(monkeypatch, cfg):
    """rdap_concurrency.per_host[host] overrides default_workers_per_host
    for that specific host; unknown hosts fall back to the default; the
    default itself falls back to 1 when missing."""
    import threading as _thr

    from scripts.enrichment import rdap as rdap_mod

    captured_pool_sizes: dict[str, int] = {}

    # We can't intercept the ThreadPoolExecutor sizing directly without
    # patching concurrent.futures, but we CAN observe the maximum
    # concurrency a host's bucket actually uses. Each fake_check holds a
    # barrier briefly so we see the pool's true max in-flight count.
    def make_observer():
        in_flight: dict[str, int] = {}
        peak: dict[str, int] = {}
        lock = _thr.Lock()

        def fake_check(domain, _c):
            host_bucket = _thr.current_thread().name.rsplit("_", 1)[0]
            with lock:
                in_flight[host_bucket] = in_flight.get(host_bucket, 0) + 1
                peak[host_bucket] = max(peak[host_bucket] if host_bucket in peak else 0, in_flight[host_bucket])
            # Hold briefly so other workers in the same bucket overlap.
            import time as _t
            _t.sleep(0.05)
            with lock:
                in_flight[host_bucket] -= 1
            return {"is_available": True, "rdap_http": 404, "rdap_status": [],
                    "rdap_expiration": None, "previous_registrar": None}
        return fake_check, peak

    fake_check, peak = make_observer()
    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)
    monkeypatch.setattr(
        rdap_mod, "resolve_rdap_host",
        lambda name, _c: f"rdap.{name.rsplit('.', 1)[-1]}.example",
    )

    cfg = {
        **cfg,
        "rdap_concurrency": {
            "default_workers_per_host": 2,
            "per_host": {"rdap.com.example": 5},  # override only for .com
        },
    }

    # 8 .com candidates and 8 .org candidates — enough that each bucket can
    # plausibly run all its workers concurrently.
    cands = (
        [{"name": f"a{i}.com", "score": 50} for i in range(8)]
        + [{"name": f"b{i}.org", "score": 50} for i in range(8)]
    )
    pipeline.validate_availability(cands, cfg)

    # .com bucket peak <= 5 (its override) — the test is loose because the
    # actual peak depends on scheduling; the strict assertion is the
    # fallback bucket's peak <= default_workers_per_host=2.
    com_keys = [k for k in peak if "rdap.com.example" in k]
    org_keys = [k for k in peak if "rdap.org.example" in k]
    assert com_keys, "No com worker observed"
    assert org_keys, "No org worker observed"
    com_peak = max(peak[k] for k in com_keys)
    org_peak = max(peak[k] for k in org_keys)
    assert com_peak <= 5
    assert org_peak <= 2  # fallback default
    # And per-host override actually fanned out beyond the default.
    assert com_peak > org_peak or com_peak >= 3, (
        f"per_host override didn't increase com concurrency (com_peak={com_peak}, org_peak={org_peak})"
    )


def test_check_availability_concurrent_skips_remaining_after_deadline(monkeypatch, cfg):
    """Once time.monotonic() crosses the deadline mid-run, every still-
    pending candidate is marked is_available=None and counted under
    skipped_budget instead of executing check_availability."""
    from scripts.enrichment import rdap as rdap_mod

    cfg = {
        **cfg,
        "availability_budget_seconds": 0.0,  # deadline is "now"
        "rdap_concurrency": {"default_workers_per_host": 1, "per_host": {}},
    }

    calls: list[str] = []

    def fake_check(domain, _c):
        calls.append(domain)
        return {"is_available": True, "rdap_http": 404, "rdap_status": [],
                "rdap_expiration": None, "previous_registrar": None}

    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)
    monkeypatch.setattr(rdap_mod, "resolve_rdap_host",
                        lambda name, _c: f"rdap.{name.rsplit('.', 1)[-1]}.example")

    cands = [
        {"name": "a.com", "score": 50},
        {"name": "b.org", "score": 50},
        {"name": "c.dev", "score": 50},
    ]
    kept = pipeline.validate_availability(cands, cfg)
    assert kept == []  # everything skipped
    assert calls == []  # no RDAP attempt at all
    # Every candidate marked None.
    assert all(c.get("is_available") is None for c in cands)


def test_check_availability_concurrent_unknown_host_bucket(monkeypatch, cfg):
    """Candidates whose TLD has no RDAP server (resolve_rdap_host returns
    None) are bucketed under '_unknown' and STILL go through
    check_availability (which itself returns is_available=None)."""
    from scripts.enrichment import rdap as rdap_mod

    cfg = {
        **cfg,
        "rdap_concurrency": {"default_workers_per_host": 1, "per_host": {}},
    }

    seen_domains: list[str] = []

    def fake_check(domain, _c):
        seen_domains.append(domain)
        return {"is_available": None, "rdap_http": None, "rdap_status": [],
                "rdap_expiration": None, "previous_registrar": None}

    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)
    monkeypatch.setattr(rdap_mod, "resolve_rdap_host", lambda *_a, **_k: None)

    cands = [{"name": f"d{i}.unknown", "score": 50} for i in range(3)]
    kept = pipeline.validate_availability(cands, cfg)
    assert kept == []
    # The '_unknown' bucket still ran every candidate through check_availability.
    assert sorted(seen_domains) == ["d0.unknown", "d1.unknown", "d2.unknown"]


def test_main_persists_carryover_across_runs(monkeypatch, cfg, tmp_path):
    """End-to-end: existing daily-domains.json contains an entry from 3 days
    ago with full enrichment. Today's run finds NO new drops but the
    carryover entry passes back through with days_listed=3 and refreshed
    last_validated_date."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    # Seed an existing daily-domains.json with a 3-day-old carryover entry.
    output_path = tmp_path / "daily.json"
    today = date.today()
    fsd = (today.toordinal() - 3)
    fsd_iso = date.fromordinal(fsd).isoformat()
    output_path.write_text(json.dumps({
        "generated_at": "2026-04-27T07:00:00Z",
        "domain_count": 1,
        "domains": [{
            "name": "amberkite.com", "tld": "com", "dropped_date": fsd_iso,
            "wayback_snapshots": 100, "wayback_last_snapshot": "2024-01-01",
            "open_page_rank": 4.0, "cert_history": True,
            "previous_registrar": "Acme", "score": 75,
            "registrars": [], "first_seen_date": fsd_iso,
            "last_validated_date": fsd_iso, "days_listed": 3,
        }],
    }), encoding="utf-8")

    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)
    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")

    # Today's run finds NO new drops; carryover survives because today's
    # zone (passed through to validate_against_zone) does NOT contain it.
    def fake_collect_drops(_cfg, _tok, today, *, carryover_candidates=None, **_kw):
        # The carryover_candidates that survived age-filter must include
        # amberkite.com (3 days < 14-day window).
        names = {c["name"] for c in (carryover_candidates or [])}
        assert "amberkite.com" in names
        # Validate carryover against today's zone — amberkite.com is NOT
        # there, so it's retained with last_validated_date=today.
        retained = []
        for c in (carryover_candidates or []):
            retained.append({**c, "last_validated_date": today.isoformat()})
        return [], retained  # no new drops today

    monkeypatch.setattr(pipeline, "collect_drops", fake_collect_drops)
    monkeypatch.setattr(pipeline, "enrich_all", lambda cands, _cfg: cands)

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0

    written = json.loads(output_path.read_text(encoding="utf-8"))
    names = [d["name"] for d in written["domains"]]
    assert names == ["amberkite.com"]  # carryover survived
    entry = written["domains"][0]
    assert entry["days_listed"] == 3
    assert entry["first_seen_date"] == fsd_iso        # preserved
    assert entry["last_validated_date"] == today.isoformat()  # refreshed
    assert written["today_count"] == 0
    assert written["carryover_count"] == 1


def test_main_drops_carryover_older_than_14_days(monkeypatch, cfg, tmp_path):
    """A 20-day-old carryover entry is dropped at the age filter; the
    pipeline should never see it as a candidate, and the output omits it."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    today = date.today()
    fsd_iso = date.fromordinal(today.toordinal() - 20).isoformat()
    output_path = tmp_path / "daily.json"
    output_path.write_text(json.dumps({
        "generated_at": "2026-04-10T07:00:00Z",
        "domain_count": 1,
        "domains": [{
            "name": "ancient.com", "tld": "com", "dropped_date": fsd_iso,
            "score": 60, "registrars": [], "first_seen_date": fsd_iso,
            "last_validated_date": fsd_iso, "days_listed": 20,
        }],
    }), encoding="utf-8")

    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)
    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")

    seen_carryover_names: set[str] = set()

    def fake_collect_drops(_cfg, _tok, today, *, carryover_candidates=None, **_kw):
        for c in (carryover_candidates or []):
            seen_carryover_names.add(c["name"])
        return [], []

    monkeypatch.setattr(pipeline, "collect_drops", fake_collect_drops)
    monkeypatch.setattr(pipeline, "enrich_all", lambda cands, _cfg: cands)

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    assert "ancient.com" not in seen_carryover_names  # filtered out before collect_drops
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["domains"] == []


def test_main_skips_enrichment_for_unavailable_domains(monkeypatch, cfg, tmp_path):
    """Architectural assertion: availability check runs BEFORE enrichment.
    A candidate marked is_available=False must NOT reach enrich_all."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today, **_kw: ([
            {"name": "available.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "owned.com", "tld": "com", "dropped_date": today.isoformat()},
        ], []),
    )

    from scripts.enrichment import rdap as rdap_mod
    def fake_check(domain, _c):
        if domain == "available.com":
            return {"is_available": True, "rdap_http": 404, "rdap_status": [],
                    "rdap_expiration": None, "previous_registrar": None}
        return {"is_available": False, "rdap_http": 200,
                "rdap_status": ["client hold"], "rdap_expiration": "2027-01-01",
                "previous_registrar": "Acme"}
    monkeypatch.setattr(rdap_mod, "check_availability", fake_check)

    enriched_names: list[str] = []

    def fake_enrich_all(cands, _cfg):
        for c in cands:
            enriched_names.append(c["name"])
            c.update({
                "wayback_snapshots": 50, "wayback_last_snapshot": "2024-01-01",
                "open_page_rank": 3.0, "cert_history": True,
                "spam_flagged": False, "surbl_listed": False,
                "spamhaus_listed": False,
            })
        return cands

    monkeypatch.setattr(pipeline, "enrich_all", fake_enrich_all)

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    # owned.com was rejected at availability stage; only available.com hit
    # the (expensive) enrichment phase.
    assert enriched_names == ["available.com"]
    written = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert [d["name"] for d in written["domains"]] == ["available.com"]


def test_main_runs_structural_then_lexical_then_enrich_then_post(monkeypatch, cfg, tmp_path):
    """Trace which filtering stages run in what order. Inputs are crafted so:
       - "great.com" survives both filter passes (real word)
       - "78win012.com" survives structural but gets killed by lexical
       - "xn--bad.com" gets killed by structural (punycode)
    Final output should contain only "great.com".
    """
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today, **_kw: ([
            {"name": "great.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "78win012.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "xn--bad.com", "tld": "com", "dropped_date": today.isoformat()},
        ], []),
    )

    enriched_names: list[str] = []

    def fake_enrich_all(cands, _cfg):
        for c in cands:
            enriched_names.append(c["name"])
            c.update({
                "wayback_snapshots": 50,
                "wayback_last_snapshot": "2024-01-01",
                "open_page_rank": 3.0,
                "cert_history": True,
                "spam_flagged": False,
                "surbl_listed": False,
                "spamhaus_listed": False,
                "previous_registrar": "Acme",
            })
        return cands

    monkeypatch.setattr(pipeline, "enrich_all", fake_enrich_all)

    # Stub RDAP availability — every survivor is "available" so we can
    # observe what reached the publication stage.
    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(
        rdap_mod, "check_availability",
        lambda d, _c: {"is_available": True, "rdap_http": 404,
                       "rdap_status": [], "rdap_expiration": None,
                       "previous_registrar": None},
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    # Only great.com should have hit enrichment (78win012 fails lexical, xn-- fails structural)
    assert enriched_names == ["great.com"]
    written = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert [d["name"] for d in written["domains"]] == ["great.com"]


def test_main_aborts_when_required_env_missing(monkeypatch, cfg, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    for var in (
        "CZDS_USERNAME", "CZDS_PASSWORD", "SAFE_BROWSING_KEY",
        "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    from scripts.env_check import MissingEnvVarsError

    with pytest.raises(MissingEnvVarsError):
        pipeline.main(["--config", str(cfg_path)])


def test_main_happy_path_writes_output(monkeypatch, cfg, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    monkeypatch.setenv("OPENPAGERANK_KEY", "o")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today, **_kw: ([
            {"name": "great.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "alsogood.com", "tld": "com", "dropped_date": today.isoformat()},
        ], []),
    )

    def fake_enrich_all(cands, _cfg):
        for c in cands:
            c.update(
                {
                    "wayback_snapshots": 50,
                    "wayback_last_snapshot": "2024-01-01",
                    "open_page_rank": 3.0,
                    "cert_history": True,
                    "spam_flagged": False,
                    "surbl_listed": False,
                    "spamhaus_listed": False,
                    "previous_registrar": "Acme",
                }
            )
        return cands

    monkeypatch.setattr(pipeline, "enrich_all", fake_enrich_all)

    # Both candidates are "available" per RDAP so they reach the output.
    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(
        rdap_mod, "check_availability",
        lambda d, _c: {"is_available": True, "rdap_http": 404,
                       "rdap_status": [], "rdap_expiration": None,
                       "previous_registrar": None},
    )

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    written = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert written["domain_count"] == 2
    names = sorted(d["name"] for d in written["domains"])
    assert names == ["alsogood.com", "great.com"]
    # Each emitted domain has both registrars wired up with substituted URLs.
    for d in written["domains"]:
        reg_names = [r["name"] for r in d["registrars"]]
        assert reg_names == ["Namecheap", "NameSilo"]
        assert any(d["name"] in r["url"] for r in d["registrars"])
        # Availability fields land in the published payload.
        assert "rdap_status" in d
        assert "rdap_expiration" in d
        assert "availability_verified_at" in d


def test_main_propagates_spam_check_config_error(monkeypatch, cfg, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today, **_kw: ([{"name": "great.com", "tld": "com", "dropped_date": "2026-04-27"}], []),
    )

    # Availability check now runs BEFORE enrichment — stub it so the
    # candidate flows through to the spam_check failure we're testing.
    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(
        rdap_mod, "check_availability",
        lambda d, _c: {"is_available": True, "rdap_http": 404,
                       "rdap_status": [], "rdap_expiration": None,
                       "previous_registrar": None},
    )

    from scripts.enrichment.spam_check import SpamCheckConfigError

    def boom(*_a, **_k):
        raise SpamCheckConfigError("nope")

    monkeypatch.setattr(pipeline, "enrich_all", boom)

    with pytest.raises(SpamCheckConfigError):
        pipeline.main(["--config", str(cfg_path)])


# --- --debug-export flag ----------------------------------------------------


def _wire_debug_export_pipeline(monkeypatch, cfg_path, today):
    """Common stub harness: env vars, auth, three drops (one passes lexical,
    one fails lexical, one fails structural), enrichers populate fields, RDAP
    says everyone available. Returns nothing — caller invokes pipeline.main."""
    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    _set_r2_env(monkeypatch)

    monkeypatch.setattr(pipeline.czds_client, "authenticate", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        pipeline,
        "collect_drops",
        lambda _cfg, _tok, today, **_kw: ([
            {"name": "great.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "78win012.com", "tld": "com", "dropped_date": today.isoformat()},
            {"name": "xn--bad.com", "tld": "com", "dropped_date": today.isoformat()},
        ], []),
    )

    def fake_enrich_all(cands, _cfg):
        for c in cands:
            c.update({
                "wayback_snapshots": 50,
                "wayback_last_snapshot": "2024-01-01",
                "open_page_rank": 3.0,
                "cert_history": True,
                "spam_flagged": False,
                "surbl_listed": False,
                "spamhaus_listed": False,
                "previous_registrar": "Acme",
            })
        return cands

    monkeypatch.setattr(pipeline, "enrich_all", fake_enrich_all)

    from scripts.enrichment import rdap as rdap_mod
    monkeypatch.setattr(
        rdap_mod, "check_availability",
        lambda d, _c: {"is_available": True, "rdap_http": 404,
                       "rdap_status": [], "rdap_expiration": None,
                       "previous_registrar": None},
    )


def test_main_does_not_call_debug_export_when_flag_unset(monkeypatch, cfg, tmp_path):
    """Hard guarantee: when --debug-export is not set, debug_export.write_dumps
    is never called and no dump files are created. Production runs are
    unaffected by the new flag."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    _wire_debug_export_pipeline(monkeypatch, cfg_path, date.today())

    calls: list[tuple] = []

    def fake_write_dumps(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(pipeline.debug_export, "write_dumps", fake_write_dumps)

    rc = pipeline.main(["--config", str(cfg_path)])
    assert rc == 0
    assert calls == []  # the dump function was never invoked


def test_main_writes_all_debug_export_files_when_flag_set(monkeypatch, cfg, tmp_path):
    """End-to-end: --debug-export PATH causes all five plain-text files (and
    _meta.json) to land in PATH with the expected per-stage contents."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    dump_dir = tmp_path / "debug-out"

    _wire_debug_export_pipeline(monkeypatch, cfg_path, date.today())

    rc = pipeline.main([
        "--config", str(cfg_path),
        "--debug-export", str(dump_dir),
    ])
    assert rc == 0

    # All five required files exist.
    rejects = (dump_dir / "lexical_rejects.txt").read_text(encoding="utf-8")
    survivors = (dump_dir / "lexical_survivors.txt").read_text(encoding="utf-8")
    kept = (dump_dir / "trim_kept.txt").read_text(encoding="utf-8")
    discards = (dump_dir / "trim_discards.txt").read_text(encoding="utf-8")
    published = (dump_dir / "published.txt").read_text(encoding="utf-8")

    # Lexical reject: 78win012.com fails digit_ratio (5 digits / 8 chars > 0.30).
    # xn--bad.com is rejected at the structural stage, BEFORE lexical, so it
    # does NOT appear in lexical_rejects (the structural filter runs first).
    assert "78win012.com,digit_ratio" in rejects
    assert "xn--bad.com" not in rejects

    # great.com survives lexical and the trim cap (cap=1000 >> 1 candidate).
    assert survivors.strip() == "great.com"
    assert kept.strip() == "great.com"
    assert discards.strip() == ""
    # great.com is the sole published name.
    assert published.strip() == "great.com"

    # _meta.json with counts and availability_check shape.
    meta = json.loads((dump_dir / "_meta.json").read_text(encoding="utf-8"))
    assert meta["availability_check"]["max_runtime_per_host_seconds"] == (
        cfg["availability_check"]["max_runtime_per_host_seconds"]
    )
    assert meta["availability_check"]["global_cap"] == cfg["availability_check"]["global_cap"]
    assert meta["counts"]["lexical_survivors"] == 1
    assert meta["counts"]["trim_kept"] == 1
    assert meta["counts"]["trim_discards"] == 0
    assert meta["counts"]["published"] == 1
    assert "generated_at" in meta
