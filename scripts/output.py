"""Write the daily JSON contract consumed by the Astro frontend.

The site reads `src/data/daily-domains.json`. The shape is locked in
PLAN.md Principle 5 with three schema migrations applied:
  - 2026-04-27 evening: single `affiliate_link` → `registrars[]` array
  - 2026-04-28 evening: added `total_candidates_evaluated` (top-level) so
    the frontend can render "Showing N of M candidates evaluated today"
    without inventing a count.
  - 2026-09-19: added `phase2_reason` per-domain field — the short (3-6
    word) justification scripts/phase2_ranker.py writes onto each
    candidate it shortlists. Until now it reached only the private R2
    overflow record, so the site could show a verdict but never a reason.
    Null on every day the ranker didn't run (mechanical-fallback days) and
    on every pre-2026-09-19 carryover entry; the frontend must treat
    absence as "no reason to show", never as an error.
  - 2026-09-19: added `total_drops_scanned` (top-level, OPTIONAL) — the
    raw number of newly-dropped domains the run examined, i.e. the size of
    today's zone-diff drop set BEFORE any filtering (pipeline.py's
    "Today's new drops: %d"). Distinct from `total_candidates_evaluated`
    (see below); the two are ~80x apart on a normal day and WILL be
    confused if read casually, so: SCANNED is the wide end of the funnel
    (every drop), EVALUATED is the narrow end (what survived the cheap
    filters and got individually checked). Absent — never 0 — whenever the
    writer didn't know the count: every pre-2026-09-19 carryover file, any
    caller that omits it, sample data. Consumers must treat absence as
    "unknown" and say nothing rather than render a zero.
  - 2026-09-20: added `cc_backlink_history` per-domain field — a list,
    NEWEST RELEASE FIRST, of {"release": str, "source_domain_count":
    int | None} for each monthly Common Crawl release we hold a derived
    SQLite for. R2 has held several releases for months but the pipeline
    only ever read the newest one, so the archive was inert; this exposes
    it. `source_domain_count: null` means "the apex is not in THAT
    release's graph at all" and stays deliberately distinct from `0`
    ("in the graph, zero inbound source domains") — the same three-state
    design as `cc_source_domain_count`. The field is DISPLAY-ONLY: it
    feeds no filter, no score and no verdict, and `cc_source_domain_count`
    keeps its exact prior meaning (latest release) and 0.30 scoring
    weight. Null whenever the feature is off, the enricher failed, or the
    entry is pre-2026-09-20 carryover; also null (never partially built)
    when the enricher hands us a shape that doesn't validate. Consumers
    must treat null as "no history available" and render nothing — it is
    never backfilled with invented numbers.
  - 2026-05-17: added `verdict` per-domain field ("Clean"/"Promising"/
    "Caution"). Previously computed client-side from score alone in
    DomainTable.astro and generate_newsletter.py; the new tightened
    Promising rule needs wayback + OPR + cc_source_domain_count, and
    soft-signal keyword detection lives in filter.py — easier to keep
    one Python implementation than to replicate the rule (and the
    soft-signal config lookup) in two places. Frontend / email fall
    back to score-only if the field is absent (sample data, old JSON).
  - 2026-09-23: the publish completeness gate counts SOURCES, not FIELDS.
    Not a schema change — nothing in the payload moved.
    `publish_min_enrichment_completeness` is still 0.50, but its
    denominator is now the three countable enrichment SOURCES listed in
    config.publish_completeness_sources (wayback, open_page_rank,
    cert_history) instead of five fields, and a source counts when AT
    LEAST ONE of its fields is non-null. Why: `previous_registrar` was
    present in 0 of 216 published rows and always will be — RDAP returns
    not-found for an AVAILABLE domain and available is the only kind we
    publish — so the gate scored against a field impossible by
    construction; and `wayback_snapshots` + `wayback_last_snapshot` are
    ONE source that was counted twice, letting Wayback alone carry 40% of
    the gate. Together those cost two days of ZERO published domains on
    2026-09-21/22: when OpenPageRank's API migrated, every candidate
    scored 2/5 = 0.40 against the 0.50 threshold and was rejected on
    completeness while the filters behaved normally. 0.50 now means 2 of
    3. Deliberately NOT outage-proof — lose OPR again and candidates fall
    to 1 of 3 and publishing stops, which is the correct answer because we
    genuinely would not know enough about them; the enrichment_coverage
    alarm is what makes that visible the same morning. A missing /
    malformed config key falls back to the old five-field counting rather
    than crashing.
  - 2026-09-23: `verdict` VALUES may now differ for sparse rows. This is a
    BEHAVIOUR change, NOT a schema change — same field, same three
    strings, same place in the contract, so no consumer needs a code
    change. `_compute_verdict` now also requires a minimum number of
    sources that actually returned a value
    (verdict_thresholds.clean_min_real_signals = 3,
    promising_min_real_signals = 2), counted with the same source map as
    the completeness gate above. A candidate failing a tier's floor
    demotes one tier: Clean → Promising if it clears that floor, else
    Caution. Why: scoring excludes null signals from the weighted average
    and renormalises the rest, so a domain we know LESS about can outscore
    one we know more about. Live case, not theory: one 2026-09-22 row
    scored 87 — the highest on the site and the ONLY Clean ever published
    — precisely because open_page_rank and cert_history were both missing,
    while every fully-enriched domain topped out at 61. The score maths is
    deliberately left ALONE (changing it re-ranks everything and needs
    recalibration against real runs); the floor is an ADDITIONAL necessary
    condition and can only ever demote, never promote. Frontend and
    newsletter read the field instead of recomputing it, so they inherit
    this for free — but their score-only FALLBACK (sample data,
    pre-2026-05-17 JSON) does not, and would still paint a sparse high
    scorer as Clean.

Output shape:
    {
        "generated_at": "2026-04-27T06:00:00Z",
        "domain_count": 47,                   # passed all filters AND quality floor
        "total_drops_scanned": 222155,        # OPTIONAL: every new drop the
                                              #   run looked at, pre-filter
        "total_candidates_evaluated": 1000,   # entered enrichment phase
                                              #   (a subset of the above)
        "domains": [
            {
                "name": "example.com", "tld": "com", "dropped_date": "2026-04-26",
                "wayback_snapshots": 142, "wayback_last_snapshot": "2024-08-15",
                "open_page_rank": 3.7, "cert_history": true,
                "previous_registrar": "GoDaddy", "score": 78,
                "cc_source_domain_count": 247, "verdict": "Clean",
                "cc_backlink_history": [                  # OPTIONAL, newest first
                    {"release": "cc-main-2026-jun-jul-aug",
                     "source_domain_count": 247},
                    {"release": "cc-main-2026-may-jun-jul",
                     "source_domain_count": null}    # not in that graph
                ],
                "registrars": [{"name": "Namecheap", "url": "https://..."}, ...]
            }
        ]
    }

Quality floor (added 2026-04-28 in response to day-3 publishing 300 random-
letter domains with mean score 12.1):
  - publish_min_score                       — drop candidates below this
  - publish_min_enrichment_completeness     — drop candidates with too few
                                               enrichment SOURCES reporting
                                               (fraction in [0.0, 1.0]; see
                                               the 2026-09-23 note above)

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
    # NOT a completeness source (see `_completeness_sources`) and not part of
    # the verdict evidence floor — absence from the CC graph is
    # informational, not a quality deficit.
    "cc_source_domain_count",
    # Common Crawl backlink history (added 2026-09-20). Newest-release-first
    # list of {"release": str, "source_domain_count": int | None} across the
    # monthly CC releases we hold; null when unavailable. DISPLAY ONLY — no
    # filter, score or verdict reads it. Like cc_source_domain_count it is
    # deliberately NOT a completeness source: a domain that predates our CC
    # archive isn't a lower-quality domain.
    "cc_backlink_history",
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
    # Phase 2 ranker justification (added 2026-09-19). Short free text from
    # the shortlist model, e.g. "explicit service intent, clear and
    # trustworthy". Null whenever the ranker didn't produce one — fallback
    # days, carryover written before this migration, and the ranker's own
    # "missing from response" placeholder (see _phase2_reason). Publishing
    # it here also means carryover entries keep their reason across days,
    # since tomorrow's run reads this file back in.
    "phase2_reason",
)

# scripts/phase2_ranker.py writes this literal when a candidate was sent to
# the model but came back absent from its response. It is a pipeline marker,
# not a justification, so it must never reach the site.
_PHASE2_REASON_PLACEHOLDER = "missing from response"

# LEGACY (pre-2026-09-23) completeness fields. Kept only as the fallback
# used when config.publish_completeness_sources is absent or malformed, so an
# old config still produces exactly the old numbers instead of crashing.
# Excludes pipeline-mandatory fields (name, tld, dropped_date, score) which
# are never null in valid candidates.
_ENRICHMENT_FIELDS_FOR_COMPLETENESS = (
    "wayback_snapshots",
    "wayback_last_snapshot",
    "open_page_rank",
    "cert_history",
    "previous_registrar",
)

# The same tuple as a source map: one field per source, which reproduces the
# old field-counting arithmetic exactly (5 sources, denominator 5).
_LEGACY_COMPLETENESS_SOURCES: dict[str, tuple[str, ...]] = {
    f: (f,) for f in _ENRICHMENT_FIELDS_FOR_COMPLETENESS
}


def _completeness_sources(config: dict | None) -> dict[str, tuple[str, ...]]:
    """Read config.publish_completeness_sources as {source: (field, ...)}.

    Added 2026-09-23. The gate counts distinct enrichment SOURCES that
    reported something, not raw fields — see the module docstring for why
    (`previous_registrar` is unreachable by construction; the two wayback
    fields are one source that was counted twice).

    Fails soft to `_LEGACY_COMPLETENESS_SOURCES` when the key is missing or
    unusable: the publish gate must not be the thing that crashes a run over
    a config typo. Keys beginning with `_` are skipped so a `_doc` note can
    live inside the block like everywhere else in config.json.
    """
    raw = (config or {}).get("publish_completeness_sources")
    if raw is None:
        return _LEGACY_COMPLETENESS_SOURCES
    if not isinstance(raw, dict):
        logger.warning(
            "publish_completeness_sources is %s, not an object - falling back "
            "to legacy field counting",
            type(raw).__name__,
        )
        return _LEGACY_COMPLETENESS_SOURCES

    sources: dict[str, tuple[str, ...]] = {}
    for source, fields in raw.items():
        if not isinstance(source, str) or source.startswith("_"):
            continue
        if isinstance(fields, str):
            fields = [fields]
        if not isinstance(fields, (list, tuple)):
            continue
        clean = tuple(f for f in fields if isinstance(f, str) and f)
        if clean:
            sources[source] = clean

    if not sources:
        logger.warning(
            "publish_completeness_sources defines no usable source - falling "
            "back to legacy field counting"
        )
        return _LEGACY_COMPLETENESS_SOURCES
    return sources


def _is_wayback_source(source: str, fields: tuple[str, ...]) -> bool:
    """True when this source IS the Wayback source, under either shape.

    Matches the configured name (`wayback`) and, for the legacy one-field-
    per-source fallback, any source whose every field is a wayback_* field.
    """
    return source == "wayback" or all(f.startswith("wayback") for f in fields)


def _count_signal_sources(
    candidate: dict,
    config: dict | None,
    *,
    count_unknown_wayback: bool = True,
) -> tuple[int, int]:
    """(sources that reported a signal, total countable sources).

    A source counts when AT LEAST ONE of its fields is not None, so a
    multi-field source can never score more than 1. 'Not None' is the whole
    test: empty string, 0 and False all count as reported — they ARE data.

    `count_unknown_wayback` selects the Wayback exemption (2026-05-17): when
    a candidate carries `wayback_unknown=True` the breaker was open or the
    call failed, so its two wayback fields are null through no fault of the
    candidate. The PUBLISH gate credits the source anyway (True) — a flaky
    Wayback day must not silently drop good domains. The VERDICT floor does
    NOT (False): that floor exists precisely to stop us advertising a domain
    we know little about, and a failed fetch is not evidence.

    `cc_source_domain_count` is deliberately absent from every source map —
    absence from the CC graph is informational, not a quality deficit.
    """
    sources = _completeness_sources(config)
    wayback_unknown = bool(candidate.get("wayback_unknown"))
    with_signal = 0
    for source, fields in sources.items():
        if any(candidate.get(f) is not None for f in fields):
            with_signal += 1
        elif (
            count_unknown_wayback
            and wayback_unknown
            and _is_wayback_source(source, fields)
        ):
            with_signal += 1
    return with_signal, len(sources)


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
      - score >= clean_min_score (default 70) AND real signals >=
        clean_min_real_signals → "Clean".
      - score >= promising_min_score (default 40) AND wayback >=
        promising_min_wayback_snapshots (default 1000) AND (OPR >=
        promising_min_open_page_rank OR cc_source_domain_count >=
        promising_min_cc_source_domain_count) AND real signals >=
        promising_min_real_signals → "Promising".
      - Anything else that survived filters → "Caution".

    None / missing wayback/opr/cc fields coerce to 0 (failing the strict
    Promising gate; demoting to Caution is the conservative call when an
    enrichment source was down).

    Evidence floor (added 2026-09-23, config.verdict_thresholds
    ._doc_min_real_signals): "real signals" is the number of enrichment
    SOURCES that actually returned a value, counted over the same source map
    as the publish completeness gate. It is an ADDITIONAL necessary condition
    on each tier, never a way to promote: a tier whose floor fails falls
    through to the tier below and is re-tested there on ITS full condition
    set, so Clean → Promising only when the candidate genuinely earns
    Promising, else Caution. Why: the score renormalises over present signals
    only, so a domain we know less about can outscore one we know more about,
    and the highest-scoring row ever published (87, the only Clean) got there
    by missing two of three sources. Either threshold at 0 / absent disables
    that tier's check — absent is the default so old configs behave exactly
    as before.

    Unlike the publish gate, this counting does NOT credit `wayback_unknown`
    candidates for wayback they never returned: a failed fetch is a reason to
    keep a domain out of the top tier, not a substitute for evidence.
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
    # Default 0 = disabled, so a config predating 2026-09-23 verdicts exactly
    # as it used to.
    clean_min_signals = int(thresholds.get("clean_min_real_signals") or 0)
    promising_min_signals = int(thresholds.get("promising_min_real_signals") or 0)

    real_signals, _total_sources = _count_signal_sources(
        candidate, config, count_unknown_wayback=False
    )

    score = float(candidate.get("score") or 0)
    if score >= clean_min and real_signals >= clean_min_signals:
        return "Clean"

    if score >= promising_min_score and real_signals >= promising_min_signals:
        wayback = float(candidate.get("wayback_snapshots") or 0)
        opr = float(candidate.get("open_page_rank") or 0)
        cc = float(candidate.get("cc_source_domain_count") or 0)
        if wayback >= promising_min_wayback and (
            opr >= promising_min_opr or cc >= promising_min_cc
        ):
            return "Promising"

    return "Caution"


def _phase2_reason(candidate: dict) -> str | None:
    """Return the ranker's justification, or None when there isn't a real one.

    None (not "") is the published value for: field absent (fallback days,
    pre-migration carryover), non-string junk, whitespace-only text, and the
    ranker's "missing from response" placeholder. The frontend renders the
    reason only when it is a non-empty string, so one null check covers
    every one of those cases.
    """
    raw = candidate.get("phase2_reason")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.lower() == _PHASE2_REASON_PLACEHOLDER:
        return None
    return text


def _cc_backlink_history(candidate: dict) -> list[dict] | None:
    """Return a validated, JSON-safe backlink history, or None.

    The enricher (scripts/enrichment/cc_backlinks.py) fails soft, so what
    lands here can be anything: absent, None, a truncated list, junk from a
    half-written cache. This coerces to exactly the documented shape —
    a list of {"release": <non-empty str>, "source_domain_count": <int|None>},
    newest first, order preserved — and returns None if ANY part of it does
    not fit.

    All-or-nothing on purpose: a partially-built history would let the site
    render "247 → (gap) → 310" as if the gap were a missing month rather
    than our own parse failure. None means "no history available", which the
    frontend already has to handle (feature disabled, enricher down,
    pre-2026-09-20 carryover). It is never backfilled with invented values.

    `source_domain_count: None` ("apex absent from that release's graph") is
    preserved as None and never coerced to 0 ("in the graph, no inbound
    edges") — the three-state distinction is the whole point of the field.

    Because the returned values are only str / int / None, the projected
    field cannot make json.dump fail (hard rule 17: invalid JSON output is
    a crash-worthy error, so the safe failure mode is to omit the field).
    """
    raw = candidate.get("cc_backlink_history")
    if raw is None:
        return None

    def _reject(reason: str) -> None:
        # One line per candidate, not per entry — a broken enricher would
        # otherwise flood the Actions log with hundreds of identical warnings.
        logger.warning(
            "Dropping malformed cc_backlink_history for %s: %s",
            candidate.get("name", "<unnamed>"),
            reason,
        )

    if not isinstance(raw, list):
        _reject(f"expected list, got {type(raw).__name__}")
        return None

    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            _reject(f"entry is {type(entry).__name__}, not a dict")
            return None
        release = entry.get("release")
        if not isinstance(release, str) or not release.strip():
            _reject("entry has a missing or non-string 'release'")
            return None
        count = entry.get("source_domain_count")
        # bool is an int subclass; True/False is never a domain count.
        if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
            _reject(
                "entry %r has a non-integer 'source_domain_count' (%s)"
                % (release, type(count).__name__)
            )
            return None
        if count is not None and count < 0:
            _reject(f"entry {release!r} has a negative 'source_domain_count'")
            return None
        cleaned.append({"release": release.strip(), "source_domain_count": count})

    # An empty list carries no more information than "no history" and would
    # make the frontend render an empty chart, so normalise it to None.
    return cleaned or None


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
        # Display-only history; deliberately NOT an input to anything below.
        "cc_backlink_history": _cc_backlink_history(candidate),
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
        "phase2_reason": _phase2_reason(candidate),
    }


def _enrichment_completeness(candidate: dict, config: dict | None = None) -> float:
    """Fraction in [0.0, 1.0] of enrichment SOURCES that reported a signal.

    Source-based since 2026-09-23 (was field-based; see the module
    docstring). A source counts when at least one of its fields is not None,
    so the two wayback fields together are worth 1, not 2. With the shipped
    config that makes this one of {0.0, 0.33, 0.67, 1.0} and the unchanged
    0.50 threshold means "2 of 3 sources".

    `config` is optional so pre-2026-09-23 callers still work: None falls
    back to the legacy five-field counting, which is also what a config
    missing `publish_completeness_sources` gets.

    Wayback exemption (2026-05-17) still applies — see `_count_signal_sources`.
    """
    with_signal, total = _count_signal_sources(candidate, config)
    if not total:  # unreachable with either source map; never divide by zero
        return 0.0
    return with_signal / total


def apply_quality_floor(
    candidates: list[dict],
    config: dict,
) -> tuple[list[dict], dict[str, int]]:
    """Drop candidates below the configured score / completeness thresholds.

    Returns (survivors, rejection_counts). The thresholds come from config:
        publish_min_score                          (default 30)
        publish_min_enrichment_completeness        (default 0.50, fraction)
        publish_completeness_sources               (the source map the
                                                    fraction is counted
                                                    over; see
                                                    `_completeness_sources`)

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
        completeness = _enrichment_completeness(c, config)
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
    total_drops_scanned: int | None = None,
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

    `total_drops_scanned` is the WIDER count: every newly-dropped domain
    the run examined before any filter ran (pipeline: `len(drops)`). Both
    counters are optional and independent — either can be None, and a None
    one is OMITTED from the payload rather than written as 0. A published
    0 would read as "we scanned nothing today", which is a lie whenever
    the truth is "the writer didn't pass the number."

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
    # Funnel counters, widest first. Each is written ONLY when the caller
    # actually supplied it — see the docstring on why a 0 default is worse
    # than an absent key.
    if total_drops_scanned is not None:
        payload["total_drops_scanned"] = int(total_drops_scanned)
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
    total_drops_scanned: int | None = None,
) -> Path:
    """Build the payload and write it atomically. Returns the written path.

    Both counters are optional end to end: pass None (or omit) and the key
    simply won't be in the written JSON.
    """
    target = Path(output_path or config.get("output_path", "src/data/daily-domains.json"))
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = build_payload(
        candidates,
        config,
        generated_at=generated_at,
        total_evaluated=total_evaluated,
        total_drops_scanned=total_drops_scanned,
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
