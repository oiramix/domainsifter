"""Unit tests for scripts/spam_signatures.py.

No network, no model, no API key — the module is pure text matching over a
dict. Every domain named here is invented (hard rule 1); the excerpt contents
are paraphrases of shapes seen on live data, never a real site's copy.

Coverage:
  - Latin terms match on WORD BOUNDARIES, case-insensitively
  - The TWO known false positives stay fixed, against the PRODUCTION term
    list in scripts/config.json AND against a deliberately hostile custom
    list: "especialistas" must not match "cialis", "booking slots" /
    "appointment slots" must not match
  - The ambiguous terms stay out of config.json (a guard against re-adding)
  - CJK terms are plain substrings, including a Latin term embedded in CJK
    text (the cloaked-casino shape the module exists for)
  - Only title / meta_description / h1 / h2 are scanned — never snapshot_url
  - Fail-soft: None, a non-dict, junk field types, a missing config section,
    a non-list term list, and an excerpt whose .get explodes all yield []
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import spam_signatures as ss

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "scripts" / "config.json"

# Ambiguous terms that MUST stay out of config.json. Each one caused, or
# would cause, a false positive on live data — see the `_doc_terms_excluded`
# key in scripts/config.json before adding any of them back.
FORBIDDEN_LATIN_TERMS = (
    "slot", "slots", "bet", "sex", "cialis", "poker", "betting", "lottery",
)


def _config(latin: list[str] | None = None, cjk: list[str] | None = None) -> dict:
    return {
        "snapshot_classifier": {
            "signature_terms_latin": latin if latin is not None else [],
            "signature_terms_cjk": cjk if cjk is not None else [],
        }
    }


def _excerpt(**fields) -> dict:
    base = {
        "snapshot_timestamp": "20251215120000",
        "snapshot_url": "http://web.archive.org/web/20251215120000/http://tideblock.io/",
        "title": None,
        "meta_description": None,
        "h1": [],
        "h2": [],
    }
    base.update(fields)
    return base


@pytest.fixture(scope="module")
def production_terms() -> dict:
    """The live term lists from scripts/config.json.

    Read rather than duplicated: a regression test for a false positive is
    only meaningful against the terms production actually ships.
    """
    if not CONFIG_PATH.exists():  # pragma: no cover - config is checked in
        pytest.skip("scripts/config.json not present")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    section = config.get("snapshot_classifier") or {}
    if "signature_terms_latin" not in section:
        pytest.skip("signature terms not yet in scripts/config.json")
    return config


# ---------------------------------------------------------------------------
# Latin matching
# ---------------------------------------------------------------------------


class TestLatinMatching:
    def test_single_term_in_title(self):
        excerpt = _excerpt(title="Marketglow Casino — welcome bonus")
        assert ss.scan_excerpt(excerpt, _config(["casino"])) == ["casino"]

    def test_case_insensitive(self):
        excerpt = _excerpt(title="TIDEBLOCK CASINO")
        assert ss.scan_excerpt(excerpt, _config(["casino"])) == ["casino"]

    def test_multiword_term(self):
        excerpt = _excerpt(meta_description="Daftar situs judi terpercaya")
        assert ss.scan_excerpt(excerpt, _config(["situs judi"])) == ["situs judi"]

    def test_term_in_h1_and_h2(self):
        assert ss.scan_excerpt(
            _excerpt(h1=["Togel hari ini"]), _config(["togel"])
        ) == ["togel"]
        assert ss.scan_excerpt(
            _excerpt(h2=["Bonus sportsbook"]), _config(["sportsbook"])
        ) == ["sportsbook"]

    def test_plural_suffix_does_not_match_singular_term(self):
        # "slots" must not be reachable from a bare "slot" term — the
        # Marrakech-clinic false positive, at the regex level.
        assert ss.scan_excerpt(
            _excerpt(title="Booking slots available"), _config(["slot"])
        ) == []

    def test_embedded_word_does_not_match(self):
        # The Spanish-psychology false positive, at the regex level.
        assert ss.scan_excerpt(
            _excerpt(title="Especialistas en psicología"), _config(["cialis"])
        ) == []

    def test_punctuation_is_a_boundary(self):
        excerpt = _excerpt(title="Marketglow (casino) — bonus")
        assert ss.scan_excerpt(excerpt, _config(["casino"])) == ["casino"]

    def test_matches_are_deduplicated_and_in_config_order(self):
        excerpt = _excerpt(
            title="Casino bonus", h1=["casino nights"], h2=["Togel", "casino"],
        )
        assert ss.scan_excerpt(excerpt, _config(["togel", "casino"])) == [
            "togel", "casino",
        ]

    def test_no_terms_configured_matches_nothing(self):
        excerpt = _excerpt(title="Casino bonus xxx viagra")
        assert ss.scan_excerpt(excerpt, _config([])) == []

    def test_clean_excerpt_returns_empty_list(self):
        excerpt = _excerpt(
            title="Coppernest Woodworking Journal",
            meta_description="Hand-tool joinery notes and shop projects.",
            h1=["Hand-tool joinery"], h2=["Recent projects"],
        )
        assert ss.scan_excerpt(excerpt, _config(["casino", "viagra"])) == []


# ---------------------------------------------------------------------------
# The two named false positives, against the PRODUCTION term list
# ---------------------------------------------------------------------------


class TestKnownFalsePositives:
    def test_especialistas_does_not_match_production_terms(self, production_terms):
        # A Spanish psychology practice. "especialistas" contains "cialis".
        excerpt = _excerpt(
            title="Especialistas en psicología clínica",
            meta_description=(
                "Nuestras especialistas atienden terapia individual y de pareja."
            ),
            h1=["Especialistas en salud mental"],
        )
        assert ss.scan_excerpt(excerpt, production_terms) == []

    def test_appointment_slots_do_not_match_production_terms(self, production_terms):
        # A beauty clinic in Marrakech. "slots" contains "slot".
        excerpt = _excerpt(
            title="Booking slots — beauty clinic Marrakech",
            meta_description="Reserve appointment slots online, no deposit.",
            h1=["Available booking slots"],
            h2=["Appointment slots this week"],
        )
        assert ss.scan_excerpt(excerpt, production_terms) == []

    def test_ambiguous_terms_absent_from_config(self, production_terms):
        latin = [
            t.lower()
            for t in production_terms["snapshot_classifier"]["signature_terms_latin"]
        ]
        for forbidden in FORBIDDEN_LATIN_TERMS:
            assert forbidden not in latin, (
                f"{forbidden!r} is a known false-positive source — see "
                "_doc_terms_excluded in scripts/config.json"
            )

    def test_production_terms_still_catch_real_spam(self, production_terms):
        excerpt = _excerpt(
            title="Situs judi slot gacor maxwin terbaru",
            h1=["Bandar togel online"],
        )
        matched = ss.scan_excerpt(excerpt, production_terms)
        assert "situs judi" in matched
        assert "slot gacor" in matched
        assert "togel" in matched


# ---------------------------------------------------------------------------
# CJK matching
# ---------------------------------------------------------------------------


class TestCjkMatching:
    def test_plain_substring_match_no_boundaries(self):
        excerpt = _excerpt(title="内蒙古机械有限公司 金沙以诚为本")
        assert ss.scan_excerpt(excerpt, _config(cjk=["金沙"])) == ["金沙"]

    def test_match_inside_longer_run_of_characters(self):
        excerpt = _excerpt(h1=["澳门威尼斯人娱乐城注册"])
        matched = ss.scan_excerpt(excerpt, _config(cjk=["威尼斯人", "娱乐城"]))
        assert matched == ["威尼斯人", "娱乐城"]

    def test_latin_term_embedded_in_cjk_text_is_found(self):
        # \b would MISS this: Python counts ideographs as word characters, so
        # there is no word boundary before "casino" in "金沙casino娱乐城".
        excerpt = _excerpt(title="9001cc金沙casino娱乐城")
        assert ss.scan_excerpt(excerpt, _config(["casino"])) == ["casino"]

    def test_cjk_and_latin_both_reported(self):
        excerpt = _excerpt(title="Orange machinery 金沙 casino")
        assert ss.scan_excerpt(excerpt, _config(["casino"], ["金沙"])) == [
            "casino", "金沙",
        ]

    def test_clean_cjk_content_not_flagged(self):
        # Non-Latin script is never a signal in itself.
        excerpt = _excerpt(title="月見うどん専門店", h1=["手打ちうどん"])
        assert ss.scan_excerpt(excerpt, _config(["casino"], ["金沙", "博彩"])) == []


# ---------------------------------------------------------------------------
# Which fields are scanned
# ---------------------------------------------------------------------------


class TestScannedFields:
    def test_snapshot_url_is_not_scanned(self):
        excerpt = _excerpt(
            title="Coppernest Woodworking",
            snapshot_url="http://web.archive.org/web/2025/http://casino.example/",
        )
        assert ss.scan_excerpt(excerpt, _config(["casino"])) == []

    def test_snapshot_timestamp_is_not_scanned(self):
        excerpt = _excerpt(title="Clean title", snapshot_timestamp="20251215120000")
        assert ss.scan_excerpt(excerpt, _config(["2025"])) == []

    def test_terms_cannot_form_across_field_boundaries(self):
        # Fields are newline-joined, so a title ending in "situs" plus an h1
        # starting with "judi" must not read as "situs judi".
        excerpt = _excerpt(title="Alamat situs", h1=["judi terpercaya"])
        assert ss.scan_excerpt(excerpt, _config(["situs judi"])) == []

    def test_excerpt_text_collects_all_four_fields(self):
        text = ss.excerpt_text(_excerpt(
            title="T", meta_description="M", h1=["A", "B"], h2=["C"],
        ))
        assert text.split("\n") == ["T", "M", "A", "B", "C"]

    def test_excerpt_text_skips_blank_and_non_string_values(self):
        text = ss.excerpt_text({
            "title": "   ", "meta_description": None,
            "h1": ["Real", 7, None, ""], "h2": "not-a-list",
        })
        assert text == "Real"


# ---------------------------------------------------------------------------
# Fail-soft (hard rules 11 / 17)
# ---------------------------------------------------------------------------


class TestFailSoft:
    def test_none_excerpt(self):
        assert ss.scan_excerpt(None, _config(["casino"])) == []

    def test_non_dict_excerpt(self):
        assert ss.scan_excerpt("casino", _config(["casino"])) == []       # type: ignore[arg-type]
        assert ss.scan_excerpt(["casino"], _config(["casino"])) == []     # type: ignore[arg-type]
        assert ss.scan_excerpt(42, _config(["casino"])) == []             # type: ignore[arg-type]

    def test_empty_excerpt(self):
        assert ss.scan_excerpt({}, _config(["casino"])) == []
        assert ss.scan_excerpt(_excerpt(), _config(["casino"])) == []

    def test_missing_config_section(self):
        excerpt = _excerpt(title="Casino bonus")
        assert ss.scan_excerpt(excerpt, {}) == []

    def test_malformed_config_section(self, caplog):
        excerpt = _excerpt(title="Casino bonus")
        assert ss.scan_excerpt(excerpt, {"snapshot_classifier": "nope"}) == []

    def test_term_list_not_a_list(self, caplog):
        excerpt = _excerpt(title="Casino bonus")
        config = {"snapshot_classifier": {"signature_terms_latin": "casino"}}
        with caplog.at_level("WARNING"):
            assert ss.scan_excerpt(excerpt, config) == []
        assert any("is not a list" in m for m in caplog.messages)

    def test_non_string_and_blank_terms_ignored(self):
        excerpt = _excerpt(title="Casino bonus")
        config = _config([None, 7, "   ", "casino"])  # type: ignore[list-item]
        assert ss.scan_excerpt(excerpt, config) == ["casino"]

    def test_terms_are_stripped(self):
        excerpt = _excerpt(title="Casino bonus")
        assert ss.scan_excerpt(excerpt, _config(["  casino  "])) == ["casino"]

    def test_exploding_excerpt_returns_empty_list(self, caplog):
        class _Hostile(dict):
            def get(self, *_a, **_k):
                raise RuntimeError("corrupt excerpt")

        with caplog.at_level("WARNING"):
            assert ss.scan_excerpt(_Hostile(), _config(["casino"])) == []
        assert any("scan failed" in m for m in caplog.messages)

    def test_regex_metacharacters_in_term_are_literal(self):
        # A term is data, not a pattern: "c.sino" must not match "casino".
        assert ss.scan_excerpt(_excerpt(title="Casino"), _config(["c.sino"])) == []
        assert ss.scan_excerpt(_excerpt(title="c.sino"), _config(["c.sino"])) == [
            "c.sino",
        ]


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


class TestCfg:
    def test_defaults_are_empty_lists(self):
        assert ss.cfg({}, "signature_terms_latin") == []
        assert ss.cfg(None, "signature_terms_cjk") == []

    def test_config_overrides_default(self):
        config = _config(["casino"], ["金沙"])
        assert ss.cfg(config, "signature_terms_latin") == ["casino"]
        assert ss.cfg(config, "signature_terms_cjk") == ["金沙"]

    def test_latin_pattern_is_boundary_guarded(self):
        assert ss.latin_pattern("casino").startswith("(?<![0-9A-Za-z])")
        assert ss.latin_pattern("casino").endswith("(?![0-9A-Za-z])")
