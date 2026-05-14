"""Unit tests for scripts/enrichment/cc_backlinks.py — the CC backlinks
enricher, wired into the pipeline on 2026-05-14.

Mocks the R2 download. Builds a tiny fixture SQLite with the same schema
the real cc_refresh.py produces and runs `enrich()` against it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.enrichment import cc_backlinks


@pytest.fixture(autouse=True)
def _clear_connection_cache():
    """Per-process sqlite3.Connection cache lives at module level; clear it
    between tests so each one starts fresh."""
    for conn in cc_backlinks._CONNECTION_CACHE.values():
        try:
            conn.close()
        except Exception:
            pass
    cc_backlinks._CONNECTION_CACHE.clear()
    yield
    for conn in cc_backlinks._CONNECTION_CACHE.values():
        try:
            conn.close()
        except Exception:
            pass
    cc_backlinks._CONNECTION_CACHE.clear()


def _make_fixture_sqlite(path: Path) -> None:
    """Mirror the schema scripts/cc_refresh.py produces, with a handful of
    fixture rows including a dangler at count=0."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("release", "fixture-release"),
                ("built_at", "2026-05-13T10:00:00Z"),
                ("schema_version", "1"),
            ],
        )
        con.execute(
            "CREATE TABLE cc_apex (apex_domain TEXT PRIMARY KEY, "
            "source_domain_count INTEGER NOT NULL)"
        )
        con.executemany(
            "INSERT INTO cc_apex VALUES (?, ?)",
            [
                ("example1.com", 1),
                ("example2.com", 2),
                ("example3.org", 1),
                ("dangling.com", 0),         # in graph, no inbound
                ("example4.co.uk", 1),
                ("popularsite.com", 42),     # higher count for variance
            ],
        )
        con.execute("CREATE INDEX idx_cc_apex_domain ON cc_apex(apex_domain)")
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Cache directory resolution
# ---------------------------------------------------------------------------


def test_resolve_cache_dir_uses_explicit_argument(tmp_path):
    custom = tmp_path / "custom-cache"
    assert cc_backlinks._resolve_cache_dir(str(custom)) == custom


def test_resolve_cache_dir_uses_env_var(monkeypatch, tmp_path):
    """CC_BACKLINKS_CACHE_DIR env var overrides the XDG default."""
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(tmp_path / "env-cache"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert cc_backlinks._resolve_cache_dir() == tmp_path / "env-cache"


def test_resolve_cache_dir_uses_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.delenv("CC_BACKLINKS_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    result = cc_backlinks._resolve_cache_dir()
    assert result == tmp_path / "xdg" / "domainsifter" / "cc"


def test_resolve_cache_dir_falls_back_to_home(monkeypatch):
    """No explicit, no env, no XDG → ~/.cache/domainsifter/cc."""
    monkeypatch.delenv("CC_BACKLINKS_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    result = cc_backlinks._resolve_cache_dir()
    assert result == Path.home() / ".cache" / "domainsifter" / "cc"


# ---------------------------------------------------------------------------
# Release resolution
# ---------------------------------------------------------------------------


def test_resolve_release_prefers_env_var(monkeypatch):
    monkeypatch.setenv("CC_BACKLINKS_RELEASE", "env-override")
    config = {"cc_backlinks": {"latest_release": "config-default"}}
    assert cc_backlinks._resolve_release(config) == "env-override"


def test_resolve_release_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("CC_BACKLINKS_RELEASE", raising=False)
    config = {"cc_backlinks": {"latest_release": "config-default"}}
    assert cc_backlinks._resolve_release(config) == "config-default"


def test_resolve_release_returns_empty_when_unset(monkeypatch):
    """Neither env nor config — returns "" so enrich() bails out cleanly."""
    monkeypatch.delenv("CC_BACKLINKS_RELEASE", raising=False)
    assert cc_backlinks._resolve_release({}) == ""
    assert cc_backlinks._resolve_release({"cc_backlinks": {}}) == ""


# ---------------------------------------------------------------------------
# SQLite cache + download
# ---------------------------------------------------------------------------


def test_ensure_local_sqlite_returns_existing_cache(tmp_path):
    """If the file already exists in the cache dir, skip the R2 download."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached = cache_dir / "fixture-release.sqlite"
    cached.write_bytes(b"x" * 64)  # plausibly non-empty

    s3 = MagicMock()
    result = cc_backlinks._ensure_local_sqlite(
        "fixture-release", {}, cache_dir=cache_dir, s3_client=s3, bucket="b",
    )
    assert result == cached
    s3.download_file.assert_not_called()


def test_ensure_local_sqlite_downloads_when_missing(tmp_path):
    cache_dir = tmp_path / "cache"
    s3 = MagicMock()

    def fake_download(Bucket, Key, Filename):
        # Simulate the download landing a file at Filename.
        Path(Filename).write_bytes(b"downloaded")

    s3.download_file.side_effect = fake_download

    config = {"cc_backlinks": {"r2_derived_key_template": "cc/derived/{release}.sqlite"}}
    result = cc_backlinks._ensure_local_sqlite(
        "rel-X", config, cache_dir=cache_dir, s3_client=s3, bucket="my-bucket",
    )
    assert result == cache_dir / "rel-X.sqlite"
    s3.download_file.assert_called_once_with(
        Bucket="my-bucket",
        Key="cc/derived/rel-X.sqlite",
        Filename=str(cache_dir / "rel-X.sqlite"),
    )
    assert result.read_bytes() == b"downloaded"


def test_ensure_local_sqlite_treats_empty_file_as_missing(tmp_path):
    """An existing zero-byte file (e.g. interrupted prior download) must
    trigger a fresh download, not be silently treated as valid cache."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "rel-X.sqlite").touch()  # 0 bytes

    s3 = MagicMock()
    def fake_download(Bucket, Key, Filename):
        Path(Filename).write_bytes(b"new-content")
    s3.download_file.side_effect = fake_download

    cc_backlinks._ensure_local_sqlite(
        "rel-X", {}, cache_dir=cache_dir, s3_client=s3, bucket="b",
    )
    s3.download_file.assert_called_once()


# ---------------------------------------------------------------------------
# enrich() — the plugin-contract function
# ---------------------------------------------------------------------------


def test_enrich_returns_count_for_known_apex(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    sqlite_path = cache_dir / "fixture-release.sqlite"
    _make_fixture_sqlite(sqlite_path)

    monkeypatch.setenv("CC_BACKLINKS_RELEASE", "fixture-release")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))

    result = cc_backlinks.enrich("example2.com", {})
    assert result == {"cc_source_domain_count": 2}


def test_enrich_returns_zero_for_dangling_apex_in_graph(tmp_path, monkeypatch):
    """A domain that's in the CC vertex set but has zero inbound edges is
    a DANGLER — distinct from 'not in graph at all'. Counts must propagate
    as 0, not be collapsed into empty-dict-equivalent. This preserves the
    three-state distinction at the enricher boundary."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    _make_fixture_sqlite(cache_dir / "fixture-release.sqlite")

    monkeypatch.setenv("CC_BACKLINKS_RELEASE", "fixture-release")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))

    result = cc_backlinks.enrich("dangling.com", {})
    assert result == {"cc_source_domain_count": 0}


def test_enrich_returns_empty_for_apex_not_in_graph(tmp_path, monkeypatch):
    """Apex not in cc_apex table → empty dict. This is operationally the
    same outcome as a query failure, BUT the upstream three-state
    distinction is preserved by the schema: rows exist for danglers, so
    'no row' means 'not in graph' specifically."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    _make_fixture_sqlite(cache_dir / "fixture-release.sqlite")

    monkeypatch.setenv("CC_BACKLINKS_RELEASE", "fixture-release")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))

    assert cc_backlinks.enrich("never-seen.com", {}) == {}


def test_enrich_lowercases_domain_before_query(tmp_path, monkeypatch):
    """Apex names in the fixture are lowercase (matching CC's format). The
    enricher must lowercase the queried domain so mixed-case inputs (e.g.
    from a UI) still hit."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    _make_fixture_sqlite(cache_dir / "fixture-release.sqlite")

    monkeypatch.setenv("CC_BACKLINKS_RELEASE", "fixture-release")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))

    assert cc_backlinks.enrich("Example2.COM", {}) == {"cc_source_domain_count": 2}


def test_enrich_returns_empty_when_no_release_configured(monkeypatch):
    """No env var, no config → return empty silently. Operator simply
    hasn't opted in; should NOT pollute logs at WARNING."""
    monkeypatch.delenv("CC_BACKLINKS_RELEASE", raising=False)
    assert cc_backlinks.enrich("anything.com", {}) == {}
    assert cc_backlinks.enrich("anything.com", {"cc_backlinks": {}}) == {}


def test_enrich_returns_empty_on_r2_download_failure(tmp_path, monkeypatch):
    """If R2 download fails (network, permissions, missing key), the
    enricher logs a warning and returns empty dict — never crashes the
    pipeline."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("CC_BACKLINKS_RELEASE", "fixture-release")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))

    # Patch the diff module's R2 client constructor so enrich's lazy
    # import path picks up a failing client.
    from scripts import diff as diff_mod

    failing_s3 = MagicMock()
    failing_s3.download_file.side_effect = RuntimeError("R2 unavailable")
    monkeypatch.setattr(diff_mod, "_r2_client", lambda: failing_s3)
    monkeypatch.setattr(diff_mod, "_bucket", lambda: "test-bucket")

    assert cc_backlinks.enrich("example2.com", {}) == {}


def test_enrich_reuses_connection_across_calls(tmp_path, monkeypatch):
    """Performance contract: the SQLite connection is opened once per
    process and reused for every subsequent enrich() call. The pipeline
    invokes enrich() many times per run; re-opening the SQLite each time
    would dominate latency."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    sqlite_path = cache_dir / "fixture-release.sqlite"
    _make_fixture_sqlite(sqlite_path)

    monkeypatch.setenv("CC_BACKLINKS_RELEASE", "fixture-release")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))

    r1 = cc_backlinks.enrich("example1.com", {})
    r2 = cc_backlinks.enrich("example2.com", {})
    r3 = cc_backlinks.enrich("example3.org", {})

    assert r1 == {"cc_source_domain_count": 1}
    assert r2 == {"cc_source_domain_count": 2}
    assert r3 == {"cc_source_domain_count": 1}
    # Connection cached for the release after first call.
    assert "fixture-release" in cc_backlinks._CONNECTION_CACHE


# ---------------------------------------------------------------------------
# Architectural assertion: REGISTERED in ENRICHMENT_MODULES
# ---------------------------------------------------------------------------


def test_cc_backlinks_in_pipeline_enrichment_modules():
    """Hard guarantee (inverted on 2026-05-14, wire-in commit): cc_backlinks
    is now part of the daily enrichment phase. If a future refactor drops
    it from ENRICHMENT_MODULES, this test fails loudly. See STATE.md
    'Common Crawl wire-in — 2026-05-14' for context."""
    from scripts import pipeline
    assert "cc_backlinks" in pipeline.ENRICHMENT_MODULES, (
        "cc_backlinks must be registered in ENRICHMENT_MODULES so the daily "
        "pipeline runs the CC backlink lookup. Wire-in was 2026-05-14; "
        "dropping it back out should be a deliberate, documented choice."
    )
