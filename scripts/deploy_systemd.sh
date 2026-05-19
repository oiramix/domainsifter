#!/usr/bin/env bash
# Deploy systemd + polkit changes from the repo:
#   systemd/*.service, systemd/*.timer → /etc/systemd/system/
#   systemd/*.rules                    → /etc/polkit-1/rules.d/
#
# Per-file compare-and-copy. Runs `systemctl daemon-reload` exactly once
# at the end iff any systemd unit file was actually updated. Polkit
# rules deploy is reload-free — polkit(8) on Debian states:
#   "Both directories are monitored so if a rules file is changed,
#    added or removed, existing rules are purged and all files are
#    read and processed again."
# So a copied .rules file is picked up automatically; no operator
# action between deploy and the next policy-checked operation.
#
# Does NOT restart any service. daemon-reload is enough for the next
# scheduled or manual invocation to pick up the new unit definition.
# Restarting is left to the operator on purpose — this script should be
# safe to run while a pipeline run is in progress.
#
# Usage (run on OVH as root):
#   sudo scripts/deploy_systemd.sh
#
# The script resolves the repo root from its own location, so the cwd
# at invocation does not matter.

set -euo pipefail

if (( EUID != 0 )); then
  echo "ERROR: scripts/deploy_systemd.sh must be run as root (use sudo)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

SYSTEMD_TARGET=/etc/systemd/system
POLKIT_TARGET=/etc/polkit-1/rules.d
systemd_updated=0
polkit_updated=0
unchanged=0

shopt -s nullglob

# --- systemd unit files (.service, .timer) ---------------------------------
for src in systemd/*.service systemd/*.timer; do
  name="$(basename "${src}")"
  dst="${SYSTEMD_TARGET}/${name}"
  if [[ -f "${dst}" ]] && cmp -s "${src}" "${dst}"; then
    echo "unchanged: ${name}"
    unchanged=$((unchanged + 1))
  else
    cp "${src}" "${dst}"
    echo "updated: ${name}"
    systemd_updated=$((systemd_updated + 1))
  fi
done

# --- polkit rules (.rules) -------------------------------------------------
# Auto-reload via polkit's directory monitor — no daemon-reload analogue.
RULES_FILES=( systemd/*.rules )
if (( ${#RULES_FILES[@]} > 0 )) && [[ ! -d "${POLKIT_TARGET}" ]]; then
  echo "ERROR: ${POLKIT_TARGET} does not exist but the repo has .rules files to deploy. Install polkit first (apt-get install polkitd)." >&2
  exit 1
fi
for src in systemd/*.rules; do
  name="$(basename "${src}")"
  dst="${POLKIT_TARGET}/${name}"
  if [[ -f "${dst}" ]] && cmp -s "${src}" "${dst}"; then
    echo "unchanged: ${name}"
    unchanged=$((unchanged + 1))
  else
    cp "${src}" "${dst}"
    echo "updated: ${name}"
    polkit_updated=$((polkit_updated + 1))
  fi
done

if (( systemd_updated > 0 )); then
  systemctl daemon-reload
  echo "daemon-reload: complete"
fi

total_updated=$((systemd_updated + polkit_updated))
echo "deploy complete: ${total_updated} files updated (${systemd_updated} systemd, ${polkit_updated} polkit), ${unchanged} unchanged"
