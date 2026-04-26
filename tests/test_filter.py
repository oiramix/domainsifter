"""Unit tests for scripts/filter.py."""

from __future__ import annotations

from scripts import filter as filter_mod

CONFIG = {
    "filter_thresholds": {
        "min_domain_length": 2,
        "max_domain_length": 30,
        "min_wayback_snapshots": 1,
    },
    "rejected_keywords": ["porn", "casino", "viagra"],
}


def _ok(**extra) -> dict:
    base = {
        "name": "marketglow.com",
        "tld": "com",
        "spam_flagged": False,
        "surbl_listed": False,
        "spamhaus_listed": False,
        "wayback_snapshots": 12,
    }
    base.update(extra)
    return base


def test_keep_accepts_clean_candidate():
    keep, reason = filter_mod.keep(_ok(), CONFIG)
    assert keep is True
    assert reason is None


def test_keep_rejects_punycode():
    keep, reason = filter_mod.keep(_ok(name="xn--example.com"), CONFIG)
    assert keep is False
    assert reason == "punycode"


def test_keep_rejects_punycode_in_subdomain_label():
    keep, reason = filter_mod.keep(_ok(name="xn--bad.foo.com"), CONFIG)
    assert keep is False
    assert reason == "punycode"


def test_keep_rejects_single_character_apex():
    keep, reason = filter_mod.keep(_ok(name="a.com"), CONFIG)
    assert keep is False
    assert reason.startswith("too_short")


def test_keep_rejects_too_long_apex():
    long_label = "a" * 31 + ".com"
    keep, reason = filter_mod.keep(_ok(name=long_label), CONFIG)
    assert keep is False
    assert reason.startswith("too_long")


def test_keep_rejects_all_numeric():
    keep, reason = filter_mod.keep(_ok(name="12345.com"), CONFIG)
    assert keep is False
    assert reason == "all_numeric"


def test_keep_rejects_keyword_match_case_insensitive():
    keep, reason = filter_mod.keep(_ok(name="bestcasinodeal.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:casino"


def test_keep_rejects_when_spam_flagged():
    keep, reason = filter_mod.keep(_ok(spam_flagged=True), CONFIG)
    assert keep is False
    assert reason == "spam_flagged"


def test_keep_rejects_when_surbl_listed():
    keep, reason = filter_mod.keep(_ok(surbl_listed=True), CONFIG)
    assert keep is False
    assert reason == "surbl_listed"


def test_keep_rejects_when_spamhaus_listed():
    keep, reason = filter_mod.keep(_ok(spamhaus_listed=True), CONFIG)
    assert keep is False
    assert reason == "spamhaus_listed"


def test_keep_rejects_zero_wayback_snapshots():
    keep, reason = filter_mod.keep(_ok(wayback_snapshots=0), CONFIG)
    assert keep is False
    assert reason.startswith("no_wayback")


def test_keep_tolerates_missing_wayback_field():
    cand = _ok()
    del cand["wayback_snapshots"]
    keep, reason = filter_mod.keep(cand, CONFIG)
    assert keep is True
    assert reason is None


def test_keep_strict_rejects_when_spam_field_missing():
    cand = _ok()
    del cand["spam_flagged"]
    keep, reason = filter_mod.keep(cand, CONFIG, strict_spam_check=True)
    assert keep is False
    assert reason == "spam_check_missing"


def test_keep_lenient_accepts_when_spam_field_missing():
    cand = _ok()
    del cand["spam_flagged"]
    keep, reason = filter_mod.keep(cand, CONFIG, strict_spam_check=False)
    assert keep is True
    assert reason is None


def test_keep_rejects_empty_name():
    keep, reason = filter_mod.keep({"name": ""}, CONFIG)
    assert keep is False
    assert reason == "empty_name"


def test_filter_candidates_returns_only_survivors_and_logs(caplog):
    import logging

    cands = [
        _ok(name="goodone.com"),
        _ok(name="evil.com", spam_flagged=True),
        _ok(name="a.com"),
        _ok(name="goodtwo.com"),
    ]
    with caplog.at_level(logging.INFO, logger="scripts.filter"):
        survivors = filter_mod.filter_candidates(cands, CONFIG)
    names = {c["name"] for c in survivors}
    assert names == {"goodone.com", "goodtwo.com"}
    log_messages = " ".join(rec.message for rec in caplog.records)
    assert "spam_flagged" in log_messages
    assert "too_short" in log_messages
