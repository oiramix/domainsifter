"""Reject rules.

Takes an enriched candidate dict and returns either:
    (True, None)             — keep the candidate
    (False, "<reason>")       — reject, reason logged for observability

A candidate dict has the shape:
    {
        "name": "example.com",
        "tld": "com",
        # ...plus whatever fields the enrichment modules merged in
    }

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


def keep(
    candidate: dict,
    config: dict,
    *,
    strict_spam_check: bool = True,
) -> tuple[bool, str | None]:
    """Apply reject rules; return (keep, reject_reason)."""
    name = candidate.get("name", "")
    if not name:
        return False, "empty_name"

    apex_label = name.split(".", 1)[0]
    thresholds = config.get("filter_thresholds", {})
    min_len = thresholds.get("min_domain_length", 2)
    max_len = thresholds.get("max_domain_length", 30)
    min_wayback = thresholds.get("min_wayback_snapshots", 1)
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


def filter_candidates(
    candidates: list[dict],
    config: dict,
    *,
    strict_spam_check: bool = True,
) -> list[dict]:
    """Apply `keep` to every candidate, log per-rule rejection counts, return survivors."""
    kept: list[dict] = []
    reasons: dict[str, int] = {}
    for cand in candidates:
        ok, reason = keep(cand, config, strict_spam_check=strict_spam_check)
        if ok:
            kept.append(cand)
        else:
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + 1
    if reasons:
        logger.info("Filter rejections: %s", dict(sorted(reasons.items())))
    logger.info("Filter kept %d / %d candidates", len(kept), len(candidates))
    return kept
