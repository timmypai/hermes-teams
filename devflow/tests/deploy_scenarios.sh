#!/usr/bin/env bash
# 範本 deploy.sh 的四個情境，用一次性 Docker 跑（kit CI 的 GitHub-hosted runner 或本機）：
#   1 成功：上線並標記 :live
#   2 build 失敗：回 1，執行中的版本不變
#   3 health 失敗：採證後回滾到 :live
#   4 smoke 情境失敗（verify.sh 的 scenario，--smoke 才會在部署後跑）：採證後回滾到 :live
# 用法：bash devflow/tests/deploy_scenarios.sh   （需要 docker compose、curl、python3、git）
set -euo pipefail
KIT=$(cd "$(dirname "$0")/.." && pwd)
PORT=${DEVFLOW_TEST_PORT:-18999}
APP=kittest$$
W=$(mktemp -d)
cleanup() {  # 以 compose 專案標籤清除：compose down 需要 config.env 的變數，這裡不依賴它
  docker ps -aq --filter "label=com.docker.compose.project=devflow-$APP" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm "devflow-${APP}_default" >/dev/null 2>&1 || true
  docker images "devflow/$APP" -q | sort -u | xargs -r docker rmi -f >/dev/null 2>&1 || true
  rm -rf "$W"
}
trap cleanup EXIT

cp -R "$KIT/templates/repo/." "$W/"
cd "$W"
sed -i "s/__APP__/$APP/; s/__PORT__/$PORT/" .devflow/config.env
# 每次健康探測間隔 2 秒，5 次夠小型 app 起來，也讓失敗情境不必等 2 分鐘。
export DEVFLOW_HEALTH_TRIES=5
# 唯讀的 smoke 情境（部署後會對正式服務跑：不 seed、不寫入），插在 verify.sh 的情境區結尾。
AC=$(cat <<'EOF'
ac1_s1_given() { curl -fsS "$DEVFLOW_BASE_URL/health" >/dev/null; }
ac1_s1_when() { curl -fsS "$DEVFLOW_BASE_URL/version" >"$W/version"; }
ac1_s1_then() { ! grep -q broken "$W/version"; }
scenario t_kit/AC-1.S1 "版本頁不是壞的" ac1_s1 --given "服務已上線" --when "讀取 /version" --then "版本不是 broken" --smoke
EOF
)
export AC
awk '/^# devflow: end of scenarios/ { print ENVIRON["AC"] } { print }' .devflow/verify.sh >.devflow/verify.sh.new
mv .devflow/verify.sh.new .devflow/verify.sh
grep -q '^scenario t_kit/AC-1.S1' .devflow/verify.sh || { echo "verify.sh 沒有情境插入點（# devflow: end of scenarios）" >&2; exit 2; }
git init -q && git config user.email t@t && git config user.name t

release() {  # release <version> <health status> [dockerfile extra]
  cat >app.py <<EOF
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        code, body = (($2, b"health") if self.path == "/health" else (200, b"$1"))
        self.send_response(code); self.end_headers(); self.wfile.write(body)
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
EOF
  printf 'FROM python:3.12-alpine\n%sCOPY app.py /app.py\nCMD ["python", "-u", "/app.py"]\n' "${3:-}" >Dockerfile
  git add -A && git commit -qm "$1"
  export GITHUB_SHA; GITHUB_SHA=$(git rev-parse HEAD)
}

fails=0
check() {  # check <name> <expected deploy rc> <actual rc> <expected served version> [diagnostics dir]
  local served live
  served=$(curl -fsS "http://127.0.0.1:$PORT/version" 2>/dev/null || echo "<down>")
  live=$(docker image inspect "devflow/$APP:live" --format '{{.Id}}' 2>/dev/null || echo none)
  local ok=true
  [[ $3 == "$2" ]] || ok=false
  [[ $served == "$4" ]] || ok=false
  [[ -z ${5:-} || -d .devflow/diagnostics/$5 ]] || ok=false
  [[ -z ${LIVE_V1:-} || $live == "$LIVE_V1" ]] || ok=false
  if $ok; then echo "PASS $1"; else
    echo "FAIL $1: rc=$3 (want $2) served=$served (want $4) diagnostics/${5:-} live=$live"; fails=$((fails + 1))
  fi
}

deploy() { set +e; bash .devflow/deploy.sh >"$W/$1.log" 2>&1; rc=$?; set -e; [[ $rc == 0 ]] || tail -n 15 "$W/$1.log"; }

if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
  echo "port $PORT already serves something; set DEVFLOW_TEST_PORT" >&2; exit 2
fi

release v1 200; deploy ok
check "1 成功上線" 0 "$rc" v1
[[ $fails == 0 ]] || { echo "deploy scenarios: scenario 1 failed; later scenarios need a live v1"; exit 1; }
LIVE_V1=$(docker image inspect "devflow/$APP:live" --format '{{.Id}}')

release v2 200 $'RUN exit 3\n'; deploy build
check "2 build 失敗不動執行中版本" 1 "$rc" v1 build-failed

release v3 500; deploy health
check "3 health 失敗回滾" 1 "$rc" v1 candidate

release broken 200; deploy verify
check "4 smoke 情境失敗回滾" 1 "$rc" v1 candidate

[[ $fails == 0 ]] && echo "deploy scenarios: 4/4 passed" || { echo "deploy scenarios: $fails failed"; exit 1; }
