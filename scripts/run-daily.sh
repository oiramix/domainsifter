#!/usr/bin/env bash
# Daily pipeline runner for the OVH self-hosted setup.
#
# Replicates what the GHA workflow used to do end-to-end:
#   1. git pull --ff-only origin main  (sync to latest code)
#   2. pip install -r requirements.txt (idempotent; cheap if up-to-date)
#   3. python -m scripts.pipeline      (writes src/data/daily-domains.json)
#   4. git add + commit + push         (publishes the refreshed JSON)
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
# leaves a clear error in journalctl. If `git push` fails (e.g. remote
# diverged due to a manual workflow_dispatch race with the GHA fallback),
# the local commit stays unpushed and the NEXT day's `git pull --ff-only`
# will refuse to merge — manual server-side cleanup is required at that
# point. Run `git status` on the server; the unpushed commit will be the
# tip of main.

set -euo pipefail

REPO_DIR="/home/domainsifter/domainsifter"
cd "${REPO_DIR}"

# Fail early with a clear message if GITHUB_TOKEN didn't get loaded — the
# alternative is a confusing failure 5+ minutes later at the push step.
: "${GITHUB_TOKEN:?GITHUB_TOKEN is not set — check ${REPO_DIR}/.env and systemd EnvironmentFile= directive}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] daily run starting"

# Step 1: sync to latest code on main. --ff-only fails loudly if local
# diverged from origin (which on a deploy-only server should never happen).
git pull --ff-only origin main

# Step 2: ensure deps are current. pip is idempotent — fast on no-change days.
.venv/bin/pip install --quiet -r requirements.txt

# Step 3: run the pipeline. Writes src/data/daily-domains.json on success.
# Any non-zero exit aborts the script via set -e.
.venv/bin/python -m scripts.pipeline --config scripts/config.json

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
