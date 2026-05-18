#!/usr/bin/env bash
# Deploy systemd unit changes from the repo to /etc/systemd/system/.
#
# For each .service and .timer file under systemd/, compare against the
# installed copy in /etc/systemd/system/ and copy over if it differs.
# Runs `systemctl daemon-reload` exactly once at the end if anything was
# actually updated.
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
updated=0
unchanged=0

shopt -s nullglob
for src in systemd/*.service systemd/*.timer; do
  name="$(basename "${src}")"
  dst="${SYSTEMD_TARGET}/${name}"
  if [[ -f "${dst}" ]] && cmp -s "${src}" "${dst}"; then
    echo "unchanged: ${name}"
    unchanged=$((unchanged + 1))
  else
    cp "${src}" "${dst}"
    echo "updated: ${name}"
    updated=$((updated + 1))
  fi
done

if (( updated > 0 )); then
  systemctl daemon-reload
  echo "daemon-reload: complete"
fi

echo "deploy complete: ${updated} files updated, ${unchanged} unchanged"
