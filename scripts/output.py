"""Write the daily JSON contract consumed by the Astro frontend.

The site reads `src/data/daily-domains.json`. The shape is locked in
PLAN.md Principle 5 with three schema migrations applied:
  - 2026-04-27 evening: single `affiliate_link` → `registrars[]` array
  - 2026-04-28 evening: added `total_candidates_evaluated` (top-level) so
    the frontend can render "Showing N of M candidates evaluated today"
    without inventing a count.
  - 2026-05-17: added `verdict` per-domain field ("Clean"/"Promising"/
    "Caution"). Previously computed client-side from score alone in
    DomainTable.astro and generate_newsletter.py; the new tightened
    Promising rule needs wayback + OPR + cc_source_domain_count, and
    soft-signal keyword detection lives in filter.py — easier to keep
    one Python implementation than to replicate the rule (and the
    soft-signal config lookup) in two places. Frontend / email fall
    back to score-only if the field is absent (sample data, old JSON).

Output shape:
    {
        "generated_at": "2026-04-27T06:00:00Z",
        "domain_count": 47,                   # passed all filters AND quality floor
        "total_candidates_evaluated": 1000,   # entered enrichment phase
        "domains": [
            {
                "name": "example.com", "tld": "com", "dropped_date": "2026-04-26",
                "wayback_snapshots": 142, "wayback_last_snapshot": "2024-08-15",
                "open_page_rank": 3.7, "cert_history": true,
                "previous_registrar": "GoDaddy", "score": 78,
                "cc_source_domain_count": 247, "verdict": "Clean",
                "registrars": [{"name": "Namecheap", "url": "https://..."}, ...]
            }
        ]
    }

Quality floor (added 2026-04-28 in response to day-3 publishing 300 random-
letter domains with mean score 12.1):
  - publish_min_score                       — drop candidates below this
  - publish_min_enrichment_completeness     — drop candidates with too few
                                               enrichment fields populated
                                               (fraction in [0.0, 1.0])

The floor is applied BEFORE the publication cap, so the cap is still a
CEILING (never pads with weak rows) and the floor is the lower bound.

The registrars list comes from config.registrars and preserves order.
The {name} placeholder in each `link_template` is substituted with the
apex domain via plain str.replace — NOT str.format. The current Namecheap
URL contains literal `%3D` (`=`) and similar percent-encoded characters
that `.format()` would either error on or mishandle as positional refs.

`write_output(...)` takes already-scored, already-sorted candidates and:
- applies the quality floor (publish_min_score, publish_min_enrichment_completeness)
- caps at config.max_candidates_for_publication (still a CEILING)
- builds each candidate's registrars list from config.registrars
- projects each candidate to ONLY the contract fields (no internal
  enrichment metadata leaks into the public JSON)
- writes atomically (temp file + os.replace) so partial writes never
  serve a half-baked file to Cloudflare Pages
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts import filter as filter_mod

logger = logging.getLogger(__name__)

CONTRACT_FIELDS = (
    "name",
    "tld",
    "dropped_date",
    "wayback_snapshots",
    "wayback_last_snapshot",
    "open_page_rank",
    "cert_history",
    "previous_registrar",
    "score",
    "registrars",
    # Availability validation fields (added 2026-04-29).
    # Optional in the published payload — frontend can render or ignore.
    "rdap_status",
    "rdap_expiration",
    "availability_verified_at",
    # Persistent rolling list fields (added 2026-04-30).
    # first_seen_date is set on first capture and never modified after;
    # last_validated_date is updated each day a successful zone check
    # confirms the domain is still absent from the registry zone;
    # days_listed is derived (today − first_seen_date) at write time.
    "first_seen_date",
    "last_validated_date",
    "days_listed",
    # Common Crawl backlinks (added 2026-05-14). Integer count of distinct
    # source domains observed linking to the apex in the latest CC release;
    # null when the apex isn't in that release's graph at all. Deliberately
    # NOT added to _ENRICHMENT_FIELDS_FOR_COMPLETENESS — absence from the CC
    # graph is informational, not a quality deficit.
    "cc_source_domain_count",
    # Server-computed verdict (added 2026-05-17). See `_compute_verdict`.
    "verdict",
    # Wayback-unknown flag + carryover age counter (added 2026-05-17).
    # Set True when scripts/enrichment/wayback.py couldn't reach Wayback
    # (breaker open or per-call failure). Persisted to JSON so tomorrow's
    # carryover.age_out_wayback_unknown can increment the attempts counter
    # and eventually drop the entry after DEFAULT_MAX_WAYBACK_UNKNOWN_DAYS.
    # Absence of the flag means wayback either succeeded or was never
    # attempted for this candidate — both safe.
    "wayback_unknown",
    "wayback_unknown_attempts",
    # Snapshot content classifier outputs (added 2026-05-20, Stage 4b
    # in pipeline.py). Five categories: legitimate / parked / toxic /
    # empty / unknown. `toxic` is rejected by filter.keep_post_enrichment
    # before we get here; `parked` / `empty` force Caution in
    # _compute_verdict. The wayback_excerpt itself is NOT in this
    # contract — it lives in the sidecar src/data/wayback_excerpts.json,
    # keyed by name, so the per-day daily-domains.json stays small for
    # the frontend's first-paint budget.
    "snapshot_category",
    "snapshot_classifier_version",
)

# Enrichment fields used to compute completeness ratio. A candidate's
# completeness = (count of these fields that are not null) / len(this tuple).
# Excludes pipeline-mandatory fields (name, tld, dropped_date, score) which
# are never null in valid candidates.
_ENRICHMENT_FIELDS_FOR_COMPLETENESS = (
    "wayback_snapshots",
    "wayback_last_snapshot",
    "open_page_rank",
    "cert_history",
    "previous_registrar",
)


def _build_registrars(name: str, configured: list[dict]) -> list[dict]:
    """Substitute {name} in every configured registrar's link_template.

    Order is preserved from config — that's the order the popover renders.
    Plain str.replace, NOT str.format: the templates contain literal
    percent-encoded characters that confuse .format().
    """
    out: list[dict] = []
    for entry in configured:
        if not isinstance(entry, dict):
            continue
        reg_name = entry.get("name")
        template = entry.get("link_template")
        if not reg_name or not template:
            continue
        out.append({"name": reg_name, "url": template.replace("{name}", name)})
    return out


def _compute_verdict(candidate: dict, config: dict) -> str:
    """Server-side verdict assignment. Tightened 2026-05-17 — see
    config.verdict_thresholds._doc.

    Rules (evaluated top-to-bottom; first match wins):
      - Soft-signal keyword in name (dating, snake-oil, get-rich, crypto)
        → "Caution" regardless of score.
      - snapshot_category in (parked, empty) → "Caution" regardless of
        score. Added 2026-05-20 with the classifier wire-in: a parking
        page or default-server placeholder has no historical-authority
        value to a buyer, even if the apex was previously well-archived.
        `toxic` is not handled here because filter.keep_post_enrichment
        rejects it upstream; `legitimate` / `unknown` pass through to
        normal scoring.
      - score >= clean_min_score (default 70) → "Clean".
      - score >= promising_min_score (default 40) AND wayback >=
        promising_min_wayback_snapshots (default 1000) AND (OPR >=
        promising_min_open_page_rank OR cc_source_domain_count >=
        promising_min_cc_source_domain_count) → "Promising".
      - Anything else that survived filters → "Caution".

    None / missing wayback/opr/cc fields coerce to 0 (failing the strict
    Promising gate; demoting to Caution is the conservative call when an
    enrichment source was down).
    """
    name = candidate.get("name", "")
    if filter_mod.has_soft_signal(name, config):
        return "Caution"

    if candidate.get("snapshot_category") in ("parked", "empty"):
        return "Caution"

    thresholds = config.get("verdict_thresholds", {}) or {}
    clean_min = float(thresholds.get("clean_min_score", 70))
    promising_min_score = float(thresholds.get("promising_min_score", 40))
    promising_min_wayback = float(
        thresholds.get("promising_min_wayback_snapshots", 1000)
    )
    promising_min_opr = float(thresholds.get("promising_min_open_page_rank", 1.5))
    promising_min_cc = float(
        thresholds.get("promising_min_cc_source_domain_count", 10)
    )

    score = float(candidate.get("score") or 0)
    if score >= clean_min:
        return "Clean"

    if score >= promising_min_score:
        wayback = float(candidate.get("wayback_snapshots") or 0)
        opr = float(candidate.get("open_page_rank") or 0)
        cc = float(candidate.get("cc_source_domain_count") or 0)
        if wayback >= promising_min_wayback and (
            opr >= promising_min_opr or cc >= promising_min_cc
        ):
            return "Promising"

    return "Caution"


def _project(candidate: dict, registrars_config: list[dict], config: dict) -> dict:
    name = candidate.get("name", "")
    return {
        "name": name,
        "tld": candidate.get("tld", name.rsplit(".", 1)[-1] if "." in name else ""),
        "dropped_date": candidate.get("dropped_date"),
        "wayback_snapshots": candidate.get("wayback_snapshots"),
        "wayback_last_snapshot": candidate.get("wayback_last_snapshot"),
        "open_page_rank": candidate.get("open_page_rank"),
        "cert_history": candidate.get("cert_history"),
        "previous_registrar": candidate.get("previous_registrar"),
        "score": candidate.get("score", 0),
        "registrars": _build_registrars(name, registrars_config),
        "rdap_status": candidate.get("rdap_status") or [],
        "rdap_expiration": candidate.get("rdap_expiration"),
        "availability_verified_at": candidate.get("availability_verified_at"),
        # Persistent-list fields. days_listed defaults to 0 — the pipeline
        # annotates this before passing to build_payload; sample-data
        # fixtures and tests can omit it and get a sensible "today" default.
        "first_seen_date": candidate.get("first_seen_date"),
        "last_validated_date": candidate.get("last_validated_date"),
        "days_listed": candidate.get("days_listed", 0),
        "cc_source_domain_count": candidate.get("cc_source_domain_count"),
        "verdict": _compute_verdict(candidate, config),
        "wayback_unknown": bool(candidate.get("wayback_unknown")) or None,
        "wayback_unknown_attempts": (
            int(candidate.get("wayback_unknown_attempts"))
            if candidate.get("wayback_unknown_attempts") not in (None, 0)
            else None
        ),
        # Pre-Phase-4 entries (sample data, legacy carryover) have neither
        # field; project them as None so JSON shape stays uniform.
        "snapshot_category": candidate.get("snapshot_category"),
        "snapshot_classifier_version": candidate.get("snapshot_classifier_version"),
    }


def _enrichment_completeness(candidate: dict) -> float:
    """Fraction in [0.0, 1.0] of enrichment fields that are populated.
    'Populated' means: key present AND value not None. Empty string and 0
    count as populated (they ARE data — just zero / empty).

    Wayback exemption (2026-05-17): when a candidate carries
    `wayback_unknown=True` (the breaker was open / call failed during its
    enrichment), the two wayback fields are counted as populated here. The
    absence is upstream's fault, not the candidate's; penalizing it in the
    completeness gate would silently drop good domains on flaky-Wayback days.
    """
    wayback_unknown = bool(candidate.get("wayback_unknown"))
    populated = 0
    for f in _ENRICHMENT_FIELDS_FOR_COMPLETENESS:
        if candidate.get(f) is not None:
            populated += 1
        elif wayback_unknown and f.startswith("wayback_"):
            populated += 1
    return populated / len(_ENRICHMENT_FIELDS_FOR_COMPLETENESS)


def apply_quality_floor(
    candidates: list[dict],
    config: dict,
) -> tuple[list[dict], dict[str, int]]:
    """Drop candidates below the configured score / completeness thresholds.

    Returns (survivors, rejection_counts). The thresholds come from config:
        publish_min_score                          (default 30)
        publish_min_enrichment_completeness        (default 0.50, fraction)

    Either threshold = 0 (or absent) means "no floor on this dimension."

    A candidate is REJECTED if EITHER threshold fails — both must pass.
    """
    min_score = float(config.get("publish_min_score", 0))
    min_completeness = float(config.get("publish_min_enrichment_completeness", 0.0))

    survivors: list[dict] = []
    rejected_score = 0
    rejected_completeness = 0

    for c in candidates:
        score = float(c.get("score", 0))
        if score < min_score:
            rejected_score += 1
            continue
        completeness = _enrichment_completeness(c)
        if completeness < min_completeness:
            rejected_completeness += 1
            continue
        survivors.append(c)

    counts = {
        "rejected_score": rejected_score,
        "rejected_completeness": rejected_completeness,
        "kept": len(survivors),
    }
    return survivors, counts


def build_payload(
    candidates: list[dict],
    config: dict,
    *,
    generated_at: datetime | None = None,
    total_evaluated: int | None = None,
) -> dict:
    """Build the final JSON payload (does not write to disk).

    Pipeline:
      1. Apply quality floor (score + completeness)
      2. Cap at max_candidates_for_publication (CEILING — never pads)
      3. Project to contract fields

    `total_evaluated` is the count of candidates that entered enrichment
    (post-lexical, post-cap-trim). It's included in the payload so the
    frontend can render "Showing N of M candidates evaluated today"
    without making up a number.

    Cap precedence: max_candidates_for_publication wins; max_candidates_per_day
    kept as a fallback so older configs / tests still parse.
    """
    cap = int(
        config.get("max_candidates_for_publication")
        or config.get("max_candidates_per_day", 500)
    )

    # Stage 1: quality floor — applied uniformly to today's drops AND
    # carryover. Carryover that passed the floor in their original run
    # passes again (their score and completeness don't change). If config
    # thresholds tighten between runs, old entries that fail the new bar
    # drop out — that's the right behaviour: the published list always
    # reflects the CURRENT quality standards.
    quality_kept, quality_counts = apply_quality_floor(candidates, config)
    if quality_counts["rejected_score"] or quality_counts["rejected_completeness"]:
        logger.info(
            "Quality floor: kept %d, rejected %d (score) + %d (completeness) of %d input",
            quality_counts["kept"],
            quality_counts["rejected_score"],
            quality_counts["rejected_completeness"],
            len(candidates),
        )

    # Stage 2: publication cap (CEILING). Sort by score desc within each
    # bucket so the cap (when it bites) keeps the strongest entries from
    # both today AND carryover, rather than e.g. truncating all carryover
    # to make room for low-score today's drops.
    quality_kept.sort(key=lambda c: -float(c.get("score") or 0))
    capped = quality_kept[:cap]

    # Stage 3: project + assemble
    registrars_config = config.get("registrars") or []
    domains = [_project(c, registrars_config, config) for c in capped]
    when = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

    today_count = sum(1 for d in domains if (d.get("days_listed") or 0) == 0)
    carryover_count = len(domains) - today_count

    payload: dict = {
        "generated_at": when,
        "domain_count": len(domains),
        "today_count": today_count,
        "carryover_count": carryover_count,
        "domains": domains,
    }
    if total_evaluated is not None:
        payload["total_candidates_evaluated"] = int(total_evaluated)
    return payload


def write_output(
    candidates: list[dict],
    config: dict,
    output_path: str | Path | None = None,
    *,
    generated_at: datetime | None = None,
    total_evaluated: int | None = None,
) -> Path:
    """Build the payload and write it atomically. Returns the written path."""
    target = Path(output_path or config.get("output_path", "src/data/daily-domains.json"))
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = build_payload(
        candidates, config, generated_at=generated_at, total_evaluated=total_evaluated,
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", dir=str(target.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    logger.info(
        "Wrote %d domains to %s (generated_at=%s)",
        payload["domain_count"],
        target,
        payload["generated_at"],
    )
    return target
