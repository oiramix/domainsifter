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
import re
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


def _rdap_429_strikes(log: str) -> int:
    """Count individual RDAP 429 strikes across all hosts this run.

    Matches the per-strike WARNING from _circuit_breaker._request_honoring_
    retry_after ("429 from <host> — strike N/L: ..."). Lets the operator
    distinguish a "1 strike, recovered" day from a "hit the limit, stopped"
    day. Counts EVERY strike including the limit-reaching one."""
    return sum(
        1 for line in log.splitlines()
        if "429 from " in line and "— strike " in line
    )


def _rdap_429_stops(log: str) -> list[str]:
    """Return the per-host STOP accounting lines for hosts that hit the 429
    strike limit (NOT 403 — those are surfaced by _rdap_403_alarms).

    Matches the pipeline's "RDAP host [<host>] STOPPED (429) after ..." warning
    emitted in _check_availability_concurrent — one line per host that reached
    its strike cap, carrying which host, when, and how many candidates were
    left unchecked. Surfaced verbatim in the report header. The "(429)" reason
    filter excludes 403 hard-stops, which get their own catastrophic banner."""
    return [
        line.strip()
        for line in log.splitlines()
        if "STOPPED (429)" in line and "left UNCHECKED" in line
    ]


def _rdap_403_alarms(log: str) -> list[str]:
    """Return RDAP 403 CATASTROPHIC-block lines from rdap.check_availability.

    A 403 is an outright block (typically IP-level), categorically worse than
    a 429 rate limit. These MUST be impossible to miss — they drive the alarm
    banner and escalate the subject line."""
    return [
        line.strip()
        for line in log.splitlines()
        if "403 FORBIDDEN" in line and "CATASTROPHIC IP-BLOCK" in line
    ]


# --- LLM-stage signals (added 2026-09-18) ----------------------------------
#
# Why these exist: on 2026-07-23 the Anthropic credit balance hit zero. Both
# LLM stages (Phase 2 ranker, snapshot classifier) failed SOFT — the pipeline
# still exited 0 and this report still said "SUCCESS" — so nobody noticed for
# roughly eight weeks. During that window the toxic-domain screen
# (snapshot_category == "toxic" -> reject, in filter.py) was wide open and
# every published domain went out unscreened.
#
# The parsers below turn that silent degradation into a subject-line alarm.
# They are deliberately tolerant: a missed parse must degrade to a
# "(unavailable)"-style placeholder, never raise, because main() must always
# exit 0.

# The em dash is what the pipeline logs, but accept a plain hyphen too so a
# transport that mangles non-ASCII doesn't silently disable the alarm.
_DASH = r"[—-]"

_LLM_BACKEND_RE = re.compile(r"llm_backend\[([^\]\s]+)\]:\s*ok\b")
_RANKER_SCORED_RE = re.compile(r"Phase 2 ranker:\s*(\d+)\s+scored\b")
_RANKER_FALLBACK_RE = re.compile(r"Phase 2 ranker FALLBACK\s*" + _DASH + r"\s*(.+)")
_CLASSIFIER_RESULTS_RE = re.compile(
    r"snapshot_classifier:\s*results\s*" + _DASH + r"\s*"
    r"(\d+)\s+legitimate,\s*(\d+)\s+parked,\s*(\d+)\s+toxic,\s*"
    r"(\d+)\s+empty,\s*(\d+)\s+unknown"
)

CLASSIFIER_CATEGORIES = ("legitimate", "parked", "toxic", "empty", "unknown")


def _llm_backend(log: str) -> str | None:
    """Return the LLM backend name(s) that actually served a call this run.

    Parses scripts/llm_backend.py's "llm_backend[<name>]: ok — ..." line
    (<name> is "claude_code" or "api"). Returns None when no such line
    appears, which means NO LLM call succeeded this run — on its own that is
    not proof of breakage (a zero-candidate day makes no calls), but combined
    with an all-unknown classifier it is the smoking gun.

    If more than one backend served calls (e.g. a mid-run rollback), all
    distinct names are returned in first-seen order, comma-joined."""
    seen: list[str] = []
    for match in _LLM_BACKEND_RE.finditer(log):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return ", ".join(seen) if seen else None


def _phase2_ranker_outcome(log: str) -> str | None:
    """Summarise the Phase 2 ranker's outcome as a one-liner.

    FALLBACK wins over the scored tally, because a fallback run logs BOTH
    (it scores, finds too few above the gate, then reverts to mechanical
    selection). The published list on a fallback day does NOT reflect
    quality ranking, so that is the fact worth surfacing.

    Returns "FALLBACK — <reason>", "RANKER (<n> scored)", or None when the
    ranker left no trace in the log (disabled / empty input / never reached).
    """
    fallbacks = _RANKER_FALLBACK_RE.findall(log)
    if fallbacks:
        # Last fallback wins. Trim the "(above_gate=A < min_eligible=B)"
        # suffix so the header line stays short; the raw line is still in
        # the full log below.
        reason = fallbacks[-1].strip()
        reason = reason.split(" (above_gate=")[0].strip()
        return f"FALLBACK — {reason}" if reason else "FALLBACK"
    scored = _RANKER_SCORED_RE.findall(log)
    if scored:
        return f"RANKER ({scored[-1]} scored)"
    return None


_TOXIC_REJECTIONS_RE = re.compile(
    r"Post-enrichment filter rejections:\s*(\{[^\n]*\})"
)


def parse_toxic_rejections(log: str) -> tuple[int, int]:
    """(evicted_by_live_check, evicted_from_memory) this run.

    Parsed from filter.py's rejection tally. `snapshot_toxic` is a domain
    this run's classifier read and judged abusive. `snapshot_toxic_remembered`
    is one the DENYLIST caught — a domain we classified toxic on some earlier
    run whose archived content could not be fetched this time.

    That second number is the whole point of the denylist: on 2026-09-20 a
    domain correctly flagged toxic the previous day came back `unknown`
    because archive.org failed, stayed published, and was given a permanent
    page. Surfacing the count is how we know the memory is earning its keep
    rather than quietly doing nothing.
    """
    live = remembered = 0
    for match in _TOXIC_REJECTIONS_RE.finditer(log):
        blob = match.group(1)
        for key, setter in (("snapshot_toxic_remembered", "r"), ("snapshot_toxic", "l")):
            hit = re.search(rf"'{key}':\s*(\d+)", blob)
            if hit:
                if setter == "r":
                    remembered = int(hit.group(1))
                else:
                    live = int(hit.group(1))
        # A run logs this line once; last occurrence wins if it ever repeats.
    return live, remembered


def _snapshot_classifier_counts(log: str) -> dict[str, int] | None:
    """Parse the classifier's per-category tally line.

    Returns {"legitimate": A, "parked": B, "toxic": C, "empty": D,
    "unknown": E} or None when the line is absent (classifier never ran).
    Last match wins, mirroring _extract_domain_count."""
    matches = _CLASSIFIER_RESULTS_RE.findall(log)
    if not matches:
        return None
    last = matches[-1]
    return {name: int(value) for name, value in zip(CLASSIFIER_CATEGORIES, last)}


def _count_credit_balance_errors(log: str) -> int:
    """Count Anthropic "credit balance is too low" occurrences.

    This is the exact phrase from the API's 400 response when the account is
    out of credit — the root cause of the 2026-07-23 silent outage. Surfaced
    as the likely-cause line under the degradation banners."""
    return log.count("credit balance is too low")


_SHADOW_VERDICTS_RE = re.compile(
    r"snapshot_classifier:\s*SHADOW verdicts[^\n]*?"
    r"(\d+)\s+legitimate,\s*(\d+)\s+parked,\s*(\d+)\s+toxic,\s*"
    r"(\d+)\s+empty,\s*(\d+)\s+unknown"
)
_SHADOW_EVICT_RE = re.compile(r"snapshot_classifier:\s*SHADOW would evict\s+(\d+)\s+as toxic")


def parse_shadow_verdicts(log: str) -> dict[str, int] | None:
    """Counts from the classifier's SHADOW line, or None when not in shadow.

    Shadow mode means the classifier ran and produced real verdicts but
    deliberately did not apply them, so `snapshot_category` is all-unknown by
    construction. Without this, the blind-screen alarm would fire every single
    day of the validation window and train the operator to ignore it — the
    precise habit that let the 2026-07-23 outage run for ~8 weeks.
    """
    match = None
    for match in _SHADOW_VERDICTS_RE.finditer(log):
        pass
    if match is None:
        return None
    keys = ("legitimate", "parked", "toxic", "empty", "unknown")
    return {k: int(match.group(i + 1)) for i, k in enumerate(keys)}


def parse_shadow_would_evict(log: str) -> int:
    """How many domains the armed gate would have dropped this run."""
    match = None
    for match in _SHADOW_EVICT_RE.finditer(log):
        pass
    return int(match.group(1)) if match else 0


def _classifier_is_blind(counts: dict[str, int] | None) -> bool:
    """True when the classifier processed >0 domains and EVERY one came back
    "unknown" — i.e. the toxic-domain screen rejected nothing because it
    learned nothing. This is the condition that went unnoticed for 8 weeks."""
    if not counts:
        return False
    total = sum(counts.values())
    return total > 0 and counts.get("unknown", 0) == total


def _format_classifier_counts(counts: dict[str, int] | None) -> str:
    """Render the classifier tally for the header, or a placeholder."""
    if not counts:
        return "(no classifier line in log)"
    return ", ".join(f"{counts.get(name, 0)} {name}" for name in CLASSIFIER_CATEGORIES)


def _likely_cause_line(credit_errors: int, llm_backend: str | None) -> str:
    """One line naming the most probable root cause of an LLM degradation."""
    if credit_errors:
        return (
            f"Likely cause     : {credit_errors} x 'credit balance is too low' in this "
            "run's log — top up the Anthropic account.\n"
        )
    if llm_backend is None:
        return (
            "Likely cause     : no 'llm_backend[...]: ok' line at all — no LLM call "
            "succeeded this run (backend misconfigured, CLI missing, or key unset).\n"
        )
    return (
        f"Likely cause     : backend '{llm_backend}' answered at least once, so check "
        "per-call failures in the log below.\n"
    )


def _truncate(log: str, max_bytes: int = _MAX_LOG_BYTES) -> str:
    """If log exceeds max_bytes, keep head + tail and replace middle with a
    notice. Preserves the most-useful portions (start: config + first errors;
    end: final tally + breakers + exit) within Brevo's 5MB email cap."""
    # errors="replace" so a lone surrogate in the journal (rare, but it has
    # happened) cannot raise UnicodeEncodeError out of main(). The round-trip
    # on the short path normalises those bytes away too, so the email always
    # ships. Suppressing the report is the worst outcome here: the alarm
    # banners this report carries are the whole point.
    encoded = log.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
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
    rdap_strikes = _rdap_429_strikes(log)
    rdap_stops = _rdap_429_stops(log)
    rdap_403s = _rdap_403_alarms(log)
    mem_peak = _memory_peak_bytes()

    llm_backend = _llm_backend(log)
    ranker_outcome = _phase2_ranker_outcome(log)
    classifier_counts = _snapshot_classifier_counts(log)
    toxic_live, toxic_remembered = parse_toxic_rejections(log)
    credit_errors = _count_credit_balance_errors(log)
    shadow_counts = parse_shadow_verdicts(log)
    shadow_would_evict = parse_shadow_would_evict(log) if shadow_counts else 0
    # In shadow mode the screen genuinely isn't evicting, so this still alarms
    # — but as a DELIBERATE state with a different banner, not as a breakage.
    # A permanent identical banner across a validation window is how an
    # operator learns to ignore it.
    classifier_blind = _classifier_is_blind(classifier_counts) and not shadow_counts
    classifier_shadow = bool(shadow_counts)
    ranker_fell_back = bool(ranker_outcome and ranker_outcome.startswith("FALLBACK"))

    count_part = f"{domain_count} domains" if domain_count is not None else "domain count unknown"
    # Subject-line escalations, most-catastrophic first. A 403 is an outright
    # IP block; a blind classifier means the toxic-domain screen published
    # unscreened domains; a ranker fallback means the list is unranked. All
    # three can happen on an exit-code-0 run, which is exactly why they have
    # to reach the subject line and not just the body.
    alarms: list[str] = []
    if rdap_403s:
        alarms.append("🚨 RDAP 403 BLOCK")
    if classifier_blind:
        alarms.append("🚨 TOXIC SCREEN OFF")
    elif classifier_shadow:
        alarms.append("👁 SCREEN IN SHADOW")
    if ranker_fell_back:
        alarms.append("🚨 PHASE 2 FALLBACK")
    # " / " between alarms keeps the single-alarm subject byte-identical to
    # the pre-2026-09-18 format ("🚨 RDAP 403 BLOCK — Daily run ...").
    alarm_prefix = " / ".join(alarms) + " — " if alarms else ""
    subject = (
        f"[DomainSifter] {alarm_prefix}Daily run {date_str} UTC: "
        f"{verdict_emoji} {verdict_word} — {count_part}"
    )

    header = [
        f"Verdict          : {verdict_emoji} {verdict_word} (exit code {pipeline_exit})",
        f"Date (UTC)       : {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Wall-clock       : {_format_duration(duration_sec)}",
        f"Memory peak      : {_format_memory_peak(mem_peak)}",
        f"Domains published: {domain_count if domain_count is not None else '(unknown — log parse miss)'}",
        f"Breakers tripped : {breaker_trips}",
        f"TLD failures     : {tld_failures}",
        f"RDAP 429 strikes : {rdap_strikes}",
        f"RDAP 429 stops   : {len(rdap_stops)} (host(s) hit the strike limit)",
        f"RDAP 403 blocks  : {len(rdap_403s)}",
        f"LLM backend      : {llm_backend or '(none used)'}",
        f"Phase 2 ranker   : {ranker_outcome or '(no ranker line in log)'}",
        f"Snapshot classes : {_format_classifier_counts(classifier_counts)}",
        f"Toxic evicted    : {toxic_live} by today's check, "
        f"{toxic_remembered} from memory (denylist)",
    ]
    if credit_errors:
        header.append(f"Credit errors    : {credit_errors} ('credit balance is too low')")

    # Loud alarm/notice blocks above the log, built only when there is
    # something to report so clean days stay clean.
    alert_blocks = ""
    if rdap_403s:
        alert_blocks += (
            "\n🚨🚨 RDAP 403 — CATASTROPHIC IP-BLOCK DETECTED 🚨🚨\n"
            "-------------------------------------------------\n"
            "A registry returned 403 FORBIDDEN (an outright block, NOT a rate\n"
            "limit). Investigate immediately — the egress IP is likely blocked.\n"
            + "\n".join(rdap_403s)
            + "\n"
        )
    if classifier_blind:
        total = sum(classifier_counts.values()) if classifier_counts else 0
        alert_blocks += (
            "\n🚨🚨 TOXIC-DOMAIN SCREEN IS NOT RUNNING 🚨🚨\n"
            "--------------------------------------------\n"
            f"The snapshot classifier processed {total} domain(s) and returned\n"
            f"'unknown' for ALL {total} of them (0 legitimate, 0 parked, 0 toxic,\n"
            "0 empty). 'unknown' is the classifier's failure value, so the\n"
            "toxic-domain screen in filter.py rejected NOTHING today: every\n"
            "domain published in this run went out UNSCREENED for spam, malware\n"
            "and abuse content.\n"
            "This fires regardless of the exit code. A green SUCCESS verdict does\n"
            "NOT mean the screen ran — that exact combination went unnoticed for\n"
            "~8 weeks after the 2026-07-23 credit-balance outage.\n"
            + _likely_cause_line(credit_errors, llm_backend)
            + f"LLM backend      : {llm_backend or '(none used)'}\n"
        )
    if classifier_shadow:
        total_shadow = sum(shadow_counts.values())
        alert_blocks += (
            "\n👁 TOXIC SCREEN RUNNING IN SHADOW MODE (deliberate) 👁\n"
            "------------------------------------------------------\n"
            f"The classifier produced real verdicts for {total_shadow} domain(s) but did\n"
            "NOT apply them: snapshot_category was left 'unknown', so nothing was\n"
            "evicted this run. This is the configured validation state\n"
            "(snapshot_classifier.shadow = true), NOT a failure.\n"
            f"Shadow verdicts  : {shadow_counts['legitimate']} legitimate, "
            f"{shadow_counts['parked']} parked, {shadow_counts['toxic']} toxic, "
            f"{shadow_counts['empty']} empty, {shadow_counts['unknown']} unknown\n"
            f"Would have evicted: {shadow_would_evict} domain(s) as toxic (names in log)\n"
            "Published domains ARE still unscreened while this mode is on. To\n"
            "arm the gate, set snapshot_classifier.shadow = false in config.json.\n"
        )
    if ranker_fell_back:
        alert_blocks += (
            "\n🚨🚨 PHASE 2 RANKER FELL BACK TO MECHANICAL SELECTION 🚨🚨\n"
            "---------------------------------------------------------\n"
            f"{ranker_outcome}\n"
            "Today's published list was chosen mechanically, NOT by quality\n"
            "ranking. A persistent fallback means the LLM ranking stage is\n"
            "effectively switched off — check before assuming list quality.\n"
            + _likely_cause_line(credit_errors, llm_backend)
        )
    if rdap_stops:
        alert_blocks += (
            "\n⚠️  RDAP hosts that backed off this run (429/403 stop-on-edge):\n"
            "-------------------------------------------------------------\n"
            + "\n".join(rdap_stops)
            + "\n"
        )

    body = (
        "DomainSifter daily run report\n"
        "=============================\n"
        + "\n".join(header)
        + "\n"
        + alert_blocks
        + "\n"
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
    except Exception as exc:  # broad by design — see the exit-0 invariant below
        # _build_email parses free-form journal text. A malformed or
        # unexpectedly-encoded log must never turn into a non-zero exit: the
        # wrapper reads this exit code, and conflating "couldn't build the
        # report" with "the pipeline failed" is exactly the confusion this
        # module exists to avoid. Mirrors the broad except around _send.
        print(
            f"send_report: failed to build email ({type(exc).__name__}: {exc}); "
            "skipping email",
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
