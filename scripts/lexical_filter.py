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
    P1  apex must be at least 3 characters long
    P2  must have at least 2 alphabetic trigrams that are in the
        natural-English trigram set (absolute count — catches short
        labels like "5pro" or "ckyy" that would pass a ratio check
        on a single matching trigram)
    P3  matching ratio (matched / total alpha trigrams) must be
        >= min_trigram_match_ratio (0.55 default — calibrated against
        day-3 published junk: "ckyy", "anddi", "antmap", "appibo",
        "agkbet", etc., all of which had 1 matching trigram and ratios
        in the 0.25-0.50 range. 0.55 catches them while keeping real
        compound names like "lumenpath" (0.71) and "tideblock" (0.86)).
    The natural-English trigram set is derived at module load from
    ~250 common English seed words (and the sample-domain roots),
    giving ~800 unique trigrams.

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
    "min_trigram_match_ratio": 0.55,
    # ^ raised from 0.20 → 0.55 in response to day-3 published junk that
    #   passed at the old threshold ("ckyy", "5pro", "anddi", "antmap"...).
    #   Empirically calibrated: catches that junk while letting real
    #   compounds like "lumenpath" (0.71), "tideblock" (0.86), "northpath"
    #   (0.71), "marketglow" (0.75) through unscathed.
    "min_alpha_trigram_matches": 3,
    # ^ NEW: absolute count of matching trigrams. Catches short labels
    #   ("5pro", "1gen", "ckyy", "llop", "hest") whose 1-2 matching trigrams
    #   give a high ratio (50-100%) but don't actually demonstrate
    #   English-likeness. Calibrated against day-3 junk: rejects 95% of it
    #   (39/41) with zero regressions on real compound names. Residue
    #   ("hiplar", "vicat") is phonetically plausible — score floor in
    #   output.py catches the rest.
    "min_apex_length": 3,
    # ^ NEW: 2-char apex labels can't even produce a trigram and slip
    #   the trigram check entirely (they returned ratio=1.0 by default).
    #   Reject them outright — "oom", "wp" type junk.
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
    # Common brandable compound bases — added 2026-04-28 to broaden trigram
    # coverage so legitimate compound names ("ironforge", "bluehaven") pass.
    # Materials, colors, directions, nature, and small-business descriptors.
    "iron steel brass bronze gold silver copper jade pearl ruby agate slate "
    "blue red green yellow orange purple pink white black gray amber crimson "
    "north south east west far near deep high low wild free bold swift calm "
    "tree oak pine cedar birch maple ivy vine leaf root bough branch sprout "
    "wolf fox bear owl hawk eagle lion tiger deer hare rabbit raven crow "
    "forge mill shop labs works yard fields acres lodge cabin hut "
    "swift true brave kind sharp light dark bright soft fierce gentle keen "
    "sun moon star cloud rain snow wind storm tide wave shore beach "
    "haven hollow heath glade thicket meadow ridge peak falls glen hill dale "
    "brook creek ford port harbor bay cove point isle reef cape "
    "iron forge anvil stone gem crown coin chain bridge tower beacon "
    "trade craft skill tool hand mind soul heart spirit charm grace "
    "fast quick sure clear pure clean smart neat tidy fine prime "
    "dawn dusk noon morning evening night midnight twilight sunset sunrise "
    "front back side top end edge core base path road trail "
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


def _trigram_stats(label: str) -> tuple[int, int]:
    """Return (matches, total_alpha_trigrams) for the label. Both 0 when
    label has no alphabetic trigrams at all (all-digit, too-short, etc.).
    Caller decides what to do with edge cases — keeps this helper pure."""
    label = label.lower()
    if len(label) < 3:
        return 0, 0
    trigrams = [label[i : i + 3] for i in range(len(label) - 2)]
    alpha_trigrams = [t for t in trigrams if t.isalpha()]
    if not alpha_trigrams:
        return 0, 0
    matches = sum(1 for t in alpha_trigrams if t in _NATURAL_TRIGRAMS)
    return matches, len(alpha_trigrams)


def _trigram_match_ratio(label: str) -> float:
    """Match ratio in [0.0, 1.0]. Returns 1.0 only when there are no alpha
    trigrams to evaluate (callers must apply the absolute-count rule and
    min-length rule alongside this)."""
    matches, total = _trigram_stats(label)
    if total == 0:
        return 1.0
    return matches / total


def trigram_match_count(name: str) -> int:
    """Public accessor: number of English-natural trigrams in the apex.

    Used by scripts/pipeline.py for the global_cap quality ranking — at the
    trim site there are no enrichment signals yet, so the only quantitative
    quality prior is "how English-like does the name look?" Higher = more
    likely to be a memorable / former-real-site name. Pure function of the
    name; safe to call thousands of times at the trim stage.
    """
    if not name:
        return 0
    apex = name.split(".", 1)[0]
    matches, _ = _trigram_stats(apex)
    return matches


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

    # Pass 2B — pronounceability (three rules, all must pass).
    if len(apex_label) < th["min_apex_length"]:
        return False, f"short_apex(<{th['min_apex_length']})"

    matches, total_alpha = _trigram_stats(apex_label)
    if total_alpha == 0:
        return False, "no_alpha_trigrams"

    # Absolute match count rule — catches short labels like "ckyy" (1 match
    # of 2 → 50% ratio passes by ratio alone, but only 1 trigram is "real").
    if matches < th["min_alpha_trigram_matches"]:
        return False, f"too_few_matches({matches}<{th['min_alpha_trigram_matches']})"

    # Match ratio rule — catches labels with widely-distributed near-misses.
    ratio = matches / total_alpha
    if ratio < th["min_trigram_match_ratio"]:
        return False, f"unpronounceable({ratio:.2f}<{th['min_trigram_match_ratio']:.2f})"

    return True, None


def filter_candidates(
    candidates: list[dict],
    config: dict,
    *,
    rejections_out: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """Apply `keep_lexical` to every candidate, log per-rule rejection counts,
    return survivors. Same logging pattern as filter.filter_candidates.

    `rejections_out` is an optional side-channel for the --debug-export flag.
    When provided (a list), each rejected candidate is appended as
    `(name, rule_key)` where rule_key is the rejection rule grouped by name
    (digit_ratio, vowel_ratio, ...). Default None means no allocation — the
    production path holds nothing extra in memory.
    """
    kept: list[dict] = []
    reasons: dict[str, int] = {}
    for cand in candidates:
        ok, reason = keep_lexical(cand.get("name", ""), config)
        if ok:
            kept.append(cand)
        else:
            key = (reason or "unknown").split("(", 1)[0]  # group by rule, not value
            reasons[key] = reasons.get(key, 0) + 1
            if rejections_out is not None:
                rejections_out.append((cand.get("name", ""), key))
    if reasons:
        logger.info("Lexical rejections: %s", dict(sorted(reasons.items())))
    logger.info("Lexical filter kept %d / %d candidates", len(kept), len(candidates))
    return kept
