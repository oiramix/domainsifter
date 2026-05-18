"""Snapshot content classifier.

Reads the most-recent Wayback snapshot of a candidate (title / meta /
h1 / h2 extracted by scripts.wayback_excerpt) and labels it as exactly
one of {legitimate, parked, toxic, empty}, or "unknown" on any failure
path. Failures are SOFT — this module never raises, never aborts the
pipeline, and never installs a circuit breaker. A total Anthropic
outage produces all-"unknown" results and the pipeline publishes
anyway.

Phase 1 status: standalone module. Nothing in scripts/ imports it yet.
The pipeline stage that calls it lands in Phase 4. The one-shot backfill
(scripts/classify_carryover.py) is the first caller.

Persisted fields written onto each candidate dict by classify_all:
    wayback_excerpt                — dict | None (the content the model saw,
                                     or None if fetch failed / no snapshot)
    snapshot_category              — str (one of the 5 categories)
    snapshot_classifier_version    — str ("v1") — bumped whenever the prompt
                                     or parsing rules change in a way that
                                     could produce different labels for the
                                     same input. Future v2/v3 runs can use
                                     this to identify entries that predate
                                     the change and re-classify selectively.

CLAUDE.md compliance notes:
    - Rule 14: logging, not print. WARNING level on every soft-fail path.
    - Rule 16: no module-level mutable state; client is passed in.
    - Rule 17: per-candidate failures contained; pipeline-level failures
      never originate here.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)


# --- Versioning + model config ----------------------------------------------

# Bump this when the system prompt, response parser, or category set changes
# in a way that could alter labels for identical input. Persisted alongside
# snapshot_category so future selective re-classifications can target
# pre-v(N) entries without re-classifying everything.
CLASSIFIER_VERSION = "v1"

HAIKU_MODEL = "claude-haiku-4-5-20251001"
# Single-word output. 8 tokens is generous (4-token English words like
# "legitimate" plus headroom for tokenization quirks) and small enough that
# a runaway response stops fast.
HAIKU_MAX_TOKENS = 8
# Deterministic — same input must always produce the same label.
HAIKU_TEMPERATURE = 0.0

VALID_CATEGORIES: tuple[str, ...] = ("legitimate", "parked", "toxic", "empty")
UNKNOWN_CATEGORY = "unknown"


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

SNAPSHOT_CLASSIFIER_SYSTEM_PROMPT = """You classify the most-recent Wayback snapshot of an expired domain into
exactly ONE of four categories: legitimate, parked, toxic, or empty.

INPUT
A JSON object with these fields, each possibly null or empty:
  title              — page <title>
  meta_description   — page <meta name="description">
  h1                 — list of up to 3 H1 headings
  h2                 — list of up to 5 H2 headings

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
  Example: title="example.com is for sale" / meta="Buy this premium .com domain"
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

RULES
- Output EXACTLY ONE WORD, lowercase, from: legitimate, parked, toxic, empty
- No reasoning, no JSON, no quotes, no punctuation, no explanation
- When borderline between legitimate and any other category, choose legitimate
- A parked page is parked even if its title is in a non-Latin script —
  look for the for-sale / buy-this-domain semantic, not the language
- Translate non-English content before classifying, do not penalize it
  for being non-English
- If your output does not match exactly one of {legitimate, parked, toxic,
  empty}, the caller will treat the result as "unknown" — there is no
  fallback advantage to deviating from the four-word output rule
"""


# --- Anthropic client wrapper ----------------------------------------------


class ClassifierClient:
    """Thin Anthropic wrapper specialized for snapshot classification.

    Deliberately separate from scripts.archive_generator.HaikuClient even
    though both target Haiku: classifier calls use temperature=0.0 +
    max_tokens=8 to enforce deterministic one-word output, vs. the archive
    generator's temperature=0.4 + max_tokens=800 for narrative prose.
    Keeping the call parameters co-located with the prompt avoids the
    parameter-mismatch class of bugs.

    The anthropic SDK import is deferred to __init__ so test code paths
    that pass a fake client via classify_all(client=FakeClient(...)) don't
    require the package or an ANTHROPIC_API_KEY in the environment.
    """

    def __init__(self, api_key: str) -> None:
        from anthropic import Anthropic
        self._client = Anthropic(api_key=api_key)

    def classify(self, user: str) -> str:
        """Return the raw response text. Caller is responsible for
        normalising / validating against VALID_CATEGORIES."""
        resp = self._client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=HAIKU_MAX_TOKENS,
            temperature=HAIKU_TEMPERATURE,
            system=SNAPSHOT_CLASSIFIER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
        out: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                out.append(text)
        return "".join(out).strip()


def make_default_client() -> "ClassifierClient | None":
    """Helper: build a client from ANTHROPIC_API_KEY in the environment.
    Returns None if the key is missing or empty — pair with classify_all's
    client=None pass-through behavior so callers can soft-fail gracefully
    on misconfiguration.
    """
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    return ClassifierClient(api_key)


# --- Pure helpers (unit-tested) ---------------------------------------------


def _build_user_message(excerpt: dict) -> str:
    """Compact JSON of just the four content fields. The metadata
    (snapshot_timestamp, snapshot_url) is bookkeeping — not signal —
    and including it would inflate input tokens without informing the
    label. ensure_ascii=False keeps non-Latin scripts as themselves rather
    than \\u escapes (smaller payload, model has explicit text to read)."""
    payload = {
        "title": excerpt.get("title"),
        "meta_description": excerpt.get("meta_description"),
        "h1": excerpt.get("h1") or [],
        "h2": excerpt.get("h2") or [],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_classification(raw: str) -> str:
    """Strip, lowercase, and validate model output against VALID_CATEGORIES.

    Tolerates trailing punctuation (model accidentally appended a period)
    and stray whitespace. Anything else degrades to UNKNOWN_CATEGORY — the
    prompt explicitly warns the model that out-of-vocabulary outputs lose
    information, so there's no fallback to invent.
    """
    if not raw:
        return UNKNOWN_CATEGORY
    word = raw.strip().lower().rstrip(".!?,;:'\"")
    if word in VALID_CATEGORIES:
        return word
    return UNKNOWN_CATEGORY


# --- Per-candidate orchestration -------------------------------------------


def classify_one(record: dict, *, client: "ClassifierClient | object") -> str:
    """Classify a single candidate. Mutates `record` in place:
        record["wayback_excerpt"]            = dict | None
        record["snapshot_category"]          = one of {legitimate, parked,
                                                       toxic, empty, unknown}
        record["snapshot_classifier_version"] = CLASSIFIER_VERSION

    Returns the assigned category (for the caller's count tally).

    All failure paths route to UNKNOWN_CATEGORY:
        - record has no wayback_last_snapshot → skip fetch entirely
        - fetch_excerpt returns None (Wayback negative / fetch error)
        - fetch_excerpt raises (defensive — contracted not to, but defended)
        - client.classify raises (Anthropic error)
        - response doesn't match any valid category
    Each path logs at WARNING; nothing propagates up.
    """
    # Local import keeps tests free to monkeypatch fetch_excerpt at the
    # scripts.wayback_excerpt module path without a hard top-of-file
    # dependency. (Also avoids importing requests/bs4 in test paths that
    # never need them.)
    from scripts.wayback_excerpt import fetch_excerpt

    name = record.get("name", "")
    last_snapshot = record.get("wayback_last_snapshot")

    # Stamp the version on every attempt — including the no-snapshot
    # fast path — so a future v2 can identify the cohort that ran under
    # v1 logic. (An entry without snapshot_classifier_version predates
    # the classifier subsystem entirely; an entry with v1 was processed
    # by v1's rules, even if the result was unknown.)
    record["snapshot_classifier_version"] = CLASSIFIER_VERSION

    if not last_snapshot:
        # No date → Availability API has nothing to look up. Don't waste
        # the call. This is the common path for wayback_unknown candidates
        # and entries with truly zero historical presence.
        record["wayback_excerpt"] = None
        record["snapshot_category"] = UNKNOWN_CATEGORY
        return UNKNOWN_CATEGORY

    try:
        excerpt = fetch_excerpt(name, last_snapshot)
    except Exception as exc:  # defence-in-depth
        logger.warning(
            "snapshot_classifier: fetch_excerpt raised for %s: %s — treating as unknown",
            name, exc,
        )
        excerpt = None

    record["wayback_excerpt"] = excerpt

    if not excerpt:
        record["snapshot_category"] = UNKNOWN_CATEGORY
        return UNKNOWN_CATEGORY

    try:
        raw = client.classify(_build_user_message(excerpt))
    except Exception as exc:
        logger.warning(
            "snapshot_classifier: Haiku call failed for %s: %s — treating as unknown",
            name, exc,
        )
        record["snapshot_category"] = UNKNOWN_CATEGORY
        return UNKNOWN_CATEGORY

    category = _parse_classification(raw)
    if category == UNKNOWN_CATEGORY and raw:
        # Log the unrecognised output so prompt regressions are visible in
        # the daily report. Truncate at 100 chars in case the model spilled
        # a full essay despite the max_tokens cap.
        logger.warning(
            "snapshot_classifier: unparseable response for %s (raw=%r) — treating as unknown",
            name, raw[:100],
        )
    record["snapshot_category"] = category
    return category


def classify_all(
    candidates: list[dict],
    *,
    client: "ClassifierClient | object | None" = None,
    pause_seconds: float = 1.0,
) -> dict[str, int]:
    """Classify every candidate. Mutates each in place. Returns a count
    dict {legitimate, parked, toxic, empty, unknown}.

    Pass-through behavior when client is None:
        Every candidate gets snapshot_category="unknown" + the version
        stamp WITHOUT any fetch_excerpt or Haiku call. Saves Wayback
        bandwidth and the pipeline still produces a valid daily list
        on a missing-key day. Caller decides whether None-client is an
        error condition (production wet run) or expected (dry-run with
        no key configured).

    pause_seconds: courtesy pacing between archive.org Availability +
    snapshot fetches. Defaults to 1.0 to match scripts.archive_generator;
    tests should pass 0.0 to avoid wall-clock cost. The sleep happens
    AFTER each candidate but is skipped on the final iteration (no point
    waiting before returning).
    """
    counts: dict[str, int] = {c: 0 for c in (*VALID_CATEGORIES, UNKNOWN_CATEGORY)}

    if not candidates:
        return counts

    if client is None:
        logger.warning(
            "snapshot_classifier: no client provided — all %d candidates "
            "pass-through as 'unknown' (ANTHROPIC_API_KEY likely missing)",
            len(candidates),
        )
        for record in candidates:
            record["wayback_excerpt"] = None
            record["snapshot_category"] = UNKNOWN_CATEGORY
            record["snapshot_classifier_version"] = CLASSIFIER_VERSION
            counts[UNKNOWN_CATEGORY] += 1
        return counts

    logger.info(
        "snapshot_classifier: classifying %d candidates (pause=%.1fs between)",
        len(candidates), pause_seconds,
    )
    last_index = len(candidates) - 1
    for i, record in enumerate(candidates):
        category = classify_one(record, client=client)
        counts[category] = counts.get(category, 0) + 1
        if pause_seconds > 0 and i < last_index:
            time.sleep(pause_seconds)

    logger.info(
        "snapshot_classifier: results — %d legitimate, %d parked, %d toxic, %d empty, %d unknown",
        counts["legitimate"], counts["parked"], counts["toxic"],
        counts["empty"], counts["unknown"],
    )
    return counts
