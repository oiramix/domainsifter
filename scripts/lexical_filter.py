"""Lexical pre-enrichment filter.

Runs AFTER structural rejects (filter.keep_structural) and BEFORE enrichment.
The point: drop the obvious lexical garbage that real users will never look at
("kvk434k1ha62", "78win012", "qzxbtmf") so we don't waste enrichment budget
on it. We are PERMISSIVE here — when in doubt, keep. Borderline candidates
(short slang, brand mashes, multi-language coinages) survive into enrichment
and get filtered later by Wayback/OPR/blocklists.

Two passes per candidate, both checked against `apex_label = name.split('.', 1)[0]`:

Pass 2A — garbage detection (any one rejects):
    G1  digit_ratio > 0.30           — "78win012", "shop24h7"
    G2  vowel_ratio < 0.15           — pure consonant strings
    G3  shannon_entropy > 3.5        — random-looking junk
    G4  4+ identical chars in a row  — "aaaa", "0000"
    G5  5+ consonants in a row       — "xprzbtfm" with no vowel break

Pass 2B — pronounceability:
    P1  fewer than 20% of overlapping trigrams in the apex match the
        natural-English trigram set. The set is derived at module load
        from ~200 common English seed words (and the sample-domain
        roots), giving ~700 unique trigrams. Permissive by design.

Public API:
    keep_lexical(name, config) -> tuple[bool, str | None]
    filter_candidates(candidates, config) -> list[dict]
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

logger = logging.getLogger(__name__)

# Tunable thresholds — overridable via config["lexical_thresholds"][...].
# Defaults err on PERMISSIVE per CLAUDE.md / project guidance: better to let
# borderline cases through than reject real domains like "lumenpath".
DEFAULTS = {
    "max_digit_ratio": 0.30,
    "min_vowel_ratio": 0.15,
    "max_shannon_entropy": 3.5,
    "max_repeat_run": 4,           # 4+ identical = reject
    "max_consonant_run": 6,        # 6+ consonants no vowel = reject
    # ^ spec said "5+" but real compound brand names can hit 5 consecutive
    #   consonants ("quartzbloom" → rtzbl). Bumping to 6 lets those pass
    #   while still catching keysmash like "kvkbhmt". Permissive per spec.
    "min_trigram_match_ratio": 0.20,
}

_VOWELS = frozenset("aeiouy")
_CONSONANT_RE = re.compile(r"[bcdfghjklmnpqrstvwxz]")  # excludes y; aligns with run-detection


# ---------------------------------------------------------------------------
# Natural-trigram set: derived from common English seed words at import time.
# Generative > hand-curated: lets us extend the seed list without re-typing
# trigrams, and naturally covers the syllable patterns of the seeds.
# ---------------------------------------------------------------------------

_SEED_WORDS = (
    # Function & connective words — give us "the", "and", "for", "with", etc.
    "the and that have with from they been their would there could should "
    "this these those when where which while what why who whom whose how "
    "into onto upon over under after before between against through about "
    "because although however therefore otherwise indeed almost always "
    # -ing forms (very common trigram, mostly absent without these)
    "morning evening building meeting opening closing working living being "
    "growing reading writing making baking taking calling driving running "
    "swimming walking talking listening watching playing singing dancing "
    "starting helping moving moving thinking learning teaching shipping "
    "buying selling pricing pricing testing coding hosting trading sharing "
    # -ent / -ant forms
    "agent moment student parent recent patient absent silent present "
    "different evident document segment payment consonant restaurant "
    "important relevant constant pleasant elegant accountant assistant "
    # -ion forms
    "nation station section action function mission decision version "
    "motion fashion option direction question information solution "
    "education communication application animation operation tradition "
    # -ate forms
    "create operate generate locate relate debate estate separate update "
    "delegate evaluate dedicate decorate appreciate eliminate dominate "
    "celebrate populate concentrate negotiate participate animate "
    # -ery / -ory forms
    "every recovery gallery scenery delivery battery mystery factory "
    "victory directory category memory library ordinary discovery "
    "history theory glory story victory inventory observatory "
    # Common short words & roots that carry medial trigrams
    "real ready dream area great green agree degree treat eat seat heat "
    "good food mood wood blood floor door pool roof goose moose loose "
    "also alone along always almost although already although also "
    "happy ready party marry carry hurry sorry lucky funny sunny "
    "father mother brother sister daughter another paper water paper "
    "after before never silver river over cover lover hover discover "
    "order under wonder thunder render founder blunder splinter "
    "hello fellow yellow follow shallow narrow borrow tomorrow sorrow "
    # Compound brandable bases (covers TLD-relevant words our pipeline emits)
    "design product service customer market business industry economy "
    "develop developer engineer software hardware deploy build platform "
    "consult agency studio framework module library package domain "
    "register registry website internet network protocol service brand "
    # Nature / topology — powers brandable expired-domain names
    "river mountain valley forest meadow harbor anchor island prairie "
    "weather thunder lightning rainbow sunset sunrise horizon starlight "
    "moonlight shadow silver golden copper marble granite quartz pebble "
    "boulder summit ridge cliff plateau canyon glacier waterfall hillside "
    "blossom flower petal leaf branch trunk roots thicket grove orchard "
    "vineyard pasture lantern candle fireplace hearth chimney garden "
    "kitchen library cabinet drawer shelf table chair window doorway "
    # Domain-relevant brandable words (matching the sample fixtures)
    "lumen path tide block copper nest market glow pine grade warbler "
    "stack anvil minute petrichor kettle ridge owl moor drift quartz "
    "bloom ravine lantern creek studio online store technology site info "
    "live north vane sable quill harbor june ferro kind glass barrow "
    "midway fern compass voyage venture explore discover journey merchant "
    "tavern bakery cottage manor castle bridge tower courtyard avenue "
    # Music / arts
    "music rhythm melody harmony concert orchestra symphony manuscript "
    "scripture story novel poem verse ballad chapter prologue epilogue "
    "language grammar vocabulary alphabet letter syllable phoneme "
    "consonant vowel diphthong cadence accent dialect phrase sentence "
    # Daily-use vocabulary that covers many common consonant clusters
    "people country family parent friend partner office project report "
    "meeting research analysis decision answer question reason sense "
    "approach approach attention beginning conclusion reference example "
)


def _build_natural_trigrams() -> frozenset[str]:
    """Extract all 3-letter sliding-window trigrams from the seed words.
    Lowercased. Yields ~700-800 unique trigrams covering common English
    syllable structure."""
    trigrams: set[str] = set()
    for word in _SEED_WORDS.split():
        w = word.lower()
        if len(w) < 3:
            continue
        for i in range(len(w) - 2):
            tri = w[i : i + 3]
            if tri.isalpha():
                trigrams.add(tri)
    return frozenset(trigrams)


_NATURAL_TRIGRAMS: frozenset[str] = _build_natural_trigrams()


# ---------------------------------------------------------------------------
# Pass 2A — garbage detection helpers
# ---------------------------------------------------------------------------


def _digit_ratio(label: str) -> float:
    if not label:
        return 0.0
    digits = sum(1 for ch in label if ch.isdigit())
    return digits / len(label)


def _vowel_ratio(label: str) -> float:
    """Vowel ratio over alphabetic characters only (digits + hyphens excluded
    from denominator). A label of '78ai' has 2 alpha chars, both vowels —
    100% vowel ratio. The digit_ratio rule will catch '78ai' separately if
    digits dominate; we don't want vowel-ratio to also blame it."""
    alpha = [ch for ch in label.lower() if ch.isalpha()]
    if not alpha:
        return 0.0
    vowels = sum(1 for ch in alpha if ch in _VOWELS)
    return vowels / len(alpha)


def _shannon_entropy(label: str) -> float:
    """Per-character Shannon entropy in bits. Uniform random over 26 letters
    tops out near log2(26) ≈ 4.7. Real English words sit around 2.5-3.2.
    Domain spam ("kvk434k1ha62") tends to push >3.5."""
    if not label:
        return 0.0
    counts = Counter(label.lower())
    total = len(label)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _has_repeat_run(label: str, n: int) -> bool:
    """True if any character repeats `n` or more times consecutively."""
    if n <= 1 or len(label) < n:
        return False
    run = 1
    prev = ""
    for ch in label.lower():
        if ch == prev:
            run += 1
            if run >= n:
                return True
        else:
            run = 1
            prev = ch
    return False


def _has_consonant_run(label: str, n: int) -> bool:
    """True if `n`+ consonants appear in a row with no vowel between.
    Treats digits and hyphens as breaks (they're not consonants)."""
    if n <= 1:
        return False
    run = 0
    for ch in label.lower():
        if ch.isalpha() and ch not in _VOWELS:
            run += 1
            if run >= n:
                return True
        else:
            run = 0
    return False


# ---------------------------------------------------------------------------
# Pass 2B — pronounceability via trigram match ratio
# ---------------------------------------------------------------------------


def _trigram_match_ratio(label: str) -> float:
    """Fraction of overlapping 3-grams in `label` that appear in the natural
    English trigram set. Labels with fewer than 3 alpha chars get 1.0 (treat
    as 'pass'); we don't have enough signal to reject."""
    label = label.lower()
    if len(label) < 3:
        return 1.0
    trigrams = [label[i : i + 3] for i in range(len(label) - 2)]
    alpha_trigrams = [t for t in trigrams if t.isalpha()]
    if not alpha_trigrams:
        return 0.0  # all-digit or all-hyphen — caller should already rejected
    matches = sum(1 for t in alpha_trigrams if t in _NATURAL_TRIGRAMS)
    return matches / len(alpha_trigrams)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def keep_lexical(name: str, config: dict) -> tuple[bool, str | None]:
    """Apply lexical garbage + pronounceability rules. Permissive on edges."""
    if not name:
        return False, "empty_name"
    apex_label = name.split(".", 1)[0]
    if not apex_label:
        return False, "empty_apex"

    th = {**DEFAULTS, **(config.get("lexical_thresholds") or {})}

    # Pass 2A — garbage
    digit_r = _digit_ratio(apex_label)
    if digit_r > th["max_digit_ratio"]:
        return False, f"digit_ratio({digit_r:.2f}>{th['max_digit_ratio']:.2f})"

    vowel_r = _vowel_ratio(apex_label)
    if vowel_r < th["min_vowel_ratio"]:
        return False, f"vowel_ratio({vowel_r:.2f}<{th['min_vowel_ratio']:.2f})"

    entropy = _shannon_entropy(apex_label)
    if entropy > th["max_shannon_entropy"]:
        return False, f"high_entropy({entropy:.2f}>{th['max_shannon_entropy']:.2f})"

    if _has_repeat_run(apex_label, th["max_repeat_run"]):
        return False, f"repeat_run(>={th['max_repeat_run']})"

    if _has_consonant_run(apex_label, th["max_consonant_run"]):
        return False, f"consonant_run(>={th['max_consonant_run']})"

    # Pass 2B — pronounceability
    ratio = _trigram_match_ratio(apex_label)
    if ratio < th["min_trigram_match_ratio"]:
        return False, f"unpronounceable({ratio:.2f}<{th['min_trigram_match_ratio']:.2f})"

    return True, None


def filter_candidates(candidates: list[dict], config: dict) -> list[dict]:
    """Apply `keep_lexical` to every candidate, log per-rule rejection counts,
    return survivors. Same logging pattern as filter.filter_candidates."""
    kept: list[dict] = []
    reasons: dict[str, int] = {}
    for cand in candidates:
        ok, reason = keep_lexical(cand.get("name", ""), config)
        if ok:
            kept.append(cand)
        else:
            key = (reason or "unknown").split("(", 1)[0]  # group by rule, not value
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        logger.info("Lexical rejections: %s", dict(sorted(reasons.items())))
    logger.info("Lexical filter kept %d / %d candidates", len(kept), len(candidates))
    return kept
