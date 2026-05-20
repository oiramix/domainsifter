"""Sidecar-first excerpt resolution tests (Phase 4, 2026-05-20).

Covers archive_generator's new behavior of reading wayback_excerpts.json
in preference to per-domain Wayback fetches. The fetch fallback path is
still exercised — sidecar misses must transparently degrade.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import archive_generator as ag


@pytest.fixture
def stub_no_sleep(monkeypatch):
    monkeypatch.setattr(ag.time, "sleep", lambda *_a, **_k: None)


# ---------------------------------------------------------------------------
# _load_sidecar_excerpts
# ---------------------------------------------------------------------------


class TestLoadSidecar:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        sidecar = tmp_path / "nonexistent.json"
        data = ag._load_sidecar_excerpts(sidecar)
        assert data == {}

    def test_unreadable_file_returns_empty_dict(self, tmp_path):
        sidecar = tmp_path / "garbage.json"
        sidecar.write_text("not json {{{", encoding="utf-8")
        data = ag._load_sidecar_excerpts(sidecar)
        assert data == {}

    def test_non_dict_payload_returns_empty_dict(self, tmp_path):
        sidecar = tmp_path / "list.json"
        sidecar.write_text(json.dumps(["unexpected", "shape"]), encoding="utf-8")
        data = ag._load_sidecar_excerpts(sidecar)
        assert data == {}

    def test_valid_payload_loaded(self, tmp_path):
        sidecar = tmp_path / "ok.json"
        payload = {
            "a.com": {"title": "Alpha"},
            "b.com": None,
        }
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        data = ag._load_sidecar_excerpts(sidecar)
        assert data == payload


# ---------------------------------------------------------------------------
# _resolve_excerpt
# ---------------------------------------------------------------------------


class TestResolveExcerpt:
    def test_sidecar_hit_dict_returns_excerpt(self, monkeypatch):
        excerpt = {"title": "Cached title"}
        sidecar = {"x.com": excerpt}

        def must_not_fetch(*_a, **_k):
            raise AssertionError("must not call fetch_excerpt on sidecar hit")
        monkeypatch.setattr(ag, "fetch_excerpt", must_not_fetch)

        record = {"name": "x.com", "wayback_last_snapshot": "2024-01-01"}
        result, source = ag._resolve_excerpt(record, sidecar)
        assert result == excerpt
        assert source == "sidecar-hit"

    def test_sidecar_null_returns_none_without_fetch(self, monkeypatch):
        """When the classifier saw the domain and got None (parking page
        with no content, empty <body>, etc.), we trust that — no refetch."""
        sidecar = {"x.com": None}

        def must_not_fetch(*_a, **_k):
            raise AssertionError("must not call fetch_excerpt on sidecar-null")
        monkeypatch.setattr(ag, "fetch_excerpt", must_not_fetch)

        record = {"name": "x.com", "wayback_last_snapshot": "2024-01-01"}
        result, source = ag._resolve_excerpt(record, sidecar)
        assert result is None
        assert source == "sidecar-null"

    def test_sidecar_miss_with_snapshot_date_fetches(self, monkeypatch):
        """No sidecar entry → fall back to fetch_excerpt. Returns the
        fetch result with source='fetch' so caller knows to sleep."""
        fetched_excerpt = {"title": "Fresh from Wayback"}
        called_with = []

        def fake_fetch(name, target_date):
            called_with.append((name, target_date))
            return fetched_excerpt
        monkeypatch.setattr(ag, "fetch_excerpt", fake_fetch)

        record = {"name": "miss.com", "wayback_last_snapshot": "2024-03-15"}
        result, source = ag._resolve_excerpt(record, {})

        assert result == fetched_excerpt
        assert source == "fetch"
        assert called_with == [("miss.com", "2024-03-15")]

    def test_sidecar_miss_without_snapshot_date_skips_fetch(self, monkeypatch):
        """Record has no wayback_last_snapshot and no sidecar entry — no
        archive.org call possible. Source='no-snapshot' so caller skips
        the courtesy sleep."""
        def must_not_fetch(*_a, **_k):
            raise AssertionError("must not call fetch_excerpt with no snapshot date")
        monkeypatch.setattr(ag, "fetch_excerpt", must_not_fetch)

        record = {"name": "miss.com"}
        result, source = ag._resolve_excerpt(record, {})
        assert result is None
        assert source == "no-snapshot"

    def test_fetch_exception_returns_none_with_fetch_error(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("simulated archive.org failure")
        monkeypatch.setattr(ag, "fetch_excerpt", boom)

        record = {"name": "miss.com", "wayback_last_snapshot": "2024-03-15"}
        result, source = ag._resolve_excerpt(record, {})
        assert result is None
        assert source == "fetch-error"

    def test_sidecar_corrupt_entry_falls_back_to_fetch(self, monkeypatch, caplog):
        """Sidecar entry is neither dict nor None (e.g. a string from
        corruption) — fall through to fetch_excerpt rather than passing
        garbage to Haiku."""
        import logging
        sidecar = {"x.com": "this is not a dict"}

        fetched_excerpt = {"title": "Fetched fallback"}
        monkeypatch.setattr(ag, "fetch_excerpt", lambda *_a, **_k: fetched_excerpt)

        record = {"name": "x.com", "wayback_last_snapshot": "2024-01-01"}
        with caplog.at_level(logging.WARNING, logger="scripts.archive_generator"):
            result, source = ag._resolve_excerpt(record, sidecar)
        assert result == fetched_excerpt
        assert source == "fetch"
        assert any("not dict|None" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Integration: generate_archive reads sidecar first
# ---------------------------------------------------------------------------


class _StubClient:
    """Returns scripted bodies, records the records it was called with so
    the test can inspect which excerpts ended up in the Haiku prompt."""

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, system, user):
        self.calls.append({"system": system, "user": user})
        return "## stub.net\n\nStub body for testing only."


def test_generate_archive_uses_sidecar_excerpt_in_haiku_prompt(
    tmp_path, monkeypatch, stub_no_sleep,
):
    """Sidecar entry is threaded into the Haiku user message via
    `enriched_record["wayback_excerpt"]`. Proves the sidecar-first path
    is wired."""
    # Set up daily-domains.json with one qualifying domain.
    daily_path = tmp_path / "daily-domains.json"
    daily_path.write_text(json.dumps({
        "domains": [{
            "name": "sidecartest.com",
            "tld": "com",
            "verdict": "Clean",
            "score": 80,
            "dropped_date": "2026-05-19",
            "wayback_snapshots": 3000,
            "wayback_last_snapshot": "2024-01-01",
            "open_page_rank": 2.5,
            "cc_source_domain_count": 100,
            "cert_history": True,
            "first_seen_date": "2026-05-19",
            "availability_verified_at": "2026-05-19T07:00:00Z",
        }],
    }), encoding="utf-8")

    index_path = tmp_path / "archive-index.json"
    content_dir = tmp_path / "archive"

    # Stub the sidecar loader to return a populated dict for our domain.
    sidecar_excerpt = {
        "title": "Stub site title from sidecar",
        "meta_description": "Stub meta description",
        "h1": ["Heading from sidecar"],
        "h2": [],
    }
    monkeypatch.setattr(
        ag, "_load_sidecar_excerpts",
        lambda *_a, **_k: {"sidecartest.com": sidecar_excerpt},
    )
    # Stub fetch_excerpt so a fallback attempt would be visible.
    fetch_calls = []
    monkeypatch.setattr(
        ag, "fetch_excerpt",
        lambda *a, **k: (fetch_calls.append(a) or {"title": "WRONG"})
    )

    client = _StubClient()
    result = ag.generate_archive(
        daily_path=daily_path,
        index_path=index_path,
        content_dir=content_dir,
        client=client,
        today=__import__("datetime").date(2026, 5, 20),
        git_push=False,
    )

    assert result["status"] == "ok"
    assert result["new_count"] == 1
    # Sidecar was used; fetch_excerpt was NOT called.
    assert fetch_calls == []
    # The user message Haiku saw contains the sidecar excerpt.
    assert len(client.calls) == 1
    user_msg = client.calls[0]["user"]
    assert "Stub site title from sidecar" in user_msg
    assert "WRONG" not in user_msg


def test_generate_archive_falls_back_to_fetch_when_sidecar_misses(
    tmp_path, monkeypatch, stub_no_sleep,
):
    """Domain not in sidecar → archive_generator falls back to
    fetch_excerpt. This is the backfill-carryover path: entries that
    predate Phase 4's pipeline wiring won't be in the sidecar."""
    daily_path = tmp_path / "daily-domains.json"
    daily_path.write_text(json.dumps({
        "domains": [{
            "name": "missingfromsidecar.com",
            "tld": "com",
            "verdict": "Promising",
            "score": 65,
            "dropped_date": "2026-05-19",
            "wayback_snapshots": 2000,
            "wayback_last_snapshot": "2024-06-01",
            "open_page_rank": 2.0,
            "cc_source_domain_count": 50,
            "cert_history": True,
            "first_seen_date": "2026-05-19",
            "availability_verified_at": "2026-05-19T07:00:00Z",
        }],
    }), encoding="utf-8")

    index_path = tmp_path / "archive-index.json"
    content_dir = tmp_path / "archive"

    # Empty sidecar — must trigger fetch_excerpt fallback.
    monkeypatch.setattr(ag, "_load_sidecar_excerpts", lambda *_a, **_k: {})

    fetched_excerpt = {
        "title": "From the live fetch",
        "meta_description": None,
        "h1": [],
        "h2": [],
    }
    fetch_calls = []
    def fake_fetch(name, target_date):
        fetch_calls.append((name, target_date))
        return fetched_excerpt
    monkeypatch.setattr(ag, "fetch_excerpt", fake_fetch)

    client = _StubClient()
    result = ag.generate_archive(
        daily_path=daily_path,
        index_path=index_path,
        content_dir=content_dir,
        client=client,
        today=__import__("datetime").date(2026, 5, 20),
        git_push=False,
    )

    assert result["status"] == "ok"
    assert fetch_calls == [("missingfromsidecar.com", "2024-06-01")]
    # The fetched excerpt was passed to Haiku.
    assert "From the live fetch" in client.calls[0]["user"]


def test_generate_archive_sidecar_null_does_not_refetch(
    tmp_path, monkeypatch, stub_no_sleep,
):
    """Sidecar entry is explicit None (classifier saw the domain and got
    no excerpt) — don't refetch. Trust the classifier's view."""
    daily_path = tmp_path / "daily-domains.json"
    daily_path.write_text(json.dumps({
        "domains": [{
            "name": "nullinsidecar.com",
            "tld": "com",
            "verdict": "Clean",
            "score": 75,
            "dropped_date": "2026-05-19",
            "wayback_snapshots": 1000,
            "wayback_last_snapshot": "2024-01-01",
            "open_page_rank": 2.0,
            "cc_source_domain_count": 50,
            "cert_history": True,
            "first_seen_date": "2026-05-19",
            "availability_verified_at": "2026-05-19T07:00:00Z",
        }],
    }), encoding="utf-8")

    index_path = tmp_path / "archive-index.json"
    content_dir = tmp_path / "archive"

    monkeypatch.setattr(
        ag, "_load_sidecar_excerpts",
        lambda *_a, **_k: {"nullinsidecar.com": None},
    )

    def must_not_fetch(*_a, **_k):
        raise AssertionError("must not refetch when sidecar has explicit None")
    monkeypatch.setattr(ag, "fetch_excerpt", must_not_fetch)

    client = _StubClient()
    result = ag.generate_archive(
        daily_path=daily_path,
        index_path=index_path,
        content_dir=content_dir,
        client=client,
        today=__import__("datetime").date(2026, 5, 20),
        git_push=False,
    )
    assert result["status"] == "ok"
    # Haiku still called — null excerpt is the "no grounding" path; Haiku
    # OMITS the Historical use section per its prompt.
    assert len(client.calls) == 1


def test_generate_for_domains_uses_sidecar(
    tmp_path, monkeypatch, stub_no_sleep,
):
    """Dry-run helper uses sidecar too — same path, same fallback."""
    daily_path = tmp_path / "daily-domains.json"
    daily_path.write_text(json.dumps({
        "domains": [{
            "name": "dryrunsidecar.com",
            "tld": "com",
            "verdict": "Clean",
            "score": 80,
            "dropped_date": "2026-05-19",
            "wayback_snapshots": 3000,
            "wayback_last_snapshot": "2024-01-01",
            "open_page_rank": 2.5,
            "cc_source_domain_count": 100,
            "cert_history": True,
            "first_seen_date": "2026-05-19",
            "availability_verified_at": "2026-05-19T07:00:00Z",
        }],
    }), encoding="utf-8")

    output_dir = tmp_path / "dry-out"

    sidecar_excerpt = {
        "title": "Dry-run sidecar title",
        "meta_description": None,
        "h1": [],
        "h2": [],
    }
    monkeypatch.setattr(
        ag, "_load_sidecar_excerpts",
        lambda *_a, **_k: {"dryrunsidecar.com": sidecar_excerpt},
    )

    def must_not_fetch(*_a, **_k):
        raise AssertionError("dry-run also reads sidecar; must not call fetch")
    monkeypatch.setattr(ag, "fetch_excerpt", must_not_fetch)

    client = _StubClient()
    result = ag.generate_for_domains(
        ["dryrunsidecar.com"],
        output_dir,
        daily_path=daily_path,
        client=client,
        today=__import__("datetime").date(2026, 5, 20),
    )
    assert result["rendered"] == ["dryrunsidecar.com"]
    assert "Dry-run sidecar title" in client.calls[0]["user"]
