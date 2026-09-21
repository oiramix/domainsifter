"""Deterministic abuse-signature scanning over a stored Wayback excerpt.

A SECOND net beside scripts/snapshot_classifier.py's model call, not a
replacement for it. The model reads an excerpt as prose and judges what the
page appears to be about; that is exactly the wrong instrument for CLOAKED
content, where a page presents as one business while its title and headings
carry another operation's brand terms. The worked example from 2026-09-21:
a domain the model labelled `legitimate` was serving casino-brand SEO
("9001cc金沙以诚为本") dressed as an industrial-machinery company. A plain
substring scan of the excerpt the model had already been shown caught it
immediately.

Two further reasons this net exists:

  - It needs no model call, so it still screens on days the LLM backend is
    down. A token outage on 2026-09-21 published 247 domains with no
    screening whatsoever; a signature scan over the stored excerpts would
    have screened all 247 for free.
  - It is deterministic. The `claude_code` backend exposes no temperature or
    seed flag, so identical evidence can still produce a different verdict
    run to run. A substring match cannot.

FALSE POSITIVES ARE THE DESIGN CONSTRAINT.
Two were found on live data on the day this module was written and both are
regression-tested in tests/test_spam_signatures.py:

  - "especialistas" (a Spanish psychology practice) contains "cialis".
  - "booking slots" / "appointment slots" (a Marrakech beauty clinic)
    contains "slot".

Hence: Latin terms match on WORD BOUNDARIES, and the irreducibly ambiguous
terms (`slot`, `bet`, `sex`, bare `cialis`, bare `poker`/`betting`/`lottery`)
are excluded from scripts/config.json entirely — see the
`_doc_terms_excluded` key there before adding anything. CJK terms need no
boundary handling: Chinese does not delimit words with spaces, so a
substring match is the correct primitive.

Public surface (one function):

    scan_excerpt(excerpt, config) -> list[str]

CLAUDE.md compliance notes:
    - Rule 9: every term comes from config; this module ships EMPTY default
      term lists, so a missing config section means "scan nothing", never a
      second hardcoded policy that silently disagrees with config.json.
    - Rule 11/17: fail soft. A malformed excerpt, a malformed config section,
      or a bad regex yields [] and a WARNING. Never raises.
    - Rule 14: logging, not print.
    - Rule 16: no module-level mutable state; config is passed in. Compiled
      patterns are left to `re`'s own internal cache rather than a module
      dict.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Signature terms live in the same config section as the classifier that
# calls us, because they are part of one screening policy.
CONFIG_SECTION = "snapshot_classifier"

# The only excerpt fields scanned. Deliberately NOT snapshot_url — a
# marketplace URL in the Wayback path says nothing about the page, and
# matching it would fire on every snapshot of a legitimate site that once
# linked to one.
TEXT_FIELDS: tuple[str, ...] = ("title", "meta_description")
LIST_FIELDS: tuple[str, ...] = ("h1", "h2")

# Empty by default (rule 9 — config is the single source of policy).
DEFAULTS: dict[str, Any] = {
    "signature_terms_latin": [],
    "signature_terms_cjk": [],
}

# Word-boundary guards for Latin terms.
#
# Why not \b: Python's \b is defined against \w, which under Unicode counts
# CJK ideographs as word characters. "金沙casino娱乐城" would therefore have NO
# boundary before "casino" and \bcasino\b would miss it — precisely the
# cloaked-CJK case this module exists for. Restricting the guard to ASCII
# alphanumerics keeps the false-positive protection that matters
# ("especialistas" still does not match "cialis", "sexxxy" still does not
# match "xxx") while letting a Latin term be found inside CJK text.
_LEFT_GUARD = r"(?<![0-9A-Za-z])"
_RIGHT_GUARD = r"(?![0-9A-Za-z])"


# --- Config (hard rule 9) ---------------------------------------------------


def cfg(config: dict | None, key: str) -> Any:
    """Read ``config["snapshot_classifier"][key]`` falling back to DEFAULTS.

    Mirrors snapshot_classifier.cfg deliberately rather than importing it:
    snapshot_classifier imports THIS module, and a two-line duplication is
    cheaper than an import cycle.
    """
    section = (config or {}).get(CONFIG_SECTION) or {}
    if not isinstance(section, dict):
        return DEFAULTS[key]
    return section.get(key, DEFAULTS[key])


def _terms(config: dict | None, key: str) -> list[str]:
    """Non-empty string terms from one config list. Anything else dropped."""
    raw = cfg(config, key)
    if not isinstance(raw, list):
        logger.warning(
            "spam_signatures: %s.%s is not a list (%s) — no terms of that "
            "class will be scanned", CONFIG_SECTION, key, type(raw).__name__,
        )
        return []
    return [term.strip() for term in raw if isinstance(term, str) and term.strip()]


# --- Pure helpers (unit-tested) ---------------------------------------------


def excerpt_text(excerpt: dict | None) -> str:
    """Flatten the scanned excerpt fields into one newline-joined string.

    Newline-joined rather than space-joined so a term can never be formed
    accidentally across a field boundary (a title ending "...situs" plus an
    h1 starting "judi..." must not read as "situs judi").
    """
    if not isinstance(excerpt, dict):
        return ""
    parts: list[str] = []
    for field in TEXT_FIELDS:
        value = excerpt.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    for field in LIST_FIELDS:
        values = excerpt.get(field)
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return "\n".join(parts)


def latin_pattern(term: str) -> str:
    """The word-boundary-guarded regex source for one Latin term."""
    return _LEFT_GUARD + re.escape(term) + _RIGHT_GUARD


def _matches_latin(text: str, term: str) -> bool:
    """Word-boundary match, case-insensitive. A term whose regex cannot be
    built is skipped rather than allowed to abort the scan."""
    try:
        return re.search(latin_pattern(term), text, re.IGNORECASE) is not None
    except re.error as exc:  # pragma: no cover - re.escape makes this unreachable
        logger.warning("spam_signatures: unusable latin term %r (%s)", term, exc)
        return False


# --- Public API -------------------------------------------------------------


def scan_excerpt(excerpt: dict | None, config: dict) -> list[str]:
    """Return the matched signature terms (empty list = clean). Never raises.

    Args:
        excerpt: a wayback_excerpt dict (the shape
            scripts.wayback_excerpt.fetch_excerpt returns) or None. Only
            title, meta_description, h1 and h2 are read.
        config: the full pipeline config; terms come from
            config["snapshot_classifier"]["signature_terms_latin"] and
            ["signature_terms_cjk"].

    Returns:
        Matched terms in config order, Latin first then CJK, de-duplicated.
        [] for a None / malformed excerpt, for an unconfigured term list, or
        on any internal failure.
    """
    try:
        text = excerpt_text(excerpt)
        if not text:
            return []
        matched: list[str] = []
        for term in _terms(config, "signature_terms_latin"):
            if term not in matched and _matches_latin(text, term):
                matched.append(term)
        # CJK: plain substring. Lowercased on both sides so a mixed-script
        # term ("cc金沙") is still matched case-insensitively; lowercasing is
        # a no-op for the ideographs themselves.
        haystack = text.lower()
        for term in _terms(config, "signature_terms_cjk"):
            if term not in matched and term.lower() in haystack:
                matched.append(term)
        return matched
    except Exception as exc:  # hard rule 17: a second net must never crash
        logger.warning(
            "spam_signatures: scan failed (%s: %s) — treating as no match",
            type(exc).__name__, exc,
        )
        return []
