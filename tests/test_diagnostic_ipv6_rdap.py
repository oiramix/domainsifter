"""Unit tests for the pure helpers in scripts/diagnostic_ipv6_rdap.

The diagnostic script itself does live network I/O against external RDAP
registries and is meant to be run by-hand on the OVH KS-6 server during a
safety window. The pure pieces — verdict heuristic, redaction, prefix
normalisation, success-rate math, header parsing — are testable without
sockets and need to be correct because the report is what feeds the
phase-2 decision.

We do NOT test rdap_get, run_phase, preflight_bind, or the CLI window
gate. Those touch the network or the system clock and are exercised in
the diagnostic's own log when it runs.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from scripts import diagnostic_ipv6_rdap as diag


# --- normalize_prefix --------------------------------------------------------


def test_normalize_prefix_accepts_4_hextets():
    assert diag.normalize_prefix("2001:41d0:abcd:ef00") == "2001:41d0:abcd:ef00"


def test_normalize_prefix_accepts_slash_64_cidr():
    assert diag.normalize_prefix("2001:41d0:abcd:ef00::/64") == "2001:41d0:abcd:ef00"


def test_normalize_prefix_rejects_wrong_prefixlen():
    with pytest.raises(ValueError, match="/64"):
        diag.normalize_prefix("2001:41d0:abcd:ef00::/56")


def test_normalize_prefix_rejects_garbage_hextet_count():
    with pytest.raises(ValueError, match="4 colon-separated hextets"):
        diag.normalize_prefix("2001:41d0:abcd")


def test_normalize_prefix_rejects_non_hex():
    with pytest.raises(ValueError):
        diag.normalize_prefix("2001:41d0:abcd:zzzz")


# --- random_v6_in_prefix -----------------------------------------------------


def test_random_v6_in_prefix_keeps_prefix():
    """The first 4 hextets of the generated address must match the prefix."""
    rng = random.Random(42)
    for _ in range(20):
        addr = diag.random_v6_in_prefix("2001:41d0:abcd:ef00", rng=rng)
        assert addr.startswith("2001:41d0:abcd:ef00:")


def test_random_v6_in_prefix_varies_suffix():
    """Two consecutive draws on the same prefix should overwhelmingly differ
    in their suffix — the whole point of rotation. With a seeded RNG the
    test is deterministic but the property still holds."""
    rng = random.Random(7)
    a = diag.random_v6_in_prefix("2001:41d0:abcd:ef00", rng=rng)
    b = diag.random_v6_in_prefix("2001:41d0:abcd:ef00", rng=rng)
    assert a != b


# --- redact_v6 ---------------------------------------------------------------


def test_redact_v6_keeps_first_four_hextets_and_masks_rest():
    """Audit trail can show the /64 (public infra) but not the chosen /128."""
    addr = "2001:41d0:abcd:ef00:1234:5678:9abc:def0"
    out = diag.redact_v6(addr)
    assert out == "2001:41d0:abcd:ef00:***"


def test_redact_v6_handles_compressed_form():
    """An expanded representation is required for stable redaction even when
    the input uses :: compression."""
    out = diag.redact_v6("2001:41d0:abcd:ef00::1")
    assert out == "2001:41d0:abcd:ef00:***"


def test_redact_v6_returns_marker_on_invalid_input():
    assert diag.redact_v6("not-an-address") == "***invalid-v6***"


# --- _parse_status_and_retry_after ------------------------------------------


def test_parse_status_extracts_2xx_code():
    raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/rdap+json\r\n"
    status, retry = diag._parse_status_and_retry_after(raw)
    assert status == 200
    assert retry is None


def test_parse_status_extracts_429_and_retry_after():
    raw = (
        b"HTTP/1.1 429 Too Many Requests\r\n"
        b"Retry-After: 60\r\n"
        b"Content-Length: 0\r\n"
    )
    status, retry = diag._parse_status_and_retry_after(raw)
    assert status == 429
    assert retry == "60"


def test_parse_status_case_insensitive_header_name():
    """Header names in HTTP are case-insensitive — registries may emit any
    capitalization."""
    raw = b"HTTP/1.1 429 Too Many\r\nretry-after: 30\r\n"
    status, retry = diag._parse_status_and_retry_after(raw)
    assert retry == "30"


def test_parse_status_returns_none_on_malformed_first_line():
    raw = b"garbage\r\nHost: x\r\n"
    status, retry = diag._parse_status_and_retry_after(raw)
    assert status is None


# --- success_rate ------------------------------------------------------------


def _counts(by_status: dict[str, int]) -> dict:
    return {
        "requests": sum(by_status.values()),
        "by_status": by_status,
        "elapsed_ms": [],
        "retry_after_samples": [],
        "aborted": False,
    }


def test_success_rate_counts_200_as_success():
    assert diag.success_rate(_counts({"200": 50})) == 100.0


def test_success_rate_counts_404_as_success():
    """A 404 proves the registry processed our request (domain unknown);
    only 429 / errors mean we were throttled."""
    assert diag.success_rate(_counts({"404": 50})) == 100.0


def test_success_rate_treats_429_as_failure():
    rate = diag.success_rate(_counts({"200": 30, "429": 20}))
    assert rate == 60.0


def test_success_rate_treats_transport_errors_as_failure():
    rate = diag.success_rate(_counts({"200": 25, "err:timeout": 25}))
    assert rate == 50.0


def test_success_rate_empty_counts():
    assert diag.success_rate(_counts({})) == 0.0


# --- verdict heuristic -------------------------------------------------------


def test_verdict_rotation_effective_when_gain_above_threshold():
    """Rotation 98% vs single-IP 76% → 22pt gain → effective."""
    rot = _counts({"200": 49, "429": 1})
    sgl = _counts({"200": 38, "429": 12})
    assert diag.verdict(rot, sgl) == "Rotation effective at /128"


def test_verdict_no_benefit_when_rates_close():
    """Rotation 44% vs single-IP 48% → 4pt difference → /64 tracking."""
    rot = _counts({"404": 22, "429": 28})
    sgl = _counts({"404": 24, "429": 26})
    assert diag.verdict(rot, sgl) == "No rotation benefit (tracking at /64 or higher)"


def test_verdict_inconclusive_when_both_failing_heavily():
    """Both >60% failure (here both >80%) → registry stress / different limit."""
    rot = _counts({"200": 5, "429": 45})
    sgl = _counts({"200": 6, "429": 44})
    assert diag.verdict(rot, sgl).startswith("Inconclusive")


def test_verdict_marginal_when_gain_in_gap_between_thresholds():
    """Gain of 14pts is above tolerance (10) but below the effective bar (20)."""
    rot = _counts({"200": 30, "429": 20})  # 60%
    sgl = _counts({"200": 23, "429": 27})  # 46%
    v = diag.verdict(rot, sgl)
    assert v.startswith("Marginal")
    assert "+14" in v or "+13" in v or "+15" in v  # rounding tolerance


def test_verdict_handles_perfect_rotation_and_full_block():
    """Best case: rotation 100% / single 0% → 100pt gain → effective."""
    rot = _counts({"200": 50})
    sgl = _counts({"429": 50})
    assert diag.verdict(rot, sgl) == "Rotation effective at /128"


# --- _in_window --------------------------------------------------------------


def test_in_window_accepts_15_utc():
    """15:00 UTC is in the 14:00-20:00 window."""
    t = datetime(2026, 5, 17, 15, 30, tzinfo=timezone.utc)
    assert diag._in_window(t) is True


def test_in_window_rejects_06_utc():
    """06:00 UTC is during the cron — refused."""
    t = datetime(2026, 5, 17, 6, 30, tzinfo=timezone.utc)
    assert diag._in_window(t) is False


def test_in_window_rejects_20_utc_boundary():
    """20:00 UTC is the inclusive end — at exactly 20:00 we're out."""
    t = datetime(2026, 5, 17, 20, 0, tzinfo=timezone.utc)
    assert diag._in_window(t) is False


def test_in_window_accepts_14_utc_boundary():
    """14:00 UTC is the inclusive start."""
    t = datetime(2026, 5, 17, 14, 0, tzinfo=timezone.utc)
    assert diag._in_window(t) is True
