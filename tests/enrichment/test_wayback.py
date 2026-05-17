"""Unit tests for scripts/enrichment/wayback.py.

Mock surface: `WaybackClient` is imported at the top of the module under
test, so each test patches `scripts.enrichment.wayback.WaybackClient` to
return a fake client whose `search()` yields the records the test wants.
This is the right boundary because the production module's contract is
"call WaybackClient.search and translate the result"; the package's HTTP
internals (retries, throttling, 429-handling) are its problem.

Migration note (2026-05-08): replaced the previous `responses`-based HTTP
mocking after swapping to the EDGI `wayback` package. The old fixtures
mocked partial CDX rows (`["timestamp"]` + `["20200101000000"]`); under the
package those rows wouldn't deserialise to CdxRecord. New fixtures mock at
the package boundary instead, which is semantically the correct place for
this kind of test.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from scripts.enrichment import wayback


CONFIG = {
    "api_endpoints": {"wayback_cdx": "https://web.archive.org/cdx/search/cdx"},
    "request_timeout_seconds": 5,
}


def _record(timestamp: datetime) -> object:
    """Minimal CdxRecord-shaped stub. The production code only reads
    `record.timestamp`, so a MagicMock with a timestamp attribute is enough."""
    rec = MagicMock()
    rec.timestamp = timestamp
    return rec


@pytest.fixture(autouse=True)
def _reset_breaker():
    """Each test starts with a fresh breaker. The module-level singleton
    persists across tests in the real run; reset() keeps tests independent."""
    wayback._BREAKER.reset()
    yield
    wayback._BREAKER.reset()


def _patch_client(monkeypatch, search_result):
    """Patch the WaybackClient constructor used inside enrich() so search()
    returns or raises whatever the test specifies. `search_result` may be:
      - a list of CdxRecord-shaped objects (success path)
      - an Exception instance (raised when search() is called)
    """
    fake_client = MagicMock()
    if isinstance(search_result, BaseException):
        fake_client.search.side_effect = search_result
    else:
        fake_client.search.return_value = iter(search_result)

    captured: dict = {}

    def fake_client_cls(*, session=None):
        captured["session"] = session
        return fake_client

    monkeypatch.setattr(wayback, "WaybackClient", fake_client_cls)
    return fake_client, captured


def _patch_session(monkeypatch):
    """Capture WaybackSession construction args so tests can assert how
    config gets translated into session kwargs (timeout, throttle, retries)."""
    captured: dict = {}

    def fake_session_cls(**kwargs):
        captured.update(kwargs)
        # Real WaybackSession; we just want to observe the args. Tests that
        # also patch WaybackClient don't actually use this session.
        sess = MagicMock()
        sess.close = MagicMock()
        return sess

    monkeypatch.setattr(wayback, "WaybackSession", fake_session_cls)
    return captured


# --- success-path -----------------------------------------------------------


def test_enrich_returns_count_and_latest_date(monkeypatch):
    _patch_session(monkeypatch)
    records = [
        _record(datetime(2020, 1, 1, 0, 0, 0)),
        _record(datetime(2024, 8, 15, 12, 0, 0)),
        _record(datetime(2018, 5, 10, 8, 0, 0)),
    ]
    _patch_client(monkeypatch, records)

    result = wayback.enrich("example.com", CONFIG)
    assert result == {"wayback_snapshots": 3, "wayback_last_snapshot": "2024-08-15"}


def test_enrich_returns_zero_when_no_snapshots(monkeypatch):
    _patch_session(monkeypatch)
    _patch_client(monkeypatch, [])
    assert wayback.enrich("example.com", CONFIG) == {
        "wayback_snapshots": 0,
        "wayback_last_snapshot": None,
    }


def test_enrich_handles_string_timestamp_defensively(monkeypatch):
    """Older package versions may surface raw string timestamps. The
    formatter falls back to slicing the first 10 chars."""
    _patch_session(monkeypatch)
    rec = MagicMock()
    rec.timestamp = "2024-03-04T00:00:00"  # ISO string, no .strftime
    _patch_client(monkeypatch, [rec])

    result = wayback.enrich("example.com", CONFIG)
    assert result == {"wayback_snapshots": 1, "wayback_last_snapshot": "2024-03-04"}


# --- failure-path -----------------------------------------------------------


def test_enrich_returns_empty_dict_on_wayback_exception(monkeypatch):
    """RateLimitError, BlockedSiteError, BlockedByRobotsError, etc. all
    inherit from WaybackException. The handler treats every one as
    'failure for this domain' and returns empty dict + records breaker
    failure."""
    from wayback.exceptions import WaybackException

    _patch_session(monkeypatch)
    _patch_client(monkeypatch, WaybackException("simulated 503 / rate-limit / etc."))

    failures_before = wayback._BREAKER.consecutive_failures
    assert wayback.enrich("example.com", CONFIG) == {"wayback_unknown": True}
    assert wayback._BREAKER.consecutive_failures == failures_before + 1


def test_enrich_returns_empty_dict_on_rate_limit_error(monkeypatch):
    """Specific subclass — verifies inheritance check works for the most
    common error mode."""
    from wayback.exceptions import RateLimitError

    _patch_session(monkeypatch)
    _patch_client(monkeypatch, RateLimitError("simulated 429", 60))

    assert wayback.enrich("example.com", CONFIG) == {"wayback_unknown": True}
    assert wayback._BREAKER.consecutive_failures == 1


def test_enrich_returns_empty_dict_on_generic_exception(monkeypatch):
    """Defence-in-depth: the package may surface urllib3 / requests errors
    that don't inherit from WaybackException. The handler still returns
    empty dict instead of letting the orchestrator crash."""
    _patch_session(monkeypatch)
    _patch_client(monkeypatch, ConnectionError("network down"))

    assert wayback.enrich("example.com", CONFIG) == {"wayback_unknown": True}
    assert wayback._BREAKER.consecutive_failures == 1


# --- breaker integration ----------------------------------------------------


def test_enrich_short_circuits_when_breaker_open(monkeypatch):
    """Once the orchestrator-level breaker is open, no WaybackClient call
    fires at all — preserves the previous module's contract."""
    _patch_session(monkeypatch)
    fake_client, _ = _patch_client(monkeypatch, [])

    # Trip the breaker (default threshold is 5 consecutive failures).
    for _ in range(wayback._BREAKER.failure_threshold):
        wayback._BREAKER.record_failure()
    assert wayback._BREAKER.is_open()

    assert wayback.enrich("example.com", CONFIG) == {"wayback_unknown": True}
    fake_client.search.assert_not_called()


# --- session config wiring --------------------------------------------------


def test_enrich_translates_min_interval_to_calls_per_second(monkeypatch):
    """api_min_interval_seconds.wayback is the seconds-between-calls knob
    that the legacy implementation used; the package wants calls-per-second.
    1/min_interval should reach WaybackSession via search_calls_per_second."""
    captured = _patch_session(monkeypatch)
    _patch_client(monkeypatch, [])

    cfg = {**CONFIG, "api_min_interval_seconds": {"wayback": 3.0}}
    wayback.enrich("example.com", cfg)
    assert captured["search_calls_per_second"] == pytest.approx(1.0 / 3.0)


def test_enrich_disables_rate_limit_when_min_interval_zero(monkeypatch):
    """min_interval=0 → cps=0 → package disables its rate-limiter (per its
    own docs: 'set to 0 to disable')."""
    captured = _patch_session(monkeypatch)
    _patch_client(monkeypatch, [])

    cfg = {**CONFIG, "api_min_interval_seconds": {"wayback": 0}}
    wayback.enrich("example.com", cfg)
    assert captured["search_calls_per_second"] == 0


def test_enrich_uses_per_enricher_timeout_override(monkeypatch):
    """api_request_timeout_seconds.wayback overrides global request_timeout_seconds
    and reaches WaybackSession via the timeout constructor arg."""
    captured = _patch_session(monkeypatch)
    _patch_client(monkeypatch, [])

    cfg = {
        **CONFIG,
        "request_timeout_seconds": 5,
        "api_request_timeout_seconds": {"wayback": 60},
    }
    wayback.enrich("example.com", cfg)
    assert captured["timeout"] == 60


def test_enrich_falls_back_to_default_timeout_when_unspecified(monkeypatch):
    """When neither override is set, the module's own default (60s,
    matching Wayback's documented slow-under-load behaviour) is used."""
    captured = _patch_session(monkeypatch)
    _patch_client(monkeypatch, [])

    wayback.enrich("example.com", {})  # empty config
    assert captured["timeout"] == 60
