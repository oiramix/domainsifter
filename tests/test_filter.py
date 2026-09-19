"""Unit tests for scripts/filter.py."""

from __future__ import annotations

from scripts import filter as filter_mod

CONFIG = {
    "filter_thresholds": {
        "min_domain_length": 2,
        "max_domain_length": 30,
        "min_wayback_snapshots": 1,
    },
    "rejected_keywords": [
        "sex", "porn", "casino", "viagra", "hentai", "xtube", "tinbongda",
        "soap2day", "xxx",
    ],
    "rejected_keyword_prefixes": [
        "porn", "hentai", "xtube", "tinbongda", "soap2day", "xxx",
    ],
    "rejected_keyword_substrings": [
        # Round 1 (2026-05-24)
        "dating", "forex", "hookup", "payday", "pharmacy", "pills",
        "replica", "weightloss",
        # Round 2 (proposed 2026-09-19) — mirrors the additions proposed
        # for scripts/config.json after two compound-apex misses reached
        # the published list. Tests below pin both the new rejections and
        # the innocent names that must keep passing.
        "betting", "casino", "gambling", "viagra", "escort",
    ],
    "soft_signal_keywords": [
        "dating", "singles", "pump", "moonshot",
    ],
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
    """Token matching keeps case-insensitivity but requires the keyword to
    appear as a full token (split on hyphens/dots/digit boundaries) — the
    bare-substring 'bestcasinodeal' that the old matcher caught is
    intentionally NO longer caught (token model is strict equality).
    Hyphenation makes 'casino' a token again."""
    keep, reason = filter_mod.keep(_ok(name="best-CASINO-deal.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:casino"


# --- Token-aware matching (added 2026-05-17) -------------------------------

def test_keep_rejects_exact_token_with_hyphen_split():
    """african-sex.net → tokens ['african','sex','net']; 'sex' is in
    rejected_keywords exact list → REJECT."""
    keep, reason = filter_mod.keep(_ok(name="african-sex.net"), CONFIG)
    assert keep is False
    assert reason == "keyword:sex"


def test_keep_rejects_prefix_stem_with_letter_suffix():
    """xtubecinema.xyz → coarse token 'xtubecinema' is one letters-only
    label; prefix-stem 'xtube' matches via startswith → REJECT. This is
    the case the old substring matcher caught via inclusion; the new
    token matcher needs the prefix list to handle it."""
    keep, reason = filter_mod.keep(_ok(name="xtubecinema.xyz"), CONFIG)
    assert keep is False
    assert reason == "keyword:xtube"


def test_keep_rejects_letter_keyword_via_digit_split_fine_token():
    """hentai2.org → fine tokens ['hentai','2','org'] (digit boundary
    splits 'hentai2'); 'hentai' is in exact keywords → REJECT."""
    keep, reason = filter_mod.keep(_ok(name="hentai2.org"), CONFIG)
    assert keep is False
    assert reason == "keyword:hentai"


def test_keep_rejects_letter_keyword_with_higher_digit_suffix():
    keep, reason = filter_mod.keep(_ok(name="porn99.org"), CONFIG)
    assert keep is False
    assert reason == "keyword:porn"


def test_keep_rejects_xxx_with_digit_suffix():
    keep, reason = filter_mod.keep(_ok(name="xxx55.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:xxx"


def test_keep_rejects_prefix_stem_with_letter_suffix_combo():
    """xtubeXXX → coarse token 'xtubexxx' startswith 'xtube' → REJECT."""
    keep, reason = filter_mod.keep(_ok(name="xtubeXXX.org"), CONFIG)
    assert keep is False
    # 'xxx' also matches exact, but the exact pass runs first, so reason
    # could be either depending on iteration order. Just check rejection.
    assert reason in ("keyword:xtube", "keyword:xxx")


def test_keep_rejects_alphanumeric_keyword_via_coarse_match():
    """soap2day.com → coarse token 'soap2day' equals exact keyword."""
    keep, reason = filter_mod.keep(_ok(name="soap2day.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:soap2day"


def test_keep_rejects_gambling_prefix_stem_with_digit_suffix():
    """tinbongda360.net → fine tokens ['tinbongda','360','net']; exact
    'tinbongda' in keywords → REJECT. (Also reachable via prefix on the
    coarse 'tinbongda360' token, but exact wins.)"""
    keep, reason = filter_mod.keep(_ok(name="tinbongda360.net"), CONFIG)
    assert keep is False
    assert reason == "keyword:tinbongda"


def test_keep_accepts_essex_no_substring_false_positive():
    """'essex.com' must NOT match 'sex' — coarse token 'essex' is not
    equal to 'sex' and (because 'sex' is intentionally NOT in
    rejected_keyword_prefixes) doesn't match by startswith either."""
    keep, reason = filter_mod.keep(_ok(name="essex.com"), CONFIG)
    assert keep is True
    assert reason is None


def test_keep_accepts_unisex_no_substring_false_positive():
    """unisex.net must NOT match 'sex' for the same reason as essex.com.
    Bonus: unisex doesn't START with 'sex' even if 'sex' were prefixed."""
    keep, reason = filter_mod.keep(_ok(name="unisex.net"), CONFIG)
    assert keep is True
    assert reason is None


def test_keep_accepts_camera_no_substring_false_positive():
    """camera.com must NOT match 'cam' — 'cam' is intentionally exact-only
    (not in rejected_keyword_prefixes). Camera tokens are ['camera','com']."""
    cfg = {**CONFIG, "rejected_keywords": [*CONFIG["rejected_keywords"], "cam"]}
    keep, reason = filter_mod.keep(_ok(name="camera.com"), cfg)
    assert keep is True
    assert reason is None


def test_keep_accepts_construction_no_false_positive():
    keep, reason = filter_mod.keep(_ok(name="construction.com"), CONFIG)
    assert keep is True


def test_keep_accepts_deepsand_no_false_positive():
    keep, reason = filter_mod.keep(_ok(name="deepsand.net"), CONFIG)
    assert keep is True


def test_keep_accepts_mastermining_no_false_positive():
    keep, reason = filter_mod.keep(_ok(name="mastermining.net"), CONFIG)
    assert keep is True


def test_soft_signal_does_not_reject_in_filter():
    """Soft-signal matches do NOT reject. The verdict computation in
    output.py reads has_soft_signal() to force a Caution verdict, but
    the candidate survives filter rejection. Demo uses 'singles' here —
    'dating' moved to rejected_keyword_substrings 2026-05-24 and so
    would hard-reject through `keep`."""
    keep, reason = filter_mod.keep(
        _ok(name="urbansinglesgroup.net"), CONFIG,
    )
    assert keep is True
    assert reason is None


def test_has_soft_signal_detects_dating_token():
    assert filter_mod.has_soft_signal("singlesdatingsingles.net", CONFIG) is True


def test_has_soft_signal_returns_false_for_clean_name():
    assert filter_mod.has_soft_signal("marketglow.com", CONFIG) is False


def test_has_soft_signal_detects_crypto_pump():
    """pump99.com → fine tokens ['pump','99','com'] → 'pump' matches."""
    assert filter_mod.has_soft_signal("pump99.com", CONFIG) is True


def test_has_soft_signal_tolerates_missing_config_key():
    """When config has no soft_signal_keywords entry, has_soft_signal
    returns False — never raises."""
    cfg = {"filter_thresholds": {}, "rejected_keywords": []}
    assert filter_mod.has_soft_signal("anything.com", cfg) is False


# --- Substring rejection (Round 1, added 2026-05-24) -----------------------
#
# Eight scam-vertical keywords that production saw slip through the token
# matchers because spam concatenates them into compound names (richmendating,
# miraclepills, onlinepharmacy). Substring matching catches all of them.


def test_substring_reject_dating_in_compound_apex():
    """The production miss: richmendating.net — token model yields
    {'richmendating', 'net'}; neither equals 'dating' and 'richmendating'
    does not start with 'dating'. Substring catches it."""
    keep, reason = filter_mod.keep(_ok(name="richmendating.net"), CONFIG)
    assert keep is False
    assert reason == "keyword:dating"


def test_substring_reject_hookup_in_compound_apex():
    keep, reason = filter_mod.keep(_ok(name="casualhookup.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:hookup"


def test_substring_reject_payday_in_middle_of_apex():
    """'payday' sits in the middle of 'fastpaydaynow' — neither prefix
    nor exact would catch it. Substring does."""
    keep, reason = filter_mod.keep(_ok(name="fastpaydaynow.org"), CONFIG)
    assert keep is False
    assert reason == "keyword:payday"


def test_substring_reject_forex_at_start_of_apex():
    keep, reason = filter_mod.keep(_ok(name="forextrading101.net"), CONFIG)
    assert keep is False
    assert reason == "keyword:forex"


def test_substring_reject_pills_in_compound_apex():
    keep, reason = filter_mod.keep(_ok(name="miraclepills.shop"), CONFIG)
    assert keep is False
    assert reason == "keyword:pills"


def test_substring_reject_weightloss_in_compound_apex():
    keep, reason = filter_mod.keep(_ok(name="fastweightloss.info"), CONFIG)
    assert keep is False
    assert reason == "keyword:weightloss"


def test_substring_reject_replica_at_start_of_apex():
    keep, reason = filter_mod.keep(_ok(name="replicawatches.store"), CONFIG)
    assert keep is False
    assert reason == "keyword:replica"


def test_substring_reject_pharmacy_in_compound_apex():
    keep, reason = filter_mod.keep(_ok(name="onlinepharmacy.org"), CONFIG)
    assert keep is False
    assert reason == "keyword:pharmacy"


def test_substring_reject_is_case_insensitive():
    keep, reason = filter_mod.keep(_ok(name="ReplicaWatches.STORE"), CONFIG)
    assert keep is False
    assert reason == "keyword:replica"


def test_substring_reject_ignores_tld_label():
    """The substring matcher looks at the apex only — .pharmacy is a real
    TLD, and a clean 'siteshop' apex on it must not be flagged just
    because the TLD characters happen to contain the keyword."""
    cand = _ok(name="siteshop.pharmacy")
    keep, reason = filter_mod.keep(cand, CONFIG)
    assert keep is True
    assert reason is None


def test_substring_reject_does_not_match_clean_compound():
    """marketglow.com (apex 'marketglow') contains none of the Round 1
    substrings — must pass cleanly."""
    keep, reason = filter_mod.keep(_ok(name="marketglow.com"), CONFIG)
    assert keep is True
    assert reason is None


def test_substring_reject_tolerates_missing_config_key():
    """When the config has no rejected_keyword_substrings key, the
    structural filter falls through cleanly (empty default, no reject)."""
    cfg = {k: v for k, v in CONFIG.items() if k != "rejected_keyword_substrings"}
    keep, reason = filter_mod.keep(_ok(name="richmendating.net"), cfg)
    assert keep is True
    assert reason is None


# --- Substring rejection (Round 2, proposed 2026-09-19) --------------------
#
# Same failure mode as Round 1, different vertical. Production published a
# gambling compound (apex shaped like "betting" + two more words, no
# separator) and a pharma compound ending in "viagra". Both keywords are in
# rejected_keywords, but the apex is a SINGLE token, so exact match never
# fires and neither stem is in rejected_keyword_prefixes.
#
# Names below are invented (CLAUDE.md rule #1); they reproduce the shape of
# the production misses, not the misses themselves.


def test_substring_reject_betting_in_compound_apex():
    """The production miss shape: one token, 'betting' buried at the start
    of a longer compound. Token model yields {'bettingglowmaster','com'} —
    no exact hit, no prefix entry. Substring catches it."""
    keep, reason = filter_mod.keep(_ok(name="bettingglowmaster.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:betting"


def test_substring_reject_betting_in_middle_of_apex():
    keep, reason = filter_mod.keep(_ok(name="sunreefbettinghub.net"), CONFIG)
    assert keep is False
    assert reason == "keyword:betting"


def test_substring_reject_casino_in_compound_apex():
    """'best-CASINO-deal.com' was already rejected via the hyphen tokens;
    the un-hyphenated compound was not, until Round 2."""
    keep, reason = filter_mod.keep(_ok(name="lanterncasinohub.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:casino"


def test_substring_reject_gambling_in_compound_apex():
    keep, reason = filter_mod.keep(_ok(name="tideblockgamblingclub.org"), CONFIG)
    assert keep is False
    assert reason == "keyword:gambling"


def test_substring_reject_viagra_at_end_of_apex():
    """Second production miss shape: pharma stem trailing a geo/qualifier
    compound. Nothing splits it, so only substring reaches it."""
    keep, reason = filter_mod.keep(_ok(name="coppernestcanadaviagra.net"), CONFIG)
    assert keep is False
    assert reason == "keyword:viagra"


def test_substring_reject_escort_in_compound_apex():
    keep, reason = filter_mod.keep(_ok(name="tideblockescortagency.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:escort"


def test_round2_substring_reject_is_case_insensitive():
    keep, reason = filter_mod.keep(_ok(name="BettingGlowMaster.COM"), CONFIG)
    assert keep is False
    assert reason == "keyword:betting"


def test_round2_substring_reject_ignores_tld_label():
    """.casino is a real TLD — a clean apex on it must not be flagged just
    because the TLD characters contain the keyword (same contract as
    siteshop.pharmacy in Round 1)."""
    keep, reason = filter_mod.keep(_ok(name="marketglow.casino"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_exact_token_still_wins_over_substring():
    """When both matchers could fire, the hard-keyword matcher runs first
    and the reason string is unchanged — log aggregation stays stable."""
    keep, reason = filter_mod.keep(_ok(name="best-CASINO-deal.com"), CONFIG)
    assert keep is False
    assert reason == "keyword:casino"


# --- Round 2 regression: innocent letter sequences must still be KEPT ------
#
# Each name below contains a near-miss of a Round 2 stem. They are the
# reason the curation rule demands >=5 chars and compound-specific stems:
# a shorter stem ('bet', 'cas', 'sort', 'gamble') would eat all of these.


def test_round2_keeps_better_not_betting():
    """'better' shares 'bet' with 'betting' but not the full stem."""
    keep, reason = filter_mod.keep(_ok(name="betterhomesglow.com"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_keeps_alphabet_compound():
    keep, reason = filter_mod.keep(_ok(name="alphabetstorehouse.com"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_keeps_showcasing_not_casino():
    """'showcasing' contains 'casin'… but ends 'ing', not 'o'."""
    keep, reason = filter_mod.keep(_ok(name="showcasingcrafts.com"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_keeps_gamble_surname_not_gambling():
    """'gamble' / 'gambler' are not 'gambling' — the stem is deliberately
    the -ing form, which is what the abuse vertical actually uses."""
    keep, reason = filter_mod.keep(_ok(name="gamblefarmstead.org"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_keeps_resort_not_escort():
    """'resort' ends in 'esort', one letter short of 'escort'."""
    keep, reason = filter_mod.keep(_ok(name="lakesideresortgroup.com"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_keeps_olive_grove_not_viagra():
    keep, reason = filter_mod.keep(_ok(name="olivegrovemarket.com"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_keeps_specialist_not_cialis():
    """'specialist' CONTAINS 'cialis' (spe-CIALIS-t), and so does
    'socialist'. That is exactly why 'cialis' was measured and then
    rejected as a substring candidate — 84% of its zone hits are
    specialist/socialist/commercialista names. This test fails loudly if
    anyone ever adds it."""
    keep, reason = filter_mod.keep(_ok(name="kidneyspecialistgroup.org"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_keeps_asteroid_not_steroid():
    """'asteroid' contains 'steroid'. 68% of zone hits for 'steroid' are
    asteroid names, so 'steroid' stays off the substring list."""
    keep, reason = filter_mod.keep(_ok(name="asteroidbeltclub.org"), CONFIG)
    assert keep is True
    assert reason is None


def test_round2_keeps_short_stems_unmatched():
    """The 2026-05-17 migration deliberately keeps 'cam', 'tube', 'sex',
    'slot' out of prefix/substring matching. Round 2 does NOT undo that:
    these compounds must still pass even though the short stems are in
    rejected_keywords."""
    cfg = {
        **CONFIG,
        "rejected_keywords": [*CONFIG["rejected_keywords"], "cam", "tube", "slot"],
    }
    for name in (
        "camshaftpartsglow.com",
        "tubefittingsdepot.com",
        "slotcarcollectors.org",
        "essexvillagetrust.org",
    ):
        keep, reason = filter_mod.keep(_ok(name=name), cfg)
        assert keep is True, f"{name} should be kept, got {reason}"


def test_round2_known_false_positive_is_documented():
    """Accepted cost, in the same spirit as 'dating' catching 'updating':
    a personal-brand name of the form 'Olivia Gra…' contains 'viagra'
    (oli-VIAGRA-…) and WILL be rejected. Measured frequency in the
    44.5M-name zone snapshots: 21 names, 1.6% of all 'viagra' hits. This
    test documents the behaviour rather than claiming it is correct."""
    keep, reason = filter_mod.keep(_ok(name="oliviagravelstudio.net"), CONFIG)
    assert keep is False
    assert reason == "keyword:viagra"


def test_round2_curation_rule_holds_in_live_config():
    """Guardrail on scripts/config.json itself: every substring entry must
    be >=5 chars and lowercase, per the curation rule in
    `_keyword_lists_doc`. Short stems in this list are how the 2026-05-17
    false-positive class comes back."""
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    live = json.loads((repo_root / "scripts" / "config.json").read_text(encoding="utf-8"))
    for entry in live.get("rejected_keyword_substrings", []):
        assert len(entry) >= 5, f"{entry!r} is shorter than the 5-char minimum"
        assert entry == entry.lower(), f"{entry!r} must be lowercase"


def test_tokenize_produces_expected_token_sets():
    """Direct sanity check of the tokenizer — covered indirectly by the
    keep_* tests above, but explicit here so regressions in _tokenize
    fail loud."""
    coarse, fine = filter_mod._tokenize("hentai2.org")
    assert coarse == {"hentai2", "org"}
    assert fine == {"hentai", "2", "org"}

    coarse, fine = filter_mod._tokenize("African-Sex.NET")
    assert coarse == {"african", "sex", "net"}
    # No digit transitions → fine equals coarse here.
    assert fine == coarse

    coarse, fine = filter_mod._tokenize("soap2day.com")
    assert coarse == {"soap2day", "com"}
    assert fine == {"soap", "2", "day", "com"}


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


def test_keep_passes_wayback_unknown_candidate():
    """Three-state semantics (2026-05-17): a candidate carrying
    wayback_unknown=True (breaker open / call failed) PASSES the post-
    enrichment filter, distinct from the no_wayback_confirmed reject when
    snapshots=0. Without this, good domains on flaky-Wayback days are
    silently dropped."""
    cand = _ok()
    del cand["wayback_snapshots"]
    cand["wayback_unknown"] = True
    keep, reason = filter_mod.keep(cand, CONFIG)
    assert keep is True
    assert reason is None


def test_filter_logs_wayback_unknown_pass_through_distinctly(caplog):
    """`wayback_unknown` candidates that pass the filter must be counted in
    a SEPARATE log line from rejection counts so an operator scanning a
    flaky-Wayback-day run can see how many candidates were affected. Also
    confirms the no_wayback reject is renamed to `no_wayback_confirmed`."""
    import logging
    cands = [
        _ok(name="ok.com"),                                   # passes cleanly
        _ok(name="zero.com", wayback_snapshots=0),            # rejected
        {**_ok(name="unknown.com"), "wayback_unknown": True}, # pass-through
        # ↑ retain the other defaults so only the wayback path differs
    ]
    cands[2].pop("wayback_snapshots", None)
    with caplog.at_level(logging.INFO, logger="scripts.filter"):
        filter_mod.filter_candidates_post_enrichment(cands, CONFIG)
    msgs = " ".join(rec.message for rec in caplog.records)
    assert "no_wayback_confirmed" in msgs
    assert "wayback_unknown_carried_forward" in msgs
    assert "informational pass-throughs" in msgs


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


# --- snapshot_category rejection (Phase 4 wire-in, 2026-05-20) -------------


class TestSnapshotToxicRejection:
    def test_toxic_rejected_with_snapshot_toxic_reason(self):
        cand = _ok(snapshot_category="toxic")
        keep, reason = filter_mod.keep_post_enrichment(cand, CONFIG)
        assert keep is False
        assert reason == "snapshot_toxic"

    def test_legitimate_passes(self):
        cand = _ok(snapshot_category="legitimate")
        keep, reason = filter_mod.keep_post_enrichment(cand, CONFIG)
        assert keep is True
        assert reason is None

    def test_parked_passes_filter_handled_in_verdict(self):
        # Parked is verdict-downgrade, not filter-reject — must pass here.
        cand = _ok(snapshot_category="parked")
        keep, reason = filter_mod.keep_post_enrichment(cand, CONFIG)
        assert keep is True
        assert reason is None

    def test_empty_passes_filter_handled_in_verdict(self):
        # Same as parked — verdict-downgrade, not reject.
        cand = _ok(snapshot_category="empty")
        keep, reason = filter_mod.keep_post_enrichment(cand, CONFIG)
        assert keep is True
        assert reason is None

    def test_unknown_passes_no_signal_no_reject(self):
        # `unknown` is the soft-fail path; must never reject.
        cand = _ok(snapshot_category="unknown")
        keep, reason = filter_mod.keep_post_enrichment(cand, CONFIG)
        assert keep is True
        assert reason is None

    def test_missing_snapshot_category_passes(self):
        # Pre-Phase-4 entries (legacy carryover, sample data) have no
        # snapshot_category at all — must pass to avoid breaking the
        # migration window.
        cand = _ok()  # no snapshot_category key
        keep, reason = filter_mod.keep_post_enrichment(cand, CONFIG)
        assert keep is True
        assert reason is None

    def test_toxic_beats_clean_wayback(self):
        # Toxic rejection fires regardless of wayback count — content
        # check overrides count check.
        cand = _ok(snapshot_category="toxic", wayback_snapshots=10_000)
        keep, reason = filter_mod.keep_post_enrichment(cand, CONFIG)
        assert keep is False
        assert reason == "snapshot_toxic"

    def test_filter_candidates_post_enrichment_evicts_toxic(self):
        # Integration: full filter call evicts toxic entries from the list.
        cands = [
            _ok(name="good.com", snapshot_category="legitimate"),
            _ok(name="bad.com", snapshot_category="toxic"),
            _ok(name="park.com", snapshot_category="parked"),
            _ok(name="empt.com", snapshot_category="empty"),
            _ok(name="huh.com", snapshot_category="unknown"),
        ]
        kept = filter_mod.filter_candidates_post_enrichment(cands, CONFIG)
        names = {c["name"] for c in kept}
        assert "bad.com" not in names
        assert names == {"good.com", "park.com", "empt.com", "huh.com"}
