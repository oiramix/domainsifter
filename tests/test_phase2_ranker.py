"""Unit tests for scripts/phase2_ranker.py — the LLM name-quality ranker.

Heavy mocking: no live Anthropic API, no live R2. The Anthropic client is a
MagicMock whose `messages.create` returns synthetic responses with a `.content`
list and `.usage` block matching the real SDK shape. The R2 client is a
MagicMock with `get_object`/`put_object` recorded for assertion.

Test surface mirrors the 22-test plan in the design proposal:
  1.  pre_narrow sort key (-trigram, apex_len, name)
  2.  pre_narrow truncates to target_n
  3.  budget planner converts EUR → target_n
  4.  haiku_batch_all aggregates usage across batches
  5.  missing names recorded as score=0 (not vanished)
  6.  retry pass recovers missing when possible
  7.  cost ceiling halts further batches
  8.  rank_and_select success returns above-gate sorted desc
  9.  rank_and_select disabled returns lexical_kept unchanged
  10. rank_and_select fallback on too_few_eligible
  11. rank_and_select fallback on Haiku exception
  12. rank_and_select fallback on ceiling below min_eligible
  13. rank_and_select PARTIAL when ceiling-hit but above min_eligible
  14. rank_and_select fallback on no api client
  15. _bucket_and_cap uses score-desc trim when candidates carry phase2_score
  16. _bucket_and_cap uses random-shuffle when no scores (REGRESSION GUARD)
  17. record_overflow writes only above-gate not in RDAP set
  18. record_overflow ages out >14d entries
  19. record_overflow appends today's entries with first_seen_date
  20. record_overflow noop on fallback / disabled
  21. pipeline integration: phase2 status surfaces in logs
  22. pipeline integration: full path with mocked Haiku
"""

from __future__ import annotations

import json
import threading
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts import phase2_ranker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cand(name: str, tld: str | None = None) -> dict:
    return {
        "name": name,
        "tld": tld or name.rsplit(".", 1)[-1],
        "dropped_date": "2026-06-02",
    }


def _config(
    *,
    enabled: bool = True,
    daily_budget_eur: float = 3.0,
    eur_to_usd: float = 1.0,
    cost_per_1000_planning_usd: float = 1.0,  # tiny target_n math for fast tests
    score_gate: int = 60,
    min_eligible: int = 10,
    batch_size: int = 5,
    concurrency: int = 2,
    max_retry_passes: int = 1,
) -> dict:
    return {
        "phase2": {
            "enabled": enabled,
            "daily_budget_eur": daily_budget_eur,
            "eur_to_usd": eur_to_usd,
            "cost_per_1000_planning_usd": cost_per_1000_planning_usd,
            "score_gate": score_gate,
            "min_eligible": min_eligible,
            "model": "test-model",
            "batch_size": batch_size,
            "concurrency": concurrency,
            "max_retry_passes": max_retry_passes,
        }
    }


def _mock_response(
    rows: list[dict],
    *,
    input_tokens: int = 100,
    output_tokens: int = 200,
    cache_creation: int = 0,
    cache_read: int = 0,
):
    """Build an anthropic-SDK-shaped mock response for messages.create()."""
    text_block = SimpleNamespace(type="text", text=json.dumps(rows))
    resp = SimpleNamespace(
        content=[text_block],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )
    return resp


def _mock_client_returning(per_call_rows: list[list[dict]]) -> MagicMock:
    """Mock client whose messages.create returns one canned response per call,
    cycling through `per_call_rows` in order. Thread-safe."""
    client = MagicMock()
    counter = {"i": 0}
    lock = threading.Lock()

    def create(**kwargs):
        with lock:
            i = counter["i"]
            counter["i"] += 1
        rows = per_call_rows[min(i, len(per_call_rows) - 1)]
        return _mock_response(rows)

    client.messages.create.side_effect = create
    return client


def _mock_client_raises(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = exc
    return client


# ---------------------------------------------------------------------------
# 1-3: pre_narrow + budget planner
# ---------------------------------------------------------------------------


def test_pre_narrow_sorts_by_trigram_desc_then_apex_len_asc():
    """Same key the existing global_cap trim uses — verifies parity."""
    cands = [
        _cand("vicat.org"),               # short, few trigrams
        _cand("tideblock.org"),           # longer, English compound (more trigrams)
        _cand("dandeliontea.org"),        # even longer English compound
        _cand("ckyy.org"),                # 4-char gibberish
    ]
    out = phase2_ranker.pre_narrow(cands, target_n=4)
    # The exact ordering depends on real trigram counts — but the longest,
    # most English-like names should rank ABOVE the short gibberish ones.
    names = [c["name"] for c in out]
    assert names.index("dandeliontea.org") < names.index("ckyy.org")
    assert names.index("tideblock.org") < names.index("ckyy.org")


def test_pre_narrow_truncates_to_target_n():
    cands = [_cand(f"name{i:02d}.org") for i in range(50)]
    out = phase2_ranker.pre_narrow(cands, target_n=10)
    assert len(out) == 10


def test_pre_narrow_returns_input_unchanged_when_under_target():
    cands = [_cand(f"name{i}.org") for i in range(5)]
    out = phase2_ranker.pre_narrow(cands, target_n=100)
    assert len(out) == 5
    assert {c["name"] for c in out} == {c["name"] for c in cands}


def test_planned_target_n_converts_budget_to_count():
    # $3 USD budget @ $0.25 / 1000 → 12,000 target
    assert phase2_ranker._planned_target_n(3.0, 0.25) == 12000
    # €3 → $3.24 USD @ $0.25 / 1000 → 12,960 (matches design proposal math)
    assert phase2_ranker._planned_target_n(3.0 * 1.08, 0.25) == 12960
    # Zero / negative planning rate is treated as 0 (degenerate config)
    assert phase2_ranker._planned_target_n(3.0, 0) == 0


# ---------------------------------------------------------------------------
# 4-7: haiku_batch_all internals (cost aggregation, missing, retry, ceiling)
# ---------------------------------------------------------------------------


def test_haiku_batch_aggregates_usage_across_batches():
    cands = [_cand(f"name{i}.org") for i in range(10)]
    # Two batches of 5; each call returns scores for all 5.
    rows_b1 = [{"domain": cands[i]["name"], "score": 70, "reason": "ok"} for i in range(5)]
    rows_b2 = [{"domain": cands[i]["name"], "score": 70, "reason": "ok"} for i in range(5, 10)]
    client = _mock_client_returning([rows_b1, rows_b2])

    cfg = _config(batch_size=5, concurrency=2, max_retry_passes=0)
    scored, meter = phase2_ranker._haiku_batch_all(
        cands, client, lambda k: phase2_ranker._config(cfg, k), ceiling_usd=10_000.0,
    )
    assert len(scored) == 10
    assert meter["batches_ok"] == 2
    # 2 batches × (input=100 + output=200) tokens
    assert meter["input_tokens"] == 200
    assert meter["output_tokens"] == 400
    assert meter["total_usd"] > 0


def test_haiku_batch_records_missing_as_below_gate():
    """If the response omits a domain, the orchestrator must record it as
    score=0 (below-gate) rather than vanishing."""
    cands = [_cand(f"name{i}.org") for i in range(5)]
    # Response only includes 3 of 5
    rows = [{"domain": cands[i]["name"], "score": 70, "reason": "ok"} for i in range(3)]
    client = _mock_client_returning([rows])
    cfg = _config(batch_size=5, max_retry_passes=0, min_eligible=1, score_gate=60)

    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=client,
    )
    # All 5 should be present in the resulting scored output even if some
    # are below-gate — but rank_and_select returns only the ABOVE-GATE set.
    # The 3 that scored 70 are above gate; the 2 missing land below gate
    # and are NOT in the returned list (they're discards).
    assert status["mode"] == "ranker"
    assert len(out) == 3
    assert status["missing_count"] == 2


def test_haiku_batch_retry_pass_recovers_missing_when_possible():
    cands = [_cand(f"name{i}.org") for i in range(5)]
    # First call: only returns 3
    rows_first = [{"domain": cands[i]["name"], "score": 70, "reason": "ok"} for i in range(3)]
    # Retry call: returns the missing 2
    rows_retry = [
        {"domain": cands[3]["name"], "score": 65, "reason": "recovered on retry"},
        {"domain": cands[4]["name"], "score": 75, "reason": "recovered on retry"},
    ]
    client = _mock_client_returning([rows_first, rows_retry])
    cfg = _config(batch_size=5, max_retry_passes=1, min_eligible=1, score_gate=60)

    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=client,
    )
    assert status["mode"] == "ranker"
    assert len(out) == 5  # all 5 above gate after retry
    assert status["missing_count"] == 0


def test_cost_ceiling_halts_further_batches():
    # Many small batches — first one alone will burn through the ceiling.
    cands = [_cand(f"name{i}.org") for i in range(20)]
    # Every call returns 5 OK rows but with absurdly high token usage.
    rows = [{"domain": cands[i]["name"], "score": 70, "reason": "ok"} for i in range(5)]
    expensive_resp = _mock_response(
        rows, input_tokens=10_000_000, output_tokens=10_000_000,
    )
    client = MagicMock()
    client.messages.create.return_value = expensive_resp

    cfg = _config(
        batch_size=5, concurrency=1, max_retry_passes=0,
        daily_budget_eur=1.0,  # ceiling will be ~$0.95
    )
    # Wrap the inner call to see how many times messages.create fires.
    scored, meter = phase2_ranker._haiku_batch_all(
        cands, client, lambda k: phase2_ranker._config(cfg, k), ceiling_usd=0.95,
    )
    assert meter["ceiling_hit"] is True
    # Concurrency=1 means batches run serially; the first batch alone
    # exceeds the ceiling, so no further batches submit.
    assert client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# 8-14: rank_and_select branches
# ---------------------------------------------------------------------------


def test_rank_and_select_success_returns_above_gate_sorted_desc():
    cands = [_cand(f"name{i:02d}.org") for i in range(10)]
    rows = [
        {"domain": "name00.org", "score": 80, "reason": "best"},
        {"domain": "name01.org", "score": 75, "reason": "good"},
        {"domain": "name02.org", "score": 70, "reason": "fine"},
        {"domain": "name03.org", "score": 65, "reason": "passing"},
        {"domain": "name04.org", "score": 60, "reason": "borderline"},
        {"domain": "name05.org", "score": 55, "reason": "below"},
        {"domain": "name06.org", "score": 40, "reason": "weak"},
        {"domain": "name07.org", "score": 30, "reason": "bad"},
        {"domain": "name08.org", "score": 20, "reason": "worse"},
        {"domain": "name09.org", "score": 10, "reason": "worst"},
    ]
    client = _mock_client_returning([rows])
    cfg = _config(batch_size=10, score_gate=60, min_eligible=1)

    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=client,
    )
    assert status["mode"] == "ranker"
    assert len(out) == 5  # scores 80, 75, 70, 65, 60 clear the 60 gate
    # Sorted by score desc.
    assert [c["phase2_score"] for c in out] == [80, 75, 70, 65, 60]
    # Each carries phase2_score + phase2_reason.
    assert all("phase2_score" in c and "phase2_reason" in c for c in out)
    assert status["above_gate_count"] == 5
    assert status["cost_usd"] > 0


def test_rank_and_select_disabled_returns_lexical_kept_unchanged():
    cands = [_cand(f"name{i}.org") for i in range(20)]
    cfg = _config(enabled=False)
    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=_mock_client_returning([]),
    )
    assert status == {"mode": "disabled"}
    assert len(out) == 20
    assert all("phase2_score" not in c for c in out)


def test_rank_and_select_fallback_on_too_few_eligible():
    cands = [_cand(f"name{i:02d}.org") for i in range(10)]
    # Only 2 above 60 — below min_eligible=5.
    rows = (
        [{"domain": cands[i]["name"], "score": 80, "reason": "ok"} for i in range(2)]
        + [{"domain": cands[i]["name"], "score": 30, "reason": "weak"} for i in range(2, 10)]
    )
    client = _mock_client_returning([rows])
    cfg = _config(batch_size=10, score_gate=60, min_eligible=5)

    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=client,
    )
    assert status["mode"] == "fallback"
    assert status["reason"] == "too_few_eligible"
    # Falls back: returns lexical_kept unchanged, NOT the above-gate set.
    assert len(out) == 10
    assert all("phase2_score" not in c for c in out)


def test_rank_and_select_fallback_on_haiku_exception():
    cands = [_cand(f"name{i}.org") for i in range(10)]
    client = _mock_client_raises(RuntimeError("transient outage"))
    cfg = _config(batch_size=10, min_eligible=1)

    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=client,
    )
    # Batch raises — orchestrator treats it as a soft failure within the
    # batch (logged warning, batch's domains end up missing-from-response).
    # If no domains scored, above_gate is empty → too_few_eligible fallback.
    assert status["mode"] == "fallback"
    assert len(out) == 10
    assert all("phase2_score" not in c for c in out)


def test_rank_and_select_fallback_on_ceiling_below_min_eligible():
    cands = [_cand(f"name{i:02d}.org") for i in range(20)]
    # Expensive responses with only 2 above gate before ceiling halts.
    rows = (
        [{"domain": cands[i]["name"], "score": 80, "reason": "ok"} for i in range(2)]
        + [{"domain": cands[i]["name"], "score": 10, "reason": "bad"} for i in range(2, 5)]
    )
    expensive_resp = _mock_response(rows, input_tokens=20_000_000, output_tokens=20_000_000)
    client = MagicMock()
    client.messages.create.return_value = expensive_resp

    cfg = _config(
        batch_size=5, concurrency=1, max_retry_passes=0,
        daily_budget_eur=1.0, score_gate=60, min_eligible=10,
    )
    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=client,
    )
    assert status["mode"] == "fallback"
    assert status["reason"] == "cost_ceiling_below_min_eligible"
    assert status["ceiling_hit"] is True
    assert len(out) == 20  # original lexical_kept, no scores attached


def test_rank_and_select_partial_when_above_min_eligible_after_ceiling_hit():
    cands = [_cand(f"name{i:02d}.org") for i in range(20)]
    # First batch: 5 above-gate. Ceiling then halts.
    rows = [{"domain": cands[i]["name"], "score": 80, "reason": "ok"} for i in range(5)]
    expensive_resp = _mock_response(rows, input_tokens=20_000_000, output_tokens=20_000_000)
    client = MagicMock()
    client.messages.create.return_value = expensive_resp

    cfg = _config(
        batch_size=5, concurrency=1, max_retry_passes=0,
        daily_budget_eur=1.0, score_gate=60, min_eligible=3,
    )
    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=client,
    )
    assert status["mode"] == "ranker_partial"
    assert status["ceiling_hit"] is True
    assert len(out) == 5
    assert status["above_gate_count"] == 5


def test_rank_and_select_fallback_when_no_api_client(monkeypatch):
    cands = [_cand(f"name{i}.org") for i in range(5)]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _config()
    # client=None and no env key → make_default_client returns None → fallback
    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=None,
    )
    assert status["mode"] == "fallback"
    assert status["reason"] == "no_api_client"
    assert len(out) == 5
    assert all("phase2_score" not in c for c in out)


# ---------------------------------------------------------------------------
# 15-16: _bucket_and_cap_for_availability behavior (in pipeline.py)
# ---------------------------------------------------------------------------


def test_bucket_and_cap_uses_score_desc_when_candidates_carry_phase2_score(monkeypatch):
    """When ranker has tagged candidates with phase2_score, the over-cap
    bucket trim should keep the highest-score ones — not random-shuffle."""
    from scripts import pipeline
    from scripts.enrichment import rdap as _rdap

    monkeypatch.setattr(_rdap, "resolve_rdap_host", lambda n, c: "rdap.test.example")

    # 5 candidates, all in the same RDAP host bucket. Per-host cap = 3.
    # Scores: a=90, b=10, c=80, d=20, e=70 → top-3 by score desc = a, c, e.
    candidates = [
        {**_cand("a.org"), "phase2_score": 90},
        {**_cand("b.org"), "phase2_score": 10},
        {**_cand("c.org"), "phase2_score": 80},
        {**_cand("d.org"), "phase2_score": 20},
        {**_cand("e.org"), "phase2_score": 70},
    ]
    config = {
        "availability_check": {
            "max_runtime_per_host_seconds": 1, "global_cap": 100,
        },
        "api_min_interval_seconds": {"rdap": 1.0},
        # workers=1, throttle=1.0, runtime=1 → cap = 1×1/1.0 = 1.
        # Bump runtime so cap = 3 (=runtime×workers/throttle).
    }
    config["availability_check"]["max_runtime_per_host_seconds"] = 3

    final, stats = pipeline._bucket_and_cap_for_availability(
        candidates, config, today=date(2026, 6, 2),
    )
    kept_names = {c["name"] for c in final}
    assert kept_names == {"a.org", "c.org", "e.org"}


def test_bucket_and_cap_uses_random_shuffle_when_no_phase2_score(monkeypatch):
    """REGRESSION GUARD: fallback parity. When no candidate carries
    phase2_score (ranker disabled / fallback path), the over-cap bucket
    trim must use the existing random-shuffle behavior bit-for-bit."""
    from scripts import pipeline
    from scripts.enrichment import rdap as _rdap

    monkeypatch.setattr(_rdap, "resolve_rdap_host", lambda n, c: "rdap.test.example")

    # 5 candidates in one bucket, no phase2_score attached.
    candidates = [_cand(f"n{i}.org") for i in range(5)]
    config = {
        "availability_check": {
            "max_runtime_per_host_seconds": 3, "global_cap": 100,
        },
        "api_min_interval_seconds": {"rdap": 1.0},
    }

    final, _ = pipeline._bucket_and_cap_for_availability(
        candidates, config, today=date(2026, 6, 2),
    )
    # Cap = 3. Same shuffle (seed=YYYYMMDD as int) every run → deterministic.
    # Run twice with same date — should get the same set.
    final2, _ = pipeline._bucket_and_cap_for_availability(
        [_cand(f"n{i}.org") for i in range(5)], config, today=date(2026, 6, 2),
    )
    assert len(final) == 3
    assert {c["name"] for c in final} == {c["name"] for c in final2}
    # None of the final candidates should carry phase2_score (purity check).
    assert all("phase2_score" not in c for c in final)


# ---------------------------------------------------------------------------
# 17-20: record_overflow
# ---------------------------------------------------------------------------


def _no_such_key():
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )


def test_record_overflow_writes_only_above_gate_not_in_rdap_set():
    above = [
        {**_cand("a.org"), "phase2_score": 80, "phase2_reason": "good"},
        {**_cand("b.org"), "phase2_score": 70, "phase2_reason": "ok"},
        {**_cand("c.org"), "phase2_score": 65, "phase2_reason": "passing"},
    ]
    selected = [above[0]]  # only a.org got into RDAP

    r2 = MagicMock()
    r2.get_object.side_effect = _no_such_key()
    captured: dict = {}

    def put_object(Bucket, Key, Body, ContentType):
        captured["body"] = Body
        captured["key"] = Key

    r2.put_object.side_effect = put_object

    n = phase2_ranker.record_overflow(
        above_gate=above, selected_for_rdap=selected,
        config={}, today=date(2026, 6, 2),
        status={"mode": "ranker"},
        r2_client=r2, r2_bucket="test-bucket",
    )
    assert n == 2  # b.org + c.org
    assert captured["key"] == "state/phase2_overflow.jsonl"
    records = [json.loads(line) for line in captured["body"].decode().splitlines() if line]
    names = {r["name"] for r in records}
    assert names == {"b.org", "c.org"}
    # Each carries first_seen_date set to today.
    assert all(r["first_seen_date"] == "2026-06-02" for r in records)


def test_record_overflow_ages_out_entries_older_than_14_days():
    above = [{**_cand("new.org"), "phase2_score": 80, "phase2_reason": "good"}]
    selected: list[dict] = []

    # Existing R2 file: one fresh, one stale.
    existing_lines = [
        json.dumps({
            "name": "fresh.org", "tld": "org", "dropped_date": "2026-05-30",
            "phase2_score": 70, "phase2_reason": "ok",
            "first_seen_date": "2026-05-30",
        }),
        json.dumps({
            "name": "stale.org", "tld": "org", "dropped_date": "2026-05-10",
            "phase2_score": 70, "phase2_reason": "ok",
            "first_seen_date": "2026-05-10",   # 23 days old, must be aged out
        }),
    ]
    existing_body = ("\n".join(existing_lines) + "\n").encode("utf-8")

    r2 = MagicMock()
    r2.get_object.return_value = {"Body": BytesIO(existing_body)}
    captured: dict = {}
    r2.put_object.side_effect = lambda Bucket, Key, Body, ContentType: captured.update(body=Body)

    phase2_ranker.record_overflow(
        above_gate=above, selected_for_rdap=selected,
        config={}, today=date(2026, 6, 2),
        status={"mode": "ranker"},
        r2_client=r2, r2_bucket="test-bucket",
    )
    records = [json.loads(line) for line in captured["body"].decode().splitlines() if line]
    names = {r["name"] for r in records}
    assert "stale.org" not in names      # aged out
    assert "fresh.org" in names           # retained
    assert "new.org" in names             # appended today


def test_record_overflow_appends_today_entries_with_first_seen_date():
    above = [{**_cand("new.org"), "phase2_score": 80, "phase2_reason": "good"}]
    r2 = MagicMock()
    r2.get_object.side_effect = _no_such_key()
    captured: dict = {}
    r2.put_object.side_effect = lambda Bucket, Key, Body, ContentType: captured.update(body=Body)

    phase2_ranker.record_overflow(
        above_gate=above, selected_for_rdap=[],
        config={}, today=date(2026, 6, 2),
        status={"mode": "ranker"},
        r2_client=r2, r2_bucket="test-bucket",
    )
    rec = json.loads(captured["body"].decode().strip())
    assert rec == {
        "name": "new.org", "tld": "org", "dropped_date": "2026-06-02",
        "phase2_score": 80, "phase2_reason": "good",
        "first_seen_date": "2026-06-02",
    }


def test_record_overflow_noop_when_status_is_fallback_or_disabled():
    r2 = MagicMock()
    for mode in ("fallback", "disabled"):
        n = phase2_ranker.record_overflow(
            above_gate=[{**_cand("x.org"), "phase2_score": 80}],
            selected_for_rdap=[],
            config={}, today=date(2026, 6, 2),
            status={"mode": mode},
            r2_client=r2, r2_bucket="test-bucket",
        )
        assert n == 0
    r2.put_object.assert_not_called()


def test_record_overflow_swallows_r2_errors():
    """Side-effect-only — never block the daily run on overflow failure."""
    above = [{**_cand("x.org"), "phase2_score": 80, "phase2_reason": "ok"}]
    r2 = MagicMock()
    r2.get_object.side_effect = RuntimeError("R2 outage")
    n = phase2_ranker.record_overflow(
        above_gate=above, selected_for_rdap=[],
        config={}, today=date(2026, 6, 2),
        status={"mode": "ranker"},
        r2_client=r2, r2_bucket="test-bucket",
    )
    assert n == 0  # error swallowed, count is 0


# ---------------------------------------------------------------------------
# 21-22: pipeline integration
# ---------------------------------------------------------------------------


def test_pipeline_integration_logs_phase2_status_lines(caplog):
    cands = [_cand(f"name{i:02d}.org") for i in range(10)]
    rows = [{"domain": cands[i]["name"], "score": 70, "reason": "ok"} for i in range(10)]
    client = _mock_client_returning([rows])
    cfg = _config(batch_size=10, score_gate=60, min_eligible=1)

    import logging
    with caplog.at_level(logging.INFO, logger="scripts.phase2_ranker"):
        out, status = phase2_ranker.rank_and_select(
            cands, cfg, today=date(2026, 6, 2), client=client,
        )

    log_text = "\n".join(caplog.messages)
    assert "Phase 2 ranker ENABLED" in log_text
    assert "Pre-narrow:" in log_text
    assert "above-gate" in log_text.lower()
    assert "MODE=RANKER" in log_text
    assert status["mode"] == "ranker"


def test_pipeline_integration_full_path_with_mocked_haiku(caplog):
    """End-to-end through the public API: lexical-survivor-shaped candidates
    in, above-gate-sorted-desc out, with all log fields populated."""
    cands = [
        _cand("dandeliontea.org"),
        _cand("getlegal.tech"),
        _cand("ckyy.org"),        # gibberish — below gate
        _cand("vicat.org"),       # gibberish — below gate
    ]
    rows = [
        {"domain": "dandeliontea.org", "score": 82, "reason": "real herbal compound"},
        {"domain": "getlegal.tech", "score": 78, "reason": "clear action words"},
        {"domain": "ckyy.org", "score": 12, "reason": "pure gibberish acronym"},
        {"domain": "vicat.org", "score": 18, "reason": "phonetic but meaningless"},
    ]
    client = _mock_client_returning([rows])
    cfg = _config(batch_size=10, score_gate=60, min_eligible=1)

    out, status = phase2_ranker.rank_and_select(
        cands, cfg, today=date(2026, 6, 2), client=client,
    )
    assert status["mode"] == "ranker"
    assert [c["name"] for c in out] == ["dandeliontea.org", "getlegal.tech"]
    assert out[0]["phase2_score"] == 82
    assert out[1]["phase2_score"] == 78
    assert "real herbal compound" in out[0]["phase2_reason"]
    # Status fields populated.
    assert "cost_usd" in status
    assert "score_distribution" in status
    assert status["above_gate_count"] == 2
    assert status["scored_count"] == 4
