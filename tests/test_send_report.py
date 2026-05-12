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
import os
from email.message import EmailMessage
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
