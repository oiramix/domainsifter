"""Unit tests for scripts/score.py."""

from __future__ import annotations

from scripts import score

CONFIG = {
    "scoring_weights": {
        "wayback_snapshots": 0.3,
        "open_page_rank": 0.4,
        "cert_history": 0.2,
        "domain_length": 0.1,
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
