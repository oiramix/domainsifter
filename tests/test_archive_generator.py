"""Unit + mocked-orchestration tests for scripts/archive_generator.py.

Touches:
  - _filter_qualifying       — pure decision rule (verdict gate + index lookup)
  - _slug_for                — pure filename sanity
  - _excerpt_has_content / _excerpt_for_prompt / _build_user_message
  - build_system_prompt      — grounded vs no-excerpt page contract
  - _build_frontmatter / _build_markdown_file  (Astro schema — must not drift)
  - make_default_client / ArchivePageClient    (scripts.llm_backend wiring)
  - generate_archive end-to-end with a stub client + mocked subprocess
    (so no real backend call, no real git push)

Does NOT touch:
  - Any real LLM backend. `scripts.llm_backend.get_backend` is monkeypatched
    where the wiring itself is under test; every other test injects a stub
    client directly.
  - Real git or network — git_push=False or subprocess patched
  - Real archive.org — wayback_excerpt.fetch_excerpt is auto-stubbed to
    return None by every test via the autouse fixture below. Tests that
    want to exercise the excerpt-wiring path do so by monkeypatching
    ag.fetch_excerpt or ag._load_sidecar_excerpts themselves AFTER this
    autouse fires.

Hard rule 1: every domain below is invented.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import archive_generator as ag
from scripts.llm_backend import LLMBackendError

# Captured at import, before any autouse monkeypatching replaces the module
# attribute, so the one test that needs the REAL loader can restore it.
_REAL_SIDECAR_LOADER = ag._load_sidecar_excerpts


@pytest.fixture(autouse=True)
def _stub_fetch_excerpt(monkeypatch):
    """Default-stub the Wayback excerpt fetch so tests never hit
    archive.org. Returns None so the generated body matches the
    'no-grounding' path. Tests can re-override with their own
    monkeypatch.setattr(ag, 'fetch_excerpt', ...) if they need a
    populated excerpt.

    Also stub time.sleep so the 1s courtesy pace in the per-domain
    loop doesn't multiply the test suite wall-clock (30 fixture
    domains x 1s = 30s of dead sleep otherwise).

    Phase 4 addition (2026-05-20): stub _load_sidecar_excerpts to
    return {} by default — tests that explicitly want sidecar coverage
    monkeypatch their own dict. Without this stub, every test would
    read the real production src/data/wayback_excerpts.json, which
    could mask real bugs if a test fixture name happens to collide
    with a real domain in the sidecar.

    2026-09-19 addition: scrub ANTHROPIC_API_KEY so a developer machine
    that happens to export one cannot make a backend-wiring test pass
    for the wrong reason. The key is no longer a gate in this module.
    """
    monkeypatch.setattr(ag, "fetch_excerpt", lambda *_a, **_k: None)
    monkeypatch.setattr(ag.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(ag, "_load_sidecar_excerpts", lambda *_a, **_k: {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _domain(
    name: str,
    *,
    verdict: str = "Clean",
    score: int = 75,
    wayback: int | None = 1500,
    opr: float | None = 2.3,
    cc: int | None = 47,
) -> dict:
    tld = name.rsplit(".", 1)[-1]
    return {
        "name": name,
        "tld": tld,
        "verdict": verdict,
        "score": score,
        "dropped_date": "2026-05-17",
        "wayback_snapshots": wayback,
        "wayback_last_snapshot": "2024-03-15" if wayback else None,
        "open_page_rank": opr,
        "cc_source_domain_count": cc,
        "cert_history": True,
        "first_seen_date": "2026-05-17",
        "availability_verified_at": "2026-05-17T07:00:00Z",
    }


def _excerpt(
    *,
    title: str | None = "Coppernest Woodworking Journal",
    meta: str | None = "Notes on hand-tool joinery and shop builds.",
    h1: list[str] | None = None,
    h2: list[str] | None = None,
) -> dict:
    return {
        "snapshot_timestamp": "20240315101500",
        "snapshot_url": "http://web.archive.org/web/20240315101500/http://coppernest.org/",
        "title": title,
        "meta_description": meta,
        "h1": ["Hand-tool joinery notes"] if h1 is None else h1,
        "h2": ["Dovetail practice log"] if h2 is None else h2,
    }


_USER_JSON_MARKER = "Generate the archive page for this domain:\n\n"


def _record_from_user(user: str) -> dict:
    """Pull the JSON record back out of the user turn."""
    return json.loads(user.split(_USER_JSON_MARKER, 1)[1])


class _StubClient:
    """In-memory replacement for the backend-backed ArchivePageClient.

    `body_fn(record, system)` produces the markdown body, so a test can vary
    the body by prompt mode. Tracking counters expose call counts and the
    exact prompts to assertions.
    """

    def __init__(self, body_fn=None, raise_on=None, raise_first_n=0, exc=RuntimeError):
        self._body_fn = body_fn or (
            lambda r, s: f"## {r['name']}\n\nLead paragraph for {r['name']}."
        )
        self._raise_on = raise_on or set()        # names that raise per-call
        self._raise_first_n = raise_first_n       # raise on the first N calls
        self._exc = exc
        self.calls: list[dict] = []

    def generate(self, system: str, user: str) -> str:
        record = _record_from_user(user)
        self.calls.append({"system": system, "user": user, "record": record})
        if record["name"] in self._raise_on:
            raise self._exc(f"simulated backend failure for {record['name']}")
        if len(self.calls) <= self._raise_first_n:
            raise self._exc(f"simulated initial failure (call {len(self.calls)})")
        return self._body_fn(record, system)


class _FakeBackend:
    """Stands in for an scripts.llm_backend Backend implementation."""

    name = "fake"

    def __init__(self, reply: str = "## fake\n\nbody", exc: Exception | None = None):
        self._reply = reply
        self._exc = exc
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, timeout_seconds: int | None = None) -> str:
        self.calls.append(
            {"system": system, "user": user, "timeout_seconds": timeout_seconds}
        )
        if self._exc is not None:
            raise self._exc
        return self._reply


# ---------------------------------------------------------------------------
# _filter_qualifying
# ---------------------------------------------------------------------------


def test_filter_qualifying_keeps_clean_and_promising():
    cands = [_domain("marketglow.com", verdict="Clean"), _domain("tideblock.io", verdict="Promising")]
    out = ag._filter_qualifying(cands, already_archived=set())
    assert [c["name"] for c in out] == ["marketglow.com", "tideblock.io"]


def test_filter_qualifying_rejects_caution():
    cands = [_domain("grimvault.com", verdict="Caution")]
    assert ag._filter_qualifying(cands, already_archived=set()) == []


def test_filter_qualifying_rejects_missing_verdict():
    """Legacy payloads or partial data without a verdict field don't
    qualify — the archive only houses entries we've labeled."""
    cand = _domain("legacyloom.com")
    del cand["verdict"]
    assert ag._filter_qualifying([cand], already_archived=set()) == []


def test_filter_qualifying_rejects_already_archived():
    cands = [_domain("dupnest.com", verdict="Clean")]
    out = ag._filter_qualifying(cands, already_archived={"dupnest.com"})
    assert out == []


def test_filter_qualifying_rejects_empty_name():
    cand = _domain("anything.com")
    cand["name"] = ""
    assert ag._filter_qualifying([cand], already_archived=set()) == []


def test_filter_qualifying_passes_mixed_set():
    cands = [
        _domain("keepone.com", verdict="Clean"),
        _domain("keeptwo.org", verdict="Promising"),
        _domain("dropone.com", verdict="Caution"),
        _domain("droptwo.net", verdict="Clean"),  # but already archived
    ]
    out = ag._filter_qualifying(cands, already_archived={"droptwo.net"})
    assert {c["name"] for c in out} == {"keepone.com", "keeptwo.org"}


# ---------------------------------------------------------------------------
# _slug_for
# ---------------------------------------------------------------------------


def test_slug_for_preserves_dots():
    """Dots stay in the filename so Astro's [domain] param resolves to
    the matching content collection entry by name."""
    assert ag._slug_for("coppernest.org") == "coppernest.org"


def test_slug_for_lowercases():
    assert ag._slug_for("UPPERGLOW.COM") == "upperglow.com"


def test_slug_for_allows_dot_dash_underscore():
    assert ag._slug_for("a-b_c.io") == "a-b_c.io"


def test_slug_for_rejects_space():
    with pytest.raises(ValueError, match="unsafe character"):
        ag._slug_for("bad name.com")


def test_slug_for_rejects_slash():
    """Defence-in-depth: a slash would write outside src/content/archive."""
    with pytest.raises(ValueError, match="unsafe character"):
        ag._slug_for("../escape.com")


def test_slug_for_rejects_empty():
    with pytest.raises(ValueError, match="empty name"):
        ag._slug_for("")


# ---------------------------------------------------------------------------
# Config (hard rule 9)
# ---------------------------------------------------------------------------


def test_cfg_prefers_section_then_top_level_then_default():
    assert ag.cfg({"archive_generator": {"timeout_seconds": 42}}, "timeout_seconds") == 42
    assert ag.cfg({"timeout_seconds": 7}, "timeout_seconds") == 7
    assert ag.cfg({}, "timeout_seconds") == ag.DEFAULTS["timeout_seconds"]


def test_cfg_default_sidecar_path_matches_documented_default():
    assert ag.DEFAULTS["sidecar_excerpts_path"] == "src/data/wayback_excerpts.json"


def test_sidecar_path_from_config_resolves_relative_against_repo_root():
    path = ag._sidecar_path_from_config({"archive_generator": {"sidecar_excerpts_path": "a/b.json"}})
    assert path == ag.REPO_ROOT / "a" / "b.json"


def test_sidecar_path_from_config_honours_absolute(tmp_path):
    abs_path = tmp_path / "sidecar.json"
    path = ag._sidecar_path_from_config(
        {"archive_generator": {"sidecar_excerpts_path": str(abs_path)}}
    )
    assert path == abs_path


def test_sidecar_path_from_config_defaults_without_config():
    assert ag._sidecar_path_from_config({}) == ag.SIDECAR_EXCERPTS_PATH


def test_consecutive_failures_abort_is_config_driven(tmp_path):
    """Hard rule 9: the breaker threshold is not a literal at the call site."""
    daily, index, content = _setup_dirs(tmp_path, [_domain(f"brk{i}.com") for i in range(5)])
    client = _StubClient(raise_first_n=5)
    with pytest.raises(RuntimeError, match="2 consecutive backend failures"):
        ag.generate_archive(
            daily_path=daily, index_path=index, content_dir=content,
            client=client, today=date(2026, 5, 17), git_push=False,
            config={"archive_generator": {"consecutive_failures_abort": 2}},
        )


# ---------------------------------------------------------------------------
# Backend wiring (scripts.llm_backend)
# ---------------------------------------------------------------------------


def test_make_default_client_builds_over_llm_backend(monkeypatch):
    """The generator no longer instantiates anthropic.Anthropic — it asks
    llm_backend for whatever config["llm"]["backend"] names."""
    seen: list[dict] = []
    backend = _FakeBackend()

    def fake_get_backend(config):
        seen.append(config)
        return backend

    monkeypatch.setattr(ag.llm_backend, "get_backend", fake_get_backend)
    config = {"llm": {"backend": "claude_code"}}
    client = ag.make_default_client(config)

    assert seen == [config]
    assert client.backend_name == "fake"
    assert isinstance(client, ag.ArchivePageClient)


def test_archive_page_client_forwards_system_user_and_timeout(monkeypatch):
    backend = _FakeBackend(reply="  ## trailing space stripped\n\nbody  ")
    monkeypatch.setattr(ag.llm_backend, "get_backend", lambda _c: backend)

    client = ag.make_default_client({"archive_generator": {"timeout_seconds": 123}})
    out = client.generate(system="SYS", user="USR")

    assert out == "## trailing space stripped\n\nbody"
    assert backend.calls == [
        {"system": "SYS", "user": "USR", "timeout_seconds": 123}
    ]


def test_make_default_client_raises_on_unknown_backend(monkeypatch):
    """A config typo is a systemic error here (hard rule 17): without a
    backend this module produces nothing at all, so failing soft would
    report success having written zero pages."""
    def boom(_config):
        raise LLMBackendError("unknown llm.backend 'typo'")

    monkeypatch.setattr(ag.llm_backend, "get_backend", boom)
    with pytest.raises(RuntimeError, match="no usable LLM backend"):
        ag.make_default_client({"llm": {"backend": "typo"}})


def test_generate_archive_uses_backend_without_anthropic_api_key(tmp_path, monkeypatch):
    """ANTHROPIC_API_KEY is no longer a gate — the backend decides. This is
    the regression that stopped page generation on 2026-07-21."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    backend = _FakeBackend(reply="## grovelane.com\n\nGenerated through the backend.")
    monkeypatch.setattr(ag.llm_backend, "get_backend", lambda _c: backend)

    daily, index, content = _setup_dirs(tmp_path, [_domain("grovelane.com")])
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        today=date(2026, 5, 17), git_push=False,
        config={"llm": {"backend": "claude_code"}},
    )

    assert result["new_count"] == 1
    assert len(backend.calls) == 1
    assert (content / "grovelane.com.md").exists()


# ---------------------------------------------------------------------------
# Prompt construction: excerpt grounding
# ---------------------------------------------------------------------------


def test_excerpt_has_content_detects_each_field():
    assert ag._excerpt_has_content({"title": "Something"})
    assert ag._excerpt_has_content({"meta_description": "Something"})
    assert ag._excerpt_has_content({"h1": ["Heading"]})
    assert ag._excerpt_has_content({"h2": ["Sub"]})


def test_excerpt_has_content_false_for_none_and_empty():
    assert not ag._excerpt_has_content(None)
    assert not ag._excerpt_has_content({})
    assert not ag._excerpt_has_content(
        {"title": "", "meta_description": None, "h1": [], "h2": []}
    )
    # Metadata alone is not content — it says when we looked, not what we saw.
    assert not ag._excerpt_has_content(
        {"snapshot_timestamp": "20240101000000", "snapshot_url": "http://x",
         "title": None, "meta_description": None, "h1": [], "h2": []}
    )
    assert not ag._excerpt_has_content({"title": "   ", "h1": ["  "]})


def test_excerpt_for_prompt_drops_snapshot_url_keeps_timestamp():
    out = ag._excerpt_for_prompt(_excerpt())
    assert out is not None
    assert "snapshot_url" not in out
    assert out["snapshot_timestamp"] == "20240315101500"
    assert set(out) == {"title", "meta_description", "h1", "h2", "snapshot_timestamp"}


def test_build_user_message_includes_real_title_and_h1():
    """THE quality fix: the model must see what the site actually was."""
    rec = _domain("coppernest.org")
    msg = ag._build_user_message(rec, _excerpt())

    assert ag.MODE_MARKER_GROUNDED in msg
    assert "Coppernest Woodworking Journal" in msg
    assert "Notes on hand-tool joinery and shop builds." in msg
    assert "Hand-tool joinery notes" in msg      # h1
    assert "Dovetail practice log" in msg        # h2
    parsed = _record_from_user(msg)
    assert parsed["wayback_excerpt"]["title"] == "Coppernest Woodworking Journal"
    assert parsed["wayback_excerpt"]["h1"] == ["Hand-tool joinery notes"]


def test_build_user_message_includes_record_as_json():
    rec = _domain("marketglow.com")
    msg = ag._build_user_message(rec, None)
    assert _USER_JSON_MARKER in msg
    parsed = _record_from_user(msg)
    assert parsed["name"] == "marketglow.com"
    assert parsed["verdict"] == "Clean"


def test_build_user_message_sorts_keys_for_determinism():
    rec = _domain("marketglow.com")
    msg_a = ag._build_user_message(rec, None)
    rec_reordered = {k: rec[k] for k in sorted(rec.keys(), reverse=True)}
    msg_b = ag._build_user_message(rec_reordered, None)
    assert msg_a == msg_b


def test_build_user_message_missing_excerpt_instructs_no_speculation():
    rec = _domain("tideblock.io")
    msg = ag._build_user_message(rec, None)
    assert ag.MODE_MARKER_NO_EXCERPT in msg
    assert "Do not speculate" in msg
    assert "could not be retrieved" in msg
    assert _record_from_user(msg)["wayback_excerpt"] is None


def test_build_user_message_empty_excerpt_is_treated_as_missing():
    """A snapshot with no title/meta/h1/h2 is evidence of nothing."""
    rec = _domain("tideblock.io")
    empty = {"snapshot_timestamp": "2024", "title": None,
             "meta_description": None, "h1": [], "h2": []}
    msg = ag._build_user_message(rec, empty)
    assert ag.MODE_MARKER_NO_EXCERPT in msg
    assert _record_from_user(msg)["wayback_excerpt"] is None


def test_build_user_message_keeps_non_english_readable():
    """ensure_ascii=False: the model has to describe the content in English,
    which it cannot do from \\uXXXX escapes."""
    rec = _domain("lanternfold.net")
    msg = ag._build_user_message(
        rec, _excerpt(title="?????????", meta="Ein Verzeichnis für Werkzeuge",
                      h1=["工具目录"], h2=[]),
    )
    assert "工具目录" in msg          # raw CJK, not escaped
    assert "\\u5de5" not in msg
    assert "Ein Verzeichnis für Werkzeuge" in msg


# ---------------------------------------------------------------------------
# System prompt: the two modes
# ---------------------------------------------------------------------------


def test_system_prompt_grounded_mode_demands_excerpt_grounding():
    sys_prompt = ag.build_system_prompt(has_excerpt=True)
    assert ag.MODE_MARKER_GROUNDED in sys_prompt
    assert ag.MODE_MARKER_NO_EXCERPT not in sys_prompt
    assert "### Historical use" in sys_prompt
    assert "grounded ONLY in the excerpt" in sys_prompt


def test_system_prompt_no_excerpt_mode_is_short_and_factual():
    sys_prompt = ag.build_system_prompt(has_excerpt=False)
    assert ag.MODE_MARKER_NO_EXCERPT in sys_prompt
    assert ag.MODE_MARKER_GROUNDED not in sys_prompt
    assert "SHORTER" in sys_prompt
    assert "could not be retrieved" in sys_prompt
    assert "must not fill the gap with a guess" in sys_prompt


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_system_prompt_forbids_name_speculation_in_both_modes(has_excerpt):
    """The 4minutes.net failure mode: four guesses from the name in one
    sentence, presented as fact. Hard rule 2."""
    sys_prompt = ag.build_system_prompt(has_excerpt)
    for banned in (
        "the name suggests",
        "based on the name structure",
        "likely operated as",
        "the linguistic composition suggests",
    ):
        assert banned in sys_prompt, f"prompt must name-and-ban {banned!r}"
    assert "the name is not evidence" in sys_prompt.lower()


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_system_prompt_forbids_marketing_and_valuations(has_excerpt):
    sys_prompt = ag.build_system_prompt(has_excerpt).lower()
    assert "testimonial" in sys_prompt
    assert "valuation" in sys_prompt
    assert "traffic" in sys_prompt
    assert "marketing" in sys_prompt


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_system_prompt_handles_non_english_excerpts(has_excerpt):
    sys_prompt = ag.build_system_prompt(has_excerpt)
    assert "NON-ENGLISH CONTENT" in sys_prompt
    assert "Chinese" in sys_prompt
    assert "untranslated" in sys_prompt


# --- anti-speculation, after the 2026-09-19 measured leak -------------------
#
# A live rehearsal produced "The domain's name suggests a connection to a
# filmmaker or entertainment project, but the excerpt itself provides no
# content evidence of what the site actually was" for a domain whose only
# archived capture was a registrar parking placeholder. A parked capture is
# content, so that generation took the GROUNDED path — the one mode whose
# block never restated the ban.
#
# NOTHING BELOW TESTS MODEL BEHAVIOUR. A prompt cannot be proven obeyed
# without a live run; these assert that the prompt SAYS the right thing, in
# both modes, in the structural positions the fix depends on.


def _mode_block(has_excerpt: bool) -> str:
    """Just the mode-specific tail, with the shared preamble removed.

    Lets a test distinguish 'the rule is somewhere in the prompt' from 'the
    rule is restated inside the branch the model is actually drafting under',
    which is the distinction the leak turned on.
    """
    full = ag.build_system_prompt(has_excerpt)
    assert full.startswith(ag._SYSTEM_PROMPT_COMMON)
    return full[len(ag._SYSTEM_PROMPT_COMMON):]


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_evidence_rule_is_stated_positively_not_only_as_a_banned_list(has_excerpt):
    """Root cause (b): a phrase blacklist is satisfiable by paraphrase. The
    rule has to name the MOVE — sourcing a claim — not just the wording."""
    sys_prompt = ag.build_system_prompt(has_excerpt)
    assert "THE EVIDENCE RULE" in sys_prompt
    assert "traceable to one named field of `wayback_excerpt`" in sys_prompt
    assert "the sentence does not go on the page" in sys_prompt


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_banned_phrase_list_is_present_in_both_modes(has_excerpt):
    """The exact phrase that leaked, plus its neighbours, named verbatim."""
    sys_prompt = ag.build_system_prompt(has_excerpt)
    for banned in (
        "the name suggests",
        "based on the name structure",
        "the linguistic composition suggests",
        "likely operated as",
        "may have served",
        "could have been used for",
        "points to a connection with",
        "hints at",
        "appears to have been",
    ):
        assert banned in sys_prompt, f"prompt must name-and-ban {banned!r}"
    assert "in either mode" in sys_prompt


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_self_qualified_guess_is_explicitly_still_a_violation(has_excerpt):
    """Root cause (c): the leaked sentence disclaimed itself in the same
    breath, which reads as compliance. It isn't — the guess is published
    either way."""
    sys_prompt = ag.build_system_prompt(has_excerpt)
    assert "A hedge does not rescue a guess." in sys_prompt
    assert "is a full violation" in sys_prompt
    # The shape of the observed failure, quoted back as the example.
    assert "but the excerpt provides no content evidence" in sys_prompt


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_parked_placeholder_capture_is_addressed_explicitly(has_excerpt):
    """Root cause (d): a parking page IS an excerpt, so it routes to the
    grounded branch, where having *some* evidence felt like licence to fill
    in around it. Say what a placeholder is evidence OF, and what it isn't."""
    sys_prompt = ag.build_system_prompt(has_excerpt)
    assert "PLACEHOLDER AND PARKED CAPTURES" in sys_prompt
    assert "parking page" in sys_prompt
    assert "domain-for-sale notice" in sys_prompt
    assert "unrecorded is unknown" in sys_prompt
    assert "NOT a licence to reconstruct the site that came before it" in sys_prompt


def test_grounded_mode_block_itself_repeats_the_ban():
    """The specific hole: the ban lived only in the shared preamble and in
    the NO-EXCERPT block. The GROUNDED block — the one that leaked — now
    carries its own restatement, at the point of maximum pressure (the
    `### Historical use` section) and again at the end."""
    block = _mode_block(has_excerpt=True)
    assert "PLACEHOLDER CAPTURES" in block
    assert "The name is still not evidence." in block
    assert "holding some evidence is never a licence to fill in around it" in block


def test_no_excerpt_mode_block_itself_repeats_the_ban():
    block = _mode_block(has_excerpt=False)
    assert "the name is not evidence" in block
    assert "must not fill the gap with a guess" in block


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_each_mode_block_ends_with_a_sourcing_recheck(has_excerpt):
    """Root cause (a): the rule was stated once, early, then buried under six
    numbered drafting sections. Recency now cuts the other way — the last
    instruction before drafting is 'source every claim or delete it'."""
    block = _mode_block(has_excerpt)
    marker = "BEFORE YOU RETURN THE PAGE"
    assert marker in block
    # It is genuinely last: nothing but the recheck follows it in the block.
    assert block.index(marker) > block.index("Closing line")
    assert "with or without a hedge attached" in block
    assert "echoes the words inside the domain name" in block


@pytest.mark.parametrize(
    ("has_excerpt", "target"), [(True, "250-400 words"), (False, "120-200 words")],
)
def test_word_targets_are_unchanged_but_are_not_quotas(has_excerpt, target):
    """The targets stay exactly as they were — but a length target the
    evidence cannot fill is precisely the pressure that produced the leak,
    so each block now says which way to resolve that conflict."""
    block = _mode_block(has_excerpt)
    assert target in block
    assert "ceiling on padding, not a quota to fill" in block


def test_grounded_mode_still_demands_at_least_four_name_mentions():
    """Regression guard on the other quota left intact: the fix relaxes how
    LONG the page must be, never the requirement to name the domain."""
    assert "at least 4 times" in _mode_block(has_excerpt=True)
    assert "at least 3 times" in _mode_block(has_excerpt=False)


def test_build_user_message_grounded_header_also_forbids_speculation():
    """The user turn's mode line is the last thing before the record. The
    no-excerpt header always carried 'Do not speculate'; the grounded one
    did not, so the reminder nearest the data was mode-dependent."""
    msg = ag._build_user_message(_domain("coppernest.org"), _excerpt())
    assert ag.MODE_MARKER_GROUNDED in msg
    assert "Do not speculate from the domain name." in msg
    assert "parked or placeholder capture" in msg


@pytest.mark.parametrize("has_excerpt", [True, False])
def test_system_prompt_never_asks_for_frontmatter(has_excerpt):
    """The script owns the YAML block; a model-written one would drift from
    the zod schema and break the Astro build."""
    sys_prompt = ag.build_system_prompt(has_excerpt)
    assert "No frontmatter" in sys_prompt
    assert "generator script adds the frontmatter" in sys_prompt


# ---------------------------------------------------------------------------
# _build_frontmatter / _build_markdown_file
#
# These assert the Astro content-collection contract (src/content/config.ts).
# A new, renamed or reordered key here is a site-build break, not a test nit.
# ---------------------------------------------------------------------------

ASTRO_SCHEMA_KEYS = [
    "name", "tld", "verdict", "score", "dropped_date", "archived_date",
    "wayback_snapshots", "wayback_last_snapshot", "open_page_rank",
    "cc_source_domain_count", "cert_history", "first_seen_date",
    "availability_verified_at",
]

# Optional evidence block (2026-09-19). Emitted only when a value exists, so
# the 113 pages written before this date stay valid against the schema.
EVIDENCE_KEYS = [
    "phase2_reason", "snapshot_category", "excerpt_title",
    "excerpt_meta_description", "excerpt_h1", "excerpt_h2",
    "excerpt_snapshot_timestamp",
]

try:  # PyYAML is not a declared dependency; use it when it happens to be here.
    import yaml as _yaml
except ImportError:  # pragma: no cover — depends on the machine, not the code
    _yaml = None


def _frontmatter_block(text: str) -> str:
    """The YAML between the delimiters.

    Delimiters are whole LINES that are exactly `---`, which is how Astro
    (and every other frontmatter reader) finds them. A naive
    `text.split("---")` is wrong and this test suite proved it: the hostile
    fixture's title contains a literal `---`, which a substring split would
    happily treat as the end of the block.
    """
    lines = text.splitlines()
    assert lines[0] == "---", f"no opening delimiter: {lines[:1]}"
    end = lines.index("---", 1)
    return "\n".join(lines[1:end])


def _parse_frontmatter(text: str) -> dict:
    """Parse the generated YAML frontmatter back into a dict.

    Prefers a real YAML parser when one is installed. The fallback leans on a
    property worth stating out loud: every scalar and sequence this module
    emits is ALSO valid JSON (double-quoted, \\uXXXX escapes, flow lists), so
    `json.loads` is a faithful reader of it.
    """
    block = _frontmatter_block(text)
    if _yaml is not None:
        parsed = _yaml.safe_load(block)
        assert isinstance(parsed, dict), f"frontmatter is not a mapping: {parsed!r}"
        return parsed
    out: dict = {}
    for line in block.strip().splitlines():
        key, _, raw = line.partition(": ")
        raw = raw.strip()
        if raw in ("null", ""):
            out[key] = None
        elif raw in ("true", "false"):
            out[key] = raw == "true"
        elif raw[0] in '"[':
            out[key] = json.loads(raw)
        else:
            out[key] = float(raw) if "." in raw else int(raw)
    return out


def test_build_frontmatter_emits_exactly_the_astro_schema_keys():
    rec = _domain("marketglow.com", verdict="Promising", score=55, wayback=2000)
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17")
    assert fm.startswith("---\n")
    assert fm.rstrip().endswith("---")
    body_lines = [ln for ln in fm.strip().splitlines() if ln != "---"]
    emitted = [ln.split(":", 1)[0] for ln in body_lines]
    assert emitted == ASTRO_SCHEMA_KEYS


def test_build_frontmatter_never_leaks_the_excerpt_into_yaml():
    """The excerpt belongs in the prompt and the body, never the schema."""
    rec = _domain("coppernest.org")
    rec["wayback_excerpt"] = _excerpt()
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17")
    assert "wayback_excerpt" not in fm
    assert "Coppernest Woodworking Journal" not in fm


def test_build_frontmatter_serializes_null_as_yaml_null():
    rec = _domain("marketglow.com", wayback=None, opr=None, cc=None)
    rec["wayback_last_snapshot"] = None
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17")
    assert "wayback_snapshots: null" in fm
    assert "open_page_rank: null" in fm
    assert "cc_source_domain_count: null" in fm


def test_build_frontmatter_quotes_strings_and_escapes_quotes():
    rec = _domain("marketglow.com")
    rec["first_seen_date"] = '2026-05-17"quoted"'  # contrived
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17")
    assert '\\"quoted\\"' in fm


# --- evidence block (2026-09-19) -------------------------------------------


def test_evidence_keys_omitted_entirely_when_absent():
    """The 113 pre-existing pages have none of these. Omission — not null —
    is the normal shape, so nothing about them can fail the site build."""
    fm = ag._build_frontmatter(_domain("marketglow.com"), archived_date="2026-05-17")
    for key in EVIDENCE_KEYS:
        assert f"{key}:" not in fm
    assert _parse_frontmatter(fm + "\nbody\n") .get("phase2_reason") is None


def test_evidence_keys_follow_the_required_contract_in_order():
    rec = _domain("coppernest.org")
    rec["phase2_reason"] = "long history, clean signals"
    rec["snapshot_category"] = "legitimate"
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17", excerpt=_excerpt())
    emitted = [
        ln.split(":", 1)[0] for ln in fm.strip().splitlines() if ln != "---"
    ]
    assert emitted == ASTRO_SCHEMA_KEYS + EVIDENCE_KEYS


def test_phase2_reason_persisted_when_present():
    rec = _domain("marketglow.com")
    rec["phase2_reason"] = "aged domain, real editorial history"
    parsed = _parse_frontmatter(
        ag._build_frontmatter(rec, archived_date="2026-05-17") + "\nbody\n"
    )
    assert parsed["phase2_reason"] == "aged domain, real editorial history"


@pytest.mark.parametrize(
    "raw",
    [
        "missing from response",
        "Missing From Response",
        "  missing from response  ",
    ],
)
def test_phase2_reason_placeholder_treated_as_absent(raw):
    """The ranker writes this when a name went to the model and came back
    absent. It is a pipeline marker, not a justification, and must never
    reach a page."""
    rec = _domain("marketglow.com")
    rec["phase2_reason"] = raw
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17")
    assert "phase2_reason" not in fm
    assert "missing from response" not in fm.lower()


@pytest.mark.parametrize("raw", [None, "", "   ", 42, {"not": "a string"}])
def test_phase2_reason_junk_treated_as_absent(raw):
    rec = _domain("marketglow.com")
    rec["phase2_reason"] = raw
    assert "phase2_reason" not in ag._build_frontmatter(rec, archived_date="2026-05-17")


def test_snapshot_category_persisted_when_present():
    rec = _domain("marketglow.com")
    rec["snapshot_category"] = "parked"
    parsed = _parse_frontmatter(
        ag._build_frontmatter(rec, archived_date="2026-05-17") + "\nbody\n"
    )
    assert parsed["snapshot_category"] == "parked"


@pytest.mark.parametrize("raw", [None, "", "   ", 7])
def test_snapshot_category_absent_or_junk_is_omitted(raw):
    rec = _domain("marketglow.com")
    rec["snapshot_category"] = raw
    assert "snapshot_category" not in ag._build_frontmatter(rec, archived_date="2026-05-17")


def test_excerpt_evidence_persisted_when_present():
    fm = ag._build_frontmatter(
        _domain("coppernest.org"), archived_date="2026-05-17", excerpt=_excerpt(),
    )
    parsed = _parse_frontmatter(fm + "\nbody\n")
    assert parsed["excerpt_title"] == "Coppernest Woodworking Journal"
    assert parsed["excerpt_meta_description"] == "Notes on hand-tool joinery and shop builds."
    assert parsed["excerpt_h1"] == ["Hand-tool joinery notes"]
    assert parsed["excerpt_h2"] == ["Dovetail practice log"]


def test_excerpt_snapshot_timestamp_persisted_with_the_text_it_dates():
    """Provenance has to be as durable as the quotation. The page says the
    text was reproduced from a capture on this date; if the date resolved
    from the sidecar alone it would vanish when the domain rolled out of it,
    leaving undated third-party content standing as evidence."""
    fm = ag._build_frontmatter(
        _domain("coppernest.org"), archived_date="2026-05-17", excerpt=_excerpt(),
    )
    parsed = _parse_frontmatter(fm + "\nbody\n")
    assert parsed["excerpt_snapshot_timestamp"] == "20240315101500"


def test_excerpt_snapshot_timestamp_omitted_when_it_would_date_nothing():
    """The mirror-image fault: a capture date attached to no content."""
    fm = ag._build_frontmatter(
        _domain("tideblock.io"), archived_date="2026-05-17",
        excerpt={"snapshot_timestamp": "20240315101500", "title": None,
                 "meta_description": "", "h1": [], "h2": []},
    )
    assert "excerpt_snapshot_timestamp" not in fm


def test_excerpt_snapshot_timestamp_omitted_when_absent_or_junk():
    for stamp in (None, "", "   ", 20240315101500):
        excerpt = dict(_excerpt(), snapshot_timestamp=stamp)
        fm = ag._build_frontmatter(
            _domain("coppernest.org"), archived_date="2026-05-17", excerpt=excerpt,
        )
        assert "excerpt_title:" in fm          # the text still lands
        assert "excerpt_snapshot_timestamp" not in fm


def test_excerpt_snapshot_timestamp_is_cleaned_like_any_untrusted_field():
    """It should be 14 digits. It is still third-party data, so it is not
    trusted to be."""
    excerpt = dict(_excerpt(), snapshot_timestamp='2024"03\n15 ‮EVIL')
    parsed = _parse_frontmatter(
        ag._build_frontmatter(
            _domain("coppernest.org"), archived_date="2026-05-17", excerpt=excerpt,
        ) + "\nbody\n"
    )
    assert parsed["excerpt_snapshot_timestamp"] == '2024"03 15 EVIL'
    assert "\n" not in parsed["excerpt_snapshot_timestamp"]


def test_excerpt_evidence_omitted_for_missing_or_empty_excerpt():
    base = _domain("tideblock.io")
    for excerpt in (None, {}, {"title": None, "meta_description": "", "h1": [], "h2": []}):
        fm = ag._build_frontmatter(base, archived_date="2026-05-17", excerpt=excerpt)
        for key in (
            "excerpt_title", "excerpt_meta_description", "excerpt_h1",
            "excerpt_h2", "excerpt_snapshot_timestamp",
        ):
            assert f"{key}:" not in fm


def test_excerpt_evidence_drops_empty_headings_but_keeps_real_ones():
    fm = ag._build_frontmatter(
        _domain("coppernest.org"), archived_date="2026-05-17",
        excerpt=_excerpt(h1=["  ", "Real heading", None], h2=[]),
    )
    parsed = _parse_frontmatter(fm + "\nbody\n")
    assert parsed["excerpt_h1"] == ["Real heading"]
    assert "excerpt_h2:" not in fm


def test_excerpt_evidence_preserves_non_english_text():
    fm = ag._build_frontmatter(
        _domain("lanternfold.net"), archived_date="2026-05-17",
        excerpt=_excerpt(
            title="工具箱目录",
            meta="Werkzeugverzeichnis für Heimwerker",
            h1=["เครื่องมือ"], h2=[],
        ),
    )
    parsed = _parse_frontmatter(fm + "\nbody\n")
    assert parsed["excerpt_title"] == "工具箱目录"
    assert parsed["excerpt_meta_description"] == "Werkzeugverzeichnis für Heimwerker"
    assert parsed["excerpt_h1"] == ["เครื่องมือ"]


# The hostile fixture. Everything here has been seen in real archived spam:
# smart and straight quotes, colons, hashes, a leading dash, embedded
# newlines, a YAML document terminator, control characters and a bidi
# override. Any one of them escaping its scalar fails `npm run build` for
# the WHOLE site, not just one page.
_HOSTILE_EXCERPT = {
    "snapshot_timestamp": "20240315101500",
    "title": 'He said: "buy now" -- 50% off #1 你好\nsecond line\r\n---',
    "meta_description": "- leading dash: colon | pipe > gt & amp \\ backslash ‮RTL‬",
    "h1": ["{ brace }: [bracket]", "tab\there\x07bell", "สวัสดี: 世界"],
    "h2": ["'single' ‘smart’ “double”", "...\n---\nname: \"injected.com\""],
}


def test_hostile_excerpt_produces_parseable_frontmatter():
    rec = _domain("coppernest.org")
    rec["phase2_reason"] = 'reason with "quotes": and a\nnewline'
    md = ag._build_markdown_file(
        rec, body="## coppernest.org\n\nBody.", archived_date="2026-05-17",
        excerpt=_HOSTILE_EXCERPT,
    )
    parsed = _parse_frontmatter(md)

    # The required contract survived intact — nothing was displaced or
    # overwritten by the injected 'name:' line in the h2 fixture.
    assert parsed["name"] == "coppernest.org"
    assert parsed["verdict"] == "Clean"
    assert parsed["score"] == 75
    for key in ASTRO_SCHEMA_KEYS:
        assert key in parsed

    # Newlines were collapsed rather than carried into the YAML, and the
    # document terminator is inert text inside a quoted scalar.
    assert "\n" not in parsed["excerpt_title"]
    assert "buy now" in parsed["excerpt_title"]
    assert parsed["phase2_reason"] == 'reason with "quotes": and a newline'
    assert len(parsed["excerpt_h2"]) == 2


def test_hostile_excerpt_cannot_inject_a_frontmatter_key():
    """The h2 fixture contains a literal `name: "injected.com"` line."""
    rec = _domain("coppernest.org")
    md = ag._build_markdown_file(
        rec, body="## coppernest.org\n\nBody.", archived_date="2026-05-17",
        excerpt=_HOSTILE_EXCERPT,
    )
    frontmatter_block = _frontmatter_block(md)
    # Exactly one `name:` line, and it is ours.
    name_lines = [ln for ln in frontmatter_block.splitlines() if ln.startswith("name:")]
    assert name_lines == ['name: "coppernest.org"']
    assert _parse_frontmatter(md)["name"] == "coppernest.org"


def test_hostile_excerpt_strips_control_and_bidi_characters():
    parsed = _parse_frontmatter(
        ag._build_markdown_file(
            _domain("coppernest.org"), body="## x\n\nb.", archived_date="2026-05-17",
            excerpt=_HOSTILE_EXCERPT,
        )
    )
    blob = json.dumps(parsed, ensure_ascii=False)
    for bad in ("\x07", "‮", "‬", "\r"):
        assert bad not in blob


def test_excerpt_text_is_capped_and_cap_is_config_driven():
    long_title = "x" * 5000
    fm = ag._build_frontmatter(
        _domain("coppernest.org"), archived_date="2026-05-17",
        excerpt=_excerpt(title=long_title, h1=["y" * 5000], h2=[]),
        config={"archive_generator": {
            "excerpt_max_field_chars": 50, "excerpt_max_heading_chars": 20,
        }},
    )
    parsed = _parse_frontmatter(fm + "\nbody\n")
    assert len(parsed["excerpt_title"]) == 50
    assert parsed["excerpt_title"].endswith("…")
    assert len(parsed["excerpt_h1"][0]) == 20


def test_excerpt_heading_count_is_capped_and_config_driven():
    fm = ag._build_frontmatter(
        _domain("coppernest.org"), archived_date="2026-05-17",
        excerpt=_excerpt(h1=[], h2=[f"heading {i}" for i in range(20)]),
        config={"archive_generator": {"excerpt_max_headings": 3}},
    )
    parsed = _parse_frontmatter(fm + "\nbody\n")
    assert parsed["excerpt_h2"] == ["heading 0", "heading 1", "heading 2"]


def test_default_excerpt_caps_match_the_astro_page():
    """These mirror MAX_FIELD_CHARS / MAX_HEADING_CHARS / MAX_HEADINGS in
    src/pages/d/[domain].astro so its truncation never double-fires."""
    assert ag.DEFAULTS["excerpt_max_field_chars"] == 300
    assert ag.DEFAULTS["excerpt_max_heading_chars"] == 140
    assert ag.DEFAULTS["excerpt_max_headings"] == 5


def test_shipped_config_agrees_with_the_in_code_defaults():
    """scripts/config.json is what production reads; DEFAULTS only cover the
    keys it hasn't got yet. Where both exist they must not disagree."""
    shipped = json.loads(ag.CONFIG_PATH.read_text(encoding="utf-8"))
    section = shipped.get("archive_generator") or {}
    for key in (
        "sidecar_excerpts_path", "timeout_seconds", "consecutive_failures_abort",
        "excerpt_max_field_chars", "excerpt_max_heading_chars",
        "excerpt_max_headings",
    ):
        assert key in section, f"{key} missing from config.json archive_generator"
        assert section[key] == ag.DEFAULTS[key], f"{key} disagrees with DEFAULTS"


def test_zod_schema_declares_every_key_the_generator_can_emit():
    """src/content/config.ts is the build-time gate. A key written here but
    absent there fails `npm run build`; a new key that is not optional fails
    it for the 113 files that predate this change."""
    schema = (ag.REPO_ROOT / "src" / "content" / "config.ts").read_text(encoding="utf-8")
    declared = dict(re.findall(r"^\s{4}(\w+):\s*(z\.[^\n]*)$", schema, re.MULTILINE))
    for key in ASTRO_SCHEMA_KEYS + EVIDENCE_KEYS:
        assert key in declared, f"{key} is missing from the zod schema"
    for key in EVIDENCE_KEYS:
        assert ".optional()" in declared[key], f"{key} must be optional"
        assert ".nullable()" in declared[key], f"{key} must be nullable"


def test_build_markdown_file_has_frontmatter_then_body():
    rec = _domain("marketglow.com")
    out = ag._build_markdown_file(
        rec, body="## marketglow.com\n\nBody text.", archived_date="2026-05-17",
    )
    assert out.startswith("---\n")
    parts = out.split("---\n", 2)
    assert "## marketglow.com" in parts[2]
    assert out.endswith("\n")


# ---------------------------------------------------------------------------
# generate_archive (mocked end-to-end)
# ---------------------------------------------------------------------------


def _setup_dirs(tmp_path: Path, domains: list[dict], index_entries: list[dict] | None = None):
    daily_path = tmp_path / "daily-domains.json"
    daily_path.write_text(json.dumps({"domains": domains}), encoding="utf-8")
    index_path = tmp_path / "archive-index.json"
    if index_entries is not None:
        index_path.write_text(
            json.dumps({"generated_at": None, "entries": index_entries}),
            encoding="utf-8",
        )
    content_dir = tmp_path / "content" / "archive"
    return daily_path, index_path, content_dir


def test_generate_archive_writes_md_and_updates_index(tmp_path):
    daily, index, content = _setup_dirs(tmp_path, [
        _domain("marketglow.com", verdict="Clean"),
        _domain("tideblock.io", verdict="Promising"),
    ])
    client = _StubClient()

    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )

    assert result["status"] == "ok"
    assert result["new_count"] == 2
    assert (content / "marketglow.com.md").exists()
    assert (content / "tideblock.io.md").exists()
    md = (content / "marketglow.com.md").read_text(encoding="utf-8")
    assert 'name: "marketglow.com"' in md
    assert "## marketglow.com" in md
    idx = json.loads(index.read_text(encoding="utf-8"))
    assert {e["name"] for e in idx["entries"]} == {"marketglow.com", "tideblock.io"}
    assert idx["entries"][0]["archived_date"] == "2026-05-17"
    assert len(client.calls) == 2


def test_generate_archive_grounded_page_quotes_the_real_site(tmp_path, monkeypatch):
    """End-to-end: a sidecar excerpt reaches the prompt, and the page the
    model wrote from it lands in the .md body."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("coppernest.org")])
    monkeypatch.setattr(
        ag, "_load_sidecar_excerpts", lambda *_a, **_k: {"coppernest.org": _excerpt()},
    )

    def body_fn(record, system):
        title = record["wayback_excerpt"]["title"]
        return (
            f"## {record['name']}\n\n### Historical use\n\n"
            f"The final snapshot of {record['name']} carried the title "
            f"“{title}”."
        )

    client = _StubClient(body_fn=body_fn)
    ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )

    assert ag.MODE_MARKER_GROUNDED in client.calls[0]["system"]
    assert "Coppernest Woodworking Journal" in client.calls[0]["user"]
    md = (content / "coppernest.org.md").read_text(encoding="utf-8")
    assert "Coppernest Woodworking Journal" in md
    assert "### Historical use" in md


def test_generate_archive_missing_excerpt_writes_short_factual_page(tmp_path):
    """No excerpt → no-excerpt prompt → the short variant is what gets
    written. The page says the content could not be retrieved rather than
    guessing at it."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("tideblock.io")])

    def body_fn(record, system):
        if ag.MODE_MARKER_NO_EXCERPT in system:
            return (
                f"## {record['name']}\n\n### Historical use\n\nThe archived "
                f"content of {record['name']} could not be retrieved, so "
                f"DomainSifter makes no claim about what the site hosted."
            )
        return f"## {record['name']}\n\nThe name suggests a tide-tracking service."

    client = _StubClient(body_fn=body_fn)
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )

    assert result["new_count"] == 1
    assert result["grounded_count"] == 0
    system = client.calls[0]["system"]
    assert ag.MODE_MARKER_NO_EXCERPT in system
    assert "must not fill the gap with a guess" in system
    md = (content / "tideblock.io.md").read_text(encoding="utf-8")
    assert "could not be retrieved" in md
    assert "name suggests" not in md


def test_generate_archive_non_english_excerpt_reaches_the_prompt(tmp_path, monkeypatch):
    """Many excerpts are Chinese or German. The characters must survive into
    the prompt (the model is told to describe them in English)."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("lanternfold.net")])
    monkeypatch.setattr(
        ag, "_load_sidecar_excerpts",
        lambda *_a, **_k: {
            "lanternfold.net": _excerpt(
                title="工具箱目录",
                meta="Werkzeugverzeichnis für Heimwerker",
                h1=["工具箱"], h2=[],
            ),
        },
    )

    def body_fn(record, system):
        return (
            f"## {record['name']}\n\n### Historical use\n\nThe final snapshot "
            f"of {record['name']} was a Chinese-language tool directory."
        )

    client = _StubClient(body_fn=body_fn)
    ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )

    user = client.calls[0]["user"]
    assert "工具箱目录" in user
    assert "Werkzeugverzeichnis für Heimwerker" in user
    assert "NON-ENGLISH CONTENT" in client.calls[0]["system"]
    md = (content / "lanternfold.net.md").read_text(encoding="utf-8")
    assert "Chinese-language tool directory" in md


def test_generate_archive_persists_evidence_into_the_md(tmp_path, monkeypatch):
    """End-to-end durability check: the reasoning and the archived-content
    evidence land in the permanent Markdown, not just in the 14-day daily
    JSON that this domain will roll off."""
    record = _domain("coppernest.org")
    record["phase2_reason"] = "aged woodworking blog, clean history"
    record["snapshot_category"] = "legitimate"
    daily, index, content = _setup_dirs(tmp_path, [record])
    monkeypatch.setattr(
        ag, "_load_sidecar_excerpts", lambda *_a, **_k: {"coppernest.org": _excerpt()},
    )

    ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=_StubClient(), today=date(2026, 5, 17), git_push=False,
    )

    md = (content / "coppernest.org.md").read_text(encoding="utf-8")
    parsed = _parse_frontmatter(md)
    assert parsed["phase2_reason"] == "aged woodworking blog, clean history"
    assert parsed["snapshot_category"] == "legitimate"
    assert parsed["excerpt_title"] == "Coppernest Woodworking Journal"
    assert parsed["excerpt_h1"] == ["Hand-tool joinery notes"]
    # The quoted text is dated, so the page can attribute it honestly.
    assert parsed["excerpt_snapshot_timestamp"] == "20240315101500"
    # Required contract untouched.
    assert parsed["name"] == "coppernest.org"
    assert parsed["verdict"] == "Clean"


def test_generate_archive_writes_valid_frontmatter_for_hostile_excerpt(
    tmp_path, monkeypatch,
):
    """A spam page's title cannot break the site build."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("coppernest.org")])
    monkeypatch.setattr(
        ag, "_load_sidecar_excerpts",
        lambda *_a, **_k: {"coppernest.org": _HOSTILE_EXCERPT},
    )
    ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=_StubClient(), today=date(2026, 5, 17), git_push=False,
    )
    parsed = _parse_frontmatter(
        (content / "coppernest.org.md").read_text(encoding="utf-8")
    )
    assert parsed["name"] == "coppernest.org"
    assert parsed["archived_date"] == "2026-05-17"


def test_generate_for_domains_persists_evidence_too(tmp_path, monkeypatch):
    """The dry-run path renders the same frontmatter the production path
    does — otherwise a pre-merge review would not show the real page."""
    record = _domain("coppernest.org")
    record["snapshot_category"] = "legitimate"
    daily, _index, _content = _setup_dirs(tmp_path, [record])
    monkeypatch.setattr(
        ag, "_load_sidecar_excerpts", lambda *_a, **_k: {"coppernest.org": _excerpt()},
    )
    out_dir = tmp_path / "review"
    ag.generate_for_domains(
        ["coppernest.org"], out_dir,
        daily_path=daily, client=_StubClient(), today=date(2026, 5, 18),
    )
    parsed = _parse_frontmatter((out_dir / "coppernest.org.md").read_text(encoding="utf-8"))
    assert parsed["excerpt_title"] == "Coppernest Woodworking Journal"
    assert parsed["snapshot_category"] == "legitimate"


def test_generate_archive_skips_already_archived(tmp_path):
    daily, index, content = _setup_dirs(
        tmp_path,
        [_domain("marketglow.com", verdict="Clean"), _domain("tideblock.io", verdict="Clean")],
        index_entries=[{"name": "marketglow.com", "archived_date": "2026-05-10"}],
    )
    client = _StubClient()
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    assert result["new_count"] == 1
    # The backend was called for tideblock only.
    assert [c["record"]["name"] for c in client.calls] == ["tideblock.io"]
    assert (content / "tideblock.io.md").exists()
    assert not (content / "marketglow.com.md").exists()


def test_generate_archive_is_idempotent_across_runs(tmp_path):
    """Second run over the same input regenerates nothing: no backend call,
    no rewrite of the .md, no duplicate index entry."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("marketglow.com")])
    first = _StubClient()
    ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=first, today=date(2026, 5, 17), git_push=False,
    )
    written = (content / "marketglow.com.md").read_text(encoding="utf-8")

    second = _StubClient(body_fn=lambda r, s: "## DIFFERENT\n\nRegenerated body.")
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=second, today=date(2026, 5, 18), git_push=False,
    )

    assert result == {"status": "no_new", "new_count": 0}
    assert second.calls == []
    assert (content / "marketglow.com.md").read_text(encoding="utf-8") == written
    idx = json.loads(index.read_text(encoding="utf-8"))
    assert [e["name"] for e in idx["entries"]] == ["marketglow.com"]


def test_generate_archive_never_overwrites_an_existing_md(tmp_path):
    """Belt-and-braces beyond the index gate: if the .md is on disk but the
    index lost the entry, the page is NOT regenerated (no spend, no churn)
    and the index self-heals."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("coppernest.org")])
    content.mkdir(parents=True, exist_ok=True)
    existing = content / "coppernest.org.md"
    existing.write_text("---\nname: \"coppernest.org\"\n---\n\n## kept\n", encoding="utf-8")

    client = _StubClient()
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )

    assert client.calls == []
    assert existing.read_text(encoding="utf-8") == (
        "---\nname: \"coppernest.org\"\n---\n\n## kept\n"
    )
    idx = json.loads(index.read_text(encoding="utf-8"))
    assert [e["name"] for e in idx["entries"]] == ["coppernest.org"]
    assert result["new_count"] == 1


def test_generate_archive_no_input_returns_status(tmp_path):
    daily, index, content = _setup_dirs(tmp_path, [])
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=_StubClient(), today=date(2026, 5, 17), git_push=False,
    )
    assert result == {"status": "no_input", "new_count": 0}


def test_generate_archive_no_new_returns_status(tmp_path):
    """All inputs already archived → no work, no index rewrite."""
    daily, index, content = _setup_dirs(
        tmp_path, [_domain("marketglow.com", verdict="Clean")],
        index_entries=[{"name": "marketglow.com"}],
    )
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=_StubClient(), today=date(2026, 5, 17), git_push=False,
    )
    assert result == {"status": "no_new", "new_count": 0}


def test_generate_archive_llm_backend_error_skips_one_domain(tmp_path):
    """LLMBackendError on one domain is a soft failure (hard rule 17): skip
    it, keep going, leave it out of the index so tomorrow retries it."""
    daily, index, content = _setup_dirs(tmp_path, [
        _domain("marketglow.com"), _domain("brokenforge.com"), _domain("tideblock.io"),
    ])
    client = _StubClient(raise_on={"brokenforge.com"}, exc=LLMBackendError)
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    assert result["new_count"] == 2
    assert (content / "marketglow.com.md").exists()
    assert (content / "tideblock.io.md").exists()
    assert not (content / "brokenforge.com.md").exists()
    idx = json.loads(index.read_text(encoding="utf-8"))
    assert "brokenforge.com" not in {e["name"] for e in idx["entries"]}


def test_generate_archive_per_domain_failure_continues(tmp_path):
    """Any exception type isolates the same way, not just LLMBackendError."""
    daily, index, content = _setup_dirs(tmp_path, [
        _domain("marketglow.com"), _domain("brokenforge.com"), _domain("tideblock.io"),
    ])
    client = _StubClient(raise_on={"brokenforge.com"})
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    assert result["new_count"] == 2


def test_generate_archive_five_consecutive_failures_raises(tmp_path):
    """Circuit-breaker: 5 consecutive backend failures aborts the run."""
    daily, index, content = _setup_dirs(tmp_path, [
        _domain(f"brk{i}.com") for i in range(10)
    ])
    client = _StubClient(raise_first_n=5, exc=LLMBackendError)
    with pytest.raises(RuntimeError, match="5 consecutive backend failures"):
        ag.generate_archive(
            daily_path=daily, index_path=index, content_dir=content,
            client=client, today=date(2026, 5, 17), git_push=False,
        )
    # No partial index write — fewer than 5 successes ago, no new entries
    # had been added yet by the time we tripped.
    idx = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {"entries": []}
    assert idx.get("entries", []) == []


def test_generate_archive_recovers_after_failure_streak(tmp_path):
    """Streak counter resets on success — 4 failures then success is fine."""
    daily, index, content = _setup_dirs(tmp_path, [
        _domain(f"brk{i}.com") for i in range(8)
    ])
    client = _StubClient(raise_first_n=4, exc=LLMBackendError)
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    # 4 failed + 4 succeeded
    assert result["new_count"] == 4


def test_generate_archive_rejects_malformed_body(tmp_path):
    """Body without an H2 (`##`) is treated as a failure — same as if the
    backend threw. Counts toward the consecutive-failures budget."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("marketglow.com")])
    client = _StubClient(body_fn=lambda r, s: "I cannot help with that request.")
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    assert result["new_count"] == 0
    assert not (content / "marketglow.com.md").exists()


def test_generate_archive_invokes_git_push_when_enabled(tmp_path, monkeypatch):
    """git_push=True → _git_commit_and_push runs and is given the token."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("marketglow.com")])
    calls: list[Any] = []

    def fake_commit_push(new_count, today, token):
        calls.append({"new_count": new_count, "today": today, "token": token})

    monkeypatch.setattr(ag, "_git_commit_and_push", fake_commit_push)
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=_StubClient(), today=date(2026, 5, 17),
        git_push=True, github_token="fake-token",
    )
    assert result["new_count"] == 1
    assert len(calls) == 1
    assert calls[0]["token"] == "fake-token"


def test_generate_archive_skips_git_push_when_no_new_entries(tmp_path, monkeypatch):
    """No new entries → no commit, no push, no token requirement either."""
    daily, index, content = _setup_dirs(
        tmp_path, [_domain("marketglow.com")],
        index_entries=[{"name": "marketglow.com"}],
    )
    fake_push = MagicMock()
    monkeypatch.setattr(ag, "_git_commit_and_push", fake_push)
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=_StubClient(), today=date(2026, 5, 17), git_push=True,
    )
    assert result["status"] == "no_new"
    fake_push.assert_not_called()


def test_generate_archive_reads_sidecar_path_from_config(tmp_path, monkeypatch):
    """Hard rule 9: the sidecar location is config, not a literal."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("coppernest.org")])
    sidecar = tmp_path / "custom-excerpts.json"
    sidecar.write_text(
        json.dumps({"coppernest.org": _excerpt()}, ensure_ascii=False), encoding="utf-8",
    )
    # Undo the autouse stub so the real loader runs against our path.
    monkeypatch.setattr(ag, "_load_sidecar_excerpts", _REAL_SIDECAR_LOADER)

    client = _StubClient()
    ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
        config={"archive_generator": {"sidecar_excerpts_path": str(sidecar)}},
    )
    assert "Coppernest Woodworking Journal" in client.calls[0]["user"]


# ---------------------------------------------------------------------------
# generate_for_domains — dry-run helper (added 2026-05-18)
# ---------------------------------------------------------------------------


def test_generate_for_domains_writes_to_output_dir_only(tmp_path):
    """Dry-run renders to the given output dir and does NOT touch the
    real src/content/archive/ nor the archive-index."""
    daily, _index, _content = _setup_dirs(tmp_path, [
        _domain("marketglow.com", verdict="Clean"),
        _domain("tideblock.io", verdict="Clean"),
        _domain("coppernest.org", verdict="Clean"),
    ])
    out_dir = tmp_path / "review"
    fake_index = tmp_path / "should_not_exist.json"  # never written

    result = ag.generate_for_domains(
        ["marketglow.com", "tideblock.io"], out_dir,
        daily_path=daily, client=_StubClient(), today=date(2026, 5, 18),
    )

    assert sorted(result["rendered"]) == ["marketglow.com", "tideblock.io"]
    assert result["missing"] == []
    assert result["skipped_verdict"] == []
    assert result["failed"] == []
    assert (out_dir / "marketglow.com.md").exists()
    assert (out_dir / "tideblock.io.md").exists()
    # coppernest.org was in daily but NOT requested → not rendered.
    assert not (out_dir / "coppernest.org.md").exists()
    # Index file path was never even touched.
    assert not fake_index.exists()


def test_generate_for_domains_reports_missing_names(tmp_path):
    daily, _index, _content = _setup_dirs(tmp_path, [_domain("marketglow.com")])
    result = ag.generate_for_domains(
        ["marketglow.com", "nonexistent.com"], tmp_path / "out",
        daily_path=daily, client=_StubClient(), today=date(2026, 5, 18),
    )
    assert result["rendered"] == ["marketglow.com"]
    assert result["missing"] == ["nonexistent.com"]


def test_generate_for_domains_skips_caution_verdict(tmp_path):
    """Even in dry-run, only Clean / Promising render — guards against
    accidentally generating a /d/{spam} page for review."""
    daily, _index, _content = _setup_dirs(tmp_path, [
        _domain("marketglow.com", verdict="Clean"),
        _domain("grimvault.com", verdict="Caution"),
    ])
    result = ag.generate_for_domains(
        ["marketglow.com", "grimvault.com"], tmp_path / "out",
        daily_path=daily, client=_StubClient(), today=date(2026, 5, 18),
    )
    assert result["rendered"] == ["marketglow.com"]
    assert result["skipped_verdict"] == ["grimvault.com"]
    assert not (tmp_path / "out" / "grimvault.com.md").exists()


def test_generate_for_domains_isolates_per_domain_failures(tmp_path):
    """One failing backend call doesn't kill the others (no breaker in
    dry-run path)."""
    daily, _index, _content = _setup_dirs(tmp_path, [
        _domain("marketglow.com"), _domain("brokenforge.com"), _domain("tideblock.io"),
    ])
    client = _StubClient(raise_on={"brokenforge.com"}, exc=LLMBackendError)
    result = ag.generate_for_domains(
        ["marketglow.com", "brokenforge.com", "tideblock.io"], tmp_path / "out",
        daily_path=daily, client=client, today=date(2026, 5, 18),
    )
    assert sorted(result["rendered"]) == ["marketglow.com", "tideblock.io"]
    assert result["failed"] == ["brokenforge.com"]


def test_generate_for_domains_returns_empty_when_no_eligible(tmp_path):
    daily, _index, _content = _setup_dirs(tmp_path, [
        _domain("grimvault.com", verdict="Caution"),
    ])
    result = ag.generate_for_domains(
        ["grimvault.com"], tmp_path / "out",
        daily_path=daily, client=_StubClient(), today=date(2026, 5, 18),
    )
    assert result["rendered"] == []
    # Did not call the client at all (no eligible domains).
    assert (tmp_path / "out").exists() is False or list((tmp_path / "out").iterdir()) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_only_without_output_dir_errors(tmp_path):
    rc = ag.main(["--only", "marketglow.com"])
    assert rc == 1


def test_cli_output_dir_without_only_errors(tmp_path):
    rc = ag.main(["--output-dir", str(tmp_path)])
    assert rc == 1


def test_cli_only_routes_to_dry_run(tmp_path, monkeypatch):
    """--only + --output-dir routes to generate_for_domains, never to
    generate_archive."""
    daily = tmp_path / "daily.json"
    daily.write_text(json.dumps({"domains": [_domain("marketglow.com")]}), encoding="utf-8")
    out_dir = tmp_path / "review"

    # Spy on both entry points so we can prove which one fired.
    dry_calls: list[Any] = []
    prod_calls: list[Any] = []

    def fake_dry(names, output_dir, **kw):
        dry_calls.append({"names": names, "output_dir": output_dir, "kw": kw})
        return {"rendered": names, "missing": [], "skipped_verdict": [], "failed": []}

    def fake_prod(**kw):
        prod_calls.append(kw)
        return {"status": "ok", "new_count": 0}

    monkeypatch.setattr(ag, "generate_for_domains", fake_dry)
    monkeypatch.setattr(ag, "generate_archive", fake_prod)

    rc = ag.main([
        "--daily-path", str(daily),
        "--only", "marketglow.com,tideblock.io",
        "--output-dir", str(out_dir),
    ])
    assert rc == 0
    assert len(dry_calls) == 1
    assert dry_calls[0]["names"] == ["marketglow.com", "tideblock.io"]
    assert dry_calls[0]["output_dir"] == out_dir
    assert prod_calls == []  # production path never invoked


def test_cli_config_flag_defaults_to_scripts_config_json(tmp_path, monkeypatch):
    """--config exists, defaults to scripts/config.json, and its contents
    reach generate_archive (the pattern scripts/classify_carryover.py uses)."""
    seen: list[dict] = []

    def fake_prod(**kw):
        seen.append(kw)
        return {"status": "ok", "new_count": 0}

    monkeypatch.setattr(ag, "generate_archive", fake_prod)
    rc = ag.main(["--no-push"])
    assert rc == 0
    assert seen[0]["config"], "default --config must load scripts/config.json"
    assert "llm" in seen[0]["config"]


def test_cli_config_flag_accepts_override(tmp_path, monkeypatch):
    cfg_path = tmp_path / "custom.json"
    cfg_path.write_text(
        json.dumps({"archive_generator": {"timeout_seconds": 99}}), encoding="utf-8",
    )
    seen: list[dict] = []

    def fake_prod(**kw):
        seen.append(kw)
        return {"status": "ok", "new_count": 0}

    monkeypatch.setattr(ag, "generate_archive", fake_prod)
    rc = ag.main(["--no-push", "--config", str(cfg_path)])
    assert rc == 0
    assert seen[0]["config"] == {"archive_generator": {"timeout_seconds": 99}}


def test_cli_missing_config_file_degrades_to_defaults(tmp_path, monkeypatch):
    """A missing config file must not crash the run — DEFAULTS cover every
    key this module reads."""
    seen: list[dict] = []
    monkeypatch.setattr(
        ag, "generate_archive",
        lambda **kw: (seen.append(kw) or {"status": "ok", "new_count": 0}),
    )
    rc = ag.main(["--no-push", "--config", str(tmp_path / "nope.json")])
    assert rc == 0
    assert seen[0]["config"] == {}
