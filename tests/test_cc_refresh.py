"""Unit tests for scripts/cc_refresh.py — standalone CC refresh tool.

External surfaces mocked:
    - requests.get (data.commoncrawl.org downloads)
    - boto3 S3 client (R2 head/upload/download — same MagicMock pattern as test_diff)
    - boto3 TransferConfig — left real, since it's a passive config object

DuckDB IS exercised against fixture gzipped TSV files (the build step is
core logic worth testing for real). No live network, no real R2.
"""

from __future__ import annotations

import gzip
import io
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts import cc_refresh


# ---------------------------------------------------------------------------
# Fixture data: tiny TSV samples that exercise every code path
# ---------------------------------------------------------------------------


def _write_fixture_zones(tmp_path: Path) -> tuple[Path, Path]:
    """Tiny vertices + edges files with known expected aggregation.

    Vertices (5 nodes):
        0: example1.com
        1: example2.com
        2: example3.org
        3: dangling.com   (will have 0 inbound)
        4: example4.co.uk (multi-label TLD — verifies un-reverse works)

    Edges:
        0 → 1, 0 → 2     example1 links to example2 + example3
        2 → 1            example3 links to example2
        3 → 0            dangling links to example1
        1 → 4            example2 links to example4

    Expected cc_apex (un-reversed apex_domain, COUNT(DISTINCT source)):
        example1.com:   1   (sources: {dangling.com [3]})
        example2.com:   2   (sources: {example1 [0], example3 [2]})
        example3.org:   1   (sources: {example1 [0]})
        dangling.com:   0   (no inbound — included as dangler)
        example4.co.uk: 1   (sources: {example2 [1]})
    """
    vertices = tmp_path / "vertices.txt.gz"
    edges = tmp_path / "edges.txt.gz"
    with gzip.open(vertices, "wt") as fh:
        fh.write("0\tcom.example1\t1\n")
        fh.write("1\tcom.example2\t1\n")
        fh.write("2\torg.example3\t1\n")
        fh.write("3\tcom.dangling\t1\n")
        fh.write("4\tuk.co.example4\t1\n")
    with gzip.open(edges, "wt") as fh:
        fh.write("0\t1\n")
        fh.write("0\t2\n")
        fh.write("2\t1\n")
        fh.write("3\t0\n")
        fh.write("1\t4\n")
    return vertices, edges


# ---------------------------------------------------------------------------
# URL/key helpers (pure functions, easy to assert against)
# ---------------------------------------------------------------------------


def test_source_url_matches_cc_convention():
    url = cc_refresh._source_url("cc-main-2026-feb-mar-apr", "vertices")
    assert url == (
        "https://data.commoncrawl.org/projects/hyperlinkgraph/"
        "cc-main-2026-feb-mar-apr/domain/"
        "cc-main-2026-feb-mar-apr-domain-vertices.txt.gz"
    )


def test_r2_raw_key_template():
    assert cc_refresh._r2_raw_key("rel", "edges") == "cc/raw/rel/edges.txt.gz"
    assert cc_refresh._r2_raw_key("rel", "vertices") == "cc/raw/rel/vertices.txt.gz"


def test_r2_derived_key_template():
    assert cc_refresh._r2_derived_key("rel") == "cc/derived/rel.sqlite"


# ---------------------------------------------------------------------------
# R2 idempotency: HEAD-based existence check
# ---------------------------------------------------------------------------


def test_r2_object_exists_true_when_head_succeeds():
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 12345}
    exists, size = cc_refresh._r2_object_exists(s3, "bucket", "key")
    assert exists is True
    assert size == 12345


def test_r2_object_exists_false_on_nosuchkey():
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "HeadObject",
    )
    exists, size = cc_refresh._r2_object_exists(s3, "bucket", "key")
    assert exists is False
    assert size == 0


def test_r2_object_exists_propagates_unexpected_errors():
    """A real auth failure or network error must NOT be silently treated as
    'object missing' — that would cause us to clobber existing data."""
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "HeadObject",
    )
    with pytest.raises(ClientError):
        cc_refresh._r2_object_exists(s3, "bucket", "key")


# ---------------------------------------------------------------------------
# Download with resume
# ---------------------------------------------------------------------------


def _fake_response(status_code: int, chunks: list[bytes]):
    resp = MagicMock()
    resp.status_code = status_code
    resp.iter_content = lambda chunk_size: iter(chunks)
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda *_a: None
    resp.raise_for_status = MagicMock()
    return resp


def test_download_with_resume_writes_full_content(tmp_path, monkeypatch):
    """First-time download: no existing file, no Range header, full body
    streams to disk."""
    captured_headers: dict = {}

    def fake_get(url, headers=None, stream=False, timeout=None):
        captured_headers.update(headers or {})
        return _fake_response(200, [b"abc", b"defg", b"hi"])

    monkeypatch.setattr(cc_refresh.requests, "get", fake_get)
    local = tmp_path / "out.bin"
    written = cc_refresh._download_with_resume("https://example/file", local)
    assert written == 9
    assert local.read_bytes() == b"abcdefghi"
    assert "Range" not in captured_headers


def test_download_with_resume_uses_range_when_partial_exists(tmp_path, monkeypatch):
    """A pre-existing partial file means we resume — Range: bytes=<size>-,
    response status 206, content appended to the existing bytes."""
    captured_headers: dict = {}

    def fake_get(url, headers=None, stream=False, timeout=None):
        captured_headers.update(headers or {})
        return _fake_response(206, [b"new"])  # appended

    monkeypatch.setattr(cc_refresh.requests, "get", fake_get)
    local = tmp_path / "out.bin"
    local.write_bytes(b"already-here")  # 12 bytes

    written = cc_refresh._download_with_resume("https://example/file", local)
    assert written == 12 + 3
    assert local.read_bytes() == b"already-herenew"
    assert captured_headers.get("Range") == "bytes=12-"


def test_download_with_resume_retries_on_connection_error(tmp_path, monkeypatch):
    """First two attempts raise ConnectionError; third succeeds. The function
    must retry with exponential backoff (sleep mock to keep test fast)."""
    import requests as real_requests

    attempts = {"count": 0}

    def flaky_get(url, headers=None, stream=False, timeout=None):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise real_requests.ConnectionError("synthetic")
        return _fake_response(200, [b"finally"])

    sleep_calls: list[float] = []
    monkeypatch.setattr(cc_refresh.requests, "get", flaky_get)
    local = tmp_path / "out.bin"
    written = cc_refresh._download_with_resume(
        "https://example/file", local, sleep_fn=sleep_calls.append,
    )
    assert written == 7
    assert local.read_bytes() == b"finally"
    assert attempts["count"] == 3
    # Two retries → 1s, 2s backoff
    assert sleep_calls == [1, 2]


def test_download_with_resume_gives_up_after_max_retries(tmp_path, monkeypatch):
    """Persistent network failure exhausts the retry budget; the function
    raises a RuntimeError naming the last underlying exception."""
    import requests as real_requests

    def always_fail(url, headers=None, stream=False, timeout=None):
        raise real_requests.Timeout("synthetic timeout")

    monkeypatch.setattr(cc_refresh.requests, "get", always_fail)
    local = tmp_path / "out.bin"
    with pytest.raises(RuntimeError, match="Download failed after"):
        cc_refresh._download_with_resume(
            "https://example/file", local,
            max_retries=2, sleep_fn=lambda _s: None,
        )


# ---------------------------------------------------------------------------
# Size validation
# ---------------------------------------------------------------------------


def test_validate_size_warns_when_outside_expected_range(caplog):
    """Vertices file 100 bytes is well below the expected 300 MiB floor —
    must produce a WARNING but not raise."""
    import logging

    with caplog.at_level(logging.WARNING, logger="scripts.cc_refresh"):
        cc_refresh._validate_size("vertices", actual=100)
    assert any("outside expected range" in r.message for r in caplog.records)


def test_validate_size_silent_when_within_range(caplog):
    """A 1 GiB vertices file is well inside the expected range — INFO only,
    no warning."""
    import logging

    with caplog.at_level(logging.INFO, logger="scripts.cc_refresh"):
        cc_refresh._validate_size("vertices", actual=1024 * 1024 * 1024)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# DuckDB-driven build (the core data transformation)
# ---------------------------------------------------------------------------


def test_build_derived_sqlite_aggregates_correctly(tmp_path):
    """Full end-to-end build against fixture TSV. Verifies:
        - Aggregation math (COUNT DISTINCT source per target)
        - Un-reverse of CC's reversed-domain format
        - Multi-label TLDs (uk.co.example4 → example4.co.uk)
        - Dangling vertices included with count=0
        - Meta table populated with release / built_at / source URLs
        - Index created on apex_domain for fast lookups
    """
    vertices, edges = _write_fixture_zones(tmp_path)
    sqlite_path = tmp_path / "out.sqlite"

    row_count = cc_refresh._build_derived_sqlite(
        vertices, edges, sqlite_path,
        release="test-release-2026",
        source_urls={"vertices": "https://example/v", "edges": "https://example/e"},
    )

    assert row_count == 5
    assert sqlite_path.exists()

    con = sqlite3.connect(str(sqlite_path))
    try:
        rows = dict(con.execute(
            "SELECT apex_domain, source_domain_count FROM cc_apex"
        ).fetchall())
        assert rows == {
            "example1.com": 1,
            "example2.com": 2,
            "example3.org": 1,
            "dangling.com": 0,         # dangler — included for three-state symmetry
            "example4.co.uk": 1,       # multi-label TLD un-reverses correctly
        }

        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        assert meta["release"] == "test-release-2026"
        assert meta["schema_version"] == "1"
        assert meta["vertices_source_url"] == "https://example/v"
        assert meta["edges_source_url"] == "https://example/e"
        assert "T" in meta["built_at"] and meta["built_at"].endswith("Z")

        # apex_domain index exists
        idx_names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        assert "idx_cc_apex_domain" in idx_names
    finally:
        con.close()


def test_build_derived_sqlite_overwrites_existing_output(tmp_path):
    """If the output path already exists (e.g. stale file from a previous
    aborted build), the build must replace it rather than ATTACH-fail."""
    vertices, edges = _write_fixture_zones(tmp_path)
    sqlite_path = tmp_path / "out.sqlite"
    sqlite_path.write_bytes(b"stale-non-sqlite-bytes")

    cc_refresh._build_derived_sqlite(
        vertices, edges, sqlite_path,
        release="test", source_urls={"vertices": "u1", "edges": "u2"},
    )

    con = sqlite3.connect(str(sqlite_path))
    try:
        # Successfully readable as SQLite — the stale bytes were replaced.
        count = con.execute("SELECT COUNT(*) FROM cc_apex").fetchone()[0]
        assert count == 5
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Phase 1 integration: idempotent download + upload of raw artifacts
# ---------------------------------------------------------------------------


def test_phase_download_skips_when_r2_already_has_raw(tmp_path, monkeypatch):
    """Idempotency: HEAD on R2 raw key returns 200 → skip download AND
    upload AND don't touch the local filesystem. --force is False by default."""
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 12345}  # exists, both keys

    # requests.get must NOT be invoked — that's the whole point.
    def must_not_call(*_a, **_kw):
        raise AssertionError("requests.get called despite R2 having the artifact")
    monkeypatch.setattr(cc_refresh.requests, "get", must_not_call)

    local = cc_refresh._phase_download_and_upload_raw(
        s3=s3, bucket="b", release="rel", workdir=tmp_path, force=False,
    )
    # Nothing landed on disk because both files were skipped.
    assert local == {}
    s3.upload_file.assert_not_called()


def test_phase_download_runs_when_force_true_even_if_r2_has_data(tmp_path, monkeypatch):
    """--force re-does every step regardless of R2 state."""
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 100}  # exists

    def fake_get(url, headers=None, stream=False, timeout=None):
        return _fake_response(200, [b"x" * 16])

    monkeypatch.setattr(cc_refresh.requests, "get", fake_get)
    # Skip size validation warning (16 bytes is way under expected)
    cc_refresh._phase_download_and_upload_raw(
        s3=s3, bucket="b", release="rel", workdir=tmp_path, force=True,
    )
    # Both files downloaded → both uploaded.
    assert s3.upload_file.call_count == 2
    upload_classes = [
        c.kwargs.get("ExtraArgs", {}).get("StorageClass")
        for c in s3.upload_file.call_args_list
    ]
    assert all(sc == "INFREQUENT_ACCESS" for sc in upload_classes)


def test_phase_download_uploads_to_infrequent_access_tier(tmp_path, monkeypatch):
    """Raw uploads go to the IA tier per the storage strategy. Cost design:
    raw is rarely re-read after the derived build, so IA's lower storage
    cost wins over Standard's free-retrieval."""
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "HeadObject",
    )

    def fake_get(url, headers=None, stream=False, timeout=None):
        return _fake_response(200, [b"y" * 32])

    monkeypatch.setattr(cc_refresh.requests, "get", fake_get)
    cc_refresh._phase_download_and_upload_raw(
        s3=s3, bucket="b", release="rel", workdir=tmp_path, force=False,
    )
    for call in s3.upload_file.call_args_list:
        assert call.kwargs["ExtraArgs"]["StorageClass"] == "INFREQUENT_ACCESS"


# ---------------------------------------------------------------------------
# Phase 2 integration: build + upload derived
# ---------------------------------------------------------------------------


def test_phase_build_skips_when_r2_already_has_derived(tmp_path):
    """Derived already on R2 + no --force → skip everything."""
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 999}  # derived exists

    cc_refresh._phase_build_and_upload_derived(
        s3=s3, bucket="b", release="rel", workdir=tmp_path,
        local_raw={}, force=False,
    )
    s3.upload_file.assert_not_called()
    s3.download_file.assert_not_called()


def test_phase_build_uploads_derived_to_standard_tier(tmp_path, monkeypatch):
    """Derived SQLite goes to Standard tier (NOT IA) — it's re-read on every
    daily run when wired in, and IA's retrieval fees would dominate."""
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "HeadObject",
    )
    vertices, edges = _write_fixture_zones(tmp_path)

    cc_refresh._phase_build_and_upload_derived(
        s3=s3, bucket="b", release="rel", workdir=tmp_path,
        local_raw={"vertices": vertices, "edges": edges}, force=False,
    )

    # Exactly one upload — the derived SQLite — with no StorageClass set
    # (defaults to Standard on R2).
    assert s3.upload_file.call_count == 1
    upload_call = s3.upload_file.call_args
    extra = upload_call.kwargs.get("ExtraArgs", {})
    assert "StorageClass" not in extra or extra["StorageClass"] == ""


def test_phase_build_downloads_raw_from_r2_when_local_missing(tmp_path):
    """If raw isn't on the local filesystem (because Phase 1 short-circuited
    due to idempotency), Phase 2 must download raw from R2 before building.
    Otherwise --build-only invocations would fail."""
    s3 = MagicMock()

    # First HEAD (derived) returns missing; subsequent calls return present
    # (we're not testing those branches here).
    s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "HeadObject",
    )

    vertices, edges = _write_fixture_zones(tmp_path)

    # download_file lays the fixture content into the workdir paths.
    def fake_download(Bucket, Key, Filename, Config=None):
        if "vertices" in Key:
            Path(Filename).write_bytes(vertices.read_bytes())
        elif "edges" in Key:
            Path(Filename).write_bytes(edges.read_bytes())
    s3.download_file.side_effect = fake_download

    cc_refresh._phase_build_and_upload_derived(
        s3=s3, bucket="b", release="rel", workdir=tmp_path,
        local_raw={}, force=False,  # nothing local
    )

    # Both raw files pulled from R2.
    pulled_keys = [c.kwargs["Key"] for c in s3.download_file.call_args_list]
    assert any("vertices" in k for k in pulled_keys)
    assert any("edges" in k for k in pulled_keys)
    # And the derived was uploaded.
    assert s3.upload_file.call_count == 1


# ---------------------------------------------------------------------------
# Disk space guard
# ---------------------------------------------------------------------------


def test_ensure_disk_space_raises_when_insufficient(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cc_refresh.shutil, "disk_usage",
        lambda _p: type("DU", (), {"free": 1024 * 1024})(),  # 1 MB free
    )
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        cc_refresh._ensure_disk_space(tmp_path, required_bytes=25 * 1024 * 1024 * 1024)


def test_ensure_disk_space_passes_when_sufficient(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cc_refresh.shutil, "disk_usage",
        lambda _p: type("DU", (), {"free": 50 * 1024 * 1024 * 1024})(),
    )
    # No exception → passes.
    cc_refresh._ensure_disk_space(tmp_path, required_bytes=25 * 1024 * 1024 * 1024)
