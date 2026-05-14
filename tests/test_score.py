"""Unit tests for scripts/score.py."""

from __future__ import annotations

from scripts import score

CONFIG = {
    "scoring_weights": {
        "wayback_snapshots": 0.3,
        "open_page_rank": 0.4,
        "cert_history": 0.2,
        "domain_length": 0.1,
        "cc_source_domain_count": 0.3,
    },
    "filter_thresholds": {"min_domain_length": 2, "max_domain_length": 30},
}


def test_score_zero_signals_returns_zero():
    cand = {"name": "z" * 30 + ".com"}
    assert score.score_candidate(cand, CONFIG) == 0


def test_score_max_signals_returns_100():
    cand = {
        "name": "ab.com",
        "wayback_snapshots": 100000,
        "open_page_rank": 10.0,
        "cert_history": True,
    }
    assert score.score_candidate(cand, CONFIG) == 100


def test_score_increases_with_more_wayback():
    base = {"name": "ab.com", "open_page_rank": 0.0, "cert_history": False}
    low = score.score_candidate({**base, "wayback_snapshots": 1}, CONFIG)
    mid = score.score_candidate({**base, "wayback_snapshots": 100}, CONFIG)
    high = score.score_candidate({**base, "wayback_snapshots": 10000}, CONFIG)
    assert low < mid < high


def test_score_increases_with_higher_opr():
    base = {"name": "ab.com", "wayback_snapshots": 0, "cert_history": False}
    low = score.score_candidate({**base, "open_page_rank": 0.0}, CONFIG)
    mid = score.score_candidate({**base, "open_page_rank": 3.0}, CONFIG)
    high = score.score_candidate({**base, "open_page_rank": 8.0}, CONFIG)
    assert low < mid < high


def test_score_cert_history_adds_value():
    base = {"name": "ab.com", "wayback_snapshots": 0, "open_page_rank": 0.0}
    no_cert = score.score_candidate({**base, "cert_history": False}, CONFIG)
    with_cert = score.score_candidate({**base, "cert_history": True}, CONFIG)
    assert with_cert > no_cert


def test_score_shorter_apex_scores_higher():
    base = {"wayback_snapshots": 0, "open_page_rank": 0.0, "cert_history": False}
    short = score.score_candidate({**base, "name": "ab.com"}, CONFIG)
    long_name = score.score_candidate({**base, "name": "a" * 25 + ".com"}, CONFIG)
    assert short > long_name


def test_full_data_score():
    """All four components populated — score is the same as before the
    null-aware refactor, since populated weight sum equals total weight."""
    cand = {
        "name": "ab.com",
        "wayback_snapshots": 100,
        "open_page_rank": 5.0,
        "cert_history": True,
    }
    s = score.score_candidate(cand, CONFIG)
    # Compute expected via the same formula manually:
    #   wayback: log10(101)/3 ≈ 0.667 × 0.3 = 0.2002
    #   opr: 0.5 × 0.4 = 0.2
    #   cert: 1.0 × 0.2 = 0.2
    #   length: 1.0 × 0.1 = 0.1
    # weighted=0.7002, total_weight=1.0, raw=0.7002, score=70
    assert s == 70


def test_partial_data_score():
    """wayback null, OPR populated — denominator excludes wayback's weight,
    so the score reflects the populated components rather than being
    capped by the missing one."""
    cand = {
        "name": "ab.com",
        "wayback_snapshots": None,   # missing — excluded
        "open_page_rank": 5.0,        # populated → 0.5
        "cert_history": True,         # populated → 1.0
    }
    s = score.score_candidate(cand, CONFIG)
    # Expected:
    #   opr: 0.5 × 0.4 = 0.2
    #   cert: 1.0 × 0.2 = 0.2
    #   length: 1.0 × 0.1 = 0.1
    # weighted=0.5, total_weight=0.4+0.2+0.1=0.7, raw=0.714, score=71
    assert s == 71
    # And it is HIGHER than the same candidate scored as if wayback were 0
    # (the old behaviour would have given a lower score).
    cand_with_zero = {**cand, "wayback_snapshots": 0}
    s_zero = score.score_candidate(cand_with_zero, CONFIG)
    assert s > s_zero, "null wayback should score better than zero wayback"


def test_no_data_returns_none():
    """No name and no enrichment fields — degenerate input. score_candidate
    returns None and score_candidates drops the row."""
    bare = {"name": ""}  # empty name → length component absent too
    assert score.score_candidate(bare, CONFIG) is None

    # And the pipeline-level entry point drops unscoreable rows.
    cands = [
        bare,
        {"name": "good.com", "wayback_snapshots": 100, "open_page_rank": 5.0, "cert_history": True},
    ]
    result = score.score_candidates(cands, CONFIG)
    assert [c["name"] for c in result] == ["good.com"]


def test_score_returns_integer_in_zero_to_hundred():
    cand = {
        "name": "midrange.com",
        "wayback_snapshots": 50,
        "open_page_rank": 4.0,
        "cert_history": True,
    }
    s = score.score_candidate(cand, CONFIG)
    assert isinstance(s, int)
    assert 0 <= s <= 100


def test_score_returns_zero_when_all_weights_zero():
    cfg = {**CONFIG, "scoring_weights": {}}
    cand = {"name": "ab.com", "wayback_snapshots": 9999, "open_page_rank": 10.0, "cert_history": True}
    assert score.score_candidate(cand, cfg) == 0


def test_score_candidates_sorts_by_score_descending():
    cands = [
        {"name": "low.com", "wayback_snapshots": 0, "open_page_rank": 0.0, "cert_history": False},
        {"name": "high.com", "wayback_snapshots": 1000, "open_page_rank": 7.0, "cert_history": True},
        {"name": "mid.com", "wayback_snapshots": 10, "open_page_rank": 2.0, "cert_history": True},
    ]
    result = score.score_candidates(cands, CONFIG)
    assert [c["name"] for c in result] == ["high.com", "mid.com", "low.com"]
    assert all("score" in c for c in result)


def test_score_candidates_breaks_ties_by_name():
    # Same-length apex labels so the length-only signal scores all three
    # identically; tie-break must fall to alphabetical name ordering.
    cands = [
        {"name": "zzzz.com"},
        {"name": "aaaa.com"},
        {"name": "mmmm.com"},
    ]
    result = score.score_candidates(cands, CONFIG)
    assert [c["name"] for c in result] == ["aaaa.com", "mmmm.com", "zzzz.com"]


# --- cc_source_domain_count (added 2026-05-14) -------------------------------


def test_score_increases_with_more_cc_backlinks():
    """Log-scaled: more inbound source domains → higher score, with
    diminishing marginal returns at the high end."""
    base = {"name": "ab.com", "wayback_snapshots": 0, "open_page_rank": 0.0, "cert_history": False}
    low = score.score_candidate({**base, "cc_source_domain_count": 1}, CONFIG)
    mid = score.score_candidate({**base, "cc_source_domain_count": 100}, CONFIG)
    high = score.score_candidate({**base, "cc_source_domain_count": 10_000}, CONFIG)
    assert low < mid < high


def test_score_cc_saturates_at_10k():
    """Divisor 4.0 means log10(10_001)/4 ≈ 1.0 → the component caps. Any
    count beyond that yields the same normalized value (1.0)."""
    base = {"name": "ab.com", "wayback_snapshots": 0, "open_page_rank": 0.0, "cert_history": False}
    at_cap = score.score_candidate({**base, "cc_source_domain_count": 10_000}, CONFIG)
    way_over = score.score_candidate({**base, "cc_source_domain_count": 16_365_926}, CONFIG)
    assert at_cap == way_over


def test_score_cc_zero_count_distinct_from_missing():
    """A dangler (in CC graph but no inbound edges) reads as 0 — distinct
    from 'not in graph' (which is None). 0 IS populated, contributing 0
    to the numerator but adding cc's weight (0.3) to the denominator —
    pulling the weighted average down. None is excluded from both.

    This is the operational consequence of the three-state distinction at
    the enricher boundary: dangler vs not-in-graph carry different scoring
    semantics even when both look like 'no inbound edges' colloquially."""
    base = {"name": "ab.com", "open_page_rank": 0.0, "cert_history": False, "wayback_snapshots": 0}
    with_zero = score.score_candidate({**base, "cc_source_domain_count": 0}, CONFIG)
    without_key = score.score_candidate(base, CONFIG)
    # without_key: cc excluded from average, length=1.0 carries the entire
    # numerator: 0.1 / 1.0 = 0.1 → score 10.
    # with_zero: cc=0 IS populated but contributes 0; length=1.0 still
    # carries: 0.1 / 1.3 ≈ 0.077 → score 8.
    # The asymmetry is the whole point: zero is a real observation, null
    # is an unknown.
    assert with_zero < without_key, (
        "dangler (cc=0) must score lower than not-in-graph (cc=null) when "
        "every other signal is zero — zero pulls the average down, null is "
        "excluded from it"
    )


def test_score_cc_null_excluded_from_average():
    """cc_source_domain_count=None means 'not in graph'. The score formula
    excludes None components from BOTH numerator and denominator — the
    candidate scores on what IS known, not penalized for absence."""
    base = {
        "name": "ab.com",
        "wayback_snapshots": 100,
        "open_page_rank": 5.0,
        "cert_history": True,
    }
    s_null = score.score_candidate({**base, "cc_source_domain_count": None}, CONFIG)
    s_missing = score.score_candidate(base, CONFIG)
    # null and key-missing both mean 'unknown' to the formula; same score.
    assert s_null == s_missing
    # And it is HIGHER than the same candidate scored as if cc were 0
    # (zero IS populated and drags the weighted average down).
    s_zero = score.score_candidate({**base, "cc_source_domain_count": 0}, CONFIG)
    assert s_null > s_zero, "null cc should score better than zero cc"


def test_score_cc_contributes_proportionally_to_weight():
    """When cc is at its cap (1.0 normalized) and every other component is
    0, the resulting score equals cc's weight share of the total. Hand-
    computed: cc weight 0.3 / total 1.0 = 30%."""
    cand = {
        "name": "z" * 30 + ".com",   # length component = 0.0 at max_len
        "wayback_snapshots": 0,       # 0.0
        "open_page_rank": 0.0,        # 0.0
        "cert_history": False,        # 0.0
        "cc_source_domain_count": 1_000_000,  # saturates to 1.0
    }
    # Weighted: 0.0 + 0.0 + 0.0 + 0.0 + 1.0×0.3 = 0.3
    # Total weight: 0.3 + 0.4 + 0.2 + 0.1 + 0.3 = 1.3
    # raw = 0.3 / 1.3 ≈ 0.2308 → round(23.08) = 23
    assert score.score_candidate(cand, CONFIG) == 23


def test_score_full_data_with_cc():
    """All five components populated. Hand-checked math: the formula
    matches the documented log-scale derivation."""
    cand = {
        "name": "ab.com",
        "wayback_snapshots": 100,
        "open_page_rank": 5.0,
        "cert_history": True,
        "cc_source_domain_count": 100,
    }
    s = score.score_candidate(cand, CONFIG)
    # wayback: log10(101)/3 ≈ 0.667 × 0.3 = 0.2002
    # cc:      log10(101)/4 ≈ 0.500 × 0.3 = 0.150
    # opr:     0.5 × 0.4 = 0.2
    # cert:    1.0 × 0.2 = 0.2
    # length:  1.0 × 0.1 = 0.1
    # weighted ≈ 0.8502 / total_weight 1.3 ≈ 0.6540 → round(65.40) = 65
    assert s == 65
