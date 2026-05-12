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


def test_keep_accepts_when_spamhaus_listed_is_none():
    """`spamhaus_listed=None` means 'unknown' — DNSBL was rate-limited or
    otherwise refused to answer authoritatively. Must NOT reject the
    candidate. (Codifies the 2026-05-12 three-state contract.)"""
    keep, reason = filter_mod.keep(_ok(spamhaus_listed=None), CONFIG)
    assert keep is True
    assert reason is None


def test_keep_accepts_when_surbl_listed_is_none():
    """Same three-state contract for SURBL — unknown means 'no signal',
    not a rejection trigger."""
    keep, reason = filter_mod.keep(_ok(surbl_listed=None), CONFIG)
    assert keep is True
    assert reason is None


def test_keep_accepts_when_spamhaus_field_missing():
    """A missing field means the enricher returned empty dict (circuit
    breaker open). Same operational meaning as None: no signal, don't
    reject."""
    cand = _ok()
    del cand["spamhaus_listed"]
    keep, reason = filter_mod.keep(cand, CONFIG)
    assert keep is True
    assert reason is None


def test_keep_accepts_when_surbl_field_missing():
    cand = _ok()
    del cand["surbl_listed"]
    keep, reason = filter_mod.keep(cand, CONFIG)
    assert keep is True
    assert reason is None


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


def test_post_enrichment_filter_logs_dnsbl_signal_distribution(caplog):
    """The post-enrichment filter must emit a `DNSBL signal distribution`
    line so daily run reports can distinguish 'domain listed' (signal) from
    'DNSBL unavailable' (no signal). On a rate-limited resolver day this
    line is the canary that tells the operator coverage was degraded.
    """
    import logging

    cands = [
        _ok(name="aaa.com"),                                     # both known not-listed
        _ok(name="bbb.com", spamhaus_listed=None),               # spamhaus unknown
        _ok(name="ccc.com", surbl_listed=None),                  # surbl unknown
        _ok(name="ddd.com", spamhaus_listed=None,
            surbl_listed=None),                                  # both unknown
        _ok(name="eee.com", spamhaus_listed=True),               # rejected
    ]
    with caplog.at_level(logging.INFO, logger="scripts.filter"):
        survivors = filter_mod.filter_candidates_post_enrichment(cands, CONFIG)

    # The three None-bearing candidates pass through (None != True);
    # the True-bearing one rejects; aaa.com is clean. → 4 survivors.
    survivor_names = {c["name"] for c in survivors}
    assert survivor_names == {"aaa.com", "bbb.com", "ccc.com", "ddd.com"}

    # spamhaus_unknown: bbb + ddd = 2; surbl_unknown: ccc + ddd = 2.
    log_messages = " ".join(rec.message for rec in caplog.records)
    assert "spamhaus_listed=1" in log_messages
    assert "spamhaus_unknown=2" in log_messages
    assert "surbl_unknown=2" in log_messages


def test_post_enrichment_filter_skips_dnsbl_log_when_all_known(caplog):
    """When no candidates have unknown DNSBL signals, the distribution log
    line is suppressed — production runs on a healthy resolver day shouldn't
    have to scan past a 'all zeros' line."""
    import logging

    cands = [_ok(name="aaa.com"), _ok(name="bbb.com")]
    with caplog.at_level(logging.INFO, logger="scripts.filter"):
        filter_mod.filter_candidates_post_enrichment(cands, CONFIG)

    log_messages = " ".join(rec.message for rec in caplog.records)
    assert "DNSBL signal distribution" not in log_messages
