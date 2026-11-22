#!/usr/bin/env bash
# Hermes 的 UI 操作：在 Playwright 官方映像內對「已部署的正式服務」跑一個操作腳本 .devflow/ui/ops/<op>.spec.ts。
# 由 operate.yml（Oracle VM self-hosted runner）執行；本機除錯可自己設 DEVFLOW_NET / DEVFLOW_INNER_URL。
# 環境：
#   DEVFLOW_OP       操作名稱（小寫英數與 -）
#   DEVFLOW_OP_ARGS  非秘密參數 JSON 物件；腳本讀 process.env.DEVFLOW_OP_ARGS
#   DEVFLOW_OPS_ENV  秘密，dotenv 格式（KEY=VALUE 一行一個，不加引號）；只進容器環境變數，不寫進證據
# 輸出（上傳成 artifact）：.devflow/evidence/operate/ 下的 result.json（腳本寫 process.env.DEVFLOW_OP_RESULT）、
# 腳本自己存的截圖（/evidence/*.png）、playwright-report.json；失敗時另有 Playwright 的失敗截圖。不錄 trace。
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . .devflow/config.env; set +a

op=${DEVFLOW_OP:-}
[[ $op =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "DEVFLOW_OP: '$op' 要是小寫英數與 -" >&2; exit 2; }
[[ -f .devflow/ui/ops/$op.spec.ts ]] || { echo "沒有操作腳本 .devflow/ui/ops/$op.spec.ts" >&2; exit 2; }
export DEVFLOW_OP_ARGS=${DEVFLOW_OP_ARGS:-"{}"}
python3 -c 'import json,os,sys; sys.exit(not isinstance(json.loads(os.environ["DEVFLOW_OP_ARGS"]), dict))' \
  || { echo "DEVFLOW_OP_ARGS 要是 JSON 物件" >&2; exit 2; }
v=$(grep -oE '"@playwright/test": *"[0-9]+\.[0-9]+\.[0-9]+"' .devflow/ui/package.json | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)
: "${v:?pin an exact @playwright/test version (x.y.z) in .devflow/ui/package.json}"

project="devflow-${DEVFLOW_APP:?}"
net=${DEVFLOW_NET:-${project}_default}
url=${DEVFLOW_INNER_URL:-http://${project}-${DEVFLOW_SERVICE:-app}-1:${DEVFLOW_APP_PORT:?}}

E=.devflow/evidence/operate
rm -rf "$E"; mkdir -p "$E" .devflow/ui/node_modules
E=$(realpath "$E")
envf=$(mktemp); chmod 600 "$envf"; trap 'rm -f "$envf"' EXIT
printf '%s\n' "${DEVFLOW_OPS_ENV:-}" >"$envf"

echo "operate $op -> $url"
# 容器以 root 跑：結束前把 /evidence 與 npm 可能新建的 package-lock.json 交還主機使用者（見 playwright.sh）。
docker run --rm --ipc=host --network "$net" --env-file "$envf" \
  -e BASE_URL="$url" -e CI=1 -e DEVFLOW_OP="$op" -e DEVFLOW_OP_ARGS -e DEVFLOW_OP_RESULT=/evidence/result.json \
  -v "$PWD/.devflow/ui:/ui" -v /ui/node_modules -v "$E:/evidence" -w /ui \
  "mcr.microsoft.com/playwright:v${v}-noble" \
  bash -c 'if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi >/dev/null && npx playwright test "ops/$DEVFLOW_OP.spec.ts" --trace off
    rc=$?; owner=$(stat -c %u:%g /evidence); chown -R "$owner" /evidence
    [ ! -f package-lock.json ] || chown "$(stat -c %u:%g /ui)" package-lock.json; exit $rc'
