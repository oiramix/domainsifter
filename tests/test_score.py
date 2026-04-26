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


def test_score_treats_missing_fields_as_zero_signal():
    bare = {"name": "ab.com"}
    full_zero = {
        "name": "ab.com",
        "wayback_snapshots": 0,
        "open_page_rank": 0.0,
        "cert_history": False,
    }
    assert score.score_candidate(bare, CONFIG) == score.score_candidate(full_zero, CONFIG)


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
    cands = [
        {"name": "zeta.com"},
        {"name": "alpha.com"},
        {"name": "mike.com"},
    ]
    result = score.score_candidates(cands, CONFIG)
    assert [c["name"] for c in result] == ["alpha.com", "mike.com", "zeta.com"]
