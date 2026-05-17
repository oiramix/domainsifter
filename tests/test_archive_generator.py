"""Unit + mocked-orchestration tests for scripts/archive_generator.py.

Touches:
  - _filter_qualifying       — pure decision rule (verdict gate + index lookup)
  - _slug_for                — pure filename sanity
  - _build_haiku_user_message
  - _build_frontmatter / _build_markdown_file
  - generate_archive end-to-end with mocked HaikuClient + mocked subprocess
    (so no real Anthropic SDK call, no real git push)

Does NOT touch:
  - The real anthropic SDK (lazy-imported inside HaikuClient — tests use a
    custom in-memory client)
  - Real git or network — git_push=False or subprocess patched
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts import archive_generator as ag


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


class _StubClient:
    """In-memory replacement for HaikuClient.

    `body_fn(record) -> str` produces the markdown body. Tests can pass
    either a fixed string or a function that varies by record. Tracking
    counters expose call counts to assertions.
    """

    def __init__(self, body_fn=None, raise_on=None, raise_first_n=0):
        self._body_fn = body_fn or (lambda r: f"## {r['name']}\n\nLead paragraph for {r['name']}.")
        self._raise_on = raise_on or set()        # names that raise per-call
        self._raise_first_n = raise_first_n       # raise on the first N calls
        self.calls: list[dict] = []

    def generate(self, system: str, user: str) -> str:
        # Parse the record back out of the user message (matches the
        # _build_haiku_user_message contract).
        json_part = user.split("\n\n", 1)[1]
        record = json.loads(json_part)
        self.calls.append({"system": system, "user": user, "record": record})
        if record["name"] in self._raise_on:
            raise RuntimeError(f"simulated API failure for {record['name']}")
        if len(self.calls) <= self._raise_first_n:
            raise RuntimeError(f"simulated initial failure (call {len(self.calls)})")
        return self._body_fn(record)


# ---------------------------------------------------------------------------
# _filter_qualifying
# ---------------------------------------------------------------------------


def test_filter_qualifying_keeps_clean_and_promising():
    cands = [_domain("a.com", verdict="Clean"), _domain("b.org", verdict="Promising")]
    out = ag._filter_qualifying(cands, already_archived=set())
    assert [c["name"] for c in out] == ["a.com", "b.org"]


def test_filter_qualifying_rejects_caution():
    cands = [_domain("scam.com", verdict="Caution")]
    assert ag._filter_qualifying(cands, already_archived=set()) == []


def test_filter_qualifying_rejects_missing_verdict():
    """Legacy payloads or partial data without a verdict field don't
    qualify — the archive only houses entries we've labeled."""
    cand = _domain("legacy.com")
    del cand["verdict"]
    assert ag._filter_qualifying([cand], already_archived=set()) == []


def test_filter_qualifying_rejects_already_archived():
    cands = [_domain("dup.com", verdict="Clean")]
    out = ag._filter_qualifying(cands, already_archived={"dup.com"})
    assert out == []


def test_filter_qualifying_rejects_empty_name():
    cand = _domain("anything.com")
    cand["name"] = ""
    assert ag._filter_qualifying([cand], already_archived=set()) == []


def test_filter_qualifying_passes_mixed_set():
    cands = [
        _domain("keep1.com", verdict="Clean"),
        _domain("keep2.org", verdict="Promising"),
        _domain("drop1.com", verdict="Caution"),
        _domain("drop2.net", verdict="Clean"),  # but already archived
    ]
    out = ag._filter_qualifying(cands, already_archived={"drop2.net"})
    assert {c["name"] for c in out} == {"keep1.com", "keep2.org"}


# ---------------------------------------------------------------------------
# _slug_for
# ---------------------------------------------------------------------------


def test_slug_for_preserves_dots():
    """Dots stay in the filename so Astro's [domain] param resolves to
    the matching content collection entry by name."""
    assert ag._slug_for("deepsand.net") == "deepsand.net"


def test_slug_for_lowercases():
    assert ag._slug_for("UPPER.COM") == "upper.com"


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
# _build_haiku_user_message
# ---------------------------------------------------------------------------


def test_build_haiku_user_message_includes_record_as_json():
    rec = _domain("ex.com")
    msg = ag._build_haiku_user_message(rec)
    assert msg.startswith("Generate the archive page for this domain:")
    # Round-trip the JSON to confirm the record is encoded faithfully.
    json_part = msg.split("\n\n", 1)[1]
    parsed = json.loads(json_part)
    assert parsed["name"] == "ex.com"
    assert parsed["verdict"] == "Clean"


def test_build_haiku_user_message_sorts_keys_for_determinism():
    rec = _domain("ex.com")
    msg_a = ag._build_haiku_user_message(rec)
    rec_reordered = {k: rec[k] for k in sorted(rec.keys(), reverse=True)}
    msg_b = ag._build_haiku_user_message(rec_reordered)
    assert msg_a == msg_b


# ---------------------------------------------------------------------------
# _build_frontmatter / _build_markdown_file
# ---------------------------------------------------------------------------


def test_build_frontmatter_emits_all_schema_fields():
    rec = _domain("ex.com", verdict="Promising", score=55, wayback=2000)
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17")
    assert fm.startswith("---\n")
    assert fm.rstrip().endswith("---")
    # All keys in the Astro schema appear.
    for key in (
        "name", "tld", "verdict", "score", "dropped_date", "archived_date",
        "wayback_snapshots", "wayback_last_snapshot", "open_page_rank",
        "cc_source_domain_count", "cert_history", "first_seen_date",
        "availability_verified_at",
    ):
        assert f"{key}:" in fm


def test_build_frontmatter_serializes_null_as_yaml_null():
    rec = _domain("ex.com", wayback=None, opr=None, cc=None)
    rec["wayback_last_snapshot"] = None
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17")
    assert "wayback_snapshots: null" in fm
    assert "open_page_rank: null" in fm
    assert "cc_source_domain_count: null" in fm


def test_build_frontmatter_quotes_strings_and_escapes_quotes():
    rec = _domain("ex.com")
    rec["first_seen_date"] = '2026-05-17"quoted"'  # contrived
    fm = ag._build_frontmatter(rec, archived_date="2026-05-17")
    assert '\\"quoted\\"' in fm


def test_build_markdown_file_has_frontmatter_then_body():
    rec = _domain("ex.com")
    out = ag._build_markdown_file(rec, body="## ex.com\n\nBody text.", archived_date="2026-05-17")
    assert out.startswith("---\n")
    parts = out.split("---\n", 2)
    assert "## ex.com" in parts[2]
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
        _domain("alpha.com", verdict="Clean"),
        _domain("beta.org", verdict="Promising"),
    ])
    client = _StubClient()

    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )

    assert result["status"] == "ok"
    assert result["new_count"] == 2
    assert (content / "alpha.com.md").exists()
    assert (content / "beta.org.md").exists()
    md = (content / "alpha.com.md").read_text(encoding="utf-8")
    assert 'name: "alpha.com"' in md
    assert "## alpha.com" in md
    idx = json.loads(index.read_text(encoding="utf-8"))
    assert {e["name"] for e in idx["entries"]} == {"alpha.com", "beta.org"}
    assert idx["entries"][0]["archived_date"] == "2026-05-17"
    assert len(client.calls) == 2


def test_generate_archive_skips_already_archived(tmp_path):
    daily, index, content = _setup_dirs(
        tmp_path,
        [_domain("alpha.com", verdict="Clean"), _domain("beta.org", verdict="Clean")],
        index_entries=[{"name": "alpha.com", "archived_date": "2026-05-10"}],
    )
    client = _StubClient()
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    assert result["new_count"] == 1
    # Haiku was called for beta only.
    assert [c["record"]["name"] for c in client.calls] == ["beta.org"]
    assert (content / "beta.org.md").exists()
    assert not (content / "alpha.com.md").exists()


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
        tmp_path, [_domain("alpha.com", verdict="Clean")],
        index_entries=[{"name": "alpha.com"}],
    )
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=_StubClient(), today=date(2026, 5, 17), git_push=False,
    )
    assert result == {"status": "no_new", "new_count": 0}


def test_generate_archive_per_domain_failure_continues(tmp_path):
    """One failing domain doesn't block the others."""
    daily, index, content = _setup_dirs(tmp_path, [
        _domain("alpha.com"), _domain("broken.com"), _domain("gamma.org"),
    ])
    client = _StubClient(raise_on={"broken.com"})
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    assert result["new_count"] == 2
    assert (content / "alpha.com.md").exists()
    assert (content / "gamma.org.md").exists()
    assert not (content / "broken.com.md").exists()


def test_generate_archive_five_consecutive_failures_raises(tmp_path):
    """Circuit-breaker: 5 consecutive Haiku failures aborts the run."""
    daily, index, content = _setup_dirs(tmp_path, [
        _domain(f"d{i}.com") for i in range(10)
    ])
    client = _StubClient(raise_first_n=5)
    with pytest.raises(RuntimeError, match="consecutive Haiku failures"):
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
        _domain(f"d{i}.com") for i in range(8)
    ])
    client = _StubClient(raise_first_n=4)
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    # 4 failed + 4 succeeded
    assert result["new_count"] == 4


def test_generate_archive_rejects_malformed_haiku_body(tmp_path):
    """Body without an H2 (`##`) is treated as a failure — same as if the
    API threw. Counts toward the consecutive-failures budget."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("a.com")])
    client = _StubClient(body_fn=lambda r: "I refuse to comply.")
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=client, today=date(2026, 5, 17), git_push=False,
    )
    assert result["new_count"] == 0
    assert not (content / "a.com.md").exists()


def test_generate_archive_invokes_git_push_when_enabled(tmp_path, monkeypatch):
    """git_push=True → _git_commit_and_push runs and is given the token."""
    daily, index, content = _setup_dirs(tmp_path, [_domain("a.com")])
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
        tmp_path, [_domain("a.com")],
        index_entries=[{"name": "a.com"}],
    )
    fake_push = MagicMock()
    monkeypatch.setattr(ag, "_git_commit_and_push", fake_push)
    result = ag.generate_archive(
        daily_path=daily, index_path=index, content_dir=content,
        client=_StubClient(), today=date(2026, 5, 17), git_push=True,
    )
    assert result["status"] == "no_new"
    fake_push.assert_not_called()
