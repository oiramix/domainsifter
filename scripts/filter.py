"""Reject rules.

Two-stage filtering:

    keep_structural — runs BEFORE enrichment. Cheap rules that need only
                      the candidate's name + tld + config. R1-R5.
    keep_post_enrichment — runs AFTER enrichment. Rules that need the
                           merged enrichment fields. R6-R10.

Splitting them lets the pipeline reject the obvious garbage (punycode,
all-numeric, banned keywords) before paying for any external API calls.
The original monolithic `keep()` is preserved as a wrapper that calls
both stages, so existing tests and any external callers keep working.

A candidate dict has the shape:
    {"name": "example.com", "tld": "com", ...enrichment fields...}

All thresholds and keyword lists come from config (CLAUDE.md rule #9).

Reject rules (any one triggers rejection):
    R1  punycode / IDN           — name starts with "xn--" or any label does
    R2  single-character apex    — labels[0] length < min_domain_length
    R3  too long                 — labels[0] length > max_domain_length
    R4  all-numeric apex         — labels[0] is digits only
    R5  rejected keyword         — any rejected_keywords substring in apex
    R6  spam_flagged             — Safe Browsing match
    R7  surbl_listed             — SURBL match
    R8  spamhaus_listed          — Spamhaus DBL match
    R9  no Wayback history       — wayback_snapshots < min_wayback_snapshots
                                   (only enforced when the field is present —
                                   a Wayback API failure leaves the field
                                   absent and we don't punish the domain)
    R10 spam_check field missing — when SAFE_BROWSING_KEY is configured, a
                                   missing spam_flagged field means the
                                   per-domain query failed; conservative
                                   reject. (Caller decides whether to
                                   enforce by passing strict_spam_check.)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _is_punycode(name: str) -> bool:
    return any(label.startswith("xn--") for label in name.split("."))


def keep_structural(candidate: dict, config: dict) -> tuple[bool, str | None]:
    """Pre-enrichment rejects (R1-R5). Cheap; runs before any network call."""
    name = candidate.get("name", "")
    if not name:
        return False, "empty_name"

    apex_label = name.split(".", 1)[0]
    thresholds = config.get("filter_thresholds", {})
    min_len = thresholds.get("min_domain_length", 2)
    max_len = thresholds.get("max_domain_length", 30)
    rejected_keywords = config.get("rejected_keywords", [])

    if _is_punycode(name):
        return False, "punycode"
    if len(apex_label) < min_len:
        return False, f"too_short(<{min_len})"
    if len(apex_label) > max_len:
        return False, f"too_long(>{max_len})"
    if apex_label.isdigit():
        return False, "all_numeric"
    apex_lower = apex_label.lower()
    for kw in rejected_keywords:
        if kw and kw.lower() in apex_lower:
            return False, f"keyword:{kw}"

    return True, None


def keep_post_enrichment(
    candidate: dict,
    config: dict,
    *,
    strict_spam_check: bool = True,
) -> tuple[bool, str | None]:
    """Post-enrichment rejects (R6-R10). Reads enrichment fields off the
    candidate dict; treats absent fields as 'unknown' (mostly tolerant)."""
    thresholds = config.get("filter_thresholds", {})
    min_wayback = thresholds.get("min_wayback_snapshots", 1)

    if candidate.get("spam_flagged") is True:
        return False, "spam_flagged"
    if candidate.get("surbl_listed") is True:
        return False, "surbl_listed"
    if candidate.get("spamhaus_listed") is True:
        return False, "spamhaus_listed"

    if "wayback_snapshots" in candidate:
        if candidate["wayback_snapshots"] < min_wayback:
            return False, f"no_wayback(<{min_wayback})"

    if strict_spam_check and "spam_flagged" not in candidate:
        return False, "spam_check_missing"

    return True, None


def keep(
    candidate: dict,
    config: dict,
    *,
    strict_spam_check: bool = True,
) -> tuple[bool, str | None]:
    """Apply structural + post-enrichment rules in sequence. Backward-
    compatible wrapper for callers that still want a single decision point."""
    ok, reason = keep_structural(candidate, config)
    if not ok:
        return False, reason
    return keep_post_enrichment(candidate, config, strict_spam_check=strict_spam_check)


def _apply(
    candidates: list[dict],
    decide,
    log_prefix: str,
) -> list[dict]:
    kept: list[dict] = []
    reasons: dict[str, int] = {}
    for cand in candidates:
        ok, reason = decide(cand)
        if ok:
            kept.append(cand)
        else:
            key = (reason or "unknown").split("(", 1)[0]
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        logger.info("%s rejections: %s", log_prefix, dict(sorted(reasons.items())))
    logger.info("%s kept %d / %d candidates", log_prefix, len(kept), len(candidates))
    return kept


def filter_candidates_structural(candidates: list[dict], config: dict) -> list[dict]:
    """Pre-enrichment filter — keeps only candidates that pass R1-R5."""
    return _apply(candidates, lambda c: keep_structural(c, config), "Structural filter")


def filter_candidates_post_enrichment(
    candidates: list[dict],
    config: dict,
    *,
    strict_spam_check: bool = True,
) -> list[dict]:
    """Post-enrichment filter — keeps only candidates that pass R6-R10."""
    return _apply(
        candidates,
        lambda c: keep_post_enrichment(c, config, strict_spam_check=strict_spam_check),
        "Post-enrichment filter",
    )


def filter_candidates(
    candidates: list[dict],
    config: dict,
    *,
    strict_spam_check: bool = True,
) -> list[dict]:
    """Apply all reject rules in one pass. Backward-compatible — preserved
    for tests and callers that don't want to split the stages."""
    return _apply(
        candidates,
        lambda c: keep(c, config, strict_spam_check=strict_spam_check),
        "Filter",
    )
