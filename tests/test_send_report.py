"""Tests for scripts/send_report.py.

Mock surfaces:
- `subprocess.run` for `journalctl` and `systemctl show` invocations
- `smtplib.SMTP` for the email send
- env vars via monkeypatch

The reporter's contract is "extract signals + send email + never crash."
These tests verify each branch of that contract.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import send_report


@pytest.fixture
def required_env(monkeypatch):
    """Set the four required env vars to placeholder values."""
    monkeypatch.setenv("BREVO_SMTP_USER", "test-user@example.invalid")
    monkeypatch.setenv("BREVO_SMTP_KEY", "test-key-not-real")
    monkeypatch.setenv("REPORT_TO_EMAIL", "to@example.invalid")
    monkeypatch.setenv("REPORT_FROM_EMAIL", "from@example.invalid")
    # A stale wrapper-exported memory peak in the dev shell would silently
    # short-circuit the systemctl-mocked fallback path. Clear it by default;
    # tests that exercise the env-var path set it explicitly.
    monkeypatch.delenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", raising=False)


@pytest.fixture(autouse=True)
def cc_sandbox(tmp_path_factory, monkeypatch):
    """Point every Common-Crawl-freshness lookup at an empty temp world.

    Autouse on purpose: on the production box a real cached SQLite under
    ~/.cache/domainsifter/cc/ and a real scripts/state/cc_refresh_result.json
    both exist, and a stale one of either would escalate the subject line and
    break every test that asserts "no alarm". Isolating it here keeps the whole
    file hermetic — nothing touches the real cache, and nothing downloads.

    Returns the sandbox root so the CC tests can plant fixtures inside it:
      <sandbox>/cache/<release>.sqlite       — the derived SQLite
      <sandbox>/repo/scripts/config.json     — the config
      <sandbox>/repo/scripts/state/*.json    — the refresh result
    """
    sandbox = tmp_path_factory.mktemp("cc-sandbox")
    monkeypatch.setenv("CC_BACKLINKS_CACHE_DIR", str(sandbox / "cache"))
    monkeypatch.delenv("CC_BACKLINKS_RELEASE", raising=False)
    monkeypatch.setattr(send_report, "REPO_ROOT", sandbox / "repo")
    monkeypatch.setattr(
        send_report, "CONFIG_PATH", sandbox / "repo" / "scripts" / "config.json",
    )
    return sandbox


# --- log parsing -----------------------------------------------------------


def test_extract_domain_count_returns_final_wrote_line():
    log = (
        "2026-05-11 06:30:00 INFO scripts.pipeline started\n"
        "2026-05-11 06:35:00 INFO scripts.output Wrote 47 domains to src/data/daily-domains.json (generated_at=...)\n"
    )
    assert send_report._extract_domain_count(log) == 47


def test_extract_domain_count_handles_multiple_wrote_lines_returns_last():
    """If anything ever wrote multiple times in one run, take the last."""
    log = (
        "Wrote 10 domains to /tmp/intermediate.json\n"
        "Wrote 47 domains to src/data/daily-domains.json (generated_at=...)\n"
    )
    assert send_report._extract_domain_count(log) == 47


def test_extract_domain_count_returns_none_when_no_match():
    assert send_report._extract_domain_count("nothing relevant here") is None
    assert send_report._extract_domain_count("") is None


def test_count_circuit_breaker_trips():
    log = (
        "Circuit breaker [wayback] opened until 2026-05-11T06:35:00\n"
        "Some other line\n"
        "Circuit breaker [crtsh] opened until 2026-05-11T06:36:00\n"
        "Circuit breaker [wayback] reset\n"  # 'opened' not present — not counted
    )
    assert send_report._count_circuit_breaker_trips(log) == 2


def test_count_tld_failures_matches_pipeline_log_lines():
    log = (
        ".com zone download failed, skipping: ...\n"
        ".net zone parse failed, skipping: OSError ...\n"
        ".org parsed 12834679 unique apex names\n"  # success — not counted
    )
    assert send_report._count_tld_failures(log) == 2


# --- RDAP 429 strikes / stops / 403 alarms ----------------------------------

_STRIKE_LINE_1 = (
    "429 from rdap.verisign.com — strike 1/3: honored 5s cooldown "
    "(Retry-After header='0'), resuming this host at its normal pace."
)
_STRIKE_LINE_3 = (
    "429 from rdap.verisign.com — strike 3/3: STRIKE LIMIT reached, STOPPING "
    "this host for the rest of the run (no resume). Honored 5s cooldown "
    "(Retry-After header='0'). Other RDAP hosts continue normally."
)
_STOP_LINE = (
    "RDAP host [rdap.verisign.com] STOPPED (429) after 312.5s into its bucket "
    "— 412/1350 candidates checked before the stop, 938 left UNCHECKED "
    "(never queried). Other hosts unaffected."
)
_403_STOP_LINE = (
    "RDAP host [rdap.gmoregistry.net] STOPPED (403) after 5.0s into its bucket "
    "— 1/40 candidates checked before the stop, 39 left UNCHECKED "
    "(never queried). Other hosts unaffected."
)
_403_LINE = (
    "RDAP 403 FORBIDDEN from rdap.verisign.com on blocked.com — CATASTROPHIC "
    "IP-BLOCK signal (NOT a rate limit). Stopping this host for the run. "
    "INVESTIGATE IMMEDIATELY: the registry has likely blocked our egress IP."
)


def test_rdap_429_strikes_counts_every_strike():
    log = _STRIKE_LINE_1 + "\n" + _STRIKE_LINE_3 + "\nunrelated line\n"
    assert send_report._rdap_429_strikes(log) == 2


def test_rdap_429_strikes_zero_on_clean_run():
    assert send_report._rdap_429_strikes("clean\nno strikes\n") == 0


def test_rdap_429_stops_matches_only_429_stop_lines():
    """The stop scanner counts hosts that hit the 429 strike limit — a 403
    hard-stop line (STOPPED (403)) is NOT a 429 stop and must be excluded."""
    log = (
        _STOP_LINE + "\n"
        + _403_STOP_LINE + "\n"
        "RDAP host bucket [rdap.org.example] done in 900.0s: 12 available\n"  # not a stop
    )
    stops = send_report._rdap_429_stops(log)
    assert len(stops) == 1  # only the (429) line
    assert "rdap.verisign.com" in stops[0] and "938 left UNCHECKED" in stops[0]


def test_rdap_429_stops_empty_on_clean_run():
    assert send_report._rdap_429_stops("a clean log\nno stops here\n") == []


def test_rdap_429_stops_empty_when_only_strikes_no_stop():
    """A 'recovered' day (strikes but no host hit the limit) shows 0 stops."""
    assert send_report._rdap_429_stops(_STRIKE_LINE_1 + "\n") == []


def test_rdap_403_alarms_matches_critical_lines():
    log = "some line\n" + _403_LINE + "\nanother line\n"
    alarms = send_report._rdap_403_alarms(log)
    assert len(alarms) == 1
    assert "rdap.verisign.com" in alarms[0]


def test_rdap_403_alarms_empty_when_only_429s():
    """A 429 strike/stop must NOT be mistaken for a 403 block."""
    assert send_report._rdap_403_alarms(_STOP_LINE + "\n" + _STRIKE_LINE_3 + "\n") == []


def test_build_email_403_escalates_subject_and_header(required_env):
    msg = send_report._build_email(
        pipeline_exit=0, log=_403_LINE + "\n", duration_sec=42.0,
    )
    assert "🚨 RDAP 403 BLOCK" in msg["Subject"]
    body = msg.get_content()
    assert "RDAP 403 blocks  : 1" in body
    assert "CATASTROPHIC IP-BLOCK DETECTED" in body


def test_build_email_recovered_day_shows_strikes_no_stop_no_alarm(required_env):
    """1 strike, recovered: header shows the strike, zero stops, no subject alarm."""
    msg = send_report._build_email(
        pipeline_exit=0, log=_STRIKE_LINE_1 + "\n", duration_sec=42.0,
    )
    assert "🚨" not in msg["Subject"]
    body = msg.get_content()
    assert "RDAP 429 strikes : 1" in body
    assert "RDAP 429 stops   : 0" in body


def test_build_email_strike_limit_day_shows_stop_no_subject_alarm(required_env):
    """Host hit the limit: strikes counted, 1 stop shown, but only 403 escalates
    the subject — a 429 stop does not."""
    msg = send_report._build_email(
        pipeline_exit=0, log=_STRIKE_LINE_1 + "\n" + _STRIKE_LINE_3 + "\n" + _STOP_LINE + "\n",
        duration_sec=42.0,
    )
    assert "🚨" not in msg["Subject"]
    body = msg.get_content()
    assert "RDAP 429 strikes : 2" in body
    assert "RDAP 429 stops   : 1" in body
    assert "938 left UNCHECKED" in body


# --- LLM-stage signals: backend / ranker / classifier / credit errors -------
#
# Regression guard for the 2026-07-23 silent outage: credits hit zero, both
# LLM stages failed soft, the report kept saying SUCCESS, and the
# toxic-domain screen was open for ~8 weeks. All domains below are invented
# (hard rule 1).

_BACKEND_CLAUDE_CODE = (
    "2026-09-18 06:31:02 INFO scripts.llm_backend "
    "llm_backend[claude_code]: ok — out=412 tokens, cache_read=18240, 2130ms"
)
_BACKEND_API = (
    "2026-09-18 06:31:02 INFO scripts.llm_backend "
    "llm_backend[api]: ok — out=388 tokens, cache_read=0, 1980ms"
)
_RANKER_SCORED = (
    "Phase 2 ranker: 312 scored, 8 missing (below-gate); $0.4120 spent of "
    "$0.9000 ceiling; 84.3s"
)
_RANKER_FALLBACK = (
    "Phase 2 ranker FALLBACK — too_few_eligible (above_gate=3 < min_eligible=40)"
)
_RANKER_FALLBACK_NO_CLIENT = (
    "Phase 2 ranker FALLBACK — no API client "
    "(ANTHROPIC_API_KEY missing or anthropic SDK absent)"
)
_CLASSIFIER_HEALTHY = (
    "snapshot_classifier: results — 18 legitimate, 7 parked, 3 toxic, "
    "2 empty, 1 unknown"
)
_CLASSIFIER_ALL_UNKNOWN = (
    "snapshot_classifier: results — 0 legitimate, 0 parked, 0 toxic, "
    "0 empty, 31 unknown"
)
_CREDIT_ERROR_LINE = (
    "snapshot_classifier: Haiku call failed for marketglow.com: "
    "Error code: 400 - {'error': {'message': 'Your credit balance is too low "
    "to access the Anthropic API...'}} — treating as unknown"
)


def test_llm_backend_parses_claude_code():
    assert send_report._llm_backend(_BACKEND_CLAUDE_CODE + "\n") == "claude_code"


def test_llm_backend_parses_api():
    assert send_report._llm_backend(_BACKEND_API + "\n") == "api"


def test_llm_backend_dedupes_repeat_lines():
    log = "\n".join([_BACKEND_CLAUDE_CODE] * 5) + "\n"
    assert send_report._llm_backend(log) == "claude_code"


def test_llm_backend_reports_both_on_mid_run_switch():
    log = _BACKEND_CLAUDE_CODE + "\n" + _BACKEND_API + "\n"
    assert send_report._llm_backend(log) == "claude_code, api"


def test_llm_backend_none_when_absent():
    assert send_report._llm_backend("nothing relevant\n") is None
    assert send_report._llm_backend("") is None


def test_phase2_ranker_outcome_reports_scored_count():
    assert send_report._phase2_ranker_outcome(_RANKER_SCORED + "\n") == "RANKER (312 scored)"


def test_phase2_ranker_outcome_reports_fallback_reason():
    outcome = send_report._phase2_ranker_outcome(_RANKER_FALLBACK + "\n")
    assert outcome == "FALLBACK — too_few_eligible"


def test_phase2_ranker_outcome_fallback_wins_over_scored_line():
    """A fallback run logs BOTH lines (it scores, then reverts). The fallback
    is the fact that matters, so it must win."""
    log = _RANKER_SCORED + "\n" + _RANKER_FALLBACK + "\n"
    assert send_report._phase2_ranker_outcome(log) == "FALLBACK — too_few_eligible"


def test_phase2_ranker_outcome_handles_no_api_client_fallback():
    """The no-client fallback has no (above_gate=...) suffix; the whole
    remainder becomes the reason."""
    outcome = send_report._phase2_ranker_outcome(_RANKER_FALLBACK_NO_CLIENT + "\n")
    assert outcome is not None
    assert outcome.startswith("FALLBACK")
    assert "no API client" in outcome


def test_phase2_ranker_outcome_none_when_absent():
    assert send_report._phase2_ranker_outcome("no ranker here\n") is None


def test_snapshot_classifier_counts_parses_all_five():
    counts = send_report._snapshot_classifier_counts(_CLASSIFIER_HEALTHY + "\n")
    assert counts == {
        "legitimate": 18, "parked": 7, "toxic": 3, "empty": 2, "unknown": 1,
    }


def test_snapshot_classifier_counts_none_when_absent():
    assert send_report._snapshot_classifier_counts("nothing\n") is None


def test_snapshot_classifier_counts_last_match_wins():
    log = (
        "snapshot_classifier: results — 1 legitimate, 0 parked, 0 toxic, 0 empty, 0 unknown\n"
        + _CLASSIFIER_HEALTHY + "\n"
    )
    counts = send_report._snapshot_classifier_counts(log)
    assert counts is not None and counts["legitimate"] == 18


def test_classifier_is_blind_true_for_all_unknown():
    counts = send_report._snapshot_classifier_counts(_CLASSIFIER_ALL_UNKNOWN + "\n")
    assert send_report._classifier_is_blind(counts) is True


def test_classifier_is_blind_false_for_healthy_mix():
    counts = send_report._snapshot_classifier_counts(_CLASSIFIER_HEALTHY + "\n")
    assert send_report._classifier_is_blind(counts) is False


def test_classifier_is_blind_false_for_zero_domains():
    """A day with no candidates to classify is not a degradation."""
    log = "snapshot_classifier: results — 0 legitimate, 0 parked, 0 toxic, 0 empty, 0 unknown\n"
    assert send_report._classifier_is_blind(send_report._snapshot_classifier_counts(log)) is False


def test_classifier_is_blind_false_when_line_missing():
    assert send_report._classifier_is_blind(None) is False


def test_count_credit_balance_errors():
    log = _CREDIT_ERROR_LINE + "\n" + _CREDIT_ERROR_LINE + "\nunrelated\n"
    assert send_report._count_credit_balance_errors(log) == 2
    assert send_report._count_credit_balance_errors("clean run\n") == 0


# --- LLM-stage signals rendered into the email ------------------------------


def test_build_email_renders_new_llm_fields(required_env):
    log = (
        _BACKEND_CLAUDE_CODE + "\n"
        + _RANKER_SCORED + "\n"
        + _CLASSIFIER_HEALTHY + "\n"
        + "Wrote 12 domains to src/data/daily-domains.json\n"
    )
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=42.0)
    body = msg.get_content()
    assert "LLM backend      : claude_code" in body
    assert "Phase 2 ranker   : RANKER (312 scored)" in body
    assert "Snapshot classes : 18 legitimate, 7 parked, 3 toxic, 2 empty, 1 unknown" in body
    # Healthy day: no escalation, no banner, no credit-errors line.
    assert "🚨" not in msg["Subject"]
    assert "TOXIC-DOMAIN SCREEN IS NOT RUNNING" not in body
    assert "Credit errors" not in body


def test_build_email_new_fields_degrade_to_placeholders(required_env):
    """Absent log lines must render placeholders, not crash or omit fields."""
    msg = send_report._build_email(pipeline_exit=0, log="nothing useful\n", duration_sec=1.0)
    body = msg.get_content()
    assert "LLM backend      : (none used)" in body
    assert "Phase 2 ranker   : (no ranker line in log)" in body
    assert "Snapshot classes : (no classifier line in log)" in body
    assert "🚨" not in msg["Subject"]


def test_build_email_all_unknown_fires_banner_and_escalates_subject(required_env):
    """THE regression guard: exit code 0, pipeline 'SUCCESS', but every
    classification came back unknown → toxic screen off. Must be loud."""
    log = (
        _CLASSIFIER_ALL_UNKNOWN + "\n"
        + "Wrote 31 domains to src/data/daily-domains.json\n"
    )
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=42.0)
    subject = msg["Subject"]
    assert "🚨 TOXIC SCREEN OFF" in subject
    assert "SUCCESS" in subject  # the exit code really was 0 — that's the point
    body = msg.get_content()
    assert "TOXIC-DOMAIN SCREEN IS NOT RUNNING" in body
    assert "UNSCREENED" in body
    assert "Snapshot classes : 0 legitimate, 0 parked, 0 toxic, 0 empty, 31 unknown" in body


def test_build_email_all_unknown_names_credit_balance_as_likely_cause(required_env):
    log = (
        _CREDIT_ERROR_LINE + "\n"
        + _CREDIT_ERROR_LINE + "\n"
        + _CLASSIFIER_ALL_UNKNOWN + "\n"
    )
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=42.0)
    body = msg.get_content()
    assert "Credit errors    : 2 ('credit balance is too low')" in body
    assert "Likely cause     : 2 x 'credit balance is too low'" in body


def test_build_email_healthy_classifier_does_not_fire_banner(required_env):
    log = _BACKEND_API + "\n" + _CLASSIFIER_HEALTHY + "\n"
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=42.0)
    assert "🚨" not in msg["Subject"]
    assert "TOXIC-DOMAIN SCREEN IS NOT RUNNING" not in msg.get_content()


def test_build_email_ranker_fallback_fires_banner_and_escalates_subject(required_env):
    log = _RANKER_SCORED + "\n" + _RANKER_FALLBACK + "\n" + _CLASSIFIER_HEALTHY + "\n"
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=42.0)
    assert "🚨 PHASE 2 FALLBACK" in msg["Subject"]
    body = msg.get_content()
    assert "PHASE 2 RANKER FELL BACK TO MECHANICAL SELECTION" in body
    assert "Phase 2 ranker   : FALLBACK — too_few_eligible" in body
    # Classifier was healthy, so only the ranker banner fires.
    assert "TOXIC-DOMAIN SCREEN IS NOT RUNNING" not in body


def test_build_email_successful_ranker_does_not_fire_fallback_banner(required_env):
    msg = send_report._build_email(
        pipeline_exit=0, log=_RANKER_SCORED + "\n", duration_sec=42.0,
    )
    assert "🚨" not in msg["Subject"]
    assert "FELL BACK TO MECHANICAL SELECTION" not in msg.get_content()


def test_build_email_both_llm_banners_fire_together(required_env):
    """The credit-exhaustion signature: no backend line, ranker fell back,
    and every classification unknown — all on an exit-0 run."""
    log = (
        _RANKER_FALLBACK_NO_CLIENT + "\n"
        + _CREDIT_ERROR_LINE + "\n"
        + _CLASSIFIER_ALL_UNKNOWN + "\n"
        + "Wrote 31 domains to src/data/daily-domains.json\n"
    )
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=42.0)
    subject = msg["Subject"]
    assert "🚨 TOXIC SCREEN OFF" in subject
    assert "🚨 PHASE 2 FALLBACK" in subject
    body = msg.get_content()
    assert "TOXIC-DOMAIN SCREEN IS NOT RUNNING" in body
    assert "PHASE 2 RANKER FELL BACK TO MECHANICAL SELECTION" in body
    assert "LLM backend      : (none used)" in body


def test_build_email_all_three_alarms_coexist_in_subject(required_env):
    """A 403 block must not displace the LLM alarms, or vice versa."""
    log = (
        _403_LINE + "\n"
        + _RANKER_FALLBACK + "\n"
        + _CLASSIFIER_ALL_UNKNOWN + "\n"
    )
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=42.0)
    subject = msg["Subject"]
    assert "🚨 RDAP 403 BLOCK" in subject
    assert "🚨 TOXIC SCREEN OFF" in subject
    assert "🚨 PHASE 2 FALLBACK" in subject


def test_build_email_banner_fires_on_failed_run_too(required_env):
    """Degradation detection is independent of the pipeline exit code."""
    msg = send_report._build_email(
        pipeline_exit=1, log=_CLASSIFIER_ALL_UNKNOWN + "\n", duration_sec=42.0,
    )
    assert "FAILED" in msg["Subject"]
    assert "🚨 TOXIC SCREEN OFF" in msg["Subject"]


# --- malformed-input resilience --------------------------------------------


@pytest.mark.parametrize("malformed", [
    "snapshot_classifier: results — banana legitimate, 7 parked\n",
    "snapshot_classifier: results —\n",
    "llm_backend[]: ok — out=1\n",
    "llm_backend[unterminated: ok\n",
    "Phase 2 ranker: scored, missing\n",
    "Phase 2 ranker FALLBACK —\n",
    "Phase 2 ranker FALLBACK\n",
    "\x00\x01 binary junk \udcff\n",
    "snapshot_classifier: results — 99999999999999999999 legitimate, 0 parked, "
    "0 toxic, 0 empty, 0 unknown\n",
])
def test_parsers_never_raise_on_malformed_input(malformed):
    send_report._llm_backend(malformed)
    send_report._phase2_ranker_outcome(malformed)
    counts = send_report._snapshot_classifier_counts(malformed)
    send_report._classifier_is_blind(counts)
    send_report._count_credit_balance_errors(malformed)


def test_build_email_survives_malformed_log(required_env):
    malformed = (
        "snapshot_classifier: results — banana legitimate\n"
        "Phase 2 ranker FALLBACK —\n"
        "llm_backend[]: ok\n"
    )
    msg = send_report._build_email(pipeline_exit=0, log=malformed, duration_sec=1.0)
    assert msg["Subject"]  # built without raising
    assert "Snapshot classes : (no classifier line in log)" in msg.get_content()


def test_main_exits_zero_on_malformed_log(required_env, monkeypatch):
    """The whole point of the exit-0 invariant: a parsing surprise in the new
    LLM fields must not stop the email (or the wrapper). Garbage in the
    classifier/ranker/backend lines still produces a delivered report."""
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    malformed_log = (
        "snapshot_classifier: results — banana legitimate,,,\n"
        "Phase 2 ranker FALLBACK —   \n"
        "llm_backend[: ok\n"
        "Wrote banana domains to nowhere\n"
    )
    journal_mock = MagicMock(returncode=0, stdout=malformed_log, stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=journal_mock))

    smtp_instance = MagicMock()
    smtp_cm = MagicMock()
    smtp_cm.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_cm.__exit__ = MagicMock(return_value=None)
    monkeypatch.setattr(send_report.smtplib, "SMTP", MagicMock(return_value=smtp_cm))

    assert send_report.main(["--pipeline-exit", "0"]) == 0
    assert smtp_instance.send_message.called


def test_main_ships_report_despite_undecodable_journal_bytes(required_env, monkeypatch):
    """A lone surrogate in the journal must NOT suppress the email.

    `_truncate` encodes with errors="replace", so undecodable bytes are
    normalised away instead of raising UnicodeEncodeError. Suppressing the
    report is the worst possible outcome: this email carries the toxic-screen
    and ranker-fallback alarms, so it has to ship even on a mangled log.
    """
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    journal_mock = MagicMock(returncode=0, stdout="log with \udcff in it\n", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=journal_mock))
    smtp_instance = MagicMock()
    smtp_cm = MagicMock()
    smtp_cm.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_cm.__exit__ = MagicMock(return_value=None)
    monkeypatch.setattr(send_report.smtplib, "SMTP", MagicMock(return_value=smtp_cm))

    assert send_report.main(["--pipeline-exit", "0"]) == 0
    assert smtp_instance.send_message.called


def test_main_exits_zero_when_email_build_raises(required_env, monkeypatch, capsys):
    """Last-ditch guard on the exit-0 invariant: an un-anticipated exception
    while building the report soft-fails rather than propagating a non-zero
    exit to the wrapper (which would mislabel a healthy run as FAILED)."""
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    journal_mock = MagicMock(returncode=0, stdout="ordinary log line\n", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=journal_mock))
    monkeypatch.setattr(
        send_report, "_build_email", MagicMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(send_report.smtplib, "SMTP", MagicMock())

    assert send_report.main(["--pipeline-exit", "0"]) == 0
    assert "failed to build email" in capsys.readouterr().err


def test_main_exits_zero_when_degradation_banners_fire(required_env, monkeypatch):
    """Banners change the subject/body but must never change the exit code."""
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    log = _CLASSIFIER_ALL_UNKNOWN + "\n" + _RANKER_FALLBACK + "\n"
    journal_mock = MagicMock(returncode=0, stdout=log, stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=journal_mock))

    smtp_instance = MagicMock()
    smtp_cm = MagicMock()
    smtp_cm.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_cm.__exit__ = MagicMock(return_value=None)
    monkeypatch.setattr(send_report.smtplib, "SMTP", MagicMock(return_value=smtp_cm))

    assert send_report.main(["--pipeline-exit", "0"]) == 0
    assert smtp_instance.send_message.called


# --- truncation ------------------------------------------------------------


def test_truncate_passes_through_small_logs():
    log = "short log\n" * 10
    assert send_report._truncate(log, max_bytes=10_000) == log


def test_truncate_keeps_head_and_tail_for_large_logs():
    log = "HEAD_LINE\n" + ("filler " * 200_000) + "\nTAIL_LINE"
    truncated = send_report._truncate(log, max_bytes=2000)
    assert "HEAD_LINE" in truncated
    assert "TAIL_LINE" in truncated
    assert "truncated" in truncated
    # Truncated output should be smaller than the original
    assert len(truncated.encode("utf-8")) < len(log.encode("utf-8"))


# --- duration / memory helpers --------------------------------------------


def test_format_bytes_handles_units():
    assert send_report._format_bytes(None) == "(unavailable)"
    assert send_report._format_bytes(512) == "512.0 B"
    assert send_report._format_bytes(2048) == "2.0 KB"
    assert send_report._format_bytes(2 * 1024 * 1024) == "2.0 MB"


# --- memory peak source: env var (preferred) vs systemctl (fallback) ------


def test_memory_peak_prefers_env_var_over_systemctl(monkeypatch):
    """The wrapper exports DOMAINSIFTER_MEMORY_PEAK_BYTES from
    /sys/fs/cgroup/.../memory.peak inside the EXIT trap. The reporter
    must read it directly and NOT consult systemctl — that's the whole
    point: systemctl races with systemd's unit teardown."""
    monkeypatch.setenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", "1965432109")

    monkeypatch.setattr(
        send_report.subprocess, "run",
        MagicMock(side_effect=AssertionError("systemctl invoked despite env var being set")),
    )
    assert send_report._memory_peak_bytes() == 1965432109


def test_memory_peak_env_var_path_accepts_small_values(monkeypatch):
    """An early-failure run that died before any real allocation should
    still flow through the env-var path rather than the systemctl
    fallback."""
    monkeypatch.setenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", "42")
    monkeypatch.setattr(
        send_report.subprocess, "run",
        MagicMock(side_effect=AssertionError("systemctl invoked")),
    )
    assert send_report._memory_peak_bytes() == 42


def test_memory_peak_falls_back_to_systemctl_when_env_unset(monkeypatch):
    """Non-wrapper invocations (operator-mode validation, pre-fix deploys)
    must keep working via the original systemctl path."""
    monkeypatch.delenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", raising=False)
    mock_result = MagicMock(returncode=0, stdout="2097152\n", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=mock_result))
    assert send_report._memory_peak_bytes() == 2097152


def test_memory_peak_falls_back_when_env_is_empty(monkeypatch):
    """Empty env var (cgroup file readable but contained no digits — edge
    case the wrapper's regex normally screens out) must fall through to
    systemctl, not crash or be interpreted as 0."""
    monkeypatch.setenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", "")
    mock_result = MagicMock(returncode=0, stdout="42\n", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=mock_result))
    assert send_report._memory_peak_bytes() == 42


def test_memory_peak_falls_back_when_env_is_non_numeric(monkeypatch):
    """Defence-in-depth: if the env var somehow contains garbage (e.g.
    independent invocation outside the wrapper), the reporter must NOT
    crash with int() raising — it must fall through to systemctl."""
    monkeypatch.setenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", "  not-a-number  ")
    mock_result = MagicMock(returncode=0, stdout="99\n", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=mock_result))
    assert send_report._memory_peak_bytes() == 99


def test_memory_peak_returns_none_when_both_paths_fail(monkeypatch):
    """No env var, no systemctl (e.g. cgroup v1 host with no memory.peak
    file AND running outside systemd) → None → "(unavailable)" in email."""
    monkeypatch.delenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", raising=False)

    def boom(*_a, **_kw):
        raise FileNotFoundError("systemctl not on PATH")

    monkeypatch.setattr(send_report.subprocess, "run", boom)
    assert send_report._memory_peak_bytes() is None


def test_memory_peak_returns_none_when_systemctl_returns_empty(monkeypatch):
    """systemctl returns 0 + empty stdout when MemoryAccounting was off OR
    when the trap-timing race triggers (the original bug). Either way:
    no signal → None → "(unavailable)"."""
    monkeypatch.delenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", raising=False)
    mock_result = MagicMock(returncode=0, stdout="\n", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=mock_result))
    assert send_report._memory_peak_bytes() is None


# --- _format_memory_peak --------------------------------------------------


def test_format_memory_peak_returns_unavailable_for_none():
    assert send_report._format_memory_peak(None) == "(unavailable)"


def test_format_memory_peak_uses_mb_below_one_gib():
    """Under 1 GiB → MB. `>= 100` shows 1 decimal; `< 100` shows 2."""
    # 743.2 MiB round-trips to "743.2 MB" (≥ 100 → 1 decimal)
    assert send_report._format_memory_peak(int(743.2 * 1024 ** 2)) == "743.2 MB"
    # 50 MiB exactly — small magnitude → 2 decimals
    assert send_report._format_memory_peak(50 * 1024 ** 2) == "50.00 MB"


def test_format_memory_peak_uses_gb_at_or_above_one_gib():
    """At 1 GiB and above → GB. The 127.7 case covers the future .com
    scenario where peak could approach KS-6's 128 GB ceiling."""
    assert send_report._format_memory_peak(int(1.83 * 1024 ** 3)) == "1.83 GB"
    assert send_report._format_memory_peak(int(127.7 * 1024 ** 3)) == "127.7 GB"


def test_format_memory_peak_boundary_at_one_gib():
    """Exactly 1 GiB switches to GB unit; one byte short stays in MB."""
    assert send_report._format_memory_peak(1024 ** 3) == "1.00 GB"
    assert send_report._format_memory_peak(1024 ** 3 - 1) == "1024.0 MB"


def test_format_duration_human_readable():
    assert send_report._format_duration(None) == "(unavailable)"
    assert send_report._format_duration(45) == "45s"
    assert send_report._format_duration(125) == "2m 5s"
    assert send_report._format_duration(3725) == "1h 2m 5s"


def test_resolve_duration_reads_wrapper_env(monkeypatch):
    import time as _time
    monkeypatch.setenv("DOMAINSIFTER_RUN_START_TS", str(int(_time.time()) - 120))
    duration = send_report._resolve_duration()
    assert duration is not None and 119 <= duration <= 125  # ~120s with slack


def test_resolve_duration_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("DOMAINSIFTER_RUN_START_TS", raising=False)
    assert send_report._resolve_duration() is None


# --- journal capture -------------------------------------------------------


def test_capture_journal_returns_placeholder_when_invocation_id_missing():
    text = send_report._capture_journal("")
    assert "INVOCATION_ID not set" in text


def test_capture_journal_since_flag_bypasses_invocation_id(monkeypatch):
    """Operator-mode --journal-since path: passes -u + --since to journalctl
    instead of filtering by _SYSTEMD_INVOCATION_ID. Verifies the command
    shape so an external probe can confirm the right log slice was queried."""
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="operator-mode capture\n", stderr="")

    monkeypatch.setattr(send_report.subprocess, "run", fake_run)
    text = send_report._capture_journal("", since="8h ago")
    assert text == "operator-mode capture\n"
    # Command should target the unit + the --since window, NOT the invocation filter.
    assert "-u" in captured["cmd"]
    assert "domainsifter.service" in captured["cmd"]
    assert "--since" in captured["cmd"]
    assert "8h ago" in captured["cmd"]
    assert not any(arg.startswith("_SYSTEMD_INVOCATION_ID=") for arg in captured["cmd"])


def test_capture_journal_returns_subprocess_stdout(monkeypatch):
    mock_result = MagicMock(returncode=0, stdout="captured journal lines\n", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=mock_result))
    text = send_report._capture_journal("test-id-1234")
    assert text == "captured journal lines\n"


def test_capture_journal_handles_journalctl_missing(monkeypatch):
    def boom(*_a, **_kw):
        raise FileNotFoundError("journalctl not on PATH")
    monkeypatch.setattr(send_report.subprocess, "run", boom)
    text = send_report._capture_journal("test-id-1234")
    assert "journalctl invocation failed" in text


def test_capture_journal_handles_nonzero_exit(monkeypatch):
    mock_result = MagicMock(returncode=1, stdout="", stderr="No entries.")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=mock_result))
    text = send_report._capture_journal("test-id-1234")
    assert "exit=1" in text and "No entries" in text


# --- email construction ---------------------------------------------------


def test_build_email_success_subject_and_headers(required_env):
    log = "Wrote 47 domains to src/data/daily-domains.json (generated_at=...)\n"
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=300.0)
    assert msg["From"] == "from@example.invalid"
    assert msg["To"] == "to@example.invalid"
    subject = msg["Subject"]
    assert "SUCCESS" in subject
    assert "47 domains" in subject
    assert "✅" in subject


def test_build_email_failure_subject_uses_failed_verdict(required_env):
    msg = send_report._build_email(pipeline_exit=137, log="", duration_sec=None)
    subject = msg["Subject"]
    assert "FAILED" in subject
    assert "❌" in subject
    assert "exit code 137" in msg.get_content()


def test_build_email_body_includes_header_and_log(required_env):
    log = "the actual run log content here\n"
    msg = send_report._build_email(pipeline_exit=0, log=log, duration_sec=42.0)
    body = msg.get_content()
    assert "Verdict" in body
    assert "Wall-clock       : 42s" in body
    assert "the actual run log content here" in body


def test_build_email_handles_missing_from_env_via_keyerror(monkeypatch):
    """REPORT_FROM_EMAIL missing should raise KeyError so main() can soft-fail."""
    monkeypatch.delenv("REPORT_FROM_EMAIL", raising=False)
    monkeypatch.setenv("REPORT_TO_EMAIL", "to@example.invalid")
    with pytest.raises(KeyError):
        send_report._build_email(pipeline_exit=0, log="", duration_sec=None)


def test_build_email_renders_memory_peak_from_env_var(required_env, monkeypatch):
    """End-to-end: wrapper exports DOMAINSIFTER_MEMORY_PEAK_BYTES → reporter
    renders it as MB/GB in the email body without ever consulting
    systemctl. The systemctl-not-called assertion is the core regression
    guard for the trap-timing bug."""
    monkeypatch.setenv("DOMAINSIFTER_MEMORY_PEAK_BYTES", str(int(1.83 * 1024 ** 3)))
    raising_mock = MagicMock(side_effect=AssertionError("systemctl invoked despite env var"))
    monkeypatch.setattr(send_report.subprocess, "run", raising_mock)

    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=42.0)
    body = msg.get_content()
    assert "Memory peak      : 1.83 GB" in body
    raising_mock.assert_not_called()


# --- main() integration ----------------------------------------------------


def test_main_returns_zero_on_clean_send(required_env, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    monkeypatch.setenv("DOMAINSIFTER_RUN_START_TS", "1000000000")

    journal_mock = MagicMock(returncode=0, stdout="Wrote 5 domains to ...\n", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=journal_mock))

    smtp_instance = MagicMock()
    smtp_cm = MagicMock()
    smtp_cm.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_cm.__exit__ = MagicMock(return_value=None)
    monkeypatch.setattr(send_report.smtplib, "SMTP", MagicMock(return_value=smtp_cm))

    assert send_report.main(["--pipeline-exit", "0"]) == 0
    assert smtp_instance.send_message.called


def test_main_returns_zero_on_smtp_failure(required_env, monkeypatch, capsys):
    """Email send failure must NOT propagate; the wrapper relies on exit 0."""
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    journal_mock = MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=journal_mock))

    def boom_smtp(*_a, **_kw):
        raise OSError("network down")
    monkeypatch.setattr(send_report.smtplib, "SMTP", boom_smtp)

    rc = send_report.main(["--pipeline-exit", "1"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "email delivery failed" in err


def test_main_returns_zero_on_missing_env(monkeypatch, capsys):
    """A missing env var (e.g. REPORT_FROM_EMAIL) is logged but doesn't crash."""
    # Clear all of the email env vars so _build_email raises KeyError.
    for key in ("BREVO_SMTP_USER", "BREVO_SMTP_KEY", "REPORT_TO_EMAIL", "REPORT_FROM_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    journal_mock = MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(send_report.subprocess, "run", MagicMock(return_value=journal_mock))

    rc = send_report.main(["--pipeline-exit", "0"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "missing required env var" in err


# ---------------------------------------------------------------------------
# Shadow mode (2026-09-18): the classifier runs and produces real verdicts but
# deliberately does not apply them. Effective counts are all-unknown by
# construction, which would otherwise trip the blind-screen alarm EVERY day of
# the validation window — the surest way to teach an operator to ignore it.
# ---------------------------------------------------------------------------

_SHADOW_VERDICTS = (
    "snapshot_classifier: SHADOW verdicts (NOT applied to snapshot_category) "
    "— 18 legitimate, 4 parked, 3 toxic, 2 empty, 4 unknown"
)
_SHADOW_EVICT = (
    "snapshot_classifier: SHADOW would evict 3 as toxic — "
    "marketglow.com, tideblock.io, coppernest.org"
)


def test_parse_shadow_verdicts_reads_counts():
    counts = send_report.parse_shadow_verdicts(_SHADOW_VERDICTS)
    assert counts == {
        "legitimate": 18, "parked": 4, "toxic": 3, "empty": 2, "unknown": 4,
    }


def test_parse_shadow_verdicts_absent_returns_none():
    assert send_report.parse_shadow_verdicts(_CLASSIFIER_ALL_UNKNOWN) is None


def test_parse_shadow_would_evict_counts_names():
    assert send_report.parse_shadow_would_evict(_SHADOW_EVICT) == 3
    assert send_report.parse_shadow_would_evict("nothing here") == 0


def test_shadow_mode_reports_as_deliberate_not_as_breakage(required_env):
    """The banner must say 'deliberate', and the subject must NOT claim the
    screen is OFF — that alarm is reserved for the genuinely broken case."""
    log = "\n".join([_SHADOW_VERDICTS, _SHADOW_EVICT, _CLASSIFIER_ALL_UNKNOWN])
    msg = send_report._build_email(0, log, 120.0)
    body = msg.get_content()

    assert "SHADOW MODE (deliberate)" in body
    assert "3 toxic" in body
    assert "Would have evicted: 3" in body
    assert "TOXIC-DOMAIN SCREEN IS NOT RUNNING" not in body
    assert "TOXIC SCREEN OFF" not in msg["Subject"]
    assert "SCREEN IN SHADOW" in msg["Subject"]


def test_all_unknown_without_shadow_line_still_alarms_loudly(required_env):
    """Regression guard: the shadow carve-out must not disarm the real alarm.
    This is the 2026-07-23 credit-outage shape — no shadow line at all."""
    msg = send_report._build_email(0, _CLASSIFIER_ALL_UNKNOWN, 120.0)
    assert "TOXIC-DOMAIN SCREEN IS NOT RUNNING" in msg.get_content()
    assert "TOXIC SCREEN OFF" in msg["Subject"]


# ---------------------------------------------------------------------------
# Toxic eviction counts (2026-09-20). `snapshot_toxic_remembered` is the
# denylist catching a domain whose archived content could not be re-fetched —
# the number that shows the durable memory is doing work rather than sitting
# idle. It exists because a domain correctly flagged toxic on 2026-09-19 came
# back `unknown` on a failed fetch, stayed published, and got a permanent page.
# ---------------------------------------------------------------------------

_REJECTIONS = (
    "2026-09-20 12:21:32,630 INFO scripts.filter Post-enrichment filter "
    "rejections: {'snapshot_toxic': 1, 'snapshot_toxic_remembered': 3, "
    "'spam_flagged': 2, 'no_wayback_confirmed': 7}"
)


def test_parse_toxic_rejections_reads_both_counts():
    assert send_report.parse_toxic_rejections(_REJECTIONS) == (1, 3)


def test_parse_toxic_rejections_live_only():
    log = (
        "INFO scripts.filter Post-enrichment filter rejections: "
        "{'snapshot_toxic': 2, 'spam_flagged': 1}"
    )
    assert send_report.parse_toxic_rejections(log) == (2, 0)


def test_parse_toxic_rejections_absent_line_is_zero_not_error():
    assert send_report.parse_toxic_rejections("a clean log\nnothing here\n") == (0, 0)


def test_parse_toxic_rejections_empty_dict():
    log = "INFO scripts.filter Post-enrichment filter rejections: {}"
    assert send_report.parse_toxic_rejections(log) == (0, 0)


def test_toxic_counts_render_in_report_body(required_env):
    body = send_report._build_email(0, _REJECTIONS, 120.0).get_content()
    assert "Toxic evicted" in body
    assert "1 by today's check" in body
    assert "3 from memory" in body


# ---------------------------------------------------------------------------
# Common Crawl backlink-data freshness (2026-09-20).
#
# cc_source_domain_count carries scoring weight 0.30 — equal to
# wayback_snapshots — and the CC release behind it was built by hand ONCE
# (2026-05-13), then sat 4 months stale with nothing alarming, because a
# stale-but-present SQLite returns plausible numbers. These tests pin the
# backstop: the daily email reports the release's real age (from the derived
# SQLite's `meta.built_at`) and escalates to the subject line when it goes
# stale or when the weekly refresh is failing.
#
# Everything runs against temp SQLite files inside the `cc_sandbox` fixture —
# no R2, no real cache, no live APIs. Domains are invented (hard rule 1).
# ---------------------------------------------------------------------------

_CC_RELEASE = "cc-main-2026-feb-mar-apr"
_CC_RESULT_REL_PATH = "scripts/state/cc_refresh_result.json"


def _cc_iso(days_ago: float) -> str:
    """ISO-8601 Z timestamp `days_ago` days before now, as cc_refresh writes it."""
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_cc_config(
    sandbox: Path,
    *,
    release: str | None = _CC_RELEASE,
    warn_days: int | None = 45,
    result_path: str | None = _CC_RESULT_REL_PATH,
) -> Path:
    """Write a minimal scripts/config.json into the sandbox repo."""
    cc_backlinks: dict = {}
    if release is not None:
        cc_backlinks["latest_release"] = release
    refresh: dict = {}
    if warn_days is not None:
        refresh["staleness_warn_days"] = warn_days
    if result_path is not None:
        refresh["result_path"] = result_path
    cc_backlinks["refresh"] = refresh
    path = sandbox / "repo" / "scripts" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cc_backlinks": cc_backlinks}), encoding="utf-8")
    return path


def _write_cc_sqlite(
    sandbox: Path,
    *,
    release: str = _CC_RELEASE,
    built_at: str | None,
    with_meta_table: bool = True,
) -> Path:
    """Build a real (tiny) derived-SQLite stand-in with a `meta` table.

    Same key/value meta shape cc_refresh.py writes; the huge cc_apex table is
    irrelevant to the freshness check and deliberately omitted.
    """
    cache_dir = sandbox / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{release}.sqlite"
    conn = sqlite3.connect(path)
    try:
        if with_meta_table:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            rows = [("release", release), ("schema_version", "1")]
            if built_at is not None:
                rows.append(("built_at", built_at))
            conn.executemany("INSERT INTO meta VALUES (?, ?)", rows)
        else:
            conn.execute("CREATE TABLE cc_apex (apex_domain TEXT, source_domain_count INT)")
            conn.execute("INSERT INTO cc_apex VALUES ('marketglow.com', 3)")
        conn.commit()
    finally:
        conn.close()
    return path


def _write_cc_result(sandbox: Path, payload: dict | None, *, raw: str | None = None) -> Path:
    """Write the refresh result file (or arbitrary `raw` text for the
    malformed-JSON case)."""
    path = sandbox / "repo" / _CC_RESULT_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return path


def _cc_result(action: str, *, days_ago: float = 1.0, reason: str | None = None) -> dict:
    return {
        "action": action,
        "release": _CC_RELEASE,
        "previous_release": None,
        "rows": 124_646_710,
        "reason": reason,
        "finished_at": _cc_iso(days_ago),
    }


# --- fresh data: reports the age, stays quiet -------------------------------


def test_cc_fresh_release_reports_age_and_does_not_escalate(required_env, cc_sandbox):
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(12))
    _write_cc_result(cc_sandbox, _cc_result("noop", days_ago=2))

    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=42.0)
    body = msg.get_content()
    assert f"CC backlink data : {_CC_RELEASE}, built 12d ago" in body
    assert "last refresh: noop" in body
    assert "🚨" not in msg["Subject"]
    assert "COMMON CRAWL" not in body


def test_cc_freshness_reads_built_at_from_sqlite_meta(cc_sandbox):
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(9))
    cc = send_report.cc_backlink_freshness()
    assert cc.release == _CC_RELEASE
    assert cc.age_source == "sqlite-meta"
    assert 8.5 < cc.age_days < 9.5
    assert cc.stale is False
    assert cc.escalates is False


def test_cc_freshness_does_not_write_to_the_sqlite(cc_sandbox):
    """The check runs every day; it must be read-only (`?mode=ro`)."""
    _write_cc_config(cc_sandbox)
    path = _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(3))
    before = (path.stat().st_mtime_ns, path.stat().st_size)
    send_report.cc_backlink_freshness()
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before
    # No -wal / -journal sidecar left behind either.
    assert not (path.parent / f"{path.name}-wal").exists()
    assert not (path.parent / f"{path.name}-journal").exists()


# --- stale data: escalates --------------------------------------------------


def test_cc_stale_release_escalates_subject_and_fires_banner(required_env, cc_sandbox):
    """THE regression guard: a release that quietly aged past the threshold."""
    _write_cc_config(cc_sandbox, warn_days=45)
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(130))
    _write_cc_result(cc_sandbox, _cc_result("noop", days_ago=1))

    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=42.0)
    subject = msg["Subject"]
    body = msg.get_content()
    assert "🚨 CC DATA STALE" in subject
    assert "SUCCESS" in subject  # exit code really was 0 — that's the point
    assert "🚨 STALE (> 45d)" in body
    assert "built 130d ago" in body
    assert "COMMON CRAWL BACKLINK DATA IS STALE" in body
    assert "scoring weight 0.30" in body


def test_cc_staleness_threshold_comes_from_config_not_hardcoded(cc_sandbox):
    """Hard rule 9: the window is config-driven. 60d old is fresh at
    warn_days=90 and stale at warn_days=45."""
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(60))

    _write_cc_config(cc_sandbox, warn_days=90)
    assert send_report.cc_backlink_freshness().stale is False

    _write_cc_config(cc_sandbox, warn_days=45)
    stale = send_report.cc_backlink_freshness()
    assert stale.stale is True
    assert stale.warn_days == 45


def test_cc_verification_failed_escalates_even_when_data_is_fresh(required_env, cc_sandbox):
    """A failing refresh is the reason data goes stale — alarm before it does."""
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(5))
    _write_cc_result(
        cc_sandbox,
        _cc_result("verification_failed", days_ago=1, reason="cc_apex rows 41200000 below floor"),
    )

    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=42.0)
    body = msg.get_content()
    assert "🚨 CC REFRESH FAILED" in msg["Subject"]
    assert "COMMON CRAWL REFRESH IS FAILING" in body
    assert "last refresh: verification_failed (cc_apex rows 41200000 below floor)" in body


def test_cc_discovery_failed_escalates(required_env, cc_sandbox):
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(5))
    _write_cc_result(cc_sandbox, _cc_result("discovery_failed", days_ago=1, reason="all HEADs 404"))

    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=42.0)
    assert "🚨 CC REFRESH FAILED" in msg["Subject"]
    assert send_report.cc_backlink_freshness().refresh_failed is True


def test_cc_skipped_action_alone_does_not_escalate(required_env, cc_sandbox):
    """`skipped` is benign — the weekly timer retries next Sunday."""
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(4))
    _write_cc_result(cc_sandbox, _cc_result("skipped", days_ago=1, reason="blackout window"))

    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=42.0)
    assert "🚨" not in msg["Subject"]
    assert "last refresh: skipped (blackout window)" in msg.get_content()


def test_cc_stale_alarm_coexists_with_the_llm_alarms(required_env, cc_sandbox):
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(200))
    msg = send_report._build_email(
        pipeline_exit=0, log=_CLASSIFIER_ALL_UNKNOWN + "\n", duration_sec=42.0,
    )
    subject = msg["Subject"]
    assert "🚨 TOXIC SCREEN OFF" in subject
    assert "🚨 CC DATA STALE" in subject


# --- fallback to the refresh result file ------------------------------------


def test_cc_falls_back_to_result_file_when_sqlite_not_cached(required_env, cc_sandbox):
    """Not having the 6.6 GB SQLite locally is a legitimate state."""
    _write_cc_config(cc_sandbox)
    _write_cc_result(cc_sandbox, _cc_result("installed", days_ago=3))

    cc = send_report.cc_backlink_freshness()
    assert cc.age_source == "refresh-result"
    assert 2.5 < cc.age_days < 3.5
    assert cc.stale is False
    body = send_report._build_email(pipeline_exit=0, log="", duration_sec=1.0).get_content()
    assert "last refresh run 3d ago" in body
    assert "no local SQLite cache" in body


def test_cc_fallback_never_downloads_the_sqlite_from_r2(cc_sandbox):
    """Hard requirement: building an email must never pull 6.6 GB from R2."""
    from scripts.enrichment import cc_backlinks

    _write_cc_config(cc_sandbox)
    _write_cc_result(cc_sandbox, _cc_result("installed", days_ago=3))
    exploder = MagicMock(side_effect=AssertionError("attempted to download the CC SQLite"))
    with patch.object(cc_backlinks, "_ensure_local_sqlite", exploder), \
            patch.object(cc_backlinks, "_get_connection", exploder):
        cc = send_report.cc_backlink_freshness()
    assert cc.age_source == "refresh-result"
    exploder.assert_not_called()
    assert not (cc_sandbox / "cache" / f"{_CC_RELEASE}.sqlite").exists()


def test_cc_fallback_marks_stale_when_last_refresh_run_is_ancient(required_env, cc_sandbox):
    """No SQLite AND the newest refresh record is months old → the timer is
    not running, so the data cannot be current either."""
    _write_cc_config(cc_sandbox, warn_days=45)
    _write_cc_result(cc_sandbox, _cc_result("noop", days_ago=120))

    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=1.0)
    assert "🚨 CC DATA STALE" in msg["Subject"]
    assert "last refresh run 120d ago" in msg.get_content()


def test_cc_unparseable_built_at_falls_back_to_result_file(cc_sandbox):
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at="not-a-timestamp")
    _write_cc_result(cc_sandbox, _cc_result("installed", days_ago=2))

    cc = send_report.cc_backlink_freshness()
    assert cc.age_source == "refresh-result"
    assert "no usable built_at" in (cc.note or "")


# --- no signal at all: "unknown", never a crash, never a false alarm --------


def test_cc_no_sqlite_and_no_result_file_reports_unknown(required_env, cc_sandbox):
    _write_cc_config(cc_sandbox)
    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=1.0)
    body = msg.get_content()
    assert f"CC backlink data : {_CC_RELEASE}, age unknown" in body
    assert "last refresh: (no result file)" in body
    # Unknown is not proof of staleness — do not cry wolf.
    assert "🚨" not in msg["Subject"]


def test_cc_missing_config_reports_unknown_without_crashing(required_env, cc_sandbox):
    """No config.json at all (CONFIG_PATH points into an empty sandbox)."""
    cc = send_report.cc_backlink_freshness()
    assert cc.age_days is None
    assert cc.age_source == "unknown"
    assert cc.stale is False
    body = send_report._build_email(pipeline_exit=0, log="", duration_sec=1.0).get_content()
    assert "CC backlink data : (no release configured), age unknown" in body


def test_cc_malformed_config_json_reports_unknown(required_env, cc_sandbox):
    path = cc_sandbox / "repo" / "scripts" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json at all", encoding="utf-8")
    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=1.0)
    assert "age unknown" in msg.get_content()
    assert "🚨" not in msg["Subject"]


def test_cc_corrupt_sqlite_reports_unknown_without_crashing(required_env, cc_sandbox):
    _write_cc_config(cc_sandbox, result_path=None)
    cache_dir = cc_sandbox / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{_CC_RELEASE}.sqlite").write_bytes(b"\x00\x01not a database\xff")

    cc = send_report.cc_backlink_freshness()
    assert cc.age_source == "unknown"
    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=1.0)
    assert "age unknown" in msg.get_content()
    assert "🚨" not in msg["Subject"]


def test_cc_sqlite_without_meta_table_reports_unknown(cc_sandbox):
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at=None, with_meta_table=False)
    assert send_report.cc_backlink_freshness().age_source == "unknown"


def test_cc_empty_sqlite_file_reports_unknown(cc_sandbox):
    """A zero-byte file is what a killed download leaves behind."""
    _write_cc_config(cc_sandbox)
    cache_dir = cc_sandbox / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{_CC_RELEASE}.sqlite").write_bytes(b"")
    assert send_report.cc_backlink_freshness().age_source == "unknown"


@pytest.mark.parametrize("raw", [
    "not json at all",
    "",
    "[1, 2, 3]",
    '{"action": "installed", "finished_at": "yesterday-ish"}',
    '{"action": null, "finished_at": null}',
    '{"action": 42, "finished_at": 42}',
])
def test_cc_malformed_result_file_reports_unknown_without_crashing(
    required_env, cc_sandbox, raw,
):
    _write_cc_config(cc_sandbox)
    _write_cc_result(cc_sandbox, None, raw=raw)

    cc = send_report.cc_backlink_freshness()
    assert cc.age_days is None
    assert cc.age_source == "unknown"
    assert cc.stale is False
    msg = send_report._build_email(pipeline_exit=0, log="", duration_sec=1.0)
    assert "age unknown" in msg.get_content()
    assert "🚨" not in msg["Subject"]


def test_cc_result_file_is_a_directory_reports_unknown(cc_sandbox):
    """OSError path: something created a directory where the file belongs."""
    _write_cc_config(cc_sandbox)
    (cc_sandbox / "repo" / _CC_RESULT_REL_PATH).mkdir(parents=True, exist_ok=True)
    assert send_report.cc_backlink_freshness().age_source == "unknown"


def test_cc_freshness_never_raises_even_if_config_load_explodes(cc_sandbox):
    """Belt-and-braces: an unexpected exception anywhere inside degrades to
    'unknown' rather than propagating out of the reporter."""
    with patch.object(
        send_report, "_load_config", MagicMock(side_effect=RuntimeError("boom")),
    ):
        cc = send_report.cc_backlink_freshness()
    assert cc.age_source == "unknown"
    assert cc.stale is False
    assert "freshness check failed (RuntimeError: boom)" in (cc.note or "")


def test_main_exits_zero_when_cc_stale_banner_fires(required_env, cc_sandbox, monkeypatch):
    """Reporter alarms must never change the pipeline's exit code (rule 17)."""
    _write_cc_config(cc_sandbox)
    _write_cc_sqlite(cc_sandbox, built_at=_cc_iso(365))
    monkeypatch.setenv("INVOCATION_ID", "cc-stale-1")
    monkeypatch.setattr(
        send_report,
        "_capture_journal",
        MagicMock(return_value="Wrote 12 domains to src/data/daily-domains.json\n"),
    )
    sent: list[EmailMessage] = []
    monkeypatch.setattr(send_report, "_send", lambda msg: sent.append(msg))

    assert send_report.main(["--pipeline-exit", "0"]) == 0
    assert "🚨 CC DATA STALE" in sent[0]["Subject"]


# --- timestamp parsing -----------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("2026-09-20T18:00:00Z", datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)),
    ("2026-09-20T18:00:00z", datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)),
    ("2026-09-20T18:00:00+00:00", datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)),
    ("2026-09-20T20:00:00+02:00", datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)),
    ("2026-09-20T18:00:00", datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)),
])
def test_parse_iso8601_utc_accepts_the_shapes_cc_refresh_writes(value, expected):
    assert send_report._parse_iso8601_utc(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "   ", "not-a-timestamp", 1758393600, [], {}, "2026-13-45T99:99:99Z",
])
def test_parse_iso8601_utc_returns_none_for_junk(value):
    assert send_report._parse_iso8601_utc(value) is None
