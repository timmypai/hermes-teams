#!/usr/bin/env bash
# devflow host runner agent: register/refresh per-repo GitHub Actions runners.
# Triggered by devflow-runner-requests.path when the Hermes container drops a
# request file (written by `devflow runner request OWNER/REPO`). Runs as root.
set -euo pipefail
. /etc/devflow/host.env   # HERMES_DATA_HOST, RUNNER_USER, RUNNER_TARBALL

REQ="$HERMES_DATA_HOST/devflow/runner/requests"
STATUS="$HERMES_DATA_HOST/devflow/runner/status"
mkdir -p "$STATUS"
OWNER=$(stat -c %u:%g "$REQ")
chown "$OWNER" "$STATUS"
uid=$(id -u "$RUNNER_USER")
home=$(getent passwd "$RUNNER_USER" | cut -d: -f6)

as_user() {
  sudo -u "$RUNNER_USER" -H env XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}

write_status() { # key repo state message
  local tmp="$STATUS/.$1.tmp"
  python3 - "$2" "$3" "$4" >"$tmp" <<'PY'
import json, sys, time
print(json.dumps({"repo": sys.argv[1], "state": sys.argv[2], "message": sys.argv[3][-600:], "at": int(time.time())}))
PY
  chown "$OWNER" "$tmp"; chmod 0644 "$tmp"; mv -f "$tmp" "$STATUS/$1.json"
}

register() { # key repo token labels
  local key=$1 repo=$2 token=$3 labels=$4 dir="$home/runners/$1"
  as_user mkdir -p "$dir"
  [[ -x "$dir/config.sh" ]] || as_user tar -xzf "$RUNNER_TARBALL" -C "$dir"
  if [[ -f "$dir/.runner" ]]; then
    as_user systemctl --user stop "devflow-runner@$key.service" || true
    as_user rm -f "$dir/.runner" "$dir/.credentials" "$dir/.credentials_rsaparams"
  fi
  as_user "$dir/config.sh" --unattended --replace --url "https://github.com/$repo" --token "$token" \
    --name "$(hostname)-$key" --labels "$labels" --work _work
  as_user systemctl --user enable --now "devflow-runner@$key.service"
}

shopt -s nullglob
for f in "$REQ"/*.json; do
  key=$(basename "$f" .json)
  if [[ ! $key =~ ^[A-Za-z0-9._-]+__[A-Za-z0-9._-]+$ ]]; then rm -f "$f"; continue; fi
  read -r repo token labels < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["repo"], d["token"], d.get("labels") or "devflow")' "$f") || { rm -f "$f"; continue; }
  rm -f "$f"   # consume the one-time token before doing anything else
  if [[ "$repo" != "${key/__//}" ]]; then write_status "$key" "$repo" error "repo/key mismatch"; continue; fi
  if out=$(register "$key" "$repo" "$token" "$labels" 2>&1); then
    write_status "$key" "$repo" online "registered as $(hostname)-$key"
  else
    write_status "$key" "$repo" error "$out"
  fi
done
