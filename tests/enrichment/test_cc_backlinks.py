"""Unit tests for scripts/enrichment/cc_backlinks.py — the CC backlinks
enricher, wired into the pipeline on 2026-05-14.

Mocks the R2 download. Builds a tiny fixture SQLite with the same schema
the real cc_refresh.py produces and runs `enrich()` against it.

The 2026-09-20 additions cover the display-only multi-release backlink
history: window auto-discovery from R2, chronological (not lexicographic)
ordering, and — the load-bearing guarantee — that no history failure can
perturb `cc_source_domain_count`, which is the only CC field that scores.
All domains here are invented (hard rule 1); nothing touches the network.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.enrichment import cc_backlinks


@pytest.fixture(autouse=True)
def _clear_connection_cache(monkeypatch):
    """Per-process sqlite3.Connection cache lives at module level; clear it
    between tests so each one starts fresh. Same for the history window
    cache and the log-once ledger, which are per-process by design.

    Also unsets CC_BACKLINKS_RELEASE so a developer's shell can't decide
    which release a test resolves.
    """
    monkeypatch.delenv("CC_BACKLINKS_RELEASE", raising=False)

    def _flush():
        for conn in cc_backlinks._CONNECTION_CACHE.values():
            try:
                conn.close()
            except Exception:
                pass
        cc_backlinks._CONNECTION_CACHE.clear()
        cc_backlinks.reset_history_caches()

    _flush()
    yield
    _flush()


# Canonical CC release names, oldest → newest. Chosen so that sorting them
# lexicographically gives a DIFFERENT (wrong) order than sorting them
# chronologically: lexicographic descending is may > mar > jun > feb > apr.
REL_FEB = "cc-main-2026-feb-mar-apr"
REL_MAR = "cc-main-2026-mar-apr-may"
REL_APR = "cc-main-2026-apr-may-jun"
REL_MAY = "cc-main-2026-may-jun-jul"
REL_JUN = "cc-main-2026-jun-jul-aug"   # latest_release in these fixtures
ALL_RELEASES = [REL_FEB, REL_MAR, REL_APR, REL_MAY, REL_JUN]


def _make_fixture_sqlite(path: Path, rows: list[tuple[str, int]] | None = None) -> None:
    """Mirror the schema scripts/cc_refresh.py produces, with a handful of
    fixture rows including a dangler at count=0."""
    if rows is None:
        rows = [
            ("example1.com", 1),
            ("example2.com", 2),
            ("example3.org", 1),
            ("dangling.com", 0),         # in graph, no inbound
            ("example4.co.uk", 1),
            ("popularsite.com", 42),     # higher count for variance
        ]
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("release", path.stem),
                ("built_at", "2026-05-13T10:00:00Z"),
                ("schema_version", "1"),
            ],
        )
        con.execute(
            "CREATE TABLE cc_apex (apex_domain TEXT PRIMARY KEY, "
            "source_domain_count INTEGER NOT NULL)"
        )
        con.executemany("INSERT INTO cc_apex VALUES (?, ?)", rows)
        con.execute("CREATE INDEX idx_cc_apex_domain ON cc_apex(apex_domain)")
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# History fixtures — fake R2 listing + on-disk archive cache
# ---------------------------------------------------------------------------


def _history_config(
    *,
    latest: str = REL_JUN,
    enabled: bool = True,
    max_releases: int = 6,
    key_template: str = "cc/derived/{release}.sqlite",
) -> dict:
    return {
        "cc_backlinks": {
            "latest_release": latest,
            "r2_derived_key_template": key_template,
            "history": {"enabled": enabled, "max_releases": max_releases},
        }
    }


def _fake_s3(keys: list[str]) -> MagicMock:
    """An R2 client whose list_objects_v2 serves `keys` under any prefix,
    one un-truncated page (pagination itself is cc_refresh._list_keys'
    tested concern)."""
    s3 = MagicMock()

    def _list(**kwargs):
        prefix = kwargs.get("Prefix", "")
        return {
            "Contents": [{"Key": k} for k in keys if k.startswith(prefix)],
            "IsTruncated": False,
        }

    s3.list_objects_v2.side_effect = _list
    return s3


def _derived_keys(releases: list[str], template: str = "cc/derived/{release}.sqlite") -> list[str]:
    return [template.format(release=r) for r in releases]


def _patch_r2(monkeypatch, s3: MagicMock, bucket: str = "test-bucket") -> None:
    """Point the module's lazy `from scripts import diff` R2 lookup at a
    fake client, for the code paths (enrich) that take no injection."""
    from scripts import diff as diff_mod

    monkeypatch.setattr(diff_mod, "_r2_client", lambda: s3)
    monkeypatch.setattr(diff_mod, "_bucket", lambda: bucket)


def _seed_cache(cache_dir: Path, counts: dict[str, dict[str, int]]) -> None:
    """Build one fixture SQLite per release in `counts`.

    counts maps release name → {apex: source_domain_count}. A release
    omitted from the mapping is simply absent from the local cache, which
    is how a not-yet-pre-warmed archive release behaves.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    for release, rows in counts.items():
        _make_fixture_sqlite(
            cache_dir / f"{release}.sqlite", rows=list(rows.items()),
        )


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


# ---------------------------------------------------------------------------
# history_window_releases() — auto-discovery of the release window
# ---------------------------------------------------------------------------


def test_history_window_sorts_chronologically_not_lexicographically():
    """The whole point of reusing cc_refresh._release_sort_key. Sorting
    these names as strings would put may-jun-jul ahead of jun-jul-aug and
    mar-apr-may ahead of apr-may-jun — i.e. a wrong 'latest'."""
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    window = cc_backlinks.history_window_releases(
        _history_config(), s3_client=s3, bucket="b",
    )
    assert window == [REL_JUN, REL_MAY, REL_APR, REL_MAR, REL_FEB]
    # Guard the negative case explicitly: string sorting gives another order.
    assert window != sorted(ALL_RELEASES, reverse=True)


def test_history_window_skips_unparseable_release_names():
    """Anything cc_refresh._parse_release won't vouch for is left out —
    non-canonical month order, junk, nested keys and wrong suffixes."""
    keys = _derived_keys([REL_JUN, REL_MAY]) + [
        "cc/derived/cc-main-2026-jan-mar-feb.sqlite",  # months out of order
        "cc/derived/totally-not-a-release.sqlite",
        "cc/derived/nested/cc-main-2026-mar-apr-may.sqlite",
        "cc/derived/cc-main-2026-apr-may-jun.sqlite.tmp",  # wrong suffix
    ]
    window = cc_backlinks.history_window_releases(
        _history_config(), s3_client=_fake_s3(keys), bucket="b",
    )
    assert window == [REL_JUN, REL_MAY]


def test_history_window_truncates_to_max_releases():
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    window = cc_backlinks.history_window_releases(
        _history_config(max_releases=3), s3_client=s3, bucket="b",
    )
    assert window == [REL_JUN, REL_MAY, REL_APR]


def test_history_window_always_leads_with_configured_latest_release():
    """latest_release is the release whose count is the SCORED field, so it
    must head the window even if the R2 listing somehow doesn't show it
    (eventual consistency, a template change, a half-finished upload)."""
    s3 = _fake_s3(_derived_keys([REL_MAY, REL_APR, REL_MAR]))
    window = cc_backlinks.history_window_releases(
        _history_config(), s3_client=s3, bucket="b",
    )
    assert window[0] == REL_JUN
    assert window == [REL_JUN, REL_MAY, REL_APR, REL_MAR]


def test_history_window_never_duplicates_latest_release():
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    window = cc_backlinks.history_window_releases(
        _history_config(), s3_client=s3, bucket="b",
    )
    assert window.count(REL_JUN) == 1


def test_history_window_falls_back_to_latest_on_listing_error():
    """R2 down / credentials wrong / bucket missing: warn and report the
    configured release alone. Never raises — hard rule 11."""
    s3 = MagicMock()
    s3.list_objects_v2.side_effect = RuntimeError("R2 unavailable")
    window = cc_backlinks.history_window_releases(
        _history_config(), s3_client=s3, bucket="b",
    )
    assert window == [REL_JUN]


def test_history_window_falls_back_on_template_without_placeholder():
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    config = _history_config(key_template="cc/derived/fixed.sqlite")
    window = cc_backlinks.history_window_releases(
        config, s3_client=s3, bucket="b",
    )
    assert window == [REL_JUN]


def test_history_window_honours_custom_key_template():
    """Names are derived from the configured template, not from a second
    hardcoded copy of `cc/derived/{release}.sqlite`."""
    template = "graphs/v2/{release}/apex.sqlite"
    s3 = _fake_s3(_derived_keys([REL_JUN, REL_MAY], template))
    window = cc_backlinks.history_window_releases(
        _history_config(key_template=template), s3_client=s3, bucket="b",
    )
    assert window == [REL_JUN, REL_MAY]


def test_history_window_is_cached_once_per_process():
    """Performance contract: one R2 list call per run, not one per
    candidate. ~2,500 candidates a day makes the difference material."""
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    config = _history_config()
    first = cc_backlinks.history_window_releases(config, s3_client=s3, bucket="b")
    second = cc_backlinks.history_window_releases(config, s3_client=s3, bucket="b")
    assert first == second
    assert s3.list_objects_v2.call_count == 1
    # ...and the reset hook tests rely on actually re-listing afterwards.
    cc_backlinks.reset_history_caches()
    cc_backlinks.history_window_releases(config, s3_client=s3, bucket="b")
    assert s3.list_objects_v2.call_count == 2


def test_history_window_returns_caller_owned_copy():
    """A caller mutating the returned list must not corrupt the cache."""
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    config = _history_config()
    window = cc_backlinks.history_window_releases(config, s3_client=s3, bucket="b")
    window.append("cc-main-2026-nonsense")
    assert cc_backlinks.history_window_releases(
        config, s3_client=s3, bucket="b",
    ) == [REL_JUN, REL_MAY, REL_APR, REL_MAR, REL_FEB]


def test_history_window_empty_when_no_release_configured():
    """No release at all → no window (and no R2 call). enrich() bails out
    before this anyway; this keeps ensure_history_cached honest."""
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    assert cc_backlinks.history_window_releases({}, s3_client=s3, bucket="b") == []
    s3.list_objects_v2.assert_not_called()


# ---------------------------------------------------------------------------
# ensure_history_cached() — pre-warming the archive cache
# ---------------------------------------------------------------------------


def test_ensure_history_cached_skips_already_cached_and_downloads_the_rest(
    tmp_path, monkeypatch,
):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {REL_JUN: {"marketglow.com": 247}})
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))

    s3 = _fake_s3(_derived_keys([REL_JUN, REL_MAY, REL_APR]))

    def fake_download(Bucket, Key, Filename):
        Path(Filename).write_bytes(b"downloaded")

    s3.download_file.side_effect = fake_download

    cached = cc_backlinks.ensure_history_cached(
        _history_config(), s3_client=s3, bucket="b",
    )
    assert cached == [REL_JUN, REL_MAY, REL_APR]
    downloaded_keys = {c.kwargs["Key"] for c in s3.download_file.call_args_list}
    assert downloaded_keys == set(_derived_keys([REL_MAY, REL_APR]))


def test_ensure_history_cached_tolerates_one_unavailable_release(
    tmp_path, monkeypatch,
):
    """One release R2 can't serve must not stop the others — the point of
    pre-warming is that the daily run finds as much as possible locally."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))

    s3 = _fake_s3(_derived_keys([REL_JUN, REL_MAY, REL_APR]))

    def fake_download(Bucket, Key, Filename):
        if REL_MAY in Key:
            raise RuntimeError("404 NoSuchKey")
        Path(Filename).write_bytes(b"downloaded")

    s3.download_file.side_effect = fake_download

    cached = cc_backlinks.ensure_history_cached(
        _history_config(), s3_client=s3, bucket="b",
    )
    assert cached == [REL_JUN, REL_APR]


def test_ensure_history_cached_noop_when_history_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(tmp_path / "cache"))
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    assert cc_backlinks.ensure_history_cached(
        _history_config(enabled=False), s3_client=s3, bucket="b",
    ) == []
    s3.download_file.assert_not_called()
    s3.list_objects_v2.assert_not_called()


# ---------------------------------------------------------------------------
# enrich() + cc_backlink_history — DISPLAY ONLY
# ---------------------------------------------------------------------------


def test_enrich_emits_history_newest_first(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {
        REL_JUN: {"marketglow.com": 247},
        REL_MAY: {"marketglow.com": 310},
        REL_APR: {"tideblock.io": 5},        # marketglow absent → None
    })
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    _patch_r2(monkeypatch, _fake_s3(_derived_keys([REL_JUN, REL_MAY, REL_APR])))

    result = cc_backlinks.enrich("marketglow.com", _history_config())

    assert result["cc_source_domain_count"] == 247
    assert result["cc_backlink_history"] == [
        {"release": REL_JUN, "source_domain_count": 247},
        {"release": REL_MAY, "source_domain_count": 310},
        {"release": REL_APR, "source_domain_count": None},
    ]
    # Entry 0 is the scored release, by construction.
    assert (
        result["cc_backlink_history"][0]["source_domain_count"]
        == result["cc_source_domain_count"]
    )


def test_enrich_history_preserves_none_versus_zero(tmp_path, monkeypatch):
    """A dangler (row present, 0 inbound) and an absent apex are DIFFERENT
    facts. Collapsing 0 into None would invent a disappearance that the
    graph never recorded."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {
        REL_JUN: {"coppernest.org": 3},
        REL_MAY: {"coppernest.org": 0},     # in graph, zero inbound
        REL_APR: {"tideblock.io": 9},       # coppernest not in graph at all
    })
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    _patch_r2(monkeypatch, _fake_s3(_derived_keys([REL_JUN, REL_MAY, REL_APR])))

    history = cc_backlinks.enrich("coppernest.org", _history_config())["cc_backlink_history"]
    counts = [e["source_domain_count"] for e in history]
    assert counts == [3, 0, None]
    assert counts[1] is not None


def test_enrich_omits_history_key_entirely_when_disabled(tmp_path, monkeypatch):
    """Disabled means the key is ABSENT, not an empty list — consumers can
    then treat presence as 'history was computed'."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {REL_JUN: {"marketglow.com": 247}})
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    s3 = _fake_s3(_derived_keys(ALL_RELEASES))
    _patch_r2(monkeypatch, s3)

    result = cc_backlinks.enrich("marketglow.com", _history_config(enabled=False))
    assert result == {"cc_source_domain_count": 247}
    s3.list_objects_v2.assert_not_called()


def test_enrich_omits_history_when_apex_absent_from_every_release(
    tmp_path, monkeypatch,
):
    """An all-null history carries no information; the module's documented
    'not in graph' answer stays exactly `{}`."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {
        REL_JUN: {"tideblock.io": 5},
        REL_MAY: {"tideblock.io": 6},
    })
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    _patch_r2(monkeypatch, _fake_s3(_derived_keys([REL_JUN, REL_MAY])))

    assert cc_backlinks.enrich("marketglow.com", _history_config()) == {}


def test_enrich_history_is_null_for_release_missing_from_local_cache(
    tmp_path, monkeypatch,
):
    """enrich() must never trigger a ~6 GB download mid-run: an archive
    release that wasn't pre-warmed degrades to null."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {REL_JUN: {"marketglow.com": 247}})
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    s3 = _fake_s3(_derived_keys([REL_JUN, REL_MAY, REL_APR]))
    _patch_r2(monkeypatch, s3)

    result = cc_backlinks.enrich("marketglow.com", _history_config())
    assert [e["source_domain_count"] for e in result["cc_backlink_history"]] == [
        247, None, None,
    ]
    s3.download_file.assert_not_called()


def test_enrich_warns_once_per_uncached_release_not_once_per_candidate(
    tmp_path, monkeypatch, caplog,
):
    """2,500 candidates × 2 uncached releases would be 5,000 identical
    warnings burying the rest of the run log."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {
        REL_JUN: {"marketglow.com": 247, "tideblock.io": 5, "coppernest.org": 9},
    })
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    _patch_r2(monkeypatch, _fake_s3(_derived_keys([REL_JUN, REL_MAY])))

    config = _history_config()
    with caplog.at_level(logging.WARNING, logger=cc_backlinks.logger.name):
        for name in ("marketglow.com", "tideblock.io", "coppernest.org"):
            cc_backlinks.enrich(name, config)

    uncached_warnings = [
        r for r in caplog.records
        if "no local SQLite" in r.getMessage() and REL_MAY in r.getMessage()
    ]
    assert len(uncached_warnings) == 1


def test_enrich_history_per_release_failure_degrades_only_that_entry(
    tmp_path, monkeypatch,
):
    """A corrupt archive SQLite yields null for its own entry; the other
    releases — including the scored one — are untouched."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {
        REL_JUN: {"marketglow.com": 247},
        REL_APR: {"marketglow.com": 190},
    })
    # REL_MAY exists on disk but is not a database at all.
    (cache_dir / f"{REL_MAY}.sqlite").write_bytes(b"this is not a sqlite file")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    _patch_r2(monkeypatch, _fake_s3(_derived_keys([REL_JUN, REL_MAY, REL_APR])))

    result = cc_backlinks.enrich("marketglow.com", _history_config())
    assert result["cc_source_domain_count"] == 247
    assert [e["source_domain_count"] for e in result["cc_backlink_history"]] == [
        247, None, 190,
    ]


def test_enrich_history_explosion_leaves_cc_source_domain_count_intact(
    tmp_path, monkeypatch,
):
    """THE load-bearing guarantee of this feature. History is display-only:
    if the entire history path detonates, the scored field — and therefore
    the published list and its ordering — must be bit-identical to what the
    pre-history code returned."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {REL_JUN: {"marketglow.com": 247}})
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    _patch_r2(monkeypatch, _fake_s3(_derived_keys(ALL_RELEASES)))

    def _detonate(*args, **kwargs):
        raise RuntimeError("history path exploded")

    monkeypatch.setattr(cc_backlinks, "_backlink_history", _detonate)

    assert cc_backlinks.enrich("marketglow.com", _history_config()) == {
        "cc_source_domain_count": 247,
    }


def test_enrich_history_explosion_preserves_not_in_graph_result(
    tmp_path, monkeypatch,
):
    """Same guarantee at the other end of the three-state distinction: an
    apex that isn't in the graph still answers `{}`."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {REL_JUN: {"tideblock.io": 5}})
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    _patch_r2(monkeypatch, _fake_s3(_derived_keys(ALL_RELEASES)))

    def _detonate(*args, **kwargs):
        raise RuntimeError("history path exploded")

    monkeypatch.setattr(cc_backlinks, "_backlink_history", _detonate)

    assert cc_backlinks.enrich("marketglow.com", _history_config()) == {}


def test_enrich_history_survives_r2_listing_failure(tmp_path, monkeypatch):
    """Window discovery fails → history shrinks to the scored release, and
    the scored value is still there."""
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {REL_JUN: {"marketglow.com": 247}})
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    s3 = MagicMock()
    s3.list_objects_v2.side_effect = RuntimeError("R2 unavailable")
    _patch_r2(monkeypatch, s3)

    result = cc_backlinks.enrich("marketglow.com", _history_config())
    assert result == {
        "cc_source_domain_count": 247,
        "cc_backlink_history": [
            {"release": REL_JUN, "source_domain_count": 247},
        ],
    }


def test_enrich_history_lowercases_the_queried_domain(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, {
        REL_JUN: {"marketglow.com": 247},
        REL_MAY: {"marketglow.com": 310},
    })
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(cache_dir))
    _patch_r2(monkeypatch, _fake_s3(_derived_keys([REL_JUN, REL_MAY])))

    result = cc_backlinks.enrich("MarketGlow.COM", _history_config())
    assert [e["source_domain_count"] for e in result["cc_backlink_history"]] == [247, 310]
