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

NULL-AWARE NORMALIZATION (changed 2026-04-30):
A field that is None means "we don't know" — the enrichment call failed,
or we never tried. The score is computed by averaging over POPULATED
components only:

    score = sum(value × weight for populated) / sum(weight for populated)

This means a domain with `wayback=None` but populated OPR/cert/length
scores on what we DO know rather than being mathematically capped at
~70 by the missing wayback weight (0.3) sitting in the denominator.

Previously we coerced missing → 0, which made the score formula treat
"unknown" as "absent of evidence = bad." That artificially squashed
scores when external APIs were flaky and was the proximate cause of
top-score=27 / median=10 on the day-3 published list.

If ALL components are None (degenerate input — no name, no enrichment),
score_candidate returns None and score_candidates drops the entry.
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


def score_candidate(candidate: dict, config: dict) -> int | None:
    """Return an integer score in [0, 100] for one candidate, or None when
    the candidate has no populated components to score on.

    A component is "populated" if its source field is not None. domain_length
    is populated whenever the candidate has a non-empty name.
    """
    weights = config.get("scoring_weights", {})
    thresholds = config.get("filter_thresholds", {})
    min_len = thresholds.get("min_domain_length", 2)
    max_len = thresholds.get("max_domain_length", 30)

    # (key, normalized_value | None). None = "missing, exclude from average".
    components: list[tuple[str, float | None]] = []

    wb = candidate.get("wayback_snapshots")
    components.append(
        ("wayback_snapshots", _norm_wayback(int(wb)) if wb is not None else None)
    )

    opr = candidate.get("open_page_rank")
    components.append(
        ("open_page_rank", _norm_opr(float(opr)) if opr is not None else None)
    )

    ch = candidate.get("cert_history")
    components.append(
        ("cert_history", _norm_cert(bool(ch)) if ch is not None else None)
    )

    name = candidate.get("name", "")
    components.append(
        ("domain_length", _norm_length(name, min_len, max_len) if name else None)
    )

    populated = [(k, v) for k, v in components if v is not None]
    if not populated:
        return None

    total_weight = sum(weights.get(k, 0.0) for k, _ in populated)
    if total_weight <= 0:
        return 0
    weighted = sum(v * weights.get(k, 0.0) for k, v in populated)
    raw = weighted / total_weight
    return max(0, min(100, round(raw * 100)))


def score_candidates(candidates: list[dict], config: dict) -> list[dict]:
    """Mutate each candidate in-place to add a `score` field, then return the
    list sorted by score descending (ties broken by name ascending for
    determinism). Candidates that score None (no populated components) are
    dropped from the list — they shouldn't reach the published payload."""
    for cand in candidates:
        cand["score"] = score_candidate(cand, config)
    # Drop unscoreable rows in-place so the caller's reference reflects the
    # filtered set (the pipeline keeps using `survivors` after this call).
    dropped = [c for c in candidates if c.get("score") is None]
    if dropped:
        logger.info("Dropped %d candidates with no scoreable signal", len(dropped))
    candidates[:] = [c for c in candidates if c.get("score") is not None]
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
