"""Snapshot content classifier.

Reads the most-recent Wayback snapshot of a candidate (title / meta /
h1 / h2 extracted by scripts.wayback_excerpt) and labels it as exactly
one of {legitimate, parked, toxic, empty}, or "unknown" on any failure
path. Failures are SOFT — this module never raises, never aborts the
pipeline, and never installs a circuit breaker. A total backend outage
produces all-"unknown" results and the pipeline publishes anyway.

2026-09-18 — TWO CHANGES, both driven by the move off the metered API
(the Anthropic credit balance hit zero on 2026-07-23; see
scripts/llm_backend.py for the full story):

1. BATCHED. The old path made one model call per domain with
   max_tokens=8. Under the `claude_code` backend every call drags ~25k
   tokens of Claude Code system prompt + tool definitions along with it,
   so a one-word answer per domain is ~99.9% overhead and 20-180 calls a
   day is unusable. Domains are now grouped `snapshot_classifier.batch_size`
   at a time (default 20) and the model returns a JSON array of
   {"domain", "category"} rows.

2. SHADOW MODE. `snapshot_classifier.shadow` (default TRUE) writes the
   real verdict to `snapshot_category_shadow` and leaves
   `snapshot_category` alone, so filter.keep_post_enrichment's toxic gate
   evicts nothing while the new backend is validated against API-era
   verdicts. Set it false to arm the gate for real.

Persisted fields written onto each candidate dict by classify_all:
    wayback_excerpt                — dict | None (the content the model saw,
                                     or None if fetch failed / no snapshot)
    snapshot_category              — str (one of the 5 categories). In shadow
                                     mode this is NEVER overwritten: an
                                     existing value is preserved and an
                                     absent one is set to "unknown".
    snapshot_category_shadow       — str, shadow mode only: the verdict that
                                     WOULD have been written. Not part of the
                                     published JSON contract — output.py and
                                     domain_archive.py both use explicit field
                                     allowlists, so this stays in the logs and
                                     in-memory records unless deliberately
                                     added there.
    snapshot_classifier_version    — str — bumped whenever the prompt or
                                     parsing rules change in a way that could
                                     produce different labels for the same
                                     input. Future runs use this to identify
                                     entries that predate the change and
                                     re-classify selectively.

CLAUDE.md compliance notes:
    - Rule 9: batch_size / shadow / timeout come from config, never literals
      at the call site (`cfg()` below supplies defaults so the module works
      before the keys land in scripts/config.json).
    - Rule 11/17: one bad batch never poisons another and never propagates.
      Every domain in a failed, refused, or truncated batch lands on
      "unknown".
    - Rule 14: logging, not print. WARNING level on every soft-fail path.
    - Rule 16: no module-level mutable state; client + config are passed in.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from scripts.llm_backend import (
    LLMBackendError,
    get_backend,
    parse_json_array,
)

logger = logging.getLogger(__name__)


# --- Versioning -------------------------------------------------------------

# Bump this when the system prompt, response parser, or category set changes
# in a way that could alter labels for identical input. Persisted alongside
# snapshot_category so future selective re-classifications can target
# pre-v(N) entries without re-classifying everything.
#
# v1 → v2 (2026-09-18): per-domain one-word replies became batched JSON, the
# prompt gained batch-output rules, and the default backend moved from the
# metered API to Claude Code. All three can move a borderline label.
CLASSIFIER_VERSION = "v2"

VALID_CATEGORIES: tuple[str, ...] = ("legitimate", "parked", "toxic", "empty")
UNKNOWN_CATEGORY = "unknown"

# Field written instead of snapshot_category while shadow mode is on.
SHADOW_FIELD = "snapshot_category_shadow"


# --- Config (hard rule 9) ---------------------------------------------------

# Applied when a key is absent from config["snapshot_classifier"], so this
# module behaves sensibly before the keys are added to scripts/config.json.
DEFAULTS: dict[str, Any] = {
    # Domains per model call. 20 keeps the reply short enough to never risk
    # an output-token truncation while amortising Claude Code's ~25k-token
    # per-call overhead across a useful number of domains.
    "batch_size": 20,
    # TRUE until the new backend's verdicts have been compared against the
    # API-era ones. See module docstring.
    "shadow": True,
    # Per-batch wall-clock cap. None → whatever llm.timeout_seconds says.
    # A batch is a few hundred output tokens, so this only exists to stop a
    # hung call from multiplying across a day's batches.
    "timeout_seconds": None,
}


def cfg(config: dict | None, key: str) -> Any:
    """Read ``config["snapshot_classifier"][key]`` falling back to DEFAULTS."""
    section = (config or {}).get("snapshot_classifier") or {}
    if not isinstance(section, dict):
        return DEFAULTS[key]
    return section.get(key, DEFAULTS[key])


# --- System prompt ----------------------------------------------------------
#
# The categories, examples, and rules below are the public surface of this
# classifier. Treat changes here as semver-affecting — bump CLASSIFIER_VERSION
# on any non-cosmetic edit (new example added is cosmetic; category boundary
# moved is not).
#
# The "non-Latin scripts are not a signal" rule is load-bearing — every
# example deliberately spans multiple scripts so the model can't acquire
# a Latin-only prior from the examples.
#
# The framing paragraph is load-bearing too, for a different reason: under the
# claude_code backend this text is appended to a conversational agent's system
# prompt, and an unframed "is this porn/pharma?" request is exactly the shape
# that earns a refusal or a hedging preamble instead of the JSON we parse
# (trap 2 in scripts/llm_backend.py). Stating the moderation purpose up front
# and demanding raw JSON is what keeps the reply machine-readable.

SNAPSHOT_CLASSIFIER_SYSTEM_PROMPT = """You are a content-safety triage classifier inside an automated pipeline.
You label archived (Wayback Machine) snapshots of EXPIRED domains so that a
domain-recommendation site can avoid republishing links to abusive content.
Assigning a label is a moderation judgement about an archived page, not an
endorsement, an accusation about any living person, or legal advice. Always
answer with the JSON described below — never with prose, a question, or a
refusal.

Each snapshot goes into exactly ONE of four categories: legitimate, parked,
toxic, or empty.

INPUT
A JSON array. Each element describes one domain:
  domain             — the domain name (echo this back verbatim)
  title              — page <title>
  meta_description   — page <meta name="description">
  h1                 — list of up to 3 H1 headings
  h2                 — list of up to 5 H2 headings
Every field except `domain` may be null or empty.

All fields may contain non-Latin scripts (Chinese, Cyrillic, Arabic,
Japanese, Korean, Thai, etc.). Non-Latin content is NOT a signal in
itself — judge by meaning, not script.

CATEGORIES

legitimate — A real website with substantive content of any kind:
  business, blog, organization, personal site, portfolio, web app,
  store, news outlet, fan/hobby community, educational resource,
  documentation, government, etc. ANY LANGUAGE.
  Default to this when borderline. We would rather publish a real
  site than reject one.
  Example: title="Acme Roofing — Boston MA" / h1=["Family-owned roofers since 1985"]
  Example: title="月見うどん専門店" / h1=["手打ちうどん"]    (Japanese udon restaurant)

parked — A registrar/parking-service placeholder OR generic "domain
  for sale" / "premium domain available" notice OR auto-generated SEO
  link-farm with no original content. Look for phrases (in any language):
  "for sale", "buy this domain", "premium domain", "this domain is
  available", "make an offer", "购买此域名", "купить домен", "هذا النطاق للبيع",
  parking-service brand names (Sedo, GoDaddy parking, Bodis, etc.).
  Example: title="marketglow.com is for sale" / meta="Buy this premium domain"
  Example: title="Купить домен" / h1=["Этот домен продается"]

toxic — Sites whose PRIMARY PURPOSE is: pornography / escort / cam
  services; online gambling spam; pharmacy / steroid / weight-loss /
  miracle-cure product sales; phishing kits; malware / cracked-software
  distribution. Translate before judging — adult content in Chinese,
  Cyrillic, etc. counts the same as in English.
  Example: title="无码高清成人影片 - XXX" / h1=["18+ 成人视频"]
  Example: title="Buy Cheap Viagra Online — 70% Off, No Prescription"
  COUNTER-EXAMPLE: An LGBTQ+ news site, a figure-drawing art gallery,
  a sex-ed blog, or a harm-reduction resource is LEGITIMATE, not toxic.
  Toxic is reserved for sites where adult/gambling/scam IS the product.

empty — The snapshot is effectively blank: a default web-server welcome
  page ("It works!", "Apache2 Default Page", "Welcome to nginx!"), a
  bare "page not found" / "site under construction" stub, an HTTP
  redirect notice, or all four content fields are null/empty.
  Example: title="Apache2 Debian Default Page" / h1=["It works!"]
  Example: title=null / meta=null / h1=[] / h2=[]

OUTPUT FORMAT
Return a JSON array with EXACTLY ONE object per input element, in the same
order, and nothing else:
[{"domain":"marketglow.com","category":"parked"},{"domain":"tideblock.io","category":"legitimate"}]

RULES
- Output raw JSON only. No markdown fence, no preamble, no commentary, no
  trailing notes, no explanation of your reasoning.
- `category` must be exactly one of: legitimate, parked, toxic, empty
  (lowercase). Any other value is discarded by the caller.
- `domain` must be copied verbatim from the input. A domain you omit, or
  spell differently, is recorded as "unknown" — there is no fallback
  advantage to deviating from the format.
- Classify every element, including ones whose content fields are all
  null (those are "empty").
- When borderline between legitimate and any other category, choose legitimate
- A parked page is parked even if its title is in a non-Latin script —
  look for the for-sale / buy-this-domain semantic, not the language
- Translate non-English content before classifying, do not penalize it
  for being non-English
"""


# --- Backend client ---------------------------------------------------------


class BatchClassifierClient:
    """Sends one batch of excerpts to an scripts.llm_backend Backend.

    Deliberately thin: the backend owns transport, auth, and timeouts; this
    class owns the system prompt so the prompt and the call parameters stay
    co-located (the parameter-mismatch class of bug the old ClassifierClient
    docstring warned about).

    Raises LLMBackendError — and only LLMBackendError — from classify_batch.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    @property
    def backend_name(self) -> str:
        return str(getattr(self._backend, "name", "unknown"))

    def classify_batch(
        self, user: str, *, timeout_seconds: int | None = None
    ) -> str:
        """Return the model's raw reply text for one batch."""
        return self._backend.complete(
            system=SNAPSHOT_CLASSIFIER_SYSTEM_PROMPT,
            user=user,
            timeout_seconds=timeout_seconds,
        )


def make_default_client(config: dict | None = None) -> "BatchClassifierClient | None":
    """Build a client from ``config["llm"]``.

    Returns None only when the backend cannot be constructed at all (an
    unknown ``llm.backend`` name) — pair with classify_all's client=None
    pass-through so a config typo degrades to all-"unknown" rather than
    crashing the pipeline. A backend that is constructible but broken at
    call time (missing binary, expired token) fails per-batch instead, which
    is the same end state with a better log.

    `config` is optional so pre-existing zero-argument callers keep working;
    they get scripts.llm_backend.DEFAULTS.
    """
    try:
        backend = get_backend(config or {})
    except LLMBackendError as exc:
        logger.error(
            "snapshot_classifier: no usable LLM backend (%s) — every candidate "
            "will be classified 'unknown'", exc,
        )
        return None
    client = BatchClassifierClient(backend)
    logger.info("snapshot_classifier: using llm backend %r", client.backend_name)
    return client


# --- Pure helpers (unit-tested) ---------------------------------------------


def _excerpt_fields(name: str, excerpt: dict) -> dict[str, Any]:
    """The four content fields plus the domain key the model echoes back.

    The excerpt's metadata (snapshot_timestamp, snapshot_url) is bookkeeping —
    not signal — and including it would inflate input tokens without informing
    the label.
    """
    return {
        "domain": name,
        "title": excerpt.get("title"),
        "meta_description": excerpt.get("meta_description"),
        "h1": excerpt.get("h1") or [],
        "h2": excerpt.get("h2") or [],
    }


def _build_user_message(batch: list[tuple[str, dict]]) -> str:
    """Render one batch as the user turn.

    ensure_ascii=False keeps non-Latin scripts as themselves rather than
    \\uXXXX escapes (smaller payload, and the model has explicit text to
    read). Compact separators for the same reason.
    """
    payload = [_excerpt_fields(name, excerpt) for name, excerpt in batch]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        f"Classify these {len(payload)} archived snapshots. Return a JSON array "
        f"of {len(payload)} objects, one per domain, each {{\"domain\",\"category\"}}.\n\n"
        f"{body}"
    )


def _parse_classification(raw: object) -> str:
    """Normalise one model-supplied category against VALID_CATEGORIES.

    Tolerates case, stray whitespace, and trailing punctuation. Anything
    else — including a non-string, a null, or the model inventing its own
    category — degrades to UNKNOWN_CATEGORY.
    """
    if not isinstance(raw, str) or not raw:
        return UNKNOWN_CATEGORY
    word = raw.strip().lower().rstrip(".!?,;:'\"")
    if word in VALID_CATEGORIES:
        return word
    return UNKNOWN_CATEGORY


def _parse_batch_reply(text: str, expected: list[str]) -> dict[str, str]:
    """Map domain → category from one batch reply.

    Only domains in `expected` are returned; rows for anything else are
    dropped with a warning (a hallucinated domain must never contaminate an
    unrelated record). Domains absent from the reply are simply absent from
    the map — the caller fills them with "unknown".

    Raises LLMBackendError when the reply is prose / a refusal / not a JSON
    array, per scripts.llm_backend.parse_json_array.
    """
    rows = parse_json_array(text)
    wanted = {name.lower(): name for name in expected}
    out: dict[str, str] = {}
    for row in rows:
        domain = row.get("domain")
        key = domain.strip().lower() if isinstance(domain, str) else ""
        original = wanted.get(key)
        if original is None:
            logger.warning(
                "snapshot_classifier: reply contained unrequested domain %r — ignored",
                str(domain)[:100],
            )
            continue
        out[original] = _parse_classification(row.get("category"))
    return out


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    """Split into consecutive chunks of at most `size` (size >= 1)."""
    step = max(1, int(size))
    return [items[i : i + step] for i in range(0, len(items), step)]


# --- Excerpt fetching -------------------------------------------------------


def _fetch_excerpt_for(record: dict) -> dict | None:
    """Fetch one record's excerpt. Never raises; None on every failure.

    Unchanged behaviour from the per-domain era: no snapshot date means no
    Availability-API lookup at all (the common path for wayback_unknown
    candidates), and a fetch_excerpt that raises despite its contract is
    defended against rather than trusted.
    """
    # Local import keeps tests free to monkeypatch fetch_excerpt at the
    # scripts.wayback_excerpt module path without a hard top-of-file
    # dependency. (Also avoids importing requests/bs4 in test paths that
    # never need them.)
    from scripts.wayback_excerpt import fetch_excerpt

    name = record.get("name", "")
    last_snapshot = record.get("wayback_last_snapshot")
    if not last_snapshot:
        return None
    try:
        return fetch_excerpt(name, last_snapshot)
    except Exception as exc:  # defence-in-depth
        logger.warning(
            "snapshot_classifier: fetch_excerpt raised for %s: %s — treating as unknown",
            name, exc,
        )
        return None


# --- Verdict application ----------------------------------------------------


def _apply_verdict(record: dict, verdict: str, *, shadow: bool) -> str:
    """Write the verdict onto `record` and return the EFFECTIVE category
    (i.e. what `snapshot_category` ends up holding, which is what
    filter.py and output.py act on).

    Shadow mode is deliberately non-destructive: it writes
    snapshot_category_shadow and leaves an existing snapshot_category
    untouched, defaulting it to "unknown" when absent. A `--force`
    re-classification run under shadow therefore cannot blank out labels
    that the API-era classifier already earned.
    """
    record["snapshot_classifier_version"] = CLASSIFIER_VERSION
    if shadow:
        record[SHADOW_FIELD] = verdict
        effective = record.get("snapshot_category") or UNKNOWN_CATEGORY
        record["snapshot_category"] = effective
        return effective
    record["snapshot_category"] = verdict
    return verdict


def _empty_counts() -> dict[str, int]:
    return {c: 0 for c in (*VALID_CATEGORIES, UNKNOWN_CATEGORY)}


def _tally(counts: dict[str, int], category: str) -> None:
    counts[category] = counts.get(category, 0) + 1


# --- Orchestration ----------------------------------------------------------


def _classify_batch(
    client: Any,
    batch: list[tuple[str, dict]],
    *,
    timeout_seconds: int | None,
) -> dict[str, str]:
    """One model call. Returns domain → category for whatever came back.

    NEVER raises (hard rule 11): a transport failure, a refusal, a prose
    reply, or an unparseable body all yield {} so every domain in this batch
    falls through to "unknown" while the other batches proceed untouched.
    """
    names = [name for name, _ in batch]
    try:
        raw = client.classify_batch(
            _build_user_message(batch), timeout_seconds=timeout_seconds
        )
    except Exception as exc:
        logger.warning(
            "snapshot_classifier: batch of %d failed (%s: %s) — those domains "
            "stay unknown: %s",
            len(names), type(exc).__name__, exc, ", ".join(names[:10]),
        )
        return {}

    try:
        verdicts = _parse_batch_reply(raw, names)
    except LLMBackendError as exc:
        logger.warning(
            "snapshot_classifier: unparseable batch reply (%s) — %d domains "
            "stay unknown",
            exc, len(names),
        )
        return {}

    missing = [name for name in names if name not in verdicts]
    if missing:
        logger.warning(
            "snapshot_classifier: reply omitted %d/%d domains — treating as "
            "unknown: %s",
            len(missing), len(names), ", ".join(missing[:10]),
        )
    return verdicts


def classify_all(
    candidates: list[dict],
    *,
    client: Any | None = None,
    pause_seconds: float = 1.0,
    config: dict | None = None,
) -> dict[str, int]:
    """Classify every candidate. Mutates each in place. Returns a count
    dict {legitimate, parked, toxic, empty, unknown} of the EFFECTIVE
    categories — i.e. of what `snapshot_category` actually holds, so in
    shadow mode the tally is all-unknown by construction. The shadow
    verdicts get their own log line and their own field.

    Pass-through behavior when client is None:
        Every candidate gets snapshot_category="unknown" + the version
        stamp WITHOUT any fetch_excerpt or model call. Saves Wayback
        bandwidth and the pipeline still produces a valid daily list on a
        backend-misconfigured day.

    pause_seconds: courtesy pacing between archive.org Availability +
    snapshot fetches. Defaults to 1.0 to match scripts.archive_generator;
    tests should pass 0.0 to avoid wall-clock cost. The sleep happens AFTER
    each fetch but is skipped on the final one. Model calls are NOT paced —
    they are few (one per batch_size domains) and hit a different host.
    """
    counts = _empty_counts()
    if not candidates:
        return counts

    shadow = bool(cfg(config, "shadow"))
    batch_size = max(1, int(cfg(config, "batch_size")))
    timeout_raw = cfg(config, "timeout_seconds")
    timeout_seconds = int(timeout_raw) if timeout_raw else None

    if client is None:
        logger.warning(
            "snapshot_classifier: no client provided — all %d candidates "
            "pass-through as 'unknown' (llm backend unavailable)",
            len(candidates),
        )
        for record in candidates:
            record["wayback_excerpt"] = None
            _tally(counts, _apply_verdict(record, UNKNOWN_CATEGORY, shadow=shadow))
        _log_summary(counts, candidates, shadow=shadow)
        return counts

    logger.info(
        "snapshot_classifier: classifying %d candidates "
        "(batch_size=%d, shadow=%s, fetch pause=%.1fs)",
        len(candidates), batch_size, shadow, pause_seconds,
    )

    # --- Pass 1: fetch excerpts (paced, one host) ---------------------------
    pending: list[tuple[str, dict]] = []   # (name, excerpt) for records to send
    fetchable = [r for r in candidates if r.get("wayback_last_snapshot")]
    last_index = len(fetchable) - 1
    fetched = 0
    for record in candidates:
        if not record.get("wayback_last_snapshot"):
            record["wayback_excerpt"] = None
            continue
        excerpt = _fetch_excerpt_for(record)
        record["wayback_excerpt"] = excerpt
        if excerpt:
            pending.append((record.get("name", ""), excerpt))
        if pause_seconds > 0 and fetched < last_index:
            time.sleep(pause_seconds)
        fetched += 1

    # --- Pass 2: classify in batches ---------------------------------------
    verdicts: dict[str, str] = {}
    batches = _chunked(pending, batch_size)
    if batches:
        logger.info(
            "snapshot_classifier: %d excerpts → %d model call(s) of up to %d",
            len(pending), len(batches), batch_size,
        )
    for batch in batches:
        verdicts.update(_classify_batch(client, batch, timeout_seconds=timeout_seconds))

    # --- Pass 3: apply -----------------------------------------------------
    for record in candidates:
        verdict = UNKNOWN_CATEGORY
        if record.get("wayback_excerpt"):
            verdict = verdicts.get(record.get("name", ""), UNKNOWN_CATEGORY)
        _tally(counts, _apply_verdict(record, verdict, shadow=shadow))

    _log_summary(counts, candidates, shadow=shadow)
    return counts


def _log_summary(
    counts: dict[str, int], candidates: list[dict], *, shadow: bool
) -> None:
    """Emit the run summary.

    The shadow line is emitted FIRST and the canonical line LAST so that any
    log consumer scanning for the historical
    "snapshot_classifier: results — N legitimate, ..." shape reads the
    effective numbers whether it takes the first match or the last.
    """
    if shadow:
        shadow_counts = _empty_counts()
        would_evict: list[str] = []
        for record in candidates:
            verdict = record.get(SHADOW_FIELD) or UNKNOWN_CATEGORY
            _tally(shadow_counts, verdict)
            if verdict == "toxic":
                would_evict.append(str(record.get("name", "")))
        logger.info(
            "snapshot_classifier: SHADOW verdicts (NOT applied to "
            "snapshot_category) — %d legitimate, %d parked, %d toxic, "
            "%d empty, %d unknown",
            shadow_counts["legitimate"], shadow_counts["parked"],
            shadow_counts["toxic"], shadow_counts["empty"],
            shadow_counts["unknown"],
        )
        if would_evict:
            # The shadow field is not in output.py's / domain_archive.py's
            # persisted-field allowlists, so this log line is the only record
            # of WHICH domains the armed gate would have dropped. Validation
            # needs the names, not just the count.
            logger.info(
                "snapshot_classifier: SHADOW would evict %d as toxic — %s",
                len(would_evict), ", ".join(sorted(would_evict)),
            )
    logger.info(
        "snapshot_classifier: results — %d legitimate, %d parked, %d toxic, %d empty, %d unknown",
        counts["legitimate"], counts["parked"], counts["toxic"],
        counts["empty"], counts["unknown"],
    )


def classify_one(
    record: dict, *, client: Any, config: dict | None = None
) -> str:
    """Classify a single candidate — a one-record batch.

    Retained for callers and tests that work one record at a time. Production
    callers should use classify_all: a batch of one wastes the per-call
    overhead that batching exists to amortise.

    Returns the EFFECTIVE category (see classify_all).
    """
    classify_all([record], client=client, pause_seconds=0.0, config=config)
    return str(record.get("snapshot_category") or UNKNOWN_CATEGORY)
