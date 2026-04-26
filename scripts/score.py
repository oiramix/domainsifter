"""Composite scoring for surviving candidates.

Each component is normalized to [0, 1] and then weighted-summed using
`scoring_weights` from config. The final score is mapped to an integer
in [0, 100] for display.

Components:
    wayback_snapshots — log-scaled. 0 snapshots → 0.0, 1 → ~0.15,
                        10 → ~0.5, 100 → ~0.75, 1000+ → ~1.0.
                        Log scale because the marginal value of a 100th
                        snapshot is much smaller than the 1st.
    open_page_rank    — already 0-10 from OPR API. Linearly normalized.
                        Most expired domains sit at 0; anything > 2 is
                        already meaningful.
    cert_history      — boolean. True → 1.0, False → 0.0.
    domain_length     — inverted, so shorter = better. min_len → 1.0,
                        max_len → 0.0, linear in between. Pulls scoring
                        toward concise, more memorable names.

Missing fields contribute 0.0 to that component (the domain still gets
scored on whatever signals we DID get — we don't punish enrichment gaps
with negative weight).
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


def _norm_wayback(snapshots: int) -> float:
    if snapshots <= 0:
        return 0.0
    return min(1.0, math.log10(snapshots + 1) / 3.0)


def _norm_opr(opr_score: float) -> float:
    if opr_score <= 0:
        return 0.0
    return min(1.0, opr_score / 10.0)


def _norm_cert(has_cert: bool) -> float:
    return 1.0 if has_cert else 0.0


def _norm_length(name: str, min_len: int, max_len: int) -> float:
    apex_label = name.split(".", 1)[0]
    n = len(apex_label)
    if n <= min_len:
        return 1.0
    if n >= max_len:
        return 0.0
    span = max_len - min_len
    if span <= 0:
        return 0.0
    return 1.0 - (n - min_len) / span


def score_candidate(candidate: dict, config: dict) -> int:
    """Return an integer score in [0, 100] for one enriched candidate."""
    weights = config.get("scoring_weights", {})
    thresholds = config.get("filter_thresholds", {})
    min_len = thresholds.get("min_domain_length", 2)
    max_len = thresholds.get("max_domain_length", 30)

    components = {
        "wayback_snapshots": _norm_wayback(int(candidate.get("wayback_snapshots") or 0)),
        "open_page_rank": _norm_opr(float(candidate.get("open_page_rank") or 0.0)),
        "cert_history": _norm_cert(bool(candidate.get("cert_history"))),
        "domain_length": _norm_length(candidate.get("name", ""), min_len, max_len),
    }

    total_weight = sum(weights.get(k, 0.0) for k in components)
    if total_weight <= 0:
        return 0
    weighted = sum(components[k] * weights.get(k, 0.0) for k in components)
    raw = weighted / total_weight
    return max(0, min(100, round(raw * 100)))


def score_candidates(candidates: list[dict], config: dict) -> list[dict]:
    """Mutate each candidate in-place to add a `score` field, then return the
    list sorted by score descending (ties broken by name ascending for
    determinism)."""
    for cand in candidates:
        cand["score"] = score_candidate(cand, config)
    candidates.sort(key=lambda c: (-c["score"], c.get("name", "")))
    if candidates:
        logger.info(
            "Scored %d candidates; top=%d, median=%d, bottom=%d",
            len(candidates),
            candidates[0]["score"],
            candidates[len(candidates) // 2]["score"],
            candidates[-1]["score"],
        )
    return candidates
