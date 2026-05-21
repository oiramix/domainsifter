#!/usr/bin/env bash
# Same-day guard wrapper around scripts/run-daily.sh.
#
# Added 2026-05-21 after that morning's catch-up incident:
#   - 06:30 UTC scheduled cron silently failed to fire (yesterday's
#     RemainAfterExit=yes left domainsifter.service "active (exited)";
#     timer's start job became a no-op).
#   - 10:12 UTC manual recovery via systemctl stop/start succeeded;
#     pipeline ran to 11:13 UTC, git push got rejected by intervening
#     OnSuccess= fix landing on origin/main, reported FAILED.
#   - 11:30 UTC Mario manually rebased + pushed today's data (1c4783f).
#   - 11:40 UTC: systemctl start domainsifter.timer (intended to
#     bring the timer back online after the morning stop) was
#     interpreted by systemd as "missed scheduled firing → run
#     Persistent= catch-up now." The pipeline started a SECOND
#     same-day invocation. Operator killed it at 11:43 UTC after
#     six TLDs had their R2 baselines overwritten with T_noon.
#
# Post-incident analysis showed R2 state was functionally identical
# (intra-day zone churn over 27 minutes is essentially zero), but the
# ARCHITECTURE allowed a class of failure that could have corrupted
# real state if the second run had completed enrichment and re-
# published daily-domains.json. The guard exists to close that class
# of failure for good.
#
# CONTRACT
#
#   Sentinel:   $XDG_STATE_HOME/domainsifter/last-success.date
#               (defaults to $HOME/.local/state/domainsifter/last-success.date)
#   Format:     single ISO-8601 UTC date (YYYY-MM-DD), no trailing newline-strip
#   Lifecycle:  written by THIS wrapper after each run-daily.sh
#               invocation, REGARDLESS of run-daily.sh's exit code.
#   Bypass:     DOMAINSIFTER_FORCE_RUN=1 in env. Used by
#               systemd/domainsifter-force.service for operator-
#               initiated forced re-runs.
#
# WHY THE SENTINEL IS UNCONDITIONAL (vs only-on-success)
#
# pipeline.py mutates R2 state mid-run: per-TLD, after parsing today's
# zone, it overwrites *_yesterday.txt before any enrichment runs. A
# crashed pipeline that got past TLD #3 of 11 has already corrupted the
# baseline for those 3 TLDs. A same-day auto-retry on this partial
# state would re-overwrite ANOTHER same-day snapshot for any TLDs that
# previously succeeded (idempotent at zero-churn) AND try to advance
# the remaining 8 TLDs (which already failed once — likely to fail
# again, leaving the baseline split across two different intraday
# snapshots).
#
# Blocking automatic same-day retries is the conservative choice. If
# the operator wants to attempt recovery (e.g., today's failure was a
# transient network issue and they want to retry the remaining TLDs),
# they explicitly run domainsifter-force.service. The guard does not
# pretend to provide retry semantics — that is an operator decision.
#
# WHAT THIS WRAPPER DOES NOT DO
#
# It does NOT modify run-daily.sh's email-reporter EXIT trap.
# run-daily.sh's trap sends the daily report on any exit code. This
# wrapper invokes run-daily.sh as a subprocess, so the trap fires
# normally inside the subprocess. The wrapper only adds the guard
# check and the sentinel write.
#
# INVOCATION
#
#   Normal (timer):   systemd/domainsifter.service ExecStart= invokes
#                     this wrapper directly.
#   Force (manual):   systemd/domainsifter-force.service sets
#                     DOMAINSIFTER_FORCE_RUN=1 and invokes the wrapper.
#                     Operator: `sudo systemctl start domainsifter-force.service`
#   Ad-hoc:           See RECOVERY.md for the rare cases where the
#                     wrapper should be invoked from a shell session.

set -euo pipefail

SENTINEL_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/domainsifter"
SENTINEL_FILE="${SENTINEL_DIR}/last-success.date"
TODAY_UTC="$(date -u +%Y-%m-%d)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- Guard ------------------------------------------------------------------

if [[ -n "${DOMAINSIFTER_FORCE_RUN:-}" ]]; then
  echo "[$(ts)] same-day guard: DOMAINSIFTER_FORCE_RUN set; bypassing guard check."
elif [[ -f "${SENTINEL_FILE}" ]]; then
  LAST_RUN_DATE="$(cat "${SENTINEL_FILE}" 2>/dev/null || echo "")"
  if [[ "${LAST_RUN_DATE}" == "${TODAY_UTC}" ]]; then
    echo "[$(ts)] same-day guard: a run already completed today (${LAST_RUN_DATE} UTC). Skipping."
    echo "[$(ts)] To force a re-run today: 'sudo systemctl start domainsifter-force.service'"
    echo "[$(ts)] See RECOVERY.md for context on when force-runs are safe."
    exit 0
  fi
  echo "[$(ts)] same-day guard: sentinel date is ${LAST_RUN_DATE} (not today); proceeding."
else
  echo "[$(ts)] same-day guard: no sentinel at ${SENTINEL_FILE}; proceeding (cold start)."
fi

# --- Invoke the real runner -------------------------------------------------

# Capture run-daily.sh's exit code without triggering set -e. The sentinel
# write below runs regardless of exit code — see the WHY THE SENTINEL IS
# UNCONDITIONAL block in the header.
echo "[$(ts)] invoking run-daily.sh"
RUN_RC=0
"${SCRIPT_DIR}/run-daily.sh" || RUN_RC=$?
echo "[$(ts)] run-daily.sh returned exit code ${RUN_RC}"

# --- Sentinel write ---------------------------------------------------------

# Unconditional. See header rationale: any invocation of pipeline.py — even
# a failed one — has potentially mutated R2 state. Block all same-day
# automatic retries; require explicit force for operator recovery.
mkdir -p "${SENTINEL_DIR}"
echo "${TODAY_UTC}" > "${SENTINEL_FILE}"
echo "[$(ts)] same-day guard: wrote ${TODAY_UTC} to ${SENTINEL_FILE}"

exit "${RUN_RC}"
