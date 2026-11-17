#!/usr/bin/env bash
# host/runner-host.sh 以 root 執行，但 requests/status 目錄由 Hermes 容器控制。用一次性 root 容器驗證：
#   1 容器把 status 換成指向 root 目錄的 symlink、request 指向 root-only 檔：root 檔案的 owner/內容不變
#   2 一般的 repo/key 不符請求：寫出 data owner 擁有的 status，請求被消耗
#   data 目錄放在別人擁有的 0700 目錄下（同 VM：/home/hermes-runtime），data owner 無法用絕對路徑到達
# 用法：bash devflow/tests/runner_host.sh   （需要 docker）
set -euo pipefail
HOST=$(cd "$(dirname "$0")/../../host" && pwd)
docker run --rm -i -v "$HOST/runner-host.sh:/runner-host.sh:ro" python:3.12-slim bash -s <<'EOF'
set -euo pipefail
useradd -m devflow
# Like the VM: data lives under the Hermes runtime user's 0700 home, so its owner (the container's
# uid) cannot reach it by absolute path.
D=/home/rt/data
mkdir -p /etc/devflow $D/devflow/runner/requests /victim
chown 2000:2000 /home/rt; chmod 700 /home/rt
printf "HERMES_DATA_HOST=$D\nRUNNER_USER=devflow\nRUNNER_TARBALL=/none\n" >/etc/devflow/host.env
echo secret >/root/canary; chmod 600 /root/canary
chown -R 1000:1000 $D
as_controller() { (cd $D && setpriv --reuid=1000 --regid=1000 --clear-groups sh -c "$1"); }
fails=0
expect() { if [[ $2 == "$3" ]]; then echo "PASS $1"; else echo "FAIL $1: got '$2', want '$3'"; fails=$((fails + 1)); fi; }

as_controller 'ln -s /victim devflow/runner/status
  ln -s /root/canary devflow/runner/requests/o__r.json
  echo "{\"repo\":\"x/y\",\"token\":\"t\"}" >devflow/runner/requests/a__b.json'
bash /runner-host.sh >/dev/null 2>&1 || true
expect "1 root 目錄不被改 owner" "$(stat -c %u:%g /victim)" 0:0
expect "1 root 目錄沒有寫入" "$(ls -A /victim | wc -l)" 0
expect "1 root-only 檔案不變" "$(stat -c %u:%a /root/canary):$(cat /root/canary)" 0:600:secret
expect "1 請求都被消耗" "$(ls -A $D/devflow/runner/requests | wc -l)" 0

as_controller 'rm devflow/runner/status
  echo "{\"repo\":\"x/y\",\"token\":\"t\"}" >devflow/runner/requests/a__b.json'
bash /runner-host.sh
st=$D/devflow/runner/status/a__b.json
expect "2 status 屬於 data owner" "$(stat -c %u:%g "$st")" 1000:1000
expect "2 status 內容" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["state"], d["message"])' "$st")" "error repo/key mismatch"
expect "2 請求被消耗" "$(ls -A $D/devflow/runner/requests | wc -l)" 0
[[ $fails == 0 ]] && echo "runner-host: all passed" || { echo "runner-host: $fails failed"; exit 1; }
EOF
