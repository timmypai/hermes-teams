#!/usr/bin/env bash
# devflow host setup (idempotent). Run as root on the Oracle VM:
#   sudo bash setup-host.sh /home/hermes-runtime/<hermes-dir>/data
# Creates the `devflow` runner user with rootless Docker, installs the runner
# tarball, the per-repo runner unit template and the request watcher.
set -euo pipefail
HERMES_DATA_HOST=${1:?usage: setup-host.sh <host path of Hermes /opt/data>}
RUNNER_USER=${RUNNER_USER:-devflow}
RUNNER_VERSION=${RUNNER_VERSION:-2.338.2}
HERE=$(cd "$(dirname "$0")" && pwd)
LIB=/usr/local/lib/devflow
case "$(uname -m)" in aarch64) ARCH=arm64 ;; x86_64) ARCH=x64 ;; *) echo "unsupported arch" >&2; exit 1 ;; esac

[[ -d "$HERMES_DATA_HOST" ]] || { echo "no such dir: $HERMES_DATA_HOST" >&2; exit 1; }
install -d -m 755 "$LIB" /etc/devflow

# 1. runner user + rootless Docker
id "$RUNNER_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$RUNNER_USER"
if ! grep -q "^$RUNNER_USER:" /etc/subuid; then
  start=$(awk -F: '{e=$2+$3; if (e>m) m=e} END {print (m<100000 ? 100000 : m)}' /etc/subuid /etc/subgid)
  usermod --add-subuids "$start-$((start + 65535))" --add-subgids "$start-$((start + 65535))" "$RUNNER_USER"
fi
loginctl enable-linger "$RUNNER_USER"
uid=$(id -u "$RUNNER_USER")
home=$(getent passwd "$RUNNER_USER" | cut -d: -f6)
for _ in $(seq 30); do [[ -S /run/user/$uid/bus ]] && break; sleep 1; done
as_user() { (cd / && sudo -u "$RUNNER_USER" -H env XDG_RUNTIME_DIR="/run/user/$uid" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" PATH=/usr/bin:/usr/sbin:/bin:/sbin "$@"); }
if ! as_user systemctl --user is-active --quiet docker.service; then
  as_user dockerd-rootless-setuptool.sh install
fi
as_user systemctl --user enable --now docker.service
as_user docker --host "unix:///run/user/$uid/docker.sock" info --format 'rootless docker {{.ServerVersion}} ok'

# 2. runner tarball
TAR="$LIB/actions-runner-linux-$ARCH-$RUNNER_VERSION.tar.gz"
if [[ ! -f "$TAR" ]]; then
  curl -fsSL -o "$TAR.part" "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-$ARCH-$RUNNER_VERSION.tar.gz"
  mv "$TAR.part" "$TAR"
fi
chmod 644 "$TAR"
# Runner native deps (libicu etc.) — once, from a scratch extract.
if [[ ! -f "$LIB/.deps-$RUNNER_VERSION" ]]; then
  tmp=$(mktemp -d); tar -xzf "$TAR" -C "$tmp"; "$tmp/bin/installdependencies.sh" >/dev/null; rm -rf "$tmp"
  touch "$LIB/.deps-$RUNNER_VERSION"
fi

# 3. config + request agent
cat >/etc/devflow/host.env <<EOF
HERMES_DATA_HOST=$HERMES_DATA_HOST
RUNNER_USER=$RUNNER_USER
RUNNER_TARBALL=$TAR
EOF
install -m 755 "$HERE/runner-host.sh" "$LIB/runner-host.sh"
data_owner=$(stat -c %u:%g "$HERMES_DATA_HOST")
for d in devflow devflow/runner devflow/runner/requests devflow/runner/status; do
  [[ -d "$HERMES_DATA_HOST/$d" ]] || install -d -m 755 -o "${data_owner%:*}" -g "${data_owner#*:}" "$HERMES_DATA_HOST/$d"
done

# 4. per-repo runner unit (user systemd of the runner user)
install -d -o "$RUNNER_USER" -g "$RUNNER_USER" "$home/.config" "$home/.config/systemd" "$home/.config/systemd/user" "$home/runners"
cat >"$home/.config/systemd/user/devflow-runner@.service" <<'EOF'
[Unit]
Description=devflow GitHub Actions runner %i
Requires=docker.service
After=docker.service

[Service]
Type=simple
WorkingDirectory=%h/runners/%i
ExecStart=%h/runners/%i/run.sh
Environment=DOCKER_HOST=unix://%t/docker.sock
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
chown "$RUNNER_USER:$RUNNER_USER" "$home/.config/systemd/user/devflow-runner@.service"
as_user systemctl --user daemon-reload

# 5. watcher (root)
cat >/etc/systemd/system/devflow-runner-requests.service <<EOF
[Unit]
Description=devflow: register requested GitHub Actions runners

[Service]
Type=oneshot
ExecStart=$LIB/runner-host.sh
EOF
cat >/etc/systemd/system/devflow-runner-requests.path <<EOF
[Unit]
Description=devflow: watch runner registration requests

[Path]
DirectoryNotEmpty=$HERMES_DATA_HOST/devflow/runner/requests

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now devflow-runner-requests.path
systemctl start devflow-runner-requests.service || true
echo "devflow host setup OK (user=$RUNNER_USER uid=$uid, runner $RUNNER_VERSION)"
