"""Write the daily JSON contract consumed by the Astro frontend.

The site reads `src/data/daily-domains.json`. The shape is locked in
PLAN.md Principle 5 with two schema migrations applied:
  - 2026-04-27 evening: single `affiliate_link` → `registrars[]` array
  - 2026-04-28 evening: added `total_candidates_evaluated` (top-level) so
    the frontend can render "Showing N of M candidates evaluated today"
    without inventing a count.

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
                "cc_source_domain_count": 247,
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


def _project(candidate: dict, registrars_config: list[dict]) -> dict:
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
    }


def _enrichment_completeness(candidate: dict) -> float:
    """Fraction in [0.0, 1.0] of enrichment fields that are populated.
    'Populated' means: key present AND value not None. Empty string and 0
    count as populated (they ARE data — just zero / empty)."""
    populated = sum(
        1 for f in _ENRICHMENT_FIELDS_FOR_COMPLETENESS if candidate.get(f) is not None
    )
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
    domains = [_project(c, registrars_config) for c in capped]
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
