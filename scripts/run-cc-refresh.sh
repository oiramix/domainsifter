#!/usr/bin/env bash
# Weekly Common Crawl refresh runner for the OVH self-hosted setup.
#
# Invoked by systemd/domainsifter-cc-refresh.service, which is started by
# systemd/domainsifter-cc-refresh.timer every Sunday at 18:00 UTC. Logs
# to journalctl via systemd's StandardOutput=journal capture.
#
# What it does, in order:
#   1. git fetch + reset --hard origin/main  (defensive sync — see Step 1)
#   2. python -m scripts.cc_refresh --auto   (discover / download / build /
#                                             upload / verify-in-R2 / swap
#                                             cc_backlinks.latest_release)
#   3. read scripts/state/cc_refresh_result.json
#   4. commit + push scripts/config.json  ONLY IF action == "installed"
#
# THE ONLY TIME THIS SCRIPT TOUCHES GIT-WRITE OPERATIONS IS
# action == "installed". noop, skipped, verification_failed and
# discovery_failed all leave git completely alone: no add, no commit, no
# push. A "verification_failed" run means cc_refresh built an artifact it
# does not trust and deliberately did NOT swap the config, so there is
# nothing to publish — pushing anything there would publish a config
# pointing at an artifact that failed its canary checks. That is why the
# result JSON is parsed with the venv python and not with grep/sed: a
# substring match for "installed" also matches nothing useful, but a
# sloppy `grep -o 'installed\|noop'` or a field-order assumption is
# exactly how "verification_failed" becomes a push.
#
# Auth: GITHUB_TOKEN comes from EnvironmentFile=.env (systemd-loaded). The
# token is used ONLY in the explicit `git push <url-with-token>` argv and
# is never written to .git/config, the local remote URL, or echoed. Do NOT
# add `set -x` to this script — it would leak the token into journalctl
# on the push command line. systemd captures stdout/stderr but not argv,
# so without set -x the token stays out of the journal.
#
# Exit code: cc_refresh.py's exit code is propagated verbatim, so a real
# failure shows up as `failed` in `systemctl status`. 0 means installed,
# no-op, or a deliberate guard skip; non-zero means discovery or
# verification failed (and in that case no config change was made, so
# the pipeline keeps scoring against the previous release — degraded,
# not down). The one case where this script invents a failure of its own
# is a push that could not be landed after retries, or a result file that
# cc_refresh exited 0 without writing: both are loud non-zero exits
# because they mean the repo state and the R2 state have diverged.
#
# Every exit path logs a single-line summary prefixed
# "cc-refresh summary:" so that
#   journalctl -u domainsifter-cc-refresh.service | grep 'cc-refresh summary'
# answers "what happened last week?" with no digging. See RECOVERY.md
# "Weekly Common Crawl refresh".
#
# No pip install step here, unlike run-daily.sh: the 09:00 UTC daily run
# already installs requirements.txt into the same .venv from the same
# checkout every morning. A second installer on the weekly path would add
# a failure surface for no benefit.
#
# No email report here on purpose. The daily operational email carries a
# Common Crawl freshness line (config cc_backlinks.refresh.staleness_warn_days),
# and that is the single alarm path.

set -euo pipefail

REPO_DIR="/home/domainsifter/domainsifter"
cd "${REPO_DIR}"

PY="${REPO_DIR}/.venv/bin/python"
CONFIG_PATH="scripts/config.json"
PUSH_MAX_ATTEMPTS=3

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- Summary state (kept in globals so the EXIT trap can always print) ------
ACTION="unknown"
RELEASE="-"
PREVIOUS="-"
ROWS="-"
REASON="-"
PUSHED="no"

# The summary must appear even when `set -e` aborts the script mid-way, so
# it lives in an EXIT trap. Reads $? as its first statement (before any
# other command can clobber it), disables itself so a hiccup inside it
# cannot recurse, then re-exits with the captured code. Same idiom as
# run-daily.sh's reporter trap.
log_summary() {
  local exit_code=$?
  trap - EXIT
  echo "[$(ts)] cc-refresh summary: action=${ACTION} release=${RELEASE} previous=${PREVIOUS} rows=${ROWS} pushed=${PUSHED} exit=${exit_code} reason=${REASON}"
  exit "${exit_code}"
}
trap log_summary EXIT

# Fail early with a clear message if GITHUB_TOKEN didn't get loaded. Unlike
# the daily run, the alternative here is discovering it HOURS later, after
# a 10 GB download and a 25-minute DuckDB build, with the config swapped
# locally and no way to publish it. REASON is pre-set because ${VAR:?}
# aborts immediately and the trap's summary line would otherwise say
# nothing useful.
REASON="GITHUB_TOKEN not set"
: "${GITHUB_TOKEN:?GITHUB_TOKEN is not set — check ${REPO_DIR}/.env and systemd EnvironmentFile= directive}"
REASON="-"

echo "[$(ts)] cc-refresh run starting"

# --- Step 1: defensive sync to origin/main ----------------------------------
#
# Identical discipline to run-daily.sh Step 1, and for the same reason:
# this box is timer-driven and persistent state lives on origin only. Any
# local divergence (an interactive session left on a branch, a dirty tree,
# an unpushed commit from a previous run whose push failed) is discarded
# here before anything else happens.
#
# Ordering matters specifically for THIS job: cc_refresh rewrites one line
# of scripts/config.json, and that edit has to land on top of current
# origin/main, not on a diverged tree. Syncing first means the commit we
# create in Step 4 is a fast-forward in the normal case, so the push
# needs no rebase at all.
#
# Note the deliberate consequence: if last week's run installed a release
# but could not push, `reset --hard` throws that local commit away and
# config goes back to the older release. That is correct and self-healing
# — this run's discovery then sees the newer release as not-installed and
# redoes the work (download resumes, uploads overwrite, verification
# re-runs), ending with a fresh commit against current origin/main.
#
# Not `git pull --ff-only`: pull refuses to merge a diverged local branch
# and needs manual server-side cleanup. That bit us twice in 24h
# (2026-05-17, 2026-05-18) on the daily path.
echo "[$(ts)] git-sync: fetching origin/main"
git fetch origin main || {
  REASON="git fetch origin main failed"
  echo "ERROR: git fetch origin main failed — cannot sync with remote, aborting" >&2
  exit 1
}
CURRENT_BRANCH="$(git branch --show-current)"
if [[ "${CURRENT_BRANCH}" != "main" ]]; then
  echo "[$(ts)] git-sync: not on main (was '${CURRENT_BRANCH}'), switching"
  git checkout -f main
fi
git reset --hard origin/main
echo "[$(ts)] git-state: branch=main HEAD=$(git rev-parse --short HEAD)"

# --- Step 2: resolve the result path from config -----------------------------
#
# Read from config rather than hardcoding the path (hard rule 9). Uses the
# venv python so there is exactly one JSON parser involved in this script.
RESULT_PATH="$("${PY}" -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    cfg = json.load(fh)
path = cfg["cc_backlinks"]["refresh"]["result_path"]
sys.stdout.write(path)
' "${CONFIG_PATH}")" || {
  REASON="could not read cc_backlinks.refresh.result_path from ${CONFIG_PATH}"
  echo "ERROR: ${REASON}" >&2
  exit 1
}
echo "[$(ts)] result file: ${RESULT_PATH}"

# Remove any previous result BEFORE the run. scripts/state/ is gitignored
# and survives the reset above, so last week's file is still sitting there.
# If cc_refresh died before writing its own result, reading a stale file
# would make us act on a week-old verdict — including pushing a config
# swap for a release that this run never touched. Absent-means-failure is
# the safe reading.
rm -f -- "${RESULT_PATH}"

# --- Step 3: run the refresh -------------------------------------------------
#
# --auto does its own discovery, blackout guard and pipeline-unit
# interlock, and writes the result JSON atomically. Capture the exit code
# instead of letting `set -e` abort: the summary and the (no-)git decision
# below must run on failure too.
echo "[$(ts)] invoking scripts.cc_refresh --auto"
REFRESH_RC=0
"${PY}" -m scripts.cc_refresh --auto || REFRESH_RC=$?
echo "[$(ts)] scripts.cc_refresh --auto returned exit code ${REFRESH_RC}"

# --- Step 4: read the result --------------------------------------------------
if [[ ! -f "${RESULT_PATH}" ]]; then
  # Contract violation if REFRESH_RC is 0; expected only when the process
  # was killed (e.g. the TimeoutStartSec=10h cap fired) before it could
  # write. Either way there is nothing to publish.
  ACTION="no-result-file"
  REASON="cc_refresh wrote no result file at ${RESULT_PATH}"
  echo "ERROR: ${REASON} (cc_refresh exit=${REFRESH_RC})" >&2
  if (( REFRESH_RC == 0 )); then
    exit 1
  fi
  exit "${REFRESH_RC}"
fi

# Parse with the venv python, never grep/sed. Fields come back in a fixed
# order, separated by US (0x1f). A malformed or non-object JSON makes
# python exit non-zero and we bail.
#
# Two details that are not cosmetic:
#   * The separator is 0x1f, NOT a tab. Tab is IFS whitespace, and bash's
#     `read` collapses runs of IFS whitespace into one delimiter — so a
#     null `previous_release` would silently shift `rows` into PREVIOUS
#     and `reason` into ROWS. 0x1f is non-whitespace, so every delimiter
#     counts exactly once. (Release names and reasons cannot contain it.)
#   * python substitutes "-" for null/empty and flattens any embedded
#     control characters, so there are no empty fields and no way for a
#     `reason` string to inject an extra delimiter.
PARSED=""
PARSE_RC=0
PARSED="$("${PY}" -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
if not isinstance(data, dict):
    raise SystemExit("cc_refresh result JSON is not an object")

def field(key: str) -> str:
    value = data.get(key)
    text = "" if value is None else str(value)
    for ch in ("\t", "\n", "\r", "\x1f"):
        text = text.replace(ch, " ")
    return text.strip() or "-"

sys.stdout.write("\x1f".join(
    field(k) for k in ("action", "release", "previous_release", "rows", "reason")
))
' "${RESULT_PATH}")" || PARSE_RC=$?

if (( PARSE_RC != 0 )); then
  ACTION="unparseable-result"
  REASON="could not parse ${RESULT_PATH} as JSON"
  echo "ERROR: ${REASON} (cc_refresh exit=${REFRESH_RC})" >&2
  if (( REFRESH_RC == 0 )); then
    exit 1
  fi
  exit "${REFRESH_RC}"
fi

IFS=$'\x1f' read -r ACTION RELEASE PREVIOUS ROWS REASON <<< "${PARSED}"
# Defensive: python already substitutes "-" for missing values, so these
# only fire if the field count itself was wrong.
ACTION="${ACTION:-unknown}"
RELEASE="${RELEASE:--}"
PREVIOUS="${PREVIOUS:--}"
ROWS="${ROWS:--}"
REASON="${REASON:--}"
echo "[$(ts)] result: action=${ACTION} release=${RELEASE} previous=${PREVIOUS} rows=${ROWS} reason=${REASON}"

# --- Step 5: git, but only for a real install --------------------------------
case "${ACTION}" in
  installed)
    : # fall through to the commit + push below
    ;;
  noop)
    echo "[$(ts)] no new Common Crawl release (${RELEASE} already installed) — git untouched"
    exit "${REFRESH_RC}"
    ;;
  skipped)
    # Guard skip: blackout window (07:00-16:00 UTC), pipeline unit active,
    # or cc_backlinks.refresh.enabled=false. Exit 0 by contract.
    echo "[$(ts)] refresh skipped by a guard (${REASON}) — git untouched"
    exit "${REFRESH_RC}"
    ;;
  verification_failed)
    # The built SQLite failed its in-R2 canary checks and cc_refresh did
    # NOT swap cc_backlinks.latest_release. The pipeline keeps scoring
    # against the previous release: degraded, not down. Nothing to push.
    echo "ERROR: in-R2 verification failed for ${RELEASE} (${REASON}) — config NOT swapped, git untouched" >&2
    echo "ERROR: pipeline continues on the previous release (${PREVIOUS}); see RECOVERY.md 'Weekly Common Crawl refresh'" >&2
    exit "${REFRESH_RC}"
    ;;
  discovery_failed)
    echo "ERROR: release discovery failed (${REASON}) — nothing downloaded, git untouched" >&2
    exit "${REFRESH_RC}"
    ;;
  *)
    # Unknown action = unknown state. Refuse to guess, refuse to push.
    echo "ERROR: unrecognised action '${ACTION}' in ${RESULT_PATH} — refusing to touch git" >&2
    if (( REFRESH_RC == 0 )); then
      exit 1
    fi
    exit "${REFRESH_RC}"
    ;;
esac

echo "[$(ts)] action=installed — committing the config swap"

# Author identity, same as the daily pipeline. `git config` writes to
# .git/config (identity only, never credentials). Idempotent.
git config user.name "domainsifter-pipeline"
git config user.email "99090280+oiramix@users.noreply.github.com"

# Stage the config. The result file is staged ONLY if git actually tracks
# it — scripts/state/ is gitignored today (".gitignore: scripts/state/*"),
# so normally it is not, and `git add` on an ignored path would fail the
# script under set -e. Check, don't assume: if someone later force-adds
# it, this picks it up without an edit here.
STAGE_PATHS=("${CONFIG_PATH}")
if git ls-files --error-unmatch -- "${RESULT_PATH}" >/dev/null 2>&1; then
  echo "[$(ts)] ${RESULT_PATH} is tracked — including it in the commit"
  STAGE_PATHS+=("${RESULT_PATH}")
else
  echo "[$(ts)] ${RESULT_PATH} is not tracked (gitignored) — committing ${CONFIG_PATH} only"
fi
git add -- "${STAGE_PATHS[@]}"

# Defensive: action=installed should always mean config.json changed. If it
# somehow didn't, do not create an empty commit and do not push — just say
# so loudly and keep cc_refresh's exit code.
if git diff --cached --quiet; then
  REASON="action=installed but ${CONFIG_PATH} is unchanged vs origin/main"
  echo "WARNING: ${REASON} — nothing committed, nothing pushed" >&2
  exit "${REFRESH_RC}"
fi

git commit -m "$(cat <<EOF
data(cc): swap Common Crawl release ${PREVIOUS} -> ${RELEASE}

Automated weekly refresh via systemd/domainsifter-cc-refresh.timer.
The derived SQLite for ${RELEASE} was uploaded to R2 and verified there
against the config canaries (${ROWS} cc_apex rows) BEFORE
cc_backlinks.latest_release was rewritten.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
echo "[$(ts)] committed $(git rev-parse --short HEAD)"

# Push with the PAT in the explicit URL argv only. Not written to
# .git/config. Not echoed (set -x is intentionally off — see header).
#
# Retry on rejection because origin can legitimately have moved while we
# were building for half an hour (the archive service, a human, another
# agent). Bounded at PUSH_MAX_ATTEMPTS: rebase the single config commit
# onto the new origin/main and try again, then fail loudly rather than
# looping forever. NEVER --force / --force-with-lease: this commit is one
# line of config, it is never worth overwriting someone else's push, and
# a conflict here is a signal that a human should look.
push_with_retry() {
  local attempt=1
  while (( attempt <= PUSH_MAX_ATTEMPTS )); do
    echo "[$(ts)] git push attempt ${attempt}/${PUSH_MAX_ATTEMPTS}"
    if git push "https://x-access-token:${GITHUB_TOKEN}@github.com/oiramix/domainsifter.git" main; then
      return 0
    fi
    echo "WARNING: git push attempt ${attempt}/${PUSH_MAX_ATTEMPTS} failed" >&2
    if (( attempt == PUSH_MAX_ATTEMPTS )); then
      break
    fi
    echo "[$(ts)] re-syncing and rebasing onto origin/main before retry"
    if ! git fetch origin main; then
      echo "ERROR: git fetch failed during push retry — giving up" >&2
      return 1
    fi
    # --autostash so an unrelated stray modification in the working tree
    # cannot turn a recoverable push rejection into a hard failure. Step 1
    # left the tree clean, but this run took half an hour and something
    # (an interactive session on the box, a tool touching a tracked file)
    # may have dirtied it since; git refuses to rebase then. The stash is
    # popped automatically afterwards.
    if ! git rebase --autostash origin/main; then
      git rebase --abort || true
      echo "ERROR: rebase onto origin/main failed — most likely a conflict because something else edited ${CONFIG_PATH}. Giving up; manual resolution required" >&2
      return 1
    fi
    attempt=$(( attempt + 1 ))
  done
  return 1
}

if push_with_retry; then
  PUSHED="yes"
  echo "[$(ts)] pushed $(git rev-parse --short HEAD) to origin/main"
  echo "[$(ts)] cc-refresh run complete: ${PREVIOUS} -> ${RELEASE}"
  exit "${REFRESH_RC}"
fi

# Push failed for good. R2 has the verified artifacts and the local tree
# has the config swap, but origin does not — so tomorrow's 09:00 UTC run
# will reset --hard and keep scoring the PREVIOUS release. Degraded, not
# down, and next Sunday's tick redoes the work. Recovery recipe (the same
# manual rebase+push as RECOVERY.md "Pipeline succeeded, push failed")
# publishes it immediately.
PUSHED="failed"
REASON="git push rejected after ${PUSH_MAX_ATTEMPTS} attempts"
echo "ERROR: ${REASON}. The commit exists locally only. See RECOVERY.md 'Weekly Common Crawl refresh'." >&2
if (( REFRESH_RC != 0 )); then
  exit "${REFRESH_RC}"
fi
exit 1
