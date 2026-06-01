"""Phase 2 — LLM-based name-quality ranker (Haiku).

Sits BETWEEN the lexical filter (scripts/lexical_filter.py) and the RDAP
availability check (scripts/pipeline.py:validate_availability). Replaces
the per-host random-shuffle trim with quality-driven selection.

Pipeline contract
-----------------
The pipeline calls `rank_and_select(lexical_kept, config, today=today)` once,
gets back `(candidates_for_bucketing, status)`:

  - On success: `candidates_for_bucketing` is the above-gate subset of the
    lexical survivors, with each candidate carrying `phase2_score: int`
    (60-100) and `phase2_reason: str`. The pipeline then passes this
    smaller, scored set into the existing `_bucket_and_cap_for_availability`,
    which uses the presence of `phase2_score` as the signal to sort each
    over-cap bucket by score desc instead of random-shuffle. Production
    publishes a quality-ranked list.
  - On fallback / disabled: `candidates_for_bucketing` IS the original
    `lexical_kept` (unchanged shape). `_bucket_and_cap_for_availability`
    sees no `phase2_score` field and runs its existing random-shuffle path
    bit-for-bit. Production publishes today's mechanical-selection list.

Fallback wires to FAILURE conditions only — never to thin yield. Per product
policy "smaller but cleaner is better": on a thin day where only ~20 above-gate
domains exist, the ranker DRIVES and publishes a short quality list rather
than reverting to a longer mechanical one. The min_eligible config knob
defaults to 10 to honor this.

Fallback triggers
-----------------
  - phase2.enabled == false                            → mode=disabled
  - lexical_kept is empty                              → mode=fallback (empty_input)
  - No Anthropic client (key missing / SDK absent)     → mode=fallback (no_api_client)
  - Cost ceiling hit BEFORE min_eligible reached       → mode=fallback (cost_ceiling_below_min_eligible)
  - Any uncaught exception during ranking              → mode=fallback (exception)
  - Above-gate count < min_eligible after ranking      → mode=fallback (too_few_eligible)

When the cost ceiling hits AFTER min_eligible is satisfied, the ranker drives
on PARTIAL results — the unranked candidates are treated as below-gate
(per "treat as below-gate not vanish"); the above-gate set found so far still
feeds RDAP. Status mode is `ranker_partial` so the email report makes the
partial run visible.

Budget enforcement (belt + suspenders)
--------------------------------------
  1. Planned budget — `target_n = floor(daily_budget_usd / cost_per_1000_planning_usd * 1000)`.
     Pre-narrow truncates `lexical_kept` to `target_n` by the existing
     mechanical pre-signal (trigram_match_count desc, apex_len asc, name asc) —
     same key the global_cap trim uses. This is the planning layer.
  2. Hard-stop circuit breaker — a thread-safe cost meter accumulates real
     token usage after each batch completes. If cumulative spend reaches
     ~95% of `daily_budget_usd`, no further batches are submitted and any
     in-flight ones complete. This is the runtime layer.

Overflow persistence
--------------------
Above-gate candidates that don't fit any RDAP bucket are recorded to R2 at
`state/phase2_overflow.jsonl` via `record_overflow()`, called after the
RDAP bucket assignment. Entries age out after `OVERFLOW_MAX_AGE_DAYS` (14),
mirroring the carryover rolling window.

JSONL append-only is intentional: small data (names + scores + dates),
easy to consume from a future second RDAP pass or paid-tier surface,
no per-day file proliferation. R2 not the repo — this is a PUBLIC repo
and overflow lists are private pick-pool data.

Pricing constants
-----------------
Haiku 4.5 pricing as of mid-2026. If pricing has shifted, update
`PRICE_*_PER_MTOK` here. The cost meter sums real token counts from each
response's usage block, so the ceiling enforcement stays accurate.

The system prompt is ~350 tokens, below Haiku's 1024-token cache-eligibility
minimum, so `cache_control` markers are silently ignored. Left in for the
day someone expands the prompt past the threshold; becomes a ~90% input
cost reduction for batches 2-N within a 5-minute cache window.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from scripts import lexical_filter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00
PRICE_CACHE_WRITE_PER_MTOK = 1.25
PRICE_CACHE_READ_PER_MTOK = 0.10

OVERFLOW_R2_KEY = "state/phase2_overflow.jsonl"
OVERFLOW_MAX_AGE_DAYS = 14
CEILING_FRACTION = 0.95  # halt at 95% of the daily budget

DEFAULTS = {
    "enabled": True,
    "daily_budget_eur": 3.0,
    "eur_to_usd": 1.08,
    "cost_per_1000_planning_usd": 0.25,
    "score_gate": 60,
    "min_eligible": 10,
    "model": "claude-haiku-4-5-20251001",
    "batch_size": 15,
    "concurrency": 6,
    "max_retry_passes": 1,
}

RANKER_SYSTEM = """You score expired-domain candidate names for REGISTRABILITY AS A BRAND.

Each candidate has already passed mechanical filters (no gibberish strings, no all-numeric, no obvious spam keywords, English-pronounceable). Do NOT re-judge those mechanical traits. Your job is the SEMANTIC and BRAND layer the regex filter cannot judge.

Score 0-100. Higher = a real business could plausibly adopt this as a name.

REWARD (push toward higher scores):
- Recognizable real words or English-readable compounds: "familyhospital", "energystorm", "spacepictures", "tideblock", "motoplaza", "dandeliontea", "getrealdogtraining", "housecalldoctor", "realbench".
- Fast to parse at a glance — meaning is obvious within a second.
- Brandable: short enough to remember, no awkward proper-noun collisions, could fit on a business card.
- Clear semantic intent (a real product, place, service, identity).

PENALIZE (push toward lower scores):
- Gibberish-shaped but English-pronounceable: "antaryami", "khtinsoft", "marchekonstantina". These passed the mechanical filter but aren't real words to anyone.
- Foreign-language compounds with no English brand value: "binario9trequarti", "mallorca-heute".
- Run-together proper-noun strings that read like nobody-could-pronounce: "ststephenstonbridge".
- Numbers embedded for spammy reasons: "animeyt2", "78win012", "10trends".
- Excessive length (over ~18 chars) without justification.
- SEO keyword-stuffing: "aeo-engine-ai-aio-seo".
- Spam-vertical patterns even if the apex itself looks innocuous: "gacor" (Indonesian slot), "viagra", casino-number patterns like "mostbet-7a".

Do NOT judge the site's eventual PURPOSE or content safety — that is handled later by the snapshot classifier reading the Wayback content. Judge the NAME ONLY.

OUTPUT FORMAT (strict — your response must be EXACTLY a JSON array, no prose, no markdown fences):
[
  {"domain": "<name>", "score": <0-100 integer>, "reason": "<3-6 word phrase>"},
  ...
]

Include EVERY domain from the input, in the same order. Reason must be 3-6 words, lowercase, no punctuation other than spaces."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CostCeilingHit(Exception):
    """Cumulative Haiku spend has crossed the configured ceiling for this run.

    Raised from a worker thread when it detects the ceiling has been reached
    BEFORE submitting another API call. Always caught in `_haiku_batch_all` —
    never propagates to the caller. `mode='ranker_partial'` or
    `mode='fallback'` (with reason=cost_ceiling_below_min_eligible) is what
    surfaces to the orchestrator instead.
    """


# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------


def make_default_client() -> Any | None:
    """Construct an `anthropic.Anthropic` client from `ANTHROPIC_API_KEY`.

    Returns None if the key is missing/empty OR if the `anthropic` package
    isn't importable. Pairs with `rank_and_select` falling back to
    mechanical selection when client is None — same soft-fail pattern as
    `scripts/snapshot_classifier.py:make_default_client`.
    """
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        import anthropic  # local — keeps the module importable without the dep installed
        return anthropic.Anthropic(api_key=api_key)
    except Exception as exc:  # pragma: no cover — defence in depth
        logger.warning("Phase 2 ranker — anthropic SDK import/init failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Pre-narrow (planning-layer budget enforcement)
# ---------------------------------------------------------------------------


def _config(config: dict, key: str) -> Any:
    """Read a phase2 setting, falling back to DEFAULTS."""
    section = (config.get("phase2") or {})
    if key in section:
        return section[key]
    return DEFAULTS[key]


def _planned_target_n(daily_budget_usd: float, cost_per_1k_planning_usd: float) -> int:
    """Floor of `daily_budget_usd / cost_per_1k_planning_usd * 1000`.

    Why a planning-headroom rate (0.25/1k vs the diagnostic-measured ~0.19/1k):
    leaves ~33% margin for retry passes, prompt drift, model-pricing surprises.
    The runtime hard-stop catches anything the planner missed.
    """
    if cost_per_1k_planning_usd <= 0:
        return 0
    return max(0, int(daily_budget_usd * 1000.0 / cost_per_1k_planning_usd))


def pre_narrow(lexical_kept: list[dict], target_n: int) -> list[dict]:
    """Sort by (-trigram_match_count, len(apex), name) and take the top N.

    Same key the existing `_bucket_and_cap_for_availability` global_cap trim
    uses (pipeline.py `_rank_key`). When the ranker pre-narrows, the resulting
    set is "best-pre-signal-first" within the budget — the highest-quality
    candidates by the cheap mechanical signal get the chance to be ranked by
    Haiku.
    """
    if target_n <= 0:
        return []
    if len(lexical_kept) <= target_n:
        return list(lexical_kept)

    def _rank_key(cand: dict) -> tuple[int, int, str]:
        name = cand.get("name", "")
        apex = name.split(".", 1)[0]
        return (-lexical_filter.trigram_match_count(name), len(apex), name)

    return sorted(lexical_kept, key=_rank_key)[:target_n]


# ---------------------------------------------------------------------------
# Haiku batching + cost meter
# ---------------------------------------------------------------------------


def _build_user_message(batch: list[dict]) -> str:
    """Compact one-domain-per-line listing. Keeps input tokens minimal."""
    lines = "\n".join(f"- {c.get('name', '')}" for c in batch)
    return f"Score these {len(batch)} candidate domains:\n\n{lines}"


def _parse_response_array(text: str) -> list[dict]:
    """Extract the JSON array even if the model wrapped it in stray text.
    Mirrors scratch/haiku_ranker_diagnostic/rank.py — defensive against
    model wrap variations across runs."""
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1)
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        raise ValueError(f"no JSON array in response: {text[:200]!r}")
    return json.loads(m.group(0))


def _response_text(resp: Any) -> str:
    """Concatenate all text content blocks."""
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


def _usage_cost_usd(usage: Any) -> tuple[float, dict]:
    """Compute USD cost for one response's usage block. Returns (usd, breakdown).
    The breakdown is added to the cost meter for end-of-run reporting."""
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    usd = (
        inp * PRICE_INPUT_PER_MTOK
        + out * PRICE_OUTPUT_PER_MTOK
        + cw * PRICE_CACHE_WRITE_PER_MTOK
        + cr * PRICE_CACHE_READ_PER_MTOK
    ) / 1_000_000.0
    return usd, {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
    }


def _new_cost_meter() -> dict:
    return {
        "lock": threading.Lock(),
        "total_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "batches_ok": 0,
        "batches_failed": 0,
        "ceiling_hit": False,
    }


def _ceiling_reached(meter: dict, ceiling_usd: float) -> bool:
    with meter["lock"]:
        return meter["total_usd"] >= ceiling_usd


def _record_batch_cost(meter: dict, usd: float, breakdown: dict) -> None:
    with meter["lock"]:
        meter["total_usd"] += usd
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            meter[k] += breakdown[k]
        meter["batches_ok"] += 1


def _score_batch(
    client: Any,
    batch: list[dict],
    model: str,
    ceiling_usd: float,
    meter: dict,
) -> dict[str, dict]:
    """Send one batch to Haiku, parse the result, update cost meter.

    Returns a dict keyed by lowercase domain name → {score, reason}. Names
    missing from the response are NOT included — the caller treats absence
    as missing-from-response.

    Raises CostCeilingHit if the ceiling has been crossed BEFORE this batch
    submitted (so we don't burn another batch's worth of tokens after the
    halt decision). In-flight batches are allowed to complete.
    """
    if _ceiling_reached(meter, ceiling_usd):
        raise CostCeilingHit()

    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": RANKER_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": _build_user_message(batch)}],
    )
    usd, breakdown = _usage_cost_usd(getattr(resp, "usage", None))
    _record_batch_cost(meter, usd, breakdown)

    text = _response_text(resp)
    try:
        rows = _parse_response_array(text)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Phase 2 ranker: JSON parse failed for batch of %d (%s); "
            "treating all as below-gate",
            len(batch), exc,
        )
        return {}

    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = (r.get("domain") or r.get("name") or "").lower().strip()
        if not name:
            continue
        try:
            score = int(r.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        reason = str(r.get("reason", ""))[:120]
        out[name] = {"score": score, "reason": reason}
    return out


def _chunked(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _haiku_batch_all(
    pre_narrowed: list[dict],
    client: Any,
    cfg_get: Callable[[str], Any],
    ceiling_usd: float,
) -> tuple[dict[str, dict], dict]:
    """Run primary batches + optional retry pass for missing names.

    Returns (scored_by_name, cost_meter). `scored_by_name` is keyed by
    lowercase domain → {score, reason}. Missing names are not present.

    Halts cleanly on CostCeilingHit (in-flight batches finish, pending
    futures are cancelled). Other exceptions propagate to the caller.
    """
    model = str(cfg_get("model"))
    batch_size = max(1, int(cfg_get("batch_size")))
    concurrency = max(1, int(cfg_get("concurrency")))
    max_retry_passes = max(0, int(cfg_get("max_retry_passes")))

    meter = _new_cost_meter()
    scored: dict[str, dict] = {}
    scored_lock = threading.Lock()

    def merge_in(rows: dict[str, dict]) -> None:
        with scored_lock:
            scored.update(rows)

    def _run_pass(batches: list[list[dict]]) -> None:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(_score_batch, client, b, model, ceiling_usd, meter): i
                for i, b in enumerate(batches)
            }
            for fut in as_completed(futures):
                try:
                    rows = fut.result()
                except CostCeilingHit:
                    meter["ceiling_hit"] = True
                    for f in futures:
                        f.cancel()
                    continue
                except Exception as exc:
                    with meter["lock"]:
                        meter["batches_failed"] += 1
                    logger.warning(
                        "Phase 2 ranker: batch raised %s — treating its domains as below-gate: %s",
                        type(exc).__name__, exc,
                    )
                    continue
                merge_in(rows)

    primary_batches = _chunked(pre_narrowed, batch_size)
    logger.info(
        "Phase 2 ranker: %d batches of %d, concurrency=%d, ceiling=$%.4f",
        len(primary_batches), batch_size, concurrency, ceiling_usd,
    )
    _run_pass(primary_batches)

    # Single retry pass for missing names — only if budget remains.
    for pass_idx in range(max_retry_passes):
        if meter["ceiling_hit"]:
            break
        missing = [c for c in pre_narrowed if c.get("name", "").lower() not in scored]
        if not missing:
            break
        retry_batches = _chunked(missing, batch_size)
        logger.info(
            "Phase 2 ranker: retry pass %d for %d missing names (%d batches)",
            pass_idx + 1, len(missing), len(retry_batches),
        )
        _run_pass(retry_batches)

    return scored, meter


# ---------------------------------------------------------------------------
# Score distribution (for logging + report)
# ---------------------------------------------------------------------------


def _score_distribution(scored: list[dict]) -> dict[str, int]:
    """10-point bands plus an `above_gate_60_plus` aggregate, for the log line
    that surfaces in the daily email report."""
    bands: dict[str, int] = {}
    above_60 = 0
    for c in scored:
        s = int(c.get("phase2_score", 0))
        band = f"{(s // 10) * 10:02d}-{(s // 10) * 10 + 9:02d}" if s < 100 else "100"
        bands[band] = bands.get(band, 0) + 1
        if s >= 60:
            above_60 += 1
    bands["above_60_plus"] = above_60
    return bands


# ---------------------------------------------------------------------------
# Public API — rank_and_select
# ---------------------------------------------------------------------------


def rank_and_select(
    lexical_kept: list[dict],
    config: dict,
    *,
    today: date,
    client: Any | None = None,
) -> tuple[list[dict], dict]:
    """Pre-narrow → Haiku rank → gate → score-sort, or fall back to mechanical.

    Returns `(candidates_for_bucketing, status)`:

      candidates_for_bucketing:
        - on success: above-gate subset sorted by score desc, each carrying
          `phase2_score: int` and `phase2_reason: str` fields
        - on disabled / fallback: the original `lexical_kept` UNCHANGED
          (no score field, so `_bucket_and_cap_for_availability` runs its
          existing random-shuffle path bit-for-bit)

      status: dict surfaced to logging + the email report. Keys:
        - mode: 'ranker' | 'ranker_partial' | 'disabled' | 'fallback'
        - reason: present when mode in ('fallback', 'ranker_partial')
        - scored_count, above_gate_count, cost_usd, ceiling_hit, score_distribution
          when ranking ran at all
    """
    cfg_get: Callable[[str], Any] = lambda k: _config(config, k)
    if not cfg_get("enabled"):
        logger.info("Phase 2 ranker DISABLED — passing %d lexical survivors through unchanged", len(lexical_kept))
        return list(lexical_kept), {"mode": "disabled"}

    if not lexical_kept:
        logger.info("Phase 2 ranker: empty input — passing through")
        return list(lexical_kept), {"mode": "fallback", "reason": "empty_input"}

    if client is None:
        client = make_default_client()
    if client is None:
        logger.warning(
            "Phase 2 ranker FALLBACK — no API client "
            "(ANTHROPIC_API_KEY missing or anthropic SDK absent)"
        )
        logger.warning("→ reverting to mechanical selection")
        return list(lexical_kept), {"mode": "fallback", "reason": "no_api_client"}

    daily_budget_eur = float(cfg_get("daily_budget_eur"))
    eur_to_usd = float(cfg_get("eur_to_usd"))
    daily_budget_usd = daily_budget_eur * eur_to_usd
    ceiling_usd = daily_budget_usd * CEILING_FRACTION
    target_n = _planned_target_n(
        daily_budget_usd, float(cfg_get("cost_per_1000_planning_usd")),
    )
    score_gate = int(cfg_get("score_gate"))
    min_eligible = int(cfg_get("min_eligible"))

    logger.info(
        "Phase 2 ranker ENABLED — lexical=%d, budget=€%.2f ($%.2f), "
        "ceiling=$%.2f, target_n=%d, gate=%d, min_eligible=%d",
        len(lexical_kept), daily_budget_eur, daily_budget_usd,
        ceiling_usd, target_n, score_gate, min_eligible,
    )

    pre_narrowed = pre_narrow(lexical_kept, target_n)
    logger.info(
        "Pre-narrow: %d → %d (top by trigram+length, fits budget)",
        len(lexical_kept), len(pre_narrowed),
    )

    start = time.monotonic()
    try:
        scored_by_name, meter = _haiku_batch_all(
            pre_narrowed, client, cfg_get, ceiling_usd,
        )
    except Exception as exc:
        logger.error(
            "Phase 2 ranker FAILED — %s: %s", type(exc).__name__, exc,
        )
        logger.warning("→ reverting to mechanical selection")
        return list(lexical_kept), {
            "mode": "fallback",
            "reason": f"exception:{type(exc).__name__}",
        }

    # Assemble the scored list — missing names treated as below-gate (score=0).
    scored: list[dict] = []
    missing_count = 0
    for cand in pre_narrowed:
        key = cand.get("name", "").lower()
        if key in scored_by_name:
            entry = scored_by_name[key]
            scored.append({
                **cand,
                "phase2_score": int(entry["score"]),
                "phase2_reason": entry["reason"],
            })
        else:
            missing_count += 1
            scored.append({
                **cand,
                "phase2_score": 0,
                "phase2_reason": "missing from response",
            })

    elapsed = time.monotonic() - start
    distribution = _score_distribution(scored)
    above_gate = [c for c in scored if int(c.get("phase2_score", 0)) >= score_gate]

    logger.info(
        "Phase 2 ranker: %d scored, %d missing (below-gate); $%.4f spent of $%.4f ceiling; %.1fs",
        len(scored) - missing_count, missing_count,
        meter["total_usd"], ceiling_usd, elapsed,
    )
    logger.info("Phase 2 ranker score distribution: %s", distribution)
    logger.info(
        "Phase 2 ranker above-gate (>=%d): %d of %d (%.1f%%)",
        score_gate, len(above_gate), len(scored),
        100.0 * len(above_gate) / max(1, len(scored)),
    )

    # Fallback: above-gate count below min_eligible.
    if len(above_gate) < min_eligible:
        ceiling_below = meter["ceiling_hit"]
        reason = (
            "cost_ceiling_below_min_eligible" if ceiling_below else "too_few_eligible"
        )
        logger.warning(
            "Phase 2 ranker FALLBACK — %s (above_gate=%d < min_eligible=%d)",
            reason, len(above_gate), min_eligible,
        )
        logger.warning("→ reverting to mechanical selection")
        logger.warning(
            "This run did NOT use quality ranking; published list reflects "
            "today's mechanical behavior"
        )
        return list(lexical_kept), {
            "mode": "fallback",
            "reason": reason,
            "above_gate_count": len(above_gate),
            "min_eligible": min_eligible,
            "cost_usd": round(meter["total_usd"], 4),
            "ceiling_hit": ceiling_below,
            "score_distribution": distribution,
        }

    above_gate.sort(
        key=lambda c: (-int(c.get("phase2_score", 0)), c.get("name", "")),
    )

    mode = "ranker_partial" if meter["ceiling_hit"] else "ranker"
    if meter["ceiling_hit"]:
        logger.warning(
            "Phase 2 ranker MODE=RANKER_PARTIAL — cost ceiling hit; "
            "feeding %d score-ordered candidates to RDAP "
            "(remaining %d treated as below-gate)",
            len(above_gate), missing_count,
        )
    else:
        logger.info(
            "Phase 2 ranker MODE=RANKER → feeding %d score-ordered candidates to RDAP bucket-and-cap",
            len(above_gate),
        )

    return above_gate, {
        "mode": mode,
        "scored_count": len(scored),
        "above_gate_count": len(above_gate),
        "missing_count": missing_count,
        "cost_usd": round(meter["total_usd"], 4),
        "ceiling_hit": meter["ceiling_hit"],
        "score_distribution": distribution,
        "batches_ok": meter["batches_ok"],
        "batches_failed": meter["batches_failed"],
        "wall_clock_seconds": round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# Overflow persistence (R2)
# ---------------------------------------------------------------------------


def _r2_get_object_or_empty(s3: Any, bucket: str, key: str) -> bytes:
    """Read overflow object from R2; return b'' if not found. Errors propagate."""
    from botocore.exceptions import ClientError

    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return b""
        raise
    return resp["Body"].read()


def _parse_overflow_jsonl(raw: bytes) -> list[dict]:
    out: list[dict] = []
    if not raw:
        return out
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Phase 2 overflow: skipping corrupt JSONL line (%s)", exc)
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _serialize_overflow_jsonl(records: list[dict]) -> bytes:
    return ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode("utf-8")


def _age_out(records: list[dict], today: date, max_age_days: int) -> list[dict]:
    """Drop records whose `first_seen_date` is older than `max_age_days` from today.
    Records with missing/unparseable first_seen_date are kept (fail-open)."""
    cutoff = today - timedelta(days=max_age_days)
    kept: list[dict] = []
    for r in records:
        fsd = r.get("first_seen_date")
        if not fsd:
            kept.append(r)
            continue
        try:
            d = datetime.strptime(fsd, "%Y-%m-%d").date()
        except ValueError:
            kept.append(r)
            continue
        if d >= cutoff:
            kept.append(r)
    return kept


def record_overflow(
    *,
    above_gate: list[dict],
    selected_for_rdap: list[dict],
    config: dict,
    today: date,
    status: dict,
    r2_client: Any | None = None,
    r2_bucket: str | None = None,
) -> int:
    """Persist above-gate candidates that didn't fit any RDAP bucket.

    Reads existing R2 overflow file, ages out entries older than
    `OVERFLOW_MAX_AGE_DAYS`, appends today's new overflow with
    `first_seen_date=today`, writes the merged set back.

    No-ops when:
      - status mode is 'disabled' or 'fallback' (no scored candidates to record)
      - above_gate is empty OR all above_gate domains made it into the RDAP set

    Returns the number of new records added today (0 if nothing to record).
    Errors from R2 are logged and swallowed — overflow loss is acceptable;
    we never block the daily run on this side-effect-only step.
    """
    if status.get("mode") not in ("ranker", "ranker_partial"):
        return 0
    if not above_gate:
        return 0

    selected_names = {c.get("name", "") for c in selected_for_rdap}
    new_overflow = [c for c in above_gate if c.get("name", "") not in selected_names]
    if not new_overflow:
        logger.info(
            "Phase 2 overflow: 0 new records (all %d above-gate fit RDAP buckets)",
            len(above_gate),
        )
        return 0

    today_str = today.isoformat()
    today_records = [
        {
            "name": c.get("name", ""),
            "tld": c.get("tld", ""),
            "dropped_date": c.get("dropped_date", today_str),
            "phase2_score": int(c.get("phase2_score", 0)),
            "phase2_reason": c.get("phase2_reason", ""),
            "first_seen_date": today_str,
        }
        for c in new_overflow
    ]

    try:
        if r2_client is None or r2_bucket is None:
            from scripts import diff
            r2_client = r2_client or diff._r2_client()
            r2_bucket = r2_bucket or diff._bucket()

        existing_raw = _r2_get_object_or_empty(r2_client, r2_bucket, OVERFLOW_R2_KEY)
        existing = _parse_overflow_jsonl(existing_raw)
        kept = _age_out(existing, today, OVERFLOW_MAX_AGE_DAYS)
        aged_out = len(existing) - len(kept)

        merged = kept + today_records
        body = _serialize_overflow_jsonl(merged)
        r2_client.put_object(
            Bucket=r2_bucket,
            Key=OVERFLOW_R2_KEY,
            Body=body,
            ContentType="application/json; charset=utf-8",
        )
        logger.info(
            "Phase 2 overflow: wrote %d records to r2://%s/%s "
            "(%d new today, %d retained from prior days, %d aged out >%dd)",
            len(merged), r2_bucket, OVERFLOW_R2_KEY,
            len(today_records), len(kept), aged_out, OVERFLOW_MAX_AGE_DAYS,
        )
        return len(today_records)
    except Exception as exc:
        # Side-effect step — log and swallow. Never block the daily run.
        logger.error(
            "Phase 2 overflow: R2 write failed (%s: %s) — %d above-gate "
            "candidates dropped from overflow tracking for today",
            type(exc).__name__, exc, len(new_overflow),
        )
        return 0
