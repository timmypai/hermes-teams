#!/usr/bin/env bash
# devflow host runner agent: register/refresh per-repo GitHub Actions runners.
# Triggered by devflow-runner-requests.path when the Hermes container drops a
# request file (written by `devflow runner request OWNER/REPO`). Runs as root.
# Everything under the Hermes data dir is controlled by the container (it can plant
# symlinks there), so root never touches it: requests are read and status is written
# as the data dir's owner; root only registers runners as $RUNNER_USER.
set -euo pipefail
. /etc/devflow/host.env   # HERMES_DATA_HOST, RUNNER_USER, RUNNER_TARBALL

REQ="$HERMES_DATA_HOST/devflow/runner/requests"
STATUS="$HERMES_DATA_HOST/devflow/runner/status"
OWNER=$(stat -c %u:%g "$HERMES_DATA_HOST")
[[ ${OWNER%:*} != 0 ]] || { echo "refusing: $HERMES_DATA_HOST is owned by root" >&2; exit 1; }
uid=$(id -u "$RUNNER_USER")
home=$(getent passwd "$RUNNER_USER" | cut -d: -f6)

as_owner() {
  (cd / && setpriv --reuid="${OWNER%:*}" --regid="${OWNER#*:}" --clear-groups -- "$@")
}

as_user() {
  sudo -u "$RUNNER_USER" -H env XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}

# One line per valid request: key repo token labels. Every request file is deleted
# before its token is used (one-time token), valid or not.
take_requests() {
  as_owner python3 - "$REQ" <<'PY'
import json, os, re, sys
d = sys.argv[1]
for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
    if not name.endswith(".json"):
        continue
    p, key = os.path.join(d, name), name[:-5]
    try:
        with open(p) as f:
            r = json.load(f)
        fields = [key, r["repo"], r["token"], r.get("labels") or "devflow"]
    except Exception:
        fields = None
    try:
        os.unlink(p)
    except OSError:
        continue
    if (fields and re.fullmatch(r"[A-Za-z0-9._-]+__[A-Za-z0-9._-]+", key)
            and all(isinstance(x, str) and x and not re.search(r"\s", x) for x in fields)):
        print(*fields)
PY
}

write_status() { # key repo state message
  as_owner python3 - "$STATUS" "$@" <<'PY'
import json, os, sys, tempfile, time
d, key, repo, state, msg = sys.argv[1:]
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{key}.")
with os.fdopen(fd, "w") as f:
    json.dump({"repo": repo, "state": state, "message": msg[-600:], "at": int(time.time())}, f)
os.chmod(tmp, 0o644)
os.replace(tmp, os.path.join(d, key + ".json"))
PY
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

while read -r key repo token labels; do
  if [[ "$repo" != "${key/__//}" ]]; then write_status "$key" "$repo" error "repo/key mismatch"; continue; fi
  if out=$(register "$key" "$repo" "$token" "$labels" 2>&1); then
    write_status "$key" "$repo" online "registered as $(hostname)-$key"
  else
    write_status "$key" "$repo" error "$out"
  fi
done < <(take_requests)
