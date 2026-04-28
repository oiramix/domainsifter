"""Unit tests for scripts/lexical_filter.py.

Two coverage axes:
  - REJECT cases: each rule fires correctly on a designed-to-fail input.
  - KEEP cases: real-looking domain bases (and the 20 sample-domain roots)
    pass through unscathed. Per project guidance, the filter is permissive —
    if any of these fail, threshold tuning has gone too aggressive.
"""

from __future__ import annotations

import pytest

from scripts import lexical_filter


# --- helpers -----------------------------------------------------------------


def _ok(name: str, config: dict | None = None) -> None:
    keep, reason = lexical_filter.keep_lexical(name, config or {})
    assert keep, f"expected keep for {name!r}, got reject({reason})"


def _reject(name: str, contains: str, config: dict | None = None) -> None:
    keep, reason = lexical_filter.keep_lexical(name, config or {})
    assert not keep, f"expected reject for {name!r}, got keep"
    assert reason is not None and contains in reason, (
        f"expected reason containing {contains!r}, got {reason!r}"
    )


# --- empty / edge-case inputs ------------------------------------------------


def test_empty_name_rejected():
    keep, reason = lexical_filter.keep_lexical("", {})
    assert not keep and reason == "empty_name"


def test_empty_apex_rejected():
    keep, reason = lexical_filter.keep_lexical(".com", {})
    assert not keep and reason == "empty_apex"


# --- Pass 2A: digit_ratio ----------------------------------------------------


def test_rejects_high_digit_ratio():
    _reject("78win012.com", "digit_ratio")     # 5/8 = 62%
    _reject("shop24h7.com", "digit_ratio")     # 4/8 = 50%


def test_keeps_low_digit_ratio():
    _ok("ferrokind1.com")  # 1/10 = 10% — under threshold


# --- Pass 2A: vowel_ratio ----------------------------------------------------


def test_rejects_low_vowel_ratio():
    # 0/7 vowels -> 0%
    _reject("kvkbhmt.com", "vowel_ratio")


def test_keeps_normal_vowel_ratio():
    _ok("market.com")          # 2/6 = 33%
    _ok("rhythm.com")           # treated as 1/6 because 'y' counts as vowel


# --- Pass 2A: shannon_entropy ------------------------------------------------


def test_rejects_high_entropy_random_string():
    # 12 distinct chars including enough vowels to clear vowel_ratio (>=15%)
    # and short-enough consonant runs to clear consonant_run, but uniform
    # distribution pushes entropy = log2(12) ≈ 3.58 > 3.5 → entropy rule fires.
    _reject("aebiojwqxkmu.com", "high_entropy")


def test_keeps_normal_entropy_word():
    _ok("kettleridge.com")
    _ok("lanterncreek.com")


# --- Pass 2A: repeat_run -----------------------------------------------------


def test_rejects_4plus_consecutive_same_char():
    _reject("aaaabigwin.com", "repeat_run")
    _reject("good0000.com", "digit_ratio")  # caught by digit_ratio first
    _reject("zzzzhello.com", "repeat_run")


def test_keeps_3_consecutive_same_char():
    # 3 in a row should NOT trip the >=4 rule
    _ok("greeen.com")  # has 'eee' (3) — pass


# --- Pass 2A: consonant_run --------------------------------------------------


def test_rejects_5plus_consonant_run():
    # 5 consonants in a row, no vowel break
    _reject("xprzbtmore.com", "consonant_run")


def test_keeps_4_consonant_run():
    # 4-cluster (e.g., "bstr") is allowed; default threshold rejects only at 6+
    _ok("abstract.com")  # a,b,s,t,r,a,c,t — 4 consonants between the two 'a's


# --- Pass 2B: pronounceability (trigram match) -------------------------------


def test_rejects_unpronounceable_random_string():
    # Bypasses 2A by having vowels and short length
    _reject("zqvwxbycaeiou.com", "high_entropy")  # falls to entropy first


def test_rejects_low_trigram_match():
    # Mostly-consonant + vowels in unnatural order
    keep, reason = lexical_filter.keep_lexical("xqkbqzvueuxq.com", {})
    assert not keep
    assert reason is not None
    # Either the entropy rule or the trigram rule should catch it.


# --- Permissive: real-looking domains pass through ---------------------------


@pytest.mark.parametrize(
    "name",
    [
        "marketglow.org",
        "tideblock.studio",
        "coppernest.org",
        "lumenpath.dev",
        "northvane.tech",
        "sablequill.online",
        "harborjune.app",
        "ferrokind.org",
        "glassbarrow.info",
        "midwayfern.site",
        "pinegrade.org",
        "warblerstack.dev",
        "anvilminute.live",
        "petrichorlab.tech",
        "kettleridge.store",
        "owlmoor.org",
        "quartzbloom.info",
        "ravinekey.app",
        "lanterncreek.org",
    ],
)
def test_keeps_sample_domain_roots(name):
    """The 19 invented-but-realistic sample domains MUST all pass.
    These are the brand of names we're trying to surface — a regression
    here means the filter has gone too aggressive."""
    _ok(name)


# --- threshold overrides via config ------------------------------------------


def test_thresholds_overridable_via_config():
    config = {"lexical_thresholds": {"max_digit_ratio": 0.10}}
    # 2 digits / 11 chars = 18% — exceeds custom 10% threshold but not default 30%
    _reject("ferro12kind.org", "digit_ratio", config=config)
    # Same name passes with default thresholds
    _ok("ferro12kind.org")


def test_thresholds_partial_override_keeps_other_defaults():
    """Overriding one key keeps other defaults — DEFAULTS dict gets merged."""
    config = {"lexical_thresholds": {"max_digit_ratio": 0.50}}
    # vowel_ratio default still applies
    _reject("kvkbhmt.com", "vowel_ratio", config=config)


# --- filter_candidates batch interface ---------------------------------------


def test_filter_candidates_returns_only_survivors_and_logs():
    candidates = [
        {"name": "lumenpath.org"},
        {"name": "78win012.com"},          # digit_ratio
        {"name": "kvkbhmt.studio"},         # vowel_ratio
        {"name": "marketglow.org"},
    ]
    kept = lexical_filter.filter_candidates(candidates, {})
    names = [c["name"] for c in kept]
    assert names == ["lumenpath.org", "marketglow.org"]


def test_filter_candidates_handles_empty_list():
    assert lexical_filter.filter_candidates([], {}) == []


# --- internals smoke checks --------------------------------------------------


def test_natural_trigrams_includes_common_english():
    """If the seed-word generator regresses, common syllables disappear and
    every real domain starts failing. Sentinel check."""
    s = lexical_filter._NATURAL_TRIGRAMS
    for tri in ("the", "ing", "ent", "ion", "ate", "ery", "lum", "men", "pat", "blo"):
        assert tri in s, f"expected {tri!r} in natural trigram set"


# --- Day-3 regression: random-letter junk that shipped on the live list -----
# The first real-data run (2026-04-28, commit 75f0992) published 300 domains,
# topped by "ckyy.xyz" — exactly the lexical garbage the filter was supposed
# to reject. Root cause: the old 20% trigram-match threshold combined with
# no absolute-count rule let single-trigram passes through ("pro" in "5pro",
# "and" in "anddi"). Three rules were tightened to fix this; these tests
# pin the regression so it can't silently re-open.


@pytest.mark.parametrize(
    "name",
    [
        "ckyy.xyz",         # 1 match (cky)
        "5pro.xyz",         # 1 alpha trigram (pro)
        "agkbet.live",      # 1 match (bet)
        "anddi.xyz",        # 1 match (and)
        "antmap.xyz",       # 1 match (ant)
        "appibo.xyz",       # 2 matches; ratio 0.50 < 0.55
        "aniyuu.xyz",       # 1 match
        "aphedu.xyz",       # 1 match
        "aoac.xyz",         # 1 match
        "aneu.xyz",         # 1 match
        "ampeh.xyz",        # 1 match
        "ifusi.xyz",        # 1 match
        "kunbet.xyz",       # 1 match
        "mwpha.xyz",        # 1 match
        "ptshow.xyz",       # 2 matches; ratio 0.50 < 0.55
        "tnida.xyz",        # 1 match
        "1gen.xyz",         # 1 alpha trigram
        "5app.xyz",         # 1 alpha trigram
        "bitetf.xyz",       # 1 match
        "lvbanv.xyz",       # 1 match
        "gflo.xyz",         # 1 match
        "chibi.xyz",        # 1 match
        "bragi.xyz",        # 1 match
        "oom.xyz",          # only 1 alpha trigram
        "masla.xyz",        # 1 match
        "gimid.xyz",        # 2 matches; <3 absolute
        "tyon.xyz",         # 1 match
        "treu.xyz",         # 1 match
        "thezum.xyz",       # 1 match
        "floqi.xyz",        # 1 match
        "hest.xyz",         # 2 matches; <3 absolute
        "delo.xyz",         # 2 matches; <3 absolute
        "apei.xyz",         # 1 match
        "apeguy.xyz",       # 1 match
        "ariald.xyz",       # 2 matches; ratio 0.50 < 0.55
        "llop.xyz",         # 2 matches; <3 absolute
        "gerr.xyz",         # 2 matches; <3 absolute
        "bowe.xyz",         # 2 matches; <3 absolute
        "arcee.xyz",        # 2 matches; <3 absolute
    ],
)
def test_rejects_day3_junk(name):
    """Each of these landed in the day-3 published list under the old rules.
    The new rules (threshold 0.55, min_alpha_trigram_matches=3) reject
    every single one. If a regression here, the filter has loosened."""
    keep, _ = lexical_filter.keep_lexical(name, {})
    assert not keep, f"{name!r} should be rejected as random-letter junk"


@pytest.mark.parametrize(
    "name",
    [
        # Sample-domain roots (these MUST keep passing — they're our brand
        # of legitimate name).
        "marketglow.com",
        "tideblock.io",
        "coppernest.org",
        "lumenpath.dev",
        "northpath.org",
        "ironforge.org",     # NEW seed coverage: "iron" + "forge"
        "bluehaven.org",     # NEW seed coverage: "blue" + "haven"
        "quartzbloom.info",
        "warblerstack.dev",
        "sablequill.online",
        "glassbarrow.info",
        "petrichorlab.tech",
        "lanterncreek.org",
        "frostledge.xyz",
        "silverbrook.store",
    ],
)
def test_keeps_real_compound_names_after_tightening(name):
    """The threshold/abs-count tightening must NOT regress real compound
    names. If any of these fail, the seed list needs another word added."""
    keep, reason = lexical_filter.keep_lexical(name, {})
    assert keep, f"{name!r} should pass; rejected by {reason}"
