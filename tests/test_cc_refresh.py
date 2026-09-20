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
    assert all(sc == "STANDARD_IA" for sc in upload_classes)


def test_phase_download_uploads_to_infrequent_access_tier(tmp_path, monkeypatch):
    """Raw uploads go to the IA tier per the storage strategy. Cost design:
    raw is rarely re-read after the derived build, so IA's lower storage
    cost wins over Standard's free-retrieval. R2's S3-compatible API uses
    the AWS-style class name `STANDARD_IA` (NOT Cloudflare's Workers-API
    `InfrequentAccess` spelling — that 400'd on the first OVH run
    2026-05-13)."""
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
        assert call.kwargs["ExtraArgs"]["StorageClass"] == "STANDARD_IA"


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


# ===========================================================================
# Automated refresh (--auto) — added 2026-09-20 alongside the weekly timer.
#
# Every fixture domain here is INVENTED (hard rule 1). The production config
# uses google.com as its `canary_present` probe because it is the most-linked
# apex in the real graph; these tests parameterise that config with invented
# names so no real domain ever lands in a fixture.
# ===========================================================================


# All 2026 releases confirmed to exist upstream on 2026-09-20, plus the two
# year-crossing names and the one that 404s today (jul-aug-sep).
_KNOWN_RELEASES: list[tuple[str, tuple[int, int]]] = [
    ("cc-main-2025-26-nov-dec-jan", (2026, 1)),
    ("cc-main-2025-26-dec-jan-feb", (2026, 2)),
    ("cc-main-2026-jan-feb-mar", (2026, 3)),
    ("cc-main-2026-feb-mar-apr", (2026, 4)),
    ("cc-main-2026-mar-apr-may", (2026, 5)),
    ("cc-main-2026-apr-may-jun", (2026, 6)),
    ("cc-main-2026-may-jun-jul", (2026, 7)),
    ("cc-main-2026-jun-jul-aug", (2026, 8)),
    ("cc-main-2026-jul-aug-sep", (2026, 9)),
]

_GIB = 1024 * 1024 * 1024


def _refresh_config(**overrides) -> dict:
    """A config shaped like the real cc_backlinks block, with invented
    canaries. Keyword overrides are merged into the `refresh` sub-block."""
    refresh = {
        "enabled": True,
        "discover_max_windows_back": 6,
        "head_timeout_seconds": 20,
        "blackout_start_utc_hour": 7,
        "blackout_end_utc_hour": 16,
        "pipeline_unit": "domainsifter.service",
        "verification": {
            "min_cc_apex_rows": 3,
            "canary_present": {"marketglow.com": 5},
            "canary_absent": ["neverseen.example", "tideblock.io"],
        },
        "prune_raw_after_releases": 2,
        "prune_local_cache": True,
        "staleness_warn_days": 45,
        "result_path": "scripts/state/cc_refresh_result.json",
    }
    refresh.update(overrides)
    return {
        "cc_backlinks": {
            "latest_release": "cc-main-2026-feb-mar-apr",
            "r2_derived_key_template": "cc/derived/{release}.sqlite",
            "refresh": refresh,
        }
    }


def _write_fixture_sqlite(
    path: Path, release: str, rows: dict[str, int] | None = None,
) -> Path:
    """A real on-disk SQLite with the schema cc_refresh builds, small enough
    to create in-test. Invented domains only."""
    rows = rows if rows is not None else {
        "marketglow.com": 9,
        "coppernest.org": 3,
        "amberflask.dev": 0,
        "quietlathe.studio": 1,
    }
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("release", release),
                ("built_at", "2026-09-20T12:00:00Z"),
                ("schema_version", "1"),
            ],
        )
        con.execute(
            "CREATE TABLE cc_apex "
            "(apex_domain TEXT PRIMARY KEY, source_domain_count INTEGER)"
        )
        con.executemany("INSERT INTO cc_apex VALUES (?, ?)", sorted(rows.items()))
        con.commit()
    finally:
        con.close()
    return path


class _FakeR2:
    """Minimal stand-in for the boto3 S3 client surface this module uses."""

    def __init__(self, keys: list[str] | None = None, payload: Path | None = None):
        self.keys = list(keys or [])
        self.payload = payload
        self.deleted: list[str] = []
        self.downloaded: list[str] = []
        self.list_calls: list[dict] = []

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        self.list_calls.append({"Prefix": Prefix, "Token": ContinuationToken})
        return {
            "Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)],
            "IsTruncated": False,
        }

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        if Key in self.keys:
            self.keys.remove(Key)

    def download_file(self, Bucket, Key, Filename, Config=None):
        self.downloaded.append(Key)
        if self.payload is None:
            raise AssertionError(f"unexpected download of {Key}")
        Path(Filename).write_bytes(self.payload.read_bytes())


# ---------------------------------------------------------------------------
# Release-name algebra — both directions, every real name, and junk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("release,key", _KNOWN_RELEASES)
def test_release_name_round_trips_for_every_known_release(release, key):
    """build → parse → build must be the identity for every name CC has
    actually published (plus jul-aug-sep, a valid name that merely 404s)."""
    assert cc_refresh._release_name(*key) == release
    assert cc_refresh._parse_release(release) == key
    assert cc_refresh._release_name(*cc_refresh._parse_release(release)) == release


def test_release_name_uses_two_year_prefix_only_when_window_crosses_years():
    # Window entirely within one calendar year → plain 4-digit year.
    assert cc_refresh._release_name(2026, 3) == "cc-main-2026-jan-feb-mar"
    assert cc_refresh._release_name(2026, 12) == "cc-main-2026-oct-nov-dec"
    # Window starting in the previous year → "<start>-<end mod 100>".
    assert cc_refresh._release_name(2026, 2) == "cc-main-2025-26-dec-jan-feb"
    assert cc_refresh._release_name(2026, 1) == "cc-main-2025-26-nov-dec-jan"
    # Century boundary still yields a zero-padded 2-digit suffix.
    assert cc_refresh._release_name(2100, 1) == "cc-main-2099-00-nov-dec-jan"


def test_release_name_rejects_impossible_month():
    with pytest.raises(ValueError):
        cc_refresh._release_name(2026, 13)


@pytest.mark.parametrize("junk", [
    "",
    "junk",
    "cc-main-2026",
    "cc-main-2026-jan-feb",                   # only two months
    "cc-main-2026-jan-feb-mar-apr",           # four months
    "cc-main-2026-jan-mar-feb",               # out of order
    "cc-main-2026-feb-mar-apr-domain-edges",  # a filename, not a release
    "cc-main-2025-27-dec-jan-feb",            # inconsistent two-year prefix
    "cc-main-2026-jan-feb-xyz",               # not a month
    "cc-main-26-jan-feb-mar",                 # 2-digit year
    "CC-MAIN-2026-JAN-FEB-MAR",               # wrong case
    "cc-main-2026-dec-jan-feb",               # crossing window, no 2-year prefix
])
def test_parse_release_returns_none_for_junk(junk):
    """Never guess. An unrecognised name means 'leave this artifact alone' —
    which is what stops the pruner deleting something it cannot date."""
    assert cc_refresh._parse_release(junk) is None


def test_parse_release_tolerates_non_string():
    assert cc_refresh._parse_release(None) is None
    assert cc_refresh._parse_release(12345) is None


def test_release_sort_key_orders_chronologically_across_year_boundary():
    names = [
        "cc-main-2026-feb-mar-apr",
        "cc-main-2025-26-nov-dec-jan",
        "cc-main-2026-jun-jul-aug",
        "cc-main-2025-26-dec-jan-feb",
        "cc-main-2026-jan-feb-mar",
    ]
    assert sorted(names, key=cc_refresh._release_sort_key) == [
        "cc-main-2025-26-nov-dec-jan",
        "cc-main-2025-26-dec-jan-feb",
        "cc-main-2026-jan-feb-mar",
        "cc-main-2026-feb-mar-apr",
        "cc-main-2026-jun-jul-aug",
    ]


def test_release_sort_key_puts_unparseable_names_first():
    """An unparseable name must never win a 'newest N' selection."""
    assert cc_refresh._release_sort_key("garbage") == (-1, -1)
    assert cc_refresh._release_sort_key("garbage") < cc_refresh._release_sort_key(
        "cc-main-2025-26-nov-dec-jan"
    )


# ---------------------------------------------------------------------------
# Discovery — walk back until both raw files answer 200
# ---------------------------------------------------------------------------


def _head_stub(
    available: set[str],
    *,
    errors: dict[str, Exception] | None = None,
    sizes: dict[str, int] | None = None,
):
    """Fake requests.head: 200 for URLs of releases in `available`, else 404.
    `errors` maps a release name to an exception to raise; `sizes` maps a file
    kind to the Content-Length to report."""
    calls: list[str] = []
    errors = errors or {}
    sizes = sizes or {}

    def fake_head(url, timeout=None, allow_redirects=None):
        calls.append(url)
        for release, exc in errors.items():
            if f"/{release}/" in url:
                raise exc
        status = 200 if any(f"/{r}/" in url for r in available) else 404
        resp = MagicMock()
        resp.status_code = status
        headers = {}
        for kind, size in sizes.items():
            if f"-domain-{kind}." in url:
                headers["Content-Length"] = str(size)
        resp.headers = headers
        return resp

    fake_head.calls = calls  # type: ignore[attr-defined]
    return fake_head


def test_discover_walks_back_to_the_newest_published_release(monkeypatch):
    """On 2026-09-20 the window ending in September 404s and the newest real
    release is jun-jul-aug — exactly the upstream state measured that day."""
    import datetime

    fake_head = _head_stub({"cc-main-2026-jun-jul-aug"})
    monkeypatch.setattr(cc_refresh.requests, "head", fake_head)

    got = cc_refresh.discover_latest_release(
        _refresh_config(), today=datetime.date(2026, 9, 20),
    )
    assert got == "cc-main-2026-jun-jul-aug"
    # Probed sep (404) before aug (200); nothing older was touched.
    assert any("cc-main-2026-jul-aug-sep" in u for u in fake_head.calls)
    assert not any("cc-main-2026-apr-may-jun" in u for u in fake_head.calls)


def test_discover_crosses_the_year_boundary_when_walking_back(monkeypatch):
    """A January run must reach back into the previous year's two-year-prefix
    names."""
    import datetime

    monkeypatch.setattr(
        cc_refresh.requests, "head", _head_stub({"cc-main-2025-26-nov-dec-jan"}),
    )
    got = cc_refresh.discover_latest_release(
        _refresh_config(), today=datetime.date(2026, 1, 15),
    )
    assert got == "cc-main-2025-26-nov-dec-jan"


def test_discover_requires_both_vertices_and_edges(monkeypatch):
    """A window mid-publication (vertices up, edges not) must be skipped —
    building from it would produce a graph where every count is 0."""
    import datetime

    def fake_head(url, timeout=None, allow_redirects=None):
        resp = MagicMock()
        if "cc-main-2026-jun-jul-aug" in url:
            resp.status_code = 200 if "vertices" in url else 404
        elif "cc-main-2026-may-jun-jul" in url:
            resp.status_code = 200
        else:
            resp.status_code = 404
        return resp

    monkeypatch.setattr(cc_refresh.requests, "head", fake_head)
    got = cc_refresh.discover_latest_release(
        _refresh_config(), today=datetime.date(2026, 9, 20),
    )
    assert got == "cc-main-2026-may-jun-jul"


def test_discover_returns_none_when_nothing_is_available(monkeypatch):
    """All 404 → None, so the caller fails soft instead of crashing the
    weekly timer."""
    import datetime

    monkeypatch.setattr(cc_refresh.requests, "head", _head_stub(set()))
    assert cc_refresh.discover_latest_release(
        _refresh_config(), today=datetime.date(2026, 9, 20),
    ) is None


def test_discover_respects_max_windows_back(monkeypatch):
    """discover_max_windows_back bounds the probes: 2 windows x 2 files = at
    most 4 HEADs, and a release 3 windows back is never found."""
    import datetime

    fake_head = _head_stub({"cc-main-2026-apr-may-jun"})
    monkeypatch.setattr(cc_refresh.requests, "head", fake_head)

    got = cc_refresh.discover_latest_release(
        _refresh_config(discover_max_windows_back=2),
        today=datetime.date(2026, 9, 20),
    )
    assert got is None
    assert len(fake_head.calls) <= 4


@pytest.mark.parametrize("exc_name", [
    "ConnectionError", "Timeout", "HTTPError", "RequestException",
])
def test_discover_treats_a_network_error_as_unavailable_and_keeps_walking(
    monkeypatch, exc_name,
):
    """Each requests exception class is handled separately (CLAUDE.md HTTP
    convention) and all of them mean 'not available for THIS candidate' —
    never a crash, never a verdict about the older candidates."""
    import datetime
    import requests as real_requests

    exc = getattr(real_requests, exc_name)("synthetic")
    fake_head = _head_stub(
        {"cc-main-2026-may-jun-jul"}, errors={"cc-main-2026-jun-jul-aug": exc},
    )
    monkeypatch.setattr(cc_refresh.requests, "head", fake_head)

    got = cc_refresh.discover_latest_release(
        _refresh_config(), today=datetime.date(2026, 9, 20),
    )
    assert got == "cc-main-2026-may-jun-jul"


def test_discover_uses_configured_timeout_and_follows_redirects(monkeypatch):
    import datetime

    seen: list[dict] = []

    def fake_head(url, timeout=None, allow_redirects=None):
        seen.append({"timeout": timeout, "allow_redirects": allow_redirects})
        return MagicMock(status_code=200)

    monkeypatch.setattr(cc_refresh.requests, "head", fake_head)
    cc_refresh.discover_latest_release(
        _refresh_config(head_timeout_seconds=7), today=datetime.date(2026, 9, 20),
    )
    assert seen and all(s["timeout"] == 7 for s in seen)
    assert all(s["allow_redirects"] is True for s in seen)


def test_discover_uses_injected_session_when_given(monkeypatch):
    """An injected session means no reliance on module-level requests."""
    import datetime

    def must_not_call(*_a, **_kw):
        raise AssertionError("module-level requests.head used despite a session")

    monkeypatch.setattr(cc_refresh.requests, "head", must_not_call)
    session = MagicMock()
    session.head.return_value = MagicMock(status_code=200)

    got = cc_refresh.discover_latest_release(
        _refresh_config(), session=session, today=datetime.date(2026, 9, 20),
    )
    assert got == "cc-main-2026-jul-aug-sep"
    assert session.head.call_count == 2


# ---------------------------------------------------------------------------
# Disk sizing — derived from the release, not a constant
# ---------------------------------------------------------------------------


def test_required_disk_bytes_scales_with_the_release_size(monkeypatch):
    """Measured 2026-09-20: raw 10.3 GB, peak filesystem demand > 45 GB. The
    requirement is raw x disk_headroom_multiplier so it tracks a graph that
    grows OR shrinks (edges went 18.2 GB → 9.4 GB between releases)."""
    monkeypatch.setattr(
        cc_refresh.requests, "head",
        _head_stub(
            {"cc-main-2026-jun-jul-aug"},
            sizes={"vertices": 1 * _GIB, "edges": 9 * _GIB},
        ),
    )
    required = cc_refresh._required_disk_bytes(
        "cc-main-2026-jun-jul-aug", _refresh_config(disk_headroom_multiplier=5.0),
    )
    assert required == 50 * _GIB


def test_required_disk_bytes_reads_the_multiplier_from_config(monkeypatch):
    """Hard rule 9 — the headroom factor is a config knob, not a literal in
    the code path."""
    monkeypatch.setattr(
        cc_refresh.requests, "head",
        _head_stub(
            {"cc-main-2026-jun-jul-aug"},
            sizes={"vertices": 1 * _GIB, "edges": 1 * _GIB},
        ),
    )
    assert cc_refresh._required_disk_bytes(
        "cc-main-2026-jun-jul-aug", _refresh_config(disk_headroom_multiplier=3.0),
    ) == 6 * _GIB


def test_required_disk_bytes_falls_back_to_in_code_default_multiplier(monkeypatch):
    """The config key lands in a separate commit, so a config without it must
    still produce a sane (5x) requirement rather than a KeyError."""
    monkeypatch.setattr(
        cc_refresh.requests, "head",
        _head_stub(
            {"cc-main-2026-jun-jul-aug"},
            sizes={"vertices": 1 * _GIB, "edges": 1 * _GIB},
        ),
    )
    config = _refresh_config()
    assert "disk_headroom_multiplier" not in config["cc_backlinks"]["refresh"]
    assert cc_refresh._required_disk_bytes(
        "cc-main-2026-jun-jul-aug", config,
    ) == int(2 * _GIB * cc_refresh._DEFAULT_DISK_HEADROOM_MULTIPLIER)


def test_required_disk_bytes_uses_the_floor_when_content_length_is_missing(
    monkeypatch,
):
    """Never skip the check: an unknown size means demand the conservative
    floor, not zero."""
    monkeypatch.setattr(
        cc_refresh.requests, "head",
        _head_stub({"cc-main-2026-jun-jul-aug"}, sizes={}),  # 200 but no length
    )
    assert cc_refresh._required_disk_bytes(
        "cc-main-2026-jun-jul-aug", _refresh_config(),
    ) == cc_refresh._DISK_REQUIREMENT_FLOOR_BYTES


def test_required_disk_bytes_uses_the_floor_on_a_network_error(monkeypatch):
    import requests as real_requests

    monkeypatch.setattr(
        cc_refresh.requests, "head",
        _head_stub(
            {"cc-main-2026-jun-jul-aug"},
            errors={"cc-main-2026-jun-jul-aug": real_requests.Timeout("synthetic")},
        ),
    )
    assert cc_refresh._required_disk_bytes(
        "cc-main-2026-jun-jul-aug", _refresh_config(),
    ) == cc_refresh._DISK_REQUIREMENT_FLOOR_BYTES


def test_required_disk_bytes_uses_the_floor_when_only_one_file_is_sized(monkeypatch):
    """Half a measurement is not a measurement."""
    monkeypatch.setattr(
        cc_refresh.requests, "head",
        _head_stub({"cc-main-2026-jun-jul-aug"}, sizes={"vertices": 1 * _GIB}),
    )
    assert cc_refresh._required_disk_bytes(
        "cc-main-2026-jun-jul-aug", _refresh_config(),
    ) == cc_refresh._DISK_REQUIREMENT_FLOOR_BYTES


def test_remote_size_ignores_a_non_200_response(monkeypatch):
    monkeypatch.setattr(
        cc_refresh.requests, "head", _head_stub(set(), sizes={"edges": 5}),
    )
    assert cc_refresh._remote_size(
        cc_refresh._source_url("cc-main-2026-jul-aug-sep", "edges"), timeout=1,
    ) is None


def test_required_disk_space_is_checked_before_any_download_begins(
    tmp_path, monkeypatch,
):
    """The whole point of the pre-flight check: on a volume between the old
    hardcoded 25 GiB and the real ~50 GB peak, the run must abort now, not
    ENOSPC two hours into a build."""
    import json as _json
    from scripts import diff as diff_module

    config = _refresh_config(disk_headroom_multiplier=5.0)
    config_path = tmp_path / "config.json"
    with open(config_path, "w", encoding="utf-8", newline="") as fh:
        _json.dump(config, fh, indent=2)
        fh.write("\n")

    monkeypatch.setattr(
        cc_refresh.requests, "head",
        _head_stub(
            {"cc-main-2026-jun-jul-aug"},
            sizes={"vertices": 1 * _GIB, "edges": 9 * _GIB},  # → 50 GiB needed
        ),
    )

    def must_not_download(*_a, **_kw):
        raise AssertionError("download started despite insufficient disk space")

    monkeypatch.setattr(cc_refresh.requests, "get", must_not_download)
    monkeypatch.setattr(
        cc_refresh.shutil, "disk_usage",
        lambda _p: type("DU", (), {"free": 30 * _GIB})(),  # passes the old check
    )
    monkeypatch.setattr(diff_module, "_r2_client", lambda: MagicMock())
    monkeypatch.setattr(diff_module, "_bucket", lambda: "test-bucket")
    for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        monkeypatch.setenv(var, "test")

    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        cc_refresh.main([
            "--release", "cc-main-2026-jun-jul-aug",
            "--workdir", str(tmp_path / "work"),
            "--config", str(config_path),
        ])


# ---------------------------------------------------------------------------
# Guards — every branch
# ---------------------------------------------------------------------------


def _utc(hour: int):
    import datetime

    return datetime.datetime(2026, 9, 20, hour, 30, tzinfo=datetime.timezone.utc)


def test_guard_blocks_when_disabled():
    reason = cc_refresh._refresh_blocked_reason(
        _refresh_config(enabled=False), now=_utc(18),
        unit_active_fn=lambda _u: False,
    )
    assert reason is not None and "enabled" in reason


def test_guard_blocks_inside_blackout_window():
    """09:30 UTC is mid-pipeline — a multi-hour job must not start."""
    reason = cc_refresh._refresh_blocked_reason(
        _refresh_config(), now=_utc(9), unit_active_fn=lambda _u: False,
    )
    assert reason is not None and "blackout" in reason


def test_guard_blackout_is_half_open_at_both_ends():
    """[7, 16): 07:xx is blocked, 16:xx is not."""
    assert cc_refresh._refresh_blocked_reason(
        _refresh_config(), now=_utc(7), unit_active_fn=lambda _u: False,
    ) is not None
    assert cc_refresh._refresh_blocked_reason(
        _refresh_config(), now=_utc(16), unit_active_fn=lambda _u: False,
    ) is None


def test_guard_allows_the_weekly_timer_slot():
    """18:00 UTC — the timer's actual slot — must be clear."""
    assert cc_refresh._refresh_blocked_reason(
        _refresh_config(), now=_utc(18), unit_active_fn=lambda _u: False,
    ) is None


def test_guard_blackout_can_wrap_past_midnight():
    """start > end wraps: [22, 3) blocks 23:xx and 01:xx, allows 12:xx."""
    cfg = _refresh_config(blackout_start_utc_hour=22, blackout_end_utc_hour=3)
    assert cc_refresh._refresh_blocked_reason(
        cfg, now=_utc(23), unit_active_fn=lambda _u: False) is not None
    assert cc_refresh._refresh_blocked_reason(
        cfg, now=_utc(1), unit_active_fn=lambda _u: False) is not None
    assert cc_refresh._refresh_blocked_reason(
        cfg, now=_utc(12), unit_active_fn=lambda _u: False) is None


def test_guard_blackout_disabled_when_start_equals_end():
    cfg = _refresh_config(blackout_start_utc_hour=0, blackout_end_utc_hour=0)
    assert cc_refresh._refresh_blocked_reason(
        cfg, now=_utc(9), unit_active_fn=lambda _u: False) is None


def test_guard_blocks_when_pipeline_unit_is_active():
    """Second, independent interlock: the clock may be clear but the daily run
    may be late or manually re-run."""
    asked: list[str] = []

    def unit_active(unit: str) -> bool:
        asked.append(unit)
        return True

    reason = cc_refresh._refresh_blocked_reason(
        _refresh_config(), now=_utc(18), unit_active_fn=unit_active,
    )
    assert reason is not None and "domainsifter.service" in reason
    assert asked == ["domainsifter.service"]


def test_guard_skips_unit_check_when_no_unit_configured():
    def must_not_call(_unit):
        raise AssertionError("probed systemd despite an empty pipeline_unit")

    assert cc_refresh._refresh_blocked_reason(
        _refresh_config(pipeline_unit=""), now=_utc(18),
        unit_active_fn=must_not_call,
    ) is None


def test_systemd_unit_active_is_false_when_systemctl_is_missing(monkeypatch):
    """Dev boxes have no systemctl; that must read as 'not active', not as an
    exception that aborts the run."""
    def boom(*_a, **_kw):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(cc_refresh.subprocess, "run", boom)
    assert cc_refresh._systemd_unit_active("whatever.service") is False


def test_systemd_unit_active_is_false_on_oserror(monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("permission denied")

    monkeypatch.setattr(cc_refresh.subprocess, "run", boom)
    assert cc_refresh._systemd_unit_active("whatever.service") is False


def test_systemd_unit_active_reads_the_exit_code(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return type("CP", (), {"returncode": 0})()

    monkeypatch.setattr(cc_refresh.subprocess, "run", fake_run)
    assert cc_refresh._systemd_unit_active("domainsifter.service") is True
    assert calls == [["systemctl", "is-active", "--quiet", "domainsifter.service"]]

    monkeypatch.setattr(
        cc_refresh.subprocess, "run",
        lambda *_a, **_kw: type("CP", (), {"returncode": 3})(),
    )
    assert cc_refresh._systemd_unit_active("domainsifter.service") is False


# ---------------------------------------------------------------------------
# Verification — the gate in front of the config swap
# ---------------------------------------------------------------------------


def _verify_env(tmp_path, release="cc-main-2026-jun-jul-aug", rows=None):
    """A fixture derived SQLite 'in R2' plus an empty local cache dir."""
    source = _write_fixture_sqlite(tmp_path / "r2-copy.sqlite", release, rows)
    return source, tmp_path / "cache"


def test_verify_downloads_from_r2_and_returns_measurements(tmp_path):
    """Verification proves the artifact AS IT EXISTS IN R2 — it downloads
    through the pipeline's own loader rather than trusting the local build,
    and leaves the cache pre-warmed for the 09:00 run."""
    release = "cc-main-2026-jun-jul-aug"
    source, cache = _verify_env(tmp_path, release)
    s3 = _FakeR2(payload=source)

    measured = cc_refresh.verify_derived_release(
        release, _refresh_config(), cache_dir=cache, s3_client=s3, bucket="b",
    )

    assert s3.downloaded == [f"cc/derived/{release}.sqlite"]
    assert measured["rows"] == 4
    assert measured["canaries"] == {"marketglow.com": 9}
    assert measured["meta"]["release"] == release
    assert (cache / f"{release}.sqlite").exists()  # cache left warm


def test_verify_deletes_a_stale_cached_copy_before_downloading(tmp_path):
    """A cached file must not be trusted: we delete it so the download — and
    therefore the R2 round-trip — really happens."""
    release = "cc-main-2026-jun-jul-aug"
    source, cache = _verify_env(tmp_path, release)
    cache.mkdir(parents=True)
    (cache / f"{release}.sqlite").write_bytes(b"stale-not-even-sqlite")
    s3 = _FakeR2(payload=source)

    cc_refresh.verify_derived_release(
        release, _refresh_config(), cache_dir=cache, s3_client=s3, bucket="b",
    )
    assert s3.downloaded == [f"cc/derived/{release}.sqlite"]


def test_verify_fails_when_meta_release_is_wrong(tmp_path):
    """Release A's bytes under release B's key is the nightmare case:
    everything looks fine but scoring uses the wrong window."""
    release = "cc-main-2026-jun-jul-aug"
    source, cache = _verify_env(tmp_path, "cc-main-2026-may-jun-jul")
    s3 = _FakeR2(payload=source)

    with pytest.raises(cc_refresh.VerificationError) as excinfo:
        cc_refresh.verify_derived_release(
            release, _refresh_config(), cache_dir=cache, s3_client=s3, bucket="b",
        )
    assert excinfo.value.check == "meta_release"
    assert not (cache / f"{release}.sqlite").exists()


def test_verify_fails_when_row_count_is_below_the_configured_floor(tmp_path):
    """A truncated build — the failure min_cc_apex_rows exists to catch."""
    release = "cc-main-2026-jun-jul-aug"
    source, cache = _verify_env(
        tmp_path, release, rows={"marketglow.com": 9, "coppernest.org": 2},
    )
    s3 = _FakeR2(payload=source)

    with pytest.raises(cc_refresh.VerificationError) as excinfo:
        cc_refresh.verify_derived_release(
            release, _refresh_config(verification={
                "min_cc_apex_rows": 999,
                "canary_present": {"marketglow.com": 5},
                "canary_absent": [],
            }),
            cache_dir=cache, s3_client=s3, bucket="b",
        )
    assert excinfo.value.check == "min_cc_apex_rows"
    assert not (cache / f"{release}.sqlite").exists()


def test_verify_fails_when_present_canary_is_missing(tmp_path):
    """No canary row at all means the edges join never ran."""
    release = "cc-main-2026-jun-jul-aug"
    source, cache = _verify_env(
        tmp_path, release,
        rows={"coppernest.org": 3, "amberflask.dev": 1, "quietlathe.studio": 2},
    )
    s3 = _FakeR2(payload=source)

    with pytest.raises(cc_refresh.VerificationError) as excinfo:
        cc_refresh.verify_derived_release(
            release, _refresh_config(), cache_dir=cache, s3_client=s3, bucket="b",
        )
    assert excinfo.value.check == "canary_present"
    assert "marketglow.com" in excinfo.value.detail
    assert not (cache / f"{release}.sqlite").exists()


def test_verify_fails_when_present_canary_is_below_its_floor(tmp_path):
    """Present but with a tiny count = a partial edges file."""
    release = "cc-main-2026-jun-jul-aug"
    source, cache = _verify_env(
        tmp_path, release,
        rows={"marketglow.com": 1, "coppernest.org": 3, "amberflask.dev": 0},
    )
    s3 = _FakeR2(payload=source)

    with pytest.raises(cc_refresh.VerificationError) as excinfo:
        cc_refresh.verify_derived_release(
            release, _refresh_config(), cache_dir=cache, s3_client=s3, bucket="b",
        )
    assert excinfo.value.check == "canary_present"
    assert not (cache / f"{release}.sqlite").exists()


def test_verify_fails_when_absent_canary_is_present(tmp_path):
    """canary_absent proves we are not returning a count for everything."""
    release = "cc-main-2026-jun-jul-aug"
    source, cache = _verify_env(
        tmp_path, release,
        rows={"marketglow.com": 9, "coppernest.org": 3,
              "amberflask.dev": 0, "tideblock.io": 7},
    )
    s3 = _FakeR2(payload=source)

    with pytest.raises(cc_refresh.VerificationError) as excinfo:
        cc_refresh.verify_derived_release(
            release, _refresh_config(), cache_dir=cache, s3_client=s3, bucket="b",
        )
    assert excinfo.value.check == "canary_absent"
    assert "tideblock.io" in excinfo.value.detail
    assert not (cache / f"{release}.sqlite").exists()


def test_verify_fails_when_the_download_is_not_a_database(tmp_path):
    """A truncated download or an HTML error page must be rejected as a
    verification failure, not crash with a raw sqlite3 error."""
    release = "cc-main-2026-jun-jul-aug"
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"<html>403 Forbidden</html>")
    cache = tmp_path / "cache"
    s3 = _FakeR2(payload=garbage)

    with pytest.raises(cc_refresh.VerificationError) as excinfo:
        cc_refresh.verify_derived_release(
            release, _refresh_config(), cache_dir=cache, s3_client=s3, bucket="b",
        )
    assert excinfo.value.check == "unreadable"
    assert not (cache / f"{release}.sqlite").exists()


def test_verify_thresholds_come_from_config_not_code(tmp_path):
    """Hard rule 9: changing the config numbers changes the verdict, with no
    code change and no real domain in the fixture."""
    release = "cc-main-2026-jun-jul-aug"
    source, cache = _verify_env(
        tmp_path, release, rows={"coppernest.org": 4, "amberflask.dev": 0},
    )
    s3 = _FakeR2(payload=source)

    measured = cc_refresh.verify_derived_release(
        release,
        _refresh_config(verification={
            "min_cc_apex_rows": 2,
            "canary_present": {"coppernest.org": 4},
            "canary_absent": ["marketglow.com"],
        }),
        cache_dir=cache, s3_client=s3, bucket="b",
    )
    assert measured["canaries"] == {"coppernest.org": 4}


# ---------------------------------------------------------------------------
# Config swap — surgical text edit, byte-preserving
# ---------------------------------------------------------------------------


_CONFIG_SAMPLE = (
    '{\n'
    '  "version": "1.0",\n'
    '  "cc_backlinks": {\n'
    '    "_doc": "mojibake lives here — and � too; a json round-trip '
    'would re-flow all of it",\n'
    '    "latest_release": "cc-main-2026-feb-mar-apr",\n'
    '    "refresh": {"enabled": true}\n'
    '  },\n'
    '  "tail_key": "untouched"\n'
    '}\n'
)


def _write_config_sample(tmp_path: Path, text: str = _CONFIG_SAMPLE) -> Path:
    path = tmp_path / "config.json"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


def test_install_release_edits_only_the_latest_release_value(tmp_path):
    """Everything except the one value — key order, the mojibake _doc, the
    trailing newline — must survive byte-for-byte."""
    import json as _json

    path = _write_config_sample(tmp_path)
    before = path.read_bytes()

    previous = cc_refresh.install_release("cc-main-2026-jun-jul-aug", path)

    assert previous == "cc-main-2026-feb-mar-apr"
    after = path.read_bytes()
    assert after == before.replace(
        b"cc-main-2026-feb-mar-apr", b"cc-main-2026-jun-jul-aug",
    )
    assert after.endswith(b"}\n")
    parsed = _json.loads(after.decode("utf-8"))
    assert parsed["cc_backlinks"]["latest_release"] == "cc-main-2026-jun-jul-aug"
    assert parsed["tail_key"] == "untouched"


def test_install_release_preserves_crlf_when_the_file_uses_it(tmp_path):
    """No newline translation in either direction — this runs on Linux but the
    file is also edited on Windows."""
    path = tmp_path / "config.json"
    raw = _CONFIG_SAMPLE.replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(raw)

    cc_refresh.install_release("cc-main-2026-jun-jul-aug", path)
    assert path.read_bytes() == raw.replace(
        b"cc-main-2026-feb-mar-apr", b"cc-main-2026-jun-jul-aug",
    )


def test_install_release_is_a_noop_when_already_current(tmp_path):
    path = _write_config_sample(tmp_path)
    before = path.read_bytes()
    assert cc_refresh.install_release("cc-main-2026-feb-mar-apr", path) == (
        "cc-main-2026-feb-mar-apr"
    )
    assert path.read_bytes() == before


def test_install_release_refuses_when_the_key_is_absent(tmp_path):
    path = _write_config_sample(tmp_path, '{\n  "no_such_key": 1\n}\n')
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="exactly one"):
        cc_refresh.install_release("cc-main-2026-jun-jul-aug", path)
    assert path.read_bytes() == before


def test_install_release_refuses_when_the_key_appears_twice(tmp_path):
    """Two matches means we cannot know which one the enricher reads."""
    path = _write_config_sample(
        tmp_path,
        '{\n  "latest_release": "a",\n  "other": {"latest_release": "b"}\n}\n',
    )
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="exactly one"):
        cc_refresh.install_release("cc-main-2026-jun-jul-aug", path)
    assert path.read_bytes() == before


def test_install_release_refuses_when_previous_does_not_match(tmp_path):
    """Guards against a concurrent edit between discovery and install."""
    path = _write_config_sample(tmp_path)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="refusing to edit"):
        cc_refresh.install_release(
            "cc-main-2026-jun-jul-aug", path, previous="cc-main-2026-jan-feb-mar",
        )
    assert path.read_bytes() == before


def test_install_release_restores_the_original_if_the_result_will_not_parse(tmp_path):
    """The one thing this must never leave behind is a config the daily
    pipeline cannot read."""
    path = _write_config_sample(tmp_path)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="no longer parses"):
        cc_refresh.install_release('bad"release', path)
    assert path.read_bytes() == before


def test_install_release_does_not_touch_newsletter_intro_text(tmp_path):
    """intro_text carries a {cc_release} placeholder substituted at render
    time; this function leaves it alone so it can never drift."""
    text = (
        '{\n'
        '  "cc_backlinks": {"latest_release": "cc-main-2026-feb-mar-apr"},\n'
        '  "newsletter": {"intro_text": "release {cc_release} blah"}\n'
        '}\n'
    )
    path = _write_config_sample(tmp_path, text)
    cc_refresh.install_release("cc-main-2026-jun-jul-aug", path)
    assert '"intro_text": "release {cc_release} blah"' in path.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Retention — raw is disposable, derived is forever
# ---------------------------------------------------------------------------


def _raw_keys(*releases: str) -> list[str]:
    keys: list[str] = []
    for r in releases:
        keys.append(f"cc/raw/{r}/vertices.txt.gz")
        keys.append(f"cc/raw/{r}/edges.txt.gz")
    return keys


def test_prune_keeps_the_newest_n_raw_releases():
    s3 = _FakeR2(keys=_raw_keys(
        "cc-main-2025-26-dec-jan-feb",
        "cc-main-2026-jan-feb-mar",
        "cc-main-2026-may-jun-jul",
        "cc-main-2026-jun-jul-aug",
    ))
    config = _refresh_config(prune_raw_after_releases=2)
    config["cc_backlinks"]["latest_release"] = "cc-main-2026-jun-jul-aug"

    deleted = cc_refresh.prune_raw_releases(
        config, s3_client=s3, bucket="b", keep_release="cc-main-2026-jun-jul-aug",
    )
    assert sorted(deleted) == [
        "cc-main-2025-26-dec-jan-feb", "cc-main-2026-jan-feb-mar",
    ]
    assert sorted(s3.deleted) == sorted(_raw_keys(
        "cc-main-2025-26-dec-jan-feb", "cc-main-2026-jan-feb-mar",
    ))


def test_prune_never_deletes_anything_under_derived():
    """The absolute rule, at every config value. Derived SQLite is the durable
    asset — a future multi-release strategy needs the history."""
    releases = (
        "cc-main-2025-26-dec-jan-feb",
        "cc-main-2026-jan-feb-mar",
        "cc-main-2026-jun-jul-aug",
    )
    derived = [f"cc/derived/{r}.sqlite" for r in releases]
    s3 = _FakeR2()

    for keep_n in (1, 2, 3, 99):
        s3.deleted.clear()
        s3.keys = _raw_keys(*releases) + list(derived)
        cc_refresh.prune_raw_releases(
            _refresh_config(prune_raw_after_releases=keep_n),
            s3_client=s3, bucket="b", keep_release="cc-main-2026-jun-jul-aug",
        )
        assert not any(k.startswith("cc/derived/") for k in s3.deleted)
        assert all(k in s3.keys for k in derived)
    # The listing was scoped to the raw prefix in the first place.
    assert all(c["Prefix"] == "cc/raw/" for c in s3.list_calls)


def test_prune_never_deletes_keep_release_or_configured_latest():
    """Even with keep_n=1 and an ancient-sorting name, the release the pipeline
    is about to use (and the one it uses now) are protected."""
    s3 = _FakeR2(keys=_raw_keys(
        "cc-main-2025-26-nov-dec-jan",   # configured latest_release — protected
        "cc-main-2026-jan-feb-mar",      # nobody's favourite — the only victim
        "cc-main-2026-feb-mar-apr",      # keep_release — protected
        "cc-main-2026-jun-jul-aug",      # newest, inside keep_n=1
    ))
    config = _refresh_config(prune_raw_after_releases=1)
    config["cc_backlinks"]["latest_release"] = "cc-main-2025-26-nov-dec-jan"

    deleted = cc_refresh.prune_raw_releases(
        config, s3_client=s3, bucket="b", keep_release="cc-main-2026-feb-mar-apr",
    )
    assert deleted == ["cc-main-2026-jan-feb-mar"]
    # The oldest-sorting name is the configured release, and keep_release sorts
    # in the middle: neither is deletable regardless of ordering.
    assert not any("nov-dec-jan" in k for k in s3.deleted)
    assert not any("feb-mar-apr" in k for k in s3.deleted)
    assert not any("jun-jul-aug" in k for k in s3.deleted)


def test_prune_skips_release_directories_it_cannot_parse():
    """An unknown naming scheme is a reason to leave data alone."""
    s3 = _FakeR2(keys=_raw_keys(
        "cc-main-2026-jan-feb-mar",
        "cc-main-2026-jun-jul-aug",
        "manual-experiment",
        "cc-main-2025-27-dec-jan-feb",
    ))
    config = _refresh_config(prune_raw_after_releases=1)
    config["cc_backlinks"]["latest_release"] = "cc-main-2026-jun-jul-aug"

    deleted = cc_refresh.prune_raw_releases(
        config, s3_client=s3, bucket="b", keep_release="cc-main-2026-jun-jul-aug",
    )
    assert deleted == ["cc-main-2026-jan-feb-mar"]
    assert not any("manual-experiment" in k for k in s3.deleted)
    assert not any("2025-27" in k for k in s3.deleted)


def test_prune_disabled_when_keep_count_is_zero():
    s3 = _FakeR2(keys=_raw_keys("cc-main-2026-jan-feb-mar", "cc-main-2026-jun-jul-aug"))
    assert cc_refresh.prune_raw_releases(
        _refresh_config(prune_raw_after_releases=0),
        s3_client=s3, bucket="b", keep_release="cc-main-2026-jun-jul-aug",
    ) == []
    assert s3.deleted == []
    assert s3.list_calls == []  # not even a listing


def test_prune_follows_continuation_tokens():
    """A listing can exceed one page; missing page two would leave orphaned
    raw objects paying storage forever."""
    pages = [
        {
            "Contents": [{"Key": "cc/raw/cc-main-2026-jan-feb-mar/vertices.txt.gz"}],
            "IsTruncated": True,
            "NextContinuationToken": "t1",
        },
        {
            "Contents": [
                {"Key": "cc/raw/cc-main-2026-jan-feb-mar/edges.txt.gz"},
                {"Key": "cc/raw/cc-main-2026-jun-jul-aug/vertices.txt.gz"},
                {"Key": "cc/raw/cc-main-2026-jun-jul-aug/edges.txt.gz"},
            ],
            "IsTruncated": False,
        },
    ]
    calls: list[dict] = []

    class Paged:
        def __init__(self):
            self.deleted: list[str] = []

        def list_objects_v2(self, **kwargs):
            calls.append(kwargs)
            return pages[len(calls) - 1]

        def delete_object(self, Bucket, Key):
            self.deleted.append(Key)

    s3 = Paged()
    config = _refresh_config(prune_raw_after_releases=1)
    config["cc_backlinks"]["latest_release"] = "cc-main-2026-jun-jul-aug"

    deleted = cc_refresh.prune_raw_releases(
        config, s3_client=s3, bucket="b", keep_release="cc-main-2026-jun-jul-aug",
    )
    assert deleted == ["cc-main-2026-jan-feb-mar"]
    # Both of the victim's objects were deleted — the edges key only exists on
    # page two, so a pruner that stopped at page one would leave it orphaned.
    assert sorted(s3.deleted) == [
        "cc/raw/cc-main-2026-jan-feb-mar/edges.txt.gz",
        "cc/raw/cc-main-2026-jan-feb-mar/vertices.txt.gz",
    ]
    assert calls[1]["ContinuationToken"] == "t1"


def test_prune_fails_soft_when_listing_errors():
    """A prune problem must never fail a run that already succeeded."""
    s3 = MagicMock()
    s3.list_objects_v2.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "ListObjectsV2",
    )
    assert cc_refresh.prune_raw_releases(
        _refresh_config(), s3_client=s3, bucket="b",
        keep_release="cc-main-2026-jun-jul-aug",
    ) == []
    s3.delete_object.assert_not_called()


def test_prune_fails_soft_when_a_delete_errors():
    class HalfBroken(_FakeR2):
        def delete_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")

    s3 = HalfBroken(keys=_raw_keys(
        "cc-main-2026-jan-feb-mar", "cc-main-2026-jun-jul-aug",
    ))
    config = _refresh_config(prune_raw_after_releases=1)
    config["cc_backlinks"]["latest_release"] = "cc-main-2026-jun-jul-aug"

    assert cc_refresh.prune_raw_releases(
        config, s3_client=s3, bucket="b", keep_release="cc-main-2026-jun-jul-aug",
    ) == []


def test_prune_local_cache_keeps_only_the_active_release(tmp_path, monkeypatch):
    cache = tmp_path / "cc"
    cache.mkdir()
    for name in (
        "cc-main-2026-jun-jul-aug.sqlite",
        "cc-main-2026-feb-mar-apr.sqlite",
        "cc-main-2026-jan-feb-mar.sqlite",
        "notes.txt",
    ):
        (cache / name).write_bytes(b"x")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache))

    removed = cc_refresh.prune_local_cache(
        _refresh_config(), "cc-main-2026-jun-jul-aug",
    )
    assert sorted(removed) == [
        "cc-main-2026-feb-mar-apr.sqlite", "cc-main-2026-jan-feb-mar.sqlite",
    ]
    assert (cache / "cc-main-2026-jun-jul-aug.sqlite").exists()
    assert (cache / "notes.txt").exists()  # only *.sqlite is ours to prune


def test_prune_local_cache_respects_the_config_flag(tmp_path, monkeypatch):
    cache = tmp_path / "cc"
    cache.mkdir()
    (cache / "cc-main-2026-feb-mar-apr.sqlite").write_bytes(b"x")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache))

    assert cc_refresh.prune_local_cache(
        _refresh_config(prune_local_cache=False), "cc-main-2026-jun-jul-aug",
    ) == []
    assert (cache / "cc-main-2026-feb-mar-apr.sqlite").exists()


def test_prune_local_cache_fails_soft_when_the_dir_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(tmp_path / "nope"))
    assert cc_refresh.prune_local_cache(
        _refresh_config(), "cc-main-2026-jun-jul-aug",
    ) == []


# ---------------------------------------------------------------------------
# Result file
# ---------------------------------------------------------------------------


def test_write_result_is_atomic_and_stamps_finished_at(tmp_path):
    import json as _json

    config = _refresh_config(result_path=str(tmp_path / "nested" / "result.json"))
    cc_refresh.write_result(config, {"action": "noop", "release": "r"})

    payload = _json.loads(
        (tmp_path / "nested" / "result.json").read_text(encoding="utf-8")
    )
    assert payload["action"] == "noop"
    assert payload["finished_at"].endswith("Z")
    # No temp files left behind.
    assert [p.name for p in (tmp_path / "nested").iterdir()] == ["result.json"]


def test_write_result_never_raises(tmp_path):
    """A bookkeeping failure must not change the exit code of the run."""
    config = _refresh_config(result_path=str(tmp_path))  # a directory, not a file
    cc_refresh.write_result(config, {"action": "noop"})  # must not raise


def test_result_path_resolves_relative_to_the_repo_root(tmp_path):
    """systemd may hand us any cwd; the result must still land in
    scripts/state/."""
    path = cc_refresh._result_path(_refresh_config())
    assert path.is_absolute()
    assert path.parts[-2:] == ("state", "cc_refresh_result.json")


# ---------------------------------------------------------------------------
# --auto: CLI contract and exit codes (the shell wrapper depends on these)
# ---------------------------------------------------------------------------


@pytest.fixture
def auto_env(tmp_path, monkeypatch):
    """A temp config.json + result path, with systemd, R2 and the build phases
    stubbed out. Returns a dict of handles for the test to assert on."""
    import json as _json

    result_path = tmp_path / "result.json"
    config = _refresh_config(
        blackout_start_utc_hour=0,
        blackout_end_utc_hour=0,          # blackout disabled by default
        result_path=str(result_path),
    )
    config_path = tmp_path / "config.json"
    with open(config_path, "w", encoding="utf-8", newline="") as fh:
        _json.dump(config, fh, indent=2)
        fh.write("\n")

    # Never shell out to systemctl, and never touch the network or R2.
    monkeypatch.setattr(cc_refresh, "_systemd_unit_active", lambda _u: False)
    monkeypatch.setattr(cc_refresh, "_ensure_disk_space", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        cc_refresh, "_required_disk_bytes", lambda *_a, **_kw: 1024,
    )
    for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        monkeypatch.setenv(var, "test")

    from scripts import diff as diff_module
    fake_s3 = MagicMock()
    monkeypatch.setattr(diff_module, "_r2_client", lambda: fake_s3)
    monkeypatch.setattr(diff_module, "_bucket", lambda: "test-bucket")

    state: dict = {"phases": []}
    monkeypatch.setattr(
        cc_refresh, "_phase_download_and_upload_raw",
        lambda **kw: state["phases"].append(("raw", kw["release"])) or {},
    )
    monkeypatch.setattr(
        cc_refresh, "_phase_build_and_upload_derived",
        lambda **kw: state["phases"].append(("derived", kw["release"])),
    )
    state["s3"] = fake_s3
    state["config_path"] = config_path
    state["result_path"] = result_path
    return state


def _read_result(path: Path) -> dict:
    import json as _json

    return _json.loads(path.read_text(encoding="utf-8"))


def test_auto_exits_zero_and_records_a_skip_when_a_guard_blocks(auto_env, monkeypatch):
    """A skip must not page anyone — a persistent skip surfaces through the
    staleness line in the daily report instead."""
    monkeypatch.setattr(
        cc_refresh, "_refresh_blocked_reason", lambda *_a, **_kw: "unit is active",
    )

    def must_not_discover(*_a, **_kw):
        raise AssertionError("discovery ran despite a blocking guard")

    monkeypatch.setattr(cc_refresh, "discover_latest_release", must_not_discover)

    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 0
    payload = _read_result(auto_env["result_path"])
    assert payload["action"] == "skipped"
    assert payload["reason"] == "unit is active"


def test_auto_exits_one_when_discovery_finds_nothing(auto_env, monkeypatch):
    monkeypatch.setattr(cc_refresh, "discover_latest_release", lambda *_a, **_kw: None)
    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 1
    payload = _read_result(auto_env["result_path"])
    assert payload["action"] == "discovery_failed"
    assert set(payload) == {"action", "finished_at"}


def test_auto_exits_zero_as_a_noop_when_already_current(auto_env, monkeypatch):
    monkeypatch.setattr(
        cc_refresh, "discover_latest_release",
        lambda *_a, **_kw: "cc-main-2026-feb-mar-apr",  # == configured
    )

    def must_not_build(**_kw):
        raise AssertionError("built despite being up to date")

    monkeypatch.setattr(cc_refresh, "_phase_download_and_upload_raw", must_not_build)

    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 0
    payload = _read_result(auto_env["result_path"])
    assert payload["action"] == "noop"
    assert payload["release"] == "cc-main-2026-feb-mar-apr"


def test_auto_installs_a_new_release_and_reports_what_it_measured(
    auto_env, monkeypatch,
):
    """The happy path: build → verify → swap → prune, exit 0, and a result
    file carrying the measurements the daily email quotes."""
    import json as _json

    new_release = "cc-main-2026-jun-jul-aug"
    monkeypatch.setattr(
        cc_refresh, "discover_latest_release", lambda *_a, **_kw: new_release,
    )
    monkeypatch.setattr(
        cc_refresh, "verify_derived_release",
        lambda release, config, **_kw: {
            "release": release,
            "rows": 124_646_710,
            "canaries": {"marketglow.com": 16_365_926},
            "meta": {},
        },
    )
    monkeypatch.setattr(
        cc_refresh, "prune_raw_releases",
        lambda *_a, **_kw: ["cc-main-2026-jan-feb-mar"],
    )
    monkeypatch.setattr(
        cc_refresh, "prune_local_cache",
        lambda *_a, **_kw: ["cc-main-2026-feb-mar-apr.sqlite"],
    )

    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 0

    payload = _read_result(auto_env["result_path"])
    assert payload["action"] == "installed"
    assert payload["release"] == new_release
    assert payload["previous_release"] == "cc-main-2026-feb-mar-apr"
    assert payload["rows"] == 124_646_710
    assert payload["canaries"] == {"marketglow.com": 16_365_926}
    assert payload["pruned_raw_releases"] == ["cc-main-2026-jan-feb-mar"]
    assert payload["finished_at"].endswith("Z")

    # Both build phases ran for the new release, and the config really swapped.
    assert auto_env["phases"] == [("raw", new_release), ("derived", new_release)]
    installed = _json.loads(auto_env["config_path"].read_text(encoding="utf-8"))
    assert installed["cc_backlinks"]["latest_release"] == new_release


def test_auto_exits_one_on_verification_failure_without_swapping_or_pruning(
    auto_env, monkeypatch,
):
    """The whole reason verification runs before the swap: a bad artifact must
    leave the pipeline on the old, known-good release."""
    import json as _json

    new_release = "cc-main-2026-jun-jul-aug"
    monkeypatch.setattr(
        cc_refresh, "discover_latest_release", lambda *_a, **_kw: new_release,
    )

    def boom(release, config, **_kw):
        raise cc_refresh.VerificationError("min_cc_apex_rows", "only 12 rows")

    monkeypatch.setattr(cc_refresh, "verify_derived_release", boom)

    def must_not_prune(*_a, **_kw):
        raise AssertionError("pruned after a failed verification")

    monkeypatch.setattr(cc_refresh, "prune_raw_releases", must_not_prune)
    monkeypatch.setattr(cc_refresh, "prune_local_cache", must_not_prune)

    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 1

    payload = _read_result(auto_env["result_path"])
    assert payload["action"] == "verification_failed"
    assert payload["release"] == new_release
    assert payload["failed_check"] == "min_cc_apex_rows"
    assert "only 12 rows" in payload["reason"]

    untouched = _json.loads(auto_env["config_path"].read_text(encoding="utf-8"))
    assert untouched["cc_backlinks"]["latest_release"] == "cc-main-2026-feb-mar-apr"


def test_auto_exits_one_and_records_a_build_failure(auto_env, monkeypatch):
    new_release = "cc-main-2026-jun-jul-aug"
    monkeypatch.setattr(
        cc_refresh, "discover_latest_release", lambda *_a, **_kw: new_release,
    )

    def boom(**_kw):
        raise RuntimeError("Insufficient disk space")

    monkeypatch.setattr(cc_refresh, "_phase_download_and_upload_raw", boom)

    def must_not_verify(*_a, **_kw):
        raise AssertionError("verified despite a failed build")

    monkeypatch.setattr(cc_refresh, "verify_derived_release", must_not_verify)

    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 1
    payload = _read_result(auto_env["result_path"])
    assert payload["action"] == "build_failed"
    assert "Insufficient disk space" in payload["reason"]


def test_auto_result_write_failure_does_not_mask_the_exit_code(auto_env, monkeypatch):
    monkeypatch.setattr(cc_refresh, "discover_latest_release", lambda *_a, **_kw: None)

    def boom(_config):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(cc_refresh, "_result_path", boom)
    assert cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])]) == 1


@pytest.mark.parametrize("extra", [
    ["--release", "cc-main-2026-jun-jul-aug"],
    ["--download-only"],
    ["--build-only"],
])
def test_auto_is_mutually_exclusive_with_the_manual_flags(auto_env, extra):
    with pytest.raises(SystemExit) as excinfo:
        cc_refresh.main(
            ["--auto", "--config", str(auto_env["config_path"])] + extra
        )
    assert excinfo.value.code == 2


def test_release_is_required_without_auto_or_discover_only(auto_env):
    with pytest.raises(SystemExit) as excinfo:
        cc_refresh.main(["--config", str(auto_env["config_path"])])
    assert excinfo.value.code == 2


def test_discover_only_prints_the_newest_release(auto_env, monkeypatch, capsys):
    monkeypatch.setattr(
        cc_refresh, "discover_latest_release",
        lambda *_a, **_kw: "cc-main-2026-jun-jul-aug",
    )
    rc = cc_refresh.main(
        ["--discover-only", "--config", str(auto_env["config_path"])]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == (
        "cc-main-2026-jun-jul-aug"
    )


def test_discover_only_exits_one_when_nothing_is_available(auto_env, monkeypatch):
    monkeypatch.setattr(cc_refresh, "discover_latest_release", lambda *_a, **_kw: None)
    assert cc_refresh.main(
        ["--discover-only", "--config", str(auto_env["config_path"])]
    ) == 1


def test_install_flag_verifies_before_swapping_on_the_manual_path(
    auto_env, monkeypatch,
):
    """--release --install is held to the same gate as --auto: a failed
    verification means exit 1 and no config swap."""
    import json as _json

    def boom(release, config, **_kw):
        raise cc_refresh.VerificationError("canary_present", "marketglow.com absent")

    monkeypatch.setattr(cc_refresh, "verify_derived_release", boom)

    rc = cc_refresh.main([
        "--release", "cc-main-2026-jun-jul-aug", "--install",
        "--config", str(auto_env["config_path"]),
    ])
    assert rc == 1
    untouched = _json.loads(auto_env["config_path"].read_text(encoding="utf-8"))
    assert untouched["cc_backlinks"]["latest_release"] == "cc-main-2026-feb-mar-apr"


def test_manual_run_without_install_does_not_touch_config(auto_env):
    """Bit-for-bit unchanged manual path: build phases only, no swap."""
    before = auto_env["config_path"].read_bytes()
    rc = cc_refresh.main([
        "--release", "cc-main-2026-jun-jul-aug",
        "--config", str(auto_env["config_path"]),
    ])
    assert rc == 0
    assert auto_env["config_path"].read_bytes() == before
    assert auto_env["phases"] == [
        ("raw", "cc-main-2026-jun-jul-aug"), ("derived", "cc-main-2026-jun-jul-aug"),
    ]


def test_install_is_rejected_with_download_only(auto_env):
    """--download-only never builds a derived SQLite, so there is nothing for
    --install to verify."""
    with pytest.raises(SystemExit) as excinfo:
        cc_refresh.main([
            "--release", "cc-main-2026-jun-jul-aug", "--install", "--download-only",
            "--config", str(auto_env["config_path"]),
        ])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# History window: local-cache retention and pre-warming
#
# `history_window_releases` and `ensure_history_cached` live in
# scripts/enrichment/cc_backlinks.py and are MOCKED here (raising=False, so
# these tests describe the contract rather than the implementation). The
# cache directory is always redirected to tmp_path via the env var
# cc_backlinks._resolve_cache_dir honours — never the operator's real
# ~/.cache/domainsifter/cc/.
# ---------------------------------------------------------------------------


_WINDOW = [
    "cc-main-2026-jun-jul-aug",
    "cc-main-2026-may-jun-jul",
    "cc-main-2026-apr-may-jun",
]


def _history_enabled_config(**history_overrides) -> dict:
    """_refresh_config() plus an enabled cc_backlinks.history block."""
    config = _refresh_config()
    history = {"enabled": True, "max_releases": 6, "prewarm_on_refresh": True}
    history.update(history_overrides)
    config["cc_backlinks"]["history"] = history
    return config


def _seed_cache(tmp_path: Path, monkeypatch, *releases: str) -> Path:
    """A fake local cache directory holding one *.sqlite per release."""
    cache = tmp_path / "cc"
    cache.mkdir(exist_ok=True)
    for release in releases:
        (cache / f"{release}.sqlite").write_bytes(b"x")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache))
    return cache


def _patch_window(monkeypatch, window):
    """Mock cc_backlinks.history_window_releases. `window` may be a list or a
    callable (use a raising callable to simulate a lookup failure)."""
    from scripts.enrichment import cc_backlinks

    calls: list[dict] = []

    def fake(config, *, s3_client=None, bucket=None):
        calls.append({"s3_client": s3_client, "bucket": bucket})
        if callable(window):
            return window()
        return list(window)

    monkeypatch.setattr(
        cc_backlinks, "history_window_releases", fake, raising=False,
    )
    return calls


def _patch_ensure_cached(monkeypatch, cached):
    """Mock cc_backlinks.ensure_history_cached. `cached` may be a list or a
    callable (raise from it to simulate a total pre-warm failure)."""
    from scripts.enrichment import cc_backlinks

    calls: list[dict] = []

    def fake(config, *, s3_client=None, bucket=None):
        calls.append({"s3_client": s3_client, "bucket": bucket})
        if callable(cached):
            return cached()
        return list(cached)

    monkeypatch.setattr(
        cc_backlinks, "ensure_history_cached", fake, raising=False,
    )
    return calls


def test_prune_local_cache_keeps_the_whole_history_window(tmp_path, monkeypatch):
    """The latent bug this fixes: pruning to the active release alone would
    delete the archive cache on every install and force a ~30 GB re-download
    inside the next daily run."""
    cache = _seed_cache(
        tmp_path, monkeypatch,
        *_WINDOW, "cc-main-2026-feb-mar-apr", "cc-main-2026-jan-feb-mar",
    )
    _patch_window(monkeypatch, _WINDOW)

    removed = cc_refresh.prune_local_cache(
        _history_enabled_config(), "cc-main-2026-jun-jul-aug",
    )

    assert sorted(removed) == [
        "cc-main-2026-feb-mar-apr.sqlite", "cc-main-2026-jan-feb-mar.sqlite",
    ]
    for release in _WINDOW:
        assert (cache / f"{release}.sqlite").exists()


def test_prune_local_cache_keeps_the_active_release_even_if_the_window_omits_it(
    tmp_path, monkeypatch,
):
    """keep_release is protected independently of the window — a window that
    has not yet noticed the new release must not get it deleted."""
    cache = _seed_cache(
        tmp_path, monkeypatch, "cc-main-2026-jul-aug-sep", *_WINDOW,
    )
    _patch_window(monkeypatch, _WINDOW)

    removed = cc_refresh.prune_local_cache(
        _history_enabled_config(), "cc-main-2026-jul-aug-sep",
    )

    assert removed == []
    assert (cache / "cc-main-2026-jul-aug-sep.sqlite").exists()


def test_prune_local_cache_keeps_everything_when_the_window_lookup_raises(
    tmp_path, monkeypatch, caplog,
):
    """Fail in the SAFE direction: a wrong deletion costs a multi-GB inline
    re-download, a skipped deletion costs some disk."""
    cache = _seed_cache(
        tmp_path, monkeypatch,
        "cc-main-2026-jun-jul-aug", "cc-main-2026-jan-feb-mar",
    )

    def boom():
        raise RuntimeError("R2 ListObjectsV2 timed out")

    _patch_window(monkeypatch, boom)

    caplog.set_level("WARNING")
    removed = cc_refresh.prune_local_cache(
        _history_enabled_config(), "cc-main-2026-jun-jul-aug",
    )

    assert removed == []
    assert (cache / "cc-main-2026-jan-feb-mar.sqlite").exists()
    assert "history window" in caplog.text.lower()


def test_prune_local_cache_keeps_everything_when_the_window_is_empty(
    tmp_path, monkeypatch,
):
    """An empty window is 'we do not know', not 'delete the archive'."""
    cache = _seed_cache(
        tmp_path, monkeypatch,
        "cc-main-2026-jun-jul-aug", "cc-main-2026-jan-feb-mar",
    )
    _patch_window(monkeypatch, [])

    assert cc_refresh.prune_local_cache(
        _history_enabled_config(), "cc-main-2026-jun-jul-aug",
    ) == []
    assert (cache / "cc-main-2026-jan-feb-mar.sqlite").exists()


def test_prune_local_cache_keeps_everything_when_the_enricher_has_no_window_fn(
    tmp_path, monkeypatch,
):
    """If the history helper is missing entirely (an older enricher), the
    pruner still must not delete the archive."""
    from scripts.enrichment import cc_backlinks

    cache = _seed_cache(
        tmp_path, monkeypatch,
        "cc-main-2026-jun-jul-aug", "cc-main-2026-jan-feb-mar",
    )
    monkeypatch.delattr(cc_backlinks, "history_window_releases", raising=False)

    assert cc_refresh.prune_local_cache(
        _history_enabled_config(), "cc-main-2026-jun-jul-aug",
    ) == []
    assert (cache / "cc-main-2026-jan-feb-mar.sqlite").exists()


def test_prune_local_cache_collapses_to_active_release_when_history_disabled(
    tmp_path, monkeypatch,
):
    """Turning the feature off must also reclaim the disk — and must not even
    ask for the window."""
    cache = _seed_cache(
        tmp_path, monkeypatch, *_WINDOW, "cc-main-2026-jan-feb-mar",
    )

    def must_not_ask():
        raise AssertionError("asked for the window despite history.enabled=false")

    _patch_window(monkeypatch, must_not_ask)

    removed = cc_refresh.prune_local_cache(
        _history_enabled_config(enabled=False), "cc-main-2026-jun-jul-aug",
    )

    assert sorted(removed) == [
        "cc-main-2026-apr-may-jun.sqlite",
        "cc-main-2026-jan-feb-mar.sqlite",
        "cc-main-2026-may-jun-jul.sqlite",
    ]
    assert (cache / "cc-main-2026-jun-jul-aug.sqlite").exists()


def test_prune_local_cache_still_respects_the_prune_flag_with_history_on(
    tmp_path, monkeypatch,
):
    cache = _seed_cache(tmp_path, monkeypatch, "cc-main-2026-jan-feb-mar")
    config = _history_enabled_config()
    config["cc_backlinks"]["refresh"]["prune_local_cache"] = False
    _patch_window(monkeypatch, _WINDOW)

    assert cc_refresh.prune_local_cache(
        config, "cc-main-2026-jun-jul-aug",
    ) == []
    assert (cache / "cc-main-2026-jan-feb-mar.sqlite").exists()


def test_prune_local_cache_forwards_the_injected_r2_client_to_the_window(
    tmp_path, monkeypatch,
):
    """One client for the whole post-install sequence — the pruner must not
    build a second one behind our back."""
    _seed_cache(tmp_path, monkeypatch, "cc-main-2026-jun-jul-aug")
    calls = _patch_window(monkeypatch, _WINDOW)
    sentinel = object()

    cc_refresh.prune_local_cache(
        _history_enabled_config(), "cc-main-2026-jun-jul-aug",
        s3_client=sentinel, bucket="test-bucket",
    )

    assert calls == [{"s3_client": sentinel, "bucket": "test-bucket"}]


def test_prune_local_cache_only_touches_sqlite_files(tmp_path, monkeypatch):
    cache = _seed_cache(tmp_path, monkeypatch, "cc-main-2026-jan-feb-mar")
    (cache / "notes.txt").write_bytes(b"x")
    _patch_window(monkeypatch, _WINDOW)

    removed = cc_refresh.prune_local_cache(
        _history_enabled_config(), "cc-main-2026-jun-jul-aug",
    )
    assert removed == ["cc-main-2026-jan-feb-mar.sqlite"]
    assert (cache / "notes.txt").exists()


def test_prune_local_cache_distrusts_a_single_release_window(tmp_path, monkeypatch):
    """`history_window_releases` returns [latest_release] when its own R2
    listing fails — indistinguishable from a healthy one-release window. With
    max_releases > 1 that has to count as 'unknown', or one transient listing
    error costs the whole archive cache."""
    cache = _seed_cache(
        tmp_path, monkeypatch, "cc-main-2026-jun-jul-aug", *_WINDOW[1:],
    )
    _patch_window(monkeypatch, ["cc-main-2026-jun-jul-aug"])

    assert cc_refresh.prune_local_cache(
        _history_enabled_config(max_releases=6), "cc-main-2026-jun-jul-aug",
    ) == []
    for release in _WINDOW:
        assert (cache / f"{release}.sqlite").exists()


def test_prune_local_cache_trusts_a_single_release_window_when_that_is_the_cap(
    tmp_path, monkeypatch,
):
    """max_releases == 1 means one release really is the whole window, so the
    prune goes ahead and reclaims the disk."""
    cache = _seed_cache(
        tmp_path, monkeypatch, "cc-main-2026-jun-jul-aug", "cc-main-2026-jan-feb-mar",
    )
    _patch_window(monkeypatch, ["cc-main-2026-jun-jul-aug"])

    assert cc_refresh.prune_local_cache(
        _history_enabled_config(max_releases=1), "cc-main-2026-jun-jul-aug",
    ) == ["cc-main-2026-jan-feb-mar.sqlite"]
    assert (cache / "cc-main-2026-jun-jul-aug.sqlite").exists()


def test_prune_local_cache_trusts_a_short_but_plural_window(tmp_path, monkeypatch):
    """A window shorter than the cap is normal while R2 is still filling up —
    only the degenerate one-entry case is treated as a failure signal."""
    cache = _seed_cache(
        tmp_path, monkeypatch, *_WINDOW[:2], "cc-main-2026-jan-feb-mar",
    )
    _patch_window(monkeypatch, _WINDOW[:2])

    assert cc_refresh.prune_local_cache(
        _history_enabled_config(max_releases=6), "cc-main-2026-jun-jul-aug",
    ) == ["cc-main-2026-jan-feb-mar.sqlite"]
    assert (cache / "cc-main-2026-may-jun-jul.sqlite").exists()


# ---------------------------------------------------------------------------
# prewarm_history_window — the fail-soft wrapper
# ---------------------------------------------------------------------------


def test_prewarm_history_window_returns_what_was_cached(monkeypatch):
    calls = _patch_ensure_cached(monkeypatch, _WINDOW)
    assert cc_refresh.prewarm_history_window(
        _history_enabled_config(), s3_client="s3", bucket="b",
    ) == _WINDOW
    assert calls == [{"s3_client": "s3", "bucket": "b"}]


def test_prewarm_history_window_never_raises(monkeypatch, caplog):
    def boom():
        raise RuntimeError("R2 refused the connection")

    _patch_ensure_cached(monkeypatch, boom)
    caplog.set_level("WARNING")
    assert cc_refresh.prewarm_history_window(_history_enabled_config()) == []
    assert "pre-warm failed" in caplog.text.lower()


def test_prewarm_after_install_is_gated_on_prewarm_on_refresh(monkeypatch):
    def must_not_run():
        raise AssertionError("pre-warmed despite prewarm_on_refresh=false")

    _patch_ensure_cached(monkeypatch, must_not_run)
    assert cc_refresh._prewarm_after_install(
        _history_enabled_config(prewarm_on_refresh=False),
    ) == []


def test_prewarm_after_install_is_gated_on_history_enabled(monkeypatch):
    def must_not_run():
        raise AssertionError("pre-warmed despite history.enabled=false")

    _patch_ensure_cached(monkeypatch, must_not_run)
    assert cc_refresh._prewarm_after_install(
        _history_enabled_config(enabled=False),
    ) == []


def test_prewarm_after_install_runs_when_both_flags_are_on(monkeypatch):
    _patch_ensure_cached(monkeypatch, _WINDOW)
    assert cc_refresh._prewarm_after_install(
        _history_enabled_config(), s3_client="s3", bucket="b",
    ) == _WINDOW


# ---------------------------------------------------------------------------
# --auto: the install sequence now pre-warms before it prunes
# ---------------------------------------------------------------------------


def _enable_history_in_config_file(config_path: Path, **overrides) -> None:
    """Add a cc_backlinks.history block to the on-disk config the CLI reads."""
    import json as _json

    data = _json.loads(config_path.read_text(encoding="utf-8"))
    history = {"enabled": True, "max_releases": 6, "prewarm_on_refresh": True}
    history.update(overrides)
    data["cc_backlinks"]["history"] = history
    with open(config_path, "w", encoding="utf-8", newline="") as fh:
        _json.dump(data, fh, indent=2)
        fh.write("\n")


def _stub_install_path(monkeypatch, new_release: str) -> None:
    """Discovery + verification stubbed so --auto reaches the install."""
    monkeypatch.setattr(
        cc_refresh, "discover_latest_release", lambda *_a, **_kw: new_release,
    )
    monkeypatch.setattr(
        cc_refresh, "verify_derived_release",
        lambda release, config, **_kw: {
            "release": release, "rows": 119_722_885,
            "canaries": {"marketglow.com": 16_365_926}, "meta": {},
        },
    )
    monkeypatch.setattr(cc_refresh, "prune_raw_releases", lambda *_a, **_kw: [])


def test_auto_prewarms_the_window_after_a_successful_install(
    auto_env, monkeypatch,
):
    new_release = "cc-main-2026-jun-jul-aug"
    _enable_history_in_config_file(auto_env["config_path"])
    _stub_install_path(monkeypatch, new_release)
    _patch_ensure_cached(monkeypatch, _WINDOW)
    monkeypatch.setattr(cc_refresh, "prune_local_cache", lambda *_a, **_kw: [])

    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 0

    payload = _read_result(auto_env["result_path"])
    assert payload["action"] == "installed"
    assert payload["prewarmed_releases"] == _WINDOW
    # The keys run-cc-refresh.sh and send_report.py parse are untouched.
    for key in ("action", "release", "previous_release", "rows",
                "pruned_raw_releases", "pruned_local_cache"):
        assert key in payload


def test_auto_does_not_prewarm_when_prewarm_on_refresh_is_false(
    auto_env, monkeypatch,
):
    new_release = "cc-main-2026-jun-jul-aug"
    _enable_history_in_config_file(auto_env["config_path"], prewarm_on_refresh=False)
    _stub_install_path(monkeypatch, new_release)

    def must_not_run():
        raise AssertionError("pre-warmed despite prewarm_on_refresh=false")

    _patch_ensure_cached(monkeypatch, must_not_run)
    monkeypatch.setattr(cc_refresh, "prune_local_cache", lambda *_a, **_kw: [])

    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 0
    assert _read_result(auto_env["result_path"])["prewarmed_releases"] == []


def test_auto_prewarm_failure_leaves_the_install_successful(auto_env, monkeypatch):
    """The release is verified and installed by then; a cold cache only costs
    time on the next run, so the exit code stays 0 and the swap stands."""
    import json as _json

    new_release = "cc-main-2026-jun-jul-aug"
    _enable_history_in_config_file(auto_env["config_path"])
    _stub_install_path(monkeypatch, new_release)

    def boom():
        raise RuntimeError("R2 download died at byte 12")

    _patch_ensure_cached(monkeypatch, boom)
    monkeypatch.setattr(cc_refresh, "prune_local_cache", lambda *_a, **_kw: [])

    rc = cc_refresh.main(["--auto", "--config", str(auto_env["config_path"])])
    assert rc == 0

    payload = _read_result(auto_env["result_path"])
    assert payload["action"] == "installed"
    assert payload["prewarmed_releases"] == []
    installed = _json.loads(auto_env["config_path"].read_text(encoding="utf-8"))
    assert installed["cc_backlinks"]["latest_release"] == new_release


def test_auto_prewarms_before_it_prunes_the_local_cache(auto_env, monkeypatch):
    """Order is load-bearing: prune must see the files the window just
    downloaded rather than racing the download."""
    order: list[str] = []
    new_release = "cc-main-2026-jun-jul-aug"
    _enable_history_in_config_file(auto_env["config_path"])
    _stub_install_path(monkeypatch, new_release)

    _patch_ensure_cached(monkeypatch, lambda: order.append("prewarm") or list(_WINDOW))
    monkeypatch.setattr(
        cc_refresh, "prune_local_cache",
        lambda *_a, **_kw: order.append("prune") or [],
    )

    assert cc_refresh.main(
        ["--auto", "--config", str(auto_env["config_path"])]
    ) == 0
    assert order == ["prewarm", "prune"]


# ---------------------------------------------------------------------------
# --prewarm-history: cache the window and exit, nothing else
# ---------------------------------------------------------------------------


def test_prewarm_history_flag_caches_the_window_and_exits_zero(
    auto_env, monkeypatch, caplog,
):
    _enable_history_in_config_file(auto_env["config_path"])
    _patch_window(monkeypatch, _WINDOW)
    _patch_ensure_cached(monkeypatch, _WINDOW[:2])

    caplog.set_level("INFO")
    rc = cc_refresh.main(
        ["--prewarm-history", "--config", str(auto_env["config_path"])]
    )
    assert rc == 0
    # Reports what it cached and what it skipped.
    assert "cc-main-2026-jun-jul-aug" in caplog.text
    assert "Skipped (1): cc-main-2026-apr-may-jun" in caplog.text


def test_prewarm_history_flag_does_no_discovery_build_or_config_write(
    auto_env, monkeypatch,
):
    before = auto_env["config_path"].read_bytes()
    _enable_history_in_config_file(auto_env["config_path"])
    after_enable = auto_env["config_path"].read_bytes()
    assert before != after_enable  # sanity: the helper really wrote something

    def must_not_run(*_a, **_kw):
        raise AssertionError("--prewarm-history did more than pre-warm")

    monkeypatch.setattr(cc_refresh, "discover_latest_release", must_not_run)
    monkeypatch.setattr(cc_refresh, "_phase_download_and_upload_raw", must_not_run)
    monkeypatch.setattr(cc_refresh, "_phase_build_and_upload_derived", must_not_run)
    monkeypatch.setattr(cc_refresh, "install_release", must_not_run)
    monkeypatch.setattr(cc_refresh, "prune_raw_releases", must_not_run)
    monkeypatch.setattr(cc_refresh, "prune_local_cache", must_not_run)
    monkeypatch.setattr(cc_refresh, "write_result", must_not_run)
    _patch_window(monkeypatch, _WINDOW)
    _patch_ensure_cached(monkeypatch, _WINDOW)

    assert cc_refresh.main(
        ["--prewarm-history", "--config", str(auto_env["config_path"])]
    ) == 0
    assert auto_env["config_path"].read_bytes() == after_enable
    assert auto_env["phases"] == []


def test_prewarm_history_flag_exits_one_when_it_could_cache_nothing(
    auto_env, monkeypatch,
):
    _enable_history_in_config_file(auto_env["config_path"])
    _patch_window(monkeypatch, _WINDOW)
    _patch_ensure_cached(monkeypatch, [])

    assert cc_refresh.main(
        ["--prewarm-history", "--config", str(auto_env["config_path"])]
    ) == 1


def test_prewarm_history_flag_exits_zero_on_a_partial_window(auto_env, monkeypatch):
    """One missing release still leaves the box warmer than it was."""
    _enable_history_in_config_file(auto_env["config_path"])
    _patch_window(monkeypatch, _WINDOW)
    _patch_ensure_cached(monkeypatch, [_WINDOW[0]])

    assert cc_refresh.main(
        ["--prewarm-history", "--config", str(auto_env["config_path"])]
    ) == 0


def test_prewarm_history_flag_exits_one_when_history_is_disabled(
    auto_env, monkeypatch,
):
    _enable_history_in_config_file(auto_env["config_path"], enabled=False)

    def must_not_run():
        raise AssertionError("pre-warmed despite history.enabled=false")

    _patch_ensure_cached(monkeypatch, must_not_run)

    assert cc_refresh.main(
        ["--prewarm-history", "--config", str(auto_env["config_path"])]
    ) == 1


def test_prewarm_history_flag_exits_one_when_the_enricher_blows_up(
    auto_env, monkeypatch,
):
    _enable_history_in_config_file(auto_env["config_path"])
    _patch_window(monkeypatch, _WINDOW)

    def boom():
        raise RuntimeError("R2 credentials rejected")

    _patch_ensure_cached(monkeypatch, boom)

    assert cc_refresh.main(
        ["--prewarm-history", "--config", str(auto_env["config_path"])]
    ) == 1


def test_prewarm_history_flag_still_caches_when_the_window_is_unknowable(
    auto_env, monkeypatch,
):
    """A failed window listing must not stop the pre-warm itself — the
    enricher's own answer is what counts."""
    _enable_history_in_config_file(auto_env["config_path"])

    def boom():
        raise RuntimeError("ListObjectsV2 timed out")

    _patch_window(monkeypatch, boom)
    _patch_ensure_cached(monkeypatch, _WINDOW)

    assert cc_refresh.main(
        ["--prewarm-history", "--config", str(auto_env["config_path"])]
    ) == 0


@pytest.mark.parametrize("extra", [
    ["--auto"],
    ["--release", "cc-main-2026-jun-jul-aug"],
    ["--download-only", "--release", "cc-main-2026-jun-jul-aug"],
    ["--build-only", "--release", "cc-main-2026-jun-jul-aug"],
    ["--install", "--release", "cc-main-2026-jun-jul-aug"],
    ["--discover-only"],
])
def test_prewarm_history_is_mutually_exclusive_with_every_other_mode(
    auto_env, extra,
):
    with pytest.raises(SystemExit) as excinfo:
        cc_refresh.main(
            ["--prewarm-history", "--config", str(auto_env["config_path"])] + extra
        )
    assert excinfo.value.code == 2


def test_install_drops_the_history_window_cache_before_prewarming(
    auto_env, monkeypatch,
):
    """The enricher caches the window keyed on config's latest_release, and
    our in-memory config still holds the OLD value after the swap. Dropping
    the cache between the swap and the pre-warm guarantees that any window
    computed earlier in this process — possibly before the new derived object
    reached R2 — cannot decide what gets pre-warmed or kept."""
    from scripts.enrichment import cc_backlinks

    order: list[str] = []
    new_release = "cc-main-2026-jun-jul-aug"

    monkeypatch.setattr(
        cc_backlinks, "reset_history_caches",
        lambda: order.append("reset"), raising=False,
    )
    monkeypatch.setattr(
        cc_refresh, "discover_latest_release", lambda *_a, **_kw: new_release,
    )
    monkeypatch.setattr(
        cc_refresh, "verify_derived_release",
        lambda release, config, **_kw: {
            "release": release, "rows": 1, "canaries": {}, "meta": {},
        },
    )
    monkeypatch.setattr(
        cc_refresh, "install_release",
        lambda *_a, **_kw: order.append("install") or "cc-main-2026-feb-mar-apr",
    )
    monkeypatch.setattr(
        cc_refresh, "_prewarm_after_install",
        lambda *_a, **_kw: order.append("prewarm") or [],
    )
    monkeypatch.setattr(cc_refresh, "prune_raw_releases", lambda *_a, **_kw: [])
    monkeypatch.setattr(cc_refresh, "prune_local_cache", lambda *_a, **_kw: [])

    assert cc_refresh.main(
        ["--auto", "--config", str(auto_env["config_path"])]
    ) == 0
    assert order == ["install", "reset", "prewarm"]


def test_a_failing_history_cache_reset_never_breaks_the_install(monkeypatch):
    """Bookkeeping after the swap must not be able to undo a good install."""
    from scripts.enrichment import cc_backlinks

    def boom():
        raise RuntimeError("synthetic")

    monkeypatch.setattr(
        cc_backlinks, "reset_history_caches", boom, raising=False,
    )
    cc_refresh._reset_history_window_cache()  # must not raise


def test_install_points_the_in_memory_config_at_the_new_release(monkeypatch):
    """Dropping the window cache is necessary but not sufficient: a window
    recomputed under a stale `latest_release` is still headed by the release
    we just superseded."""
    from scripts.enrichment import cc_backlinks

    monkeypatch.setattr(
        cc_backlinks, "reset_history_caches", lambda: None, raising=False,
    )
    config = _history_enabled_config()
    assert config["cc_backlinks"]["latest_release"] == "cc-main-2026-feb-mar-apr"

    cc_refresh._sync_installed_release(config, "cc-main-2026-jun-jul-aug")

    assert config["cc_backlinks"]["latest_release"] == "cc-main-2026-jun-jul-aug"


def test_auto_prewarms_and_prunes_against_the_newly_installed_window_head(
    auto_env, monkeypatch, tmp_path,
):
    """The case that actually distinguishes right from wrong: max_releases 2
    with three releases discoverable. Under the stale-config bug the window
    would be [feb-mar-apr (superseded), jun-jul-aug], so the pre-warm would
    fetch the wrong file and the keep-set would protect the release we just
    replaced while deleting one the window wants. At the default cap of 6 the
    window holds everything either way and proves nothing."""
    from scripts.enrichment import cc_backlinks

    new_release = "cc-main-2026-jun-jul-aug"
    second = "cc-main-2026-may-jun-jul"
    superseded = "cc-main-2026-feb-mar-apr"
    discovered = [new_release, second, superseded]

    _enable_history_in_config_file(auto_env["config_path"], max_releases=2)
    _stub_install_path(monkeypatch, new_release)
    cache = _seed_cache(tmp_path, monkeypatch, *discovered)

    order: list[str] = []

    def fake_window(config, *, s3_client=None, bucket=None):
        """Mirrors the enricher's contract: latest_release is always the head,
        then the rest newest-first, truncated to max_releases."""
        cc = config["cc_backlinks"]
        latest = cc["latest_release"]
        rest = [r for r in discovered if r != latest]
        return ([latest] + rest)[: int(cc["history"]["max_releases"])]

    def fake_ensure(config, *, s3_client=None, bucket=None):
        order.append("prewarm")
        return fake_window(config, s3_client=s3_client, bucket=bucket)

    monkeypatch.setattr(
        cc_backlinks, "history_window_releases", fake_window, raising=False,
    )
    monkeypatch.setattr(
        cc_backlinks, "ensure_history_cached", fake_ensure, raising=False,
    )
    monkeypatch.setattr(
        cc_backlinks, "reset_history_caches",
        lambda: order.append("reset"), raising=False,
    )

    assert cc_refresh.main(
        ["--auto", "--config", str(auto_env["config_path"])]
    ) == 0

    payload = _read_result(auto_env["result_path"])
    assert payload["prewarmed_releases"] == [new_release, second]
    assert payload["pruned_local_cache"] == [f"{superseded}.sqlite"]
    assert (cache / f"{new_release}.sqlite").exists()
    assert (cache / f"{second}.sqlite").exists()
    assert not (cache / f"{superseded}.sqlite").exists()
    # The cache drop happens before anything reads the window.
    assert order[0] == "reset"
