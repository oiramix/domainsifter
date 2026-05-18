#!/usr/bin/env bash
# Daily pipeline runner for the OVH self-hosted setup.
#
# Replicates what the GHA workflow used to do end-to-end:
#   1. git fetch + reset --hard origin/main (defensive sync — see Step 1)
#   2. pip install -r requirements.txt      (idempotent; cheap if up-to-date)
#   3. python -m scripts.pipeline           (writes src/data/daily-domains.json)
#   4. git add + commit + push              (publishes the refreshed JSON)
#
# Invoked by systemd/domainsifter.timer at 06:30 UTC daily. Logs to
# journalctl via systemd's StandardOutput=journal capture.
#
# Auth: GITHUB_TOKEN comes from EnvironmentFile=.env (systemd-loaded). The
# token is used ONLY in the explicit `git push <url-with-token>` argv and
# is never written to .git/config, the local remote URL, or echoed. Do NOT
# add `set -x` to this script — it would leak the token into journalctl
# on the push command line.
#
# Failure mode: any step's non-zero exit aborts the run via `set -e` and
# leaves a clear error in journalctl. Step 1's `reset --hard origin/main`
# discards any local divergence (wrong branch, dirty tree, unpushed
# commits from a failed previous run) before doing anything else, so
# manual server-side state cannot corrupt the next run. If `git push`
# fails, the local commit stays on the server until the next morning,
# when Step 1 discards it — acceptable, since the pipeline regenerates
# daily and a missed publish day re-publishes the next morning. We hit
# the old `git pull --ff-only` failure mode twice (2026-05-17, -05-18).
#
# Email report: an EXIT trap fires scripts/send_report.py on success OR
# failure, sending the journalctl log of this systemd invocation to
# hello@domainsifter.com via Brevo SMTP. Reporter failures never propagate;
# the wrapper's exit code is the pipeline's, not the reporter's.
#
# Additional .env vars required for email reporting (paste on the server
# before the first run that includes this script — values are NOT in the
# repo):
#   BREVO_SMTP_USER    — Brevo SMTP login (from STATE.md "Active accounts")
#   BREVO_SMTP_KEY     — Brevo SMTP key (same source)
#   REPORT_TO_EMAIL    — destination, typically hello@domainsifter.com
#   REPORT_FROM_EMAIL  — Brevo-verified sender, typically hello@domainsifter.com

set -euo pipefail

REPO_DIR="/home/domainsifter/domainsifter"
cd "${REPO_DIR}"

# Capture run-start time for the reporter's wall-clock readout. Exported so
# scripts.send_report (a child process) can read it via os.environ.
export DOMAINSIFTER_RUN_START_TS="$(date -u +%s)"

# Reporter must fire on ANY exit (success, failure, signal). We use a trap
# so this is reliable even if `set -e` aborts mid-script. The trap reads
# $? as the very first statement (before any other command can clobber it),
# then disables itself so a hiccup inside the reporter can't recurse, then
# re-exits with the captured code.
send_report() {
  local exit_code=$?
  trap - EXIT

  # Capture peak memory FROM THE CGROUP, before invoking the reporter.
  # Why here and not from send_report.py:
  #   `systemctl show -p MemoryPeak` races with systemd's unit teardown —
  #   by the time the trap-spawned python process reaches systemctl, the
  #   unit property has already been cleared and the reporter logs
  #   "Memory peak: (unavailable)". The cgroup file, in contrast, lives
  #   until the script's main process actually exits, which is later than
  #   when we read it here. cgroup v2 only; older v1-only hosts will not
  #   have memory.peak and the read silently falls through.
  if [[ -r /proc/self/cgroup ]]; then
    local cg_path
    cg_path="$(awk -F: '/^0::/{print $3; exit}' /proc/self/cgroup 2>/dev/null || true)"
    if [[ -n "${cg_path}" ]]; then
      local peak_file="/sys/fs/cgroup${cg_path}/memory.peak"
      if [[ -r "${peak_file}" ]]; then
        local peak_value
        peak_value="$(cat "${peak_file}" 2>/dev/null || true)"
        # memory.peak is a single integer in bytes. Reject empty / non-numeric
        # defensively so send_report.py's env-var-isdigit check never sees
        # garbage on the cgroup-readable-but-empty edge case.
        if [[ "${peak_value}" =~ ^[0-9]+$ ]]; then
          export DOMAINSIFTER_MEMORY_PEAK_BYTES="${peak_value}"
        fi
      fi
    fi
  fi

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] sending daily report (pipeline exit=${exit_code})"
  .venv/bin/python -m scripts.send_report --pipeline-exit "${exit_code}" \
    || echo "WARNING: email report failed to send (run exit=${exit_code})" >&2
  exit "${exit_code}"
}
trap send_report EXIT

# Fail early with a clear message if GITHUB_TOKEN didn't get loaded — the
# alternative is a confusing failure 5+ minutes later at the push step.
: "${GITHUB_TOKEN:?GITHUB_TOKEN is not set — check ${REPO_DIR}/.env and systemd EnvironmentFile= directive}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] daily run starting"

# Step 1: defensive sync to origin/main. This server is cron-driven;
# persistent state lives on origin only. Any local divergence (manual ops,
# dry-runs, partial previous runs, wrong branch from an interactive
# session) gets discarded here. Runs BEFORE anything else so we never
# publish from stale or unintended code.
#
# Why not `git pull --ff-only` as before: pull refuses to merge if the
# local branch diverged, requiring manual server-side cleanup. We hit
# that twice in 24h (2026-05-17 manual-push race; 2026-05-18 wrong
# branch left over from dry-run review). reset --hard makes the script
# idempotent on any local state.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] git-sync: fetching origin/main"
git fetch origin main || {
  echo "ERROR: git fetch origin main failed — cannot sync with remote, aborting" >&2
  exit 1
}
CURRENT_BRANCH="$(git branch --show-current)"
if [[ "${CURRENT_BRANCH}" != "main" ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] git-sync: not on main (was '${CURRENT_BRANCH}'), switching"
  git checkout -f main
fi
git reset --hard origin/main
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] git-state: branch=main HEAD=$(git rev-parse --short HEAD)"

# Step 2: ensure deps are current. pip is idempotent — fast on no-change days.
.venv/bin/pip install --quiet -r requirements.txt

# Step 3: run the pipeline. Writes src/data/daily-domains.json on success.
# Any non-zero exit aborts the script via set -e.
.venv/bin/python -m scripts.pipeline --config scripts/config.json

# Step 3b: generate the Buttondown newsletter draft from the fresh JSON.
# Failures here are LOGGED but do NOT abort the daily run — newsletter is
# optional, pipeline JSON publish is the source of truth. The module
# already returns 0 for the no-op states (newsletter.enabled=false,
# duplicate-found, zero-domains), so this `|| true` mostly catches genuine
# API / config errors. Toggle on by setting config.newsletter.enabled=true
# AND BUTTONDOWN_API_KEY in .env. See STATE.md 'Daily newsletter — 2026-05-14'.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] generating newsletter draft"
.venv/bin/python -m scripts.generate_newsletter --config scripts/config.json \
  || echo "WARNING: newsletter generation failed (non-fatal; continuing)" >&2

# Step 4: replicate the GHA workflow's "Commit refreshed daily output" step.
# `git config` writes to .git/config (local, not credentials — just author
# identity). Idempotent on every run.
git config user.name "domainsifter-pipeline"
git config user.email "99090280+oiramix@users.noreply.github.com"

git add src/data/daily-domains.json

# Skip-if-no-changes guard — matches the GHA workflow exactly. Without it,
# a no-change day would create an empty commit that's noise in the log.
if git diff --cached --quiet; then
  echo "No changes to commit (daily output unchanged)."
  exit 0
fi

TODAY="$(date -u +%Y-%m-%d)"
git commit -m "data: daily refresh ${TODAY}"

# Push with the PAT in the explicit URL argv only. Not written to
# .git/config. Not echoed (set -x is intentionally off — see header).
# systemd captures stdout/stderr to journal but does NOT capture argv,
# so the token stays out of journalctl.
git push "https://x-access-token:${GITHUB_TOKEN}@github.com/oiramix/domainsifter.git" main

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] daily run complete"
