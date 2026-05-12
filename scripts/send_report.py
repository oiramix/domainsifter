"""Post-run email report for the OVH-self-hosted daily pipeline.

Invoked by scripts/run-daily.sh from an EXIT trap, so it fires on BOTH
success and failure paths. Captures the current systemd run's journal
(filtered by INVOCATION_ID so we get exactly THIS run, not the whole unit
history), extracts a few quick-glance signals for the email header, and
sends the full raw log as the body via Brevo SMTP.

Delivery: smtp-relay.brevo.com:587 with STARTTLS.

Env vars (loaded from .env via systemd's EnvironmentFile=):
    BREVO_SMTP_USER     — Brevo SMTP login
    BREVO_SMTP_KEY      — Brevo SMTP password / API key
    REPORT_TO_EMAIL     — destination address
    REPORT_FROM_EMAIL   — From: header (must be a Brevo-verified sender)
    INVOCATION_ID       — set by systemd; the unit invocation ID
    DOMAINSIFTER_RUN_START_TS — unix-ts of run start, set by the wrapper

Operator-mode flag: --journal-since <expr> bypasses the INVOCATION_ID path
and instead captures `domainsifter.service` journal entries since the given
time (passed verbatim to journalctl --since, e.g. "8h ago" or
"2026-05-11 06:00"). For one-off validation runs invoked outside systemd.

Exit code: ALWAYS 0. Email-send failures log to stderr but never propagate;
we don't want the wrapper to conflate "pipeline failed" with "couldn't
send the notification about the failure."

Signal handling: this module runs from a bash EXIT trap in run-daily.sh,
which fires correctly on SIGTERM / SIGINT. SIGKILL is uncatchable by Unix
design and would bypass the email — but systemd only escalates to SIGKILL
after TimeoutStopSec=90s, which is a much bigger problem than the missing
email anyway.
"""

from __future__ import annotations

import argparse
import os
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage

# Brevo accepts up to 5 MB total; cap the plain-text body at 500 KB with
# head+tail kept and the middle truncated. Typical days are 50-100 KB so
# this only bites on pathological logs (e.g. breaker re-tripping in a tight
# loop). Anyone wanting the full log goes to journalctl on the server.
_MAX_LOG_BYTES = 500_000


def _capture_journal(invocation_id: str, since: str | None = None) -> str:
    """Return the raw journal text for the run we're reporting on.

    Two modes:
      - Default (production): filter by `_SYSTEMD_INVOCATION_ID=<id>` so we
        get exactly THIS systemd-spawned run.
      - Operator override (`since` arg, from --journal-since): filter by
        `-u domainsifter.service --since <expr>` so the operator can capture
        a prior run's logs for one-off validation outside systemd.
    """
    if since:
        cmd = [
            "journalctl",
            "-u", "domainsifter.service",
            "--since", since,
            "--no-pager",
            "-o", "cat",
        ]
    elif invocation_id:
        cmd = [
            "journalctl",
            f"_SYSTEMD_INVOCATION_ID={invocation_id}",
            "--no-pager",
            "-o", "cat",
        ]
    else:
        return (
            "(INVOCATION_ID not set and --journal-since not passed — "
            "journal log unavailable; running outside systemd?)"
        )
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"(journalctl invocation failed: {exc})"
    if result.returncode != 0:
        return (
            f"(journalctl returned exit={result.returncode}: "
            f"{result.stderr.strip() or 'no stderr'})"
        )
    return result.stdout or "(empty journal)"


def _memory_peak_bytes() -> int | None:
    """Peak resident memory the run consumed, in bytes. Returns None when
    no usable source is available.

    Source order (changed 2026-05-12):
      1. `DOMAINSIFTER_MEMORY_PEAK_BYTES` env var, exported by
         scripts/run-daily.sh from `/sys/fs/cgroup/<unit>/memory.peak`
         BEFORE this reporter is invoked. The wrapper reads the cgroup
         file directly while the cgroup is still live — avoids a race
         where `systemctl show -p MemoryPeak` returns empty because
         systemd has already cleared the unit-level property by the time
         the EXIT-trap-spawned subprocess gets to it.
      2. `systemctl show -p MemoryPeak --value domainsifter.service`.
         Retained for non-wrapper invocations (operator-mode validation
         outside systemd, older deploys without the wrapper change). On
         the trap path this is expected to return empty.

    Either source returning None / empty / non-numeric is treated as
    "no signal"; the caller renders "(unavailable)" in the email body.
    """
    env_val = os.environ.get("DOMAINSIFTER_MEMORY_PEAK_BYTES", "").strip()
    if env_val.isdigit():
        return int(env_val)

    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "MemoryPeak", "--value", "domainsifter.service"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value or not value.isdigit():
        return None
    return int(value)


def _format_bytes(n: int | None) -> str:
    if n is None:
        return "(unavailable)"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _format_memory_peak(n: int | None) -> str:
    """Render a byte count as MB or GB depending on magnitude.

    Picks GB at >= 1 GiB, MB otherwise (the realistic range for the
    pipeline's memory peak — sub-MB doesn't happen for a Python process
    that loads zone files; sub-GB is small-TLD days, GB-scale is large-
    TLD days, multi-GB is the future .com case).

    Precision: 1 decimal at >= 100, 2 decimals below — so "743.2 MB" and
    "1.83 GB" both read naturally and "50.00 MB" / "127.7 GB" both keep
    three significant figures.
    """
    if n is None:
        return "(unavailable)"
    if n < 1024 ** 3:
        size = n / (1024 ** 2)
        unit = "MB"
    else:
        size = n / (1024 ** 3)
        unit = "GB"
    decimals = 1 if size >= 100 else 2
    return f"{size:.{decimals}f} {unit}"


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "(unavailable)"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def _extract_domain_count(log: str) -> int | None:
    """Find the pipeline's "Wrote N domains to <path>" line in output.py.
    Returns N (the final published count) or None if not found.

    Matches both bare ("Wrote 47 domains to ...") and logger-prefixed
    ("2026-05-11 ... INFO scripts.output Wrote 47 domains to ...") forms.
    If multiple matches exist, the last one wins — handles the unlikely
    case of intermediate writes during a run."""
    target = None
    for line in log.splitlines():
        if "Wrote " in line and " domains to " in line:
            tokens = line.split()
            for i, tok in enumerate(tokens):
                if tok == "Wrote" and i + 1 < len(tokens):
                    try:
                        target = int(tokens[i + 1])
                    except ValueError:
                        continue
    return target


def _count_circuit_breaker_trips(log: str) -> int:
    """Count "Circuit breaker [...] opened" warnings in the log."""
    return sum(
        1 for line in log.splitlines()
        if "Circuit breaker [" in line and "opened" in line
    )


def _count_tld_failures(log: str) -> int:
    """Count per-TLD zone download/parse failures from pipeline.collect_drops."""
    return sum(
        1 for line in log.splitlines()
        if "zone download failed" in line or "zone parse failed" in line
    )


def _truncate(log: str, max_bytes: int = _MAX_LOG_BYTES) -> str:
    """If log exceeds max_bytes, keep head + tail and replace middle with a
    notice. Preserves the most-useful portions (start: config + first errors;
    end: final tally + breakers + exit) within Brevo's 5MB email cap."""
    encoded = log.encode("utf-8")
    if len(encoded) <= max_bytes:
        return log
    keep_each = max_bytes // 2
    head = encoded[:keep_each].decode("utf-8", errors="replace")
    tail = encoded[-keep_each:].decode("utf-8", errors="replace")
    removed = len(encoded) - 2 * keep_each
    return (
        head
        + f"\n\n[... {removed} bytes truncated; full log on server: "
        + "`journalctl _SYSTEMD_INVOCATION_ID=$INVOCATION_ID` ...]\n\n"
        + tail
    )


def _build_email(pipeline_exit: int, log: str, duration_sec: float | None) -> EmailMessage:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    success = pipeline_exit == 0
    verdict_emoji = "✅" if success else "❌"
    verdict_word = "SUCCESS" if success else "FAILED"

    domain_count = _extract_domain_count(log)
    breaker_trips = _count_circuit_breaker_trips(log)
    tld_failures = _count_tld_failures(log)
    mem_peak = _memory_peak_bytes()

    count_part = f"{domain_count} domains" if domain_count is not None else "domain count unknown"
    subject = f"[DomainSifter] Daily run {date_str} UTC: {verdict_emoji} {verdict_word} — {count_part}"

    header = [
        f"Verdict          : {verdict_emoji} {verdict_word} (exit code {pipeline_exit})",
        f"Date (UTC)       : {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Wall-clock       : {_format_duration(duration_sec)}",
        f"Memory peak      : {_format_memory_peak(mem_peak)}",
        f"Domains published: {domain_count if domain_count is not None else '(unknown — log parse miss)'}",
        f"Breakers tripped : {breaker_trips}",
        f"TLD failures     : {tld_failures}",
    ]

    body = (
        "DomainSifter daily run report\n"
        "=============================\n"
        + "\n".join(header)
        + "\n\n"
        + "Full run log (journalctl, this invocation only):\n"
        + "------------------------------------------------\n"
        + _truncate(log)
        + "\n"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["REPORT_FROM_EMAIL"]
    msg["To"] = os.environ["REPORT_TO_EMAIL"]
    msg.set_content(body)
    return msg


def _send(msg: EmailMessage) -> None:
    user = os.environ["BREVO_SMTP_USER"]
    password = os.environ["BREVO_SMTP_KEY"]
    with smtplib.SMTP("smtp-relay.brevo.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(msg)


def _resolve_duration() -> float | None:
    """Wall-clock from the wrapper-exported DOMAINSIFTER_RUN_START_TS env."""
    raw = os.environ.get("DOMAINSIFTER_RUN_START_TS", "").strip()
    if not raw or not raw.isdigit():
        return None
    return time.time() - int(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily run email report")
    parser.add_argument(
        "--pipeline-exit",
        type=int,
        required=True,
        help="Exit code of the upstream pipeline run (0 = success).",
    )
    parser.add_argument(
        "--journal-since",
        default=None,
        help=(
            "OPERATOR USE ONLY: bypass INVOCATION_ID-based journal capture "
            "and instead read domainsifter.service journal entries since "
            "the given time (passed to journalctl --since, e.g. '8h ago' "
            "or '2026-05-11 06:00'). Useful for one-off validation runs "
            "invoked outside systemd."
        ),
    )
    args = parser.parse_args(argv)

    invocation_id = os.environ.get("INVOCATION_ID", "")
    log = _capture_journal(invocation_id, since=args.journal_since)
    duration = _resolve_duration()

    try:
        msg = _build_email(args.pipeline_exit, log, duration)
    except KeyError as exc:
        print(
            f"send_report: missing required env var {exc}; skipping email",
            file=sys.stderr,
        )
        return 0

    try:
        _send(msg)
    except Exception as exc:  # broad: smtplib raises a zoo of subclasses
        print(f"send_report: email delivery failed: {exc}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
