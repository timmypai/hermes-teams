#!/usr/bin/env bash
# Push the devflow kit to the Oracle VM and apply it.
#   ./deploy-kit.sh            # container kit + install.py
#   ./deploy-kit.sh --host     # also (re)run host runner setup (sudo)
#   ./deploy-kit.sh --kit-only # copy kit only (skills/CLI/templates edits; no profile reconfigure)
# Env: VM, VM_KEY, HERMES_CONTAINER, HERMES_RUNTIME_UID, HERMES_DATA_HOST.
set -euo pipefail
cd "$(dirname "$0")"
KEY=${VM_KEY:-/tmp/ok.key}
VM=${VM:-ubuntu@161.33.168.214}
SSH=(ssh -o IdentitiesOnly=yes -o BatchMode=yes -i "$KEY" "$VM")
CONTAINER=${HERMES_CONTAINER:-hermes-controller-r5}
RUNTIME_UID=${HERMES_RUNTIME_UID:-1004}
DOCKER="sudo -H -u '#$RUNTIME_UID' -- /usr/bin/docker --host unix:///run/user/$RUNTIME_UID/docker.sock"

# 1. kit -> container /opt/data/devflow (state/, runner/, vendor/, roles/ are runtime data and kept)
tar -czf - --exclude='__pycache__' devflow | "${SSH[@]}" \
  "cd / && $DOCKER exec -i -u hermes $CONTAINER tar -xzf - -C /opt/data"
echo "kit copied"
[[ "${1:-}" == "--kit-only" ]] && exit 0

# 2. host runner setup
if [[ "${1:-}" == "--host" ]]; then
  data_host=${HERMES_DATA_HOST:-$("${SSH[@]}" "cd / && $DOCKER inspect $CONTAINER --format '{{range .Mounts}}{{if eq .Destination \"/opt/data\"}}{{.Source}}{{end}}{{end}}'")}
  tar -czf - host | "${SSH[@]}" "rm -rf /tmp/devflow-host && mkdir -p /tmp/devflow-host && tar -xzf - -C /tmp/devflow-host"
  "${SSH[@]}" "sudo bash /tmp/devflow-host/host/setup-host.sh '$data_host'"
fi

# 3. install into Hermes profiles. DBUS_SESSION_BUS_ADDRESS=disabled: is a precaution: one earlier run
#    hung until the 600 s timeout while session dbus-daemons appeared in the container. Suspected (not
#    proven) cause: gh/keyring autolaunching a dbus-daemon that keeps the ssh pipe open.
"${SSH[@]}" "cd / && $DOCKER exec -i -u hermes -e HERMES_HOME=/opt/data -e DBUS_SESSION_BUS_ADDRESS=disabled: $CONTAINER /opt/hermes/.venv/bin/python /opt/data/devflow/install.py"
