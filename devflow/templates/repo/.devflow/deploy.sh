#!/usr/bin/env bash
# devflow 部署：在 Oracle VM self-hosted runner（rootless Docker）上執行。
# build → up → 健康檢查 → verify。up / 健康 / verify 失敗時先採證（capture.sh：狀態、退出原因、
# 本次部署開始後的 log，已遮罩）再回滾到上一個成功版本；採證寫在 .devflow/diagnostics/，由 workflow 上傳。
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . .devflow/config.env; set +a

if [[ "${DEVFLOW_DEPLOY:-true}" == "false" ]]; then
  echo "DEVFLOW_DEPLOY=false; skip deploy"; exit 0
fi

: "${DEVFLOW_APP:?}" "${DEVFLOW_PORT:?}"
SHA="${GITHUB_SHA:-$(git rev-parse HEAD)}"
REPO_IMAGE="devflow/${DEVFLOW_APP}"
export DEVFLOW_IMAGE="${REPO_IMAGE}:${SHA:0:12}"
PROJECT="devflow-${DEVFLOW_APP}"
COMPOSE=(docker compose -p "$PROJECT" -f deploy/compose.yml)
touch deploy/.env   # 由 workflow 從 DEVFLOW_APP_ENV secret 寫入；沒有就空檔
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)   # 診斷只收這次部署開始之後的 log
rm -rf .devflow/diagnostics

capture() { bash .devflow/capture.sh "$PROJECT" "$1" "$SINCE"; }

healthy() {
  local tries="${DEVFLOW_HEALTH_TRIES:-60}"
  for ((i = 1; i <= tries; i++)); do
    if [[ -n "${DEVFLOW_HEALTH_PATH:-}" ]]; then
      if curl -fsS -o /dev/null "http://127.0.0.1:${DEVFLOW_PORT}${DEVFLOW_HEALTH_PATH}"; then return 0; fi
    else
      local running
      running=$("${COMPOSE[@]}" ps --status running -q | wc -l)
      [[ "$running" -gt 0 ]] && return 0
    fi
    sleep 2
  done
  return 1
}

# 部署後驗收：對剛上線的服務跑 .devflow/verify.sh（與 PR CI 同一套 journeys）。
verify() {
  [[ -x .devflow/verify.sh ]] || return 0
  echo "::group::verify"
  DEVFLOW_BASE_URL="http://127.0.0.1:${DEVFLOW_PORT}" DEVFLOW_NET="${PROJECT}_default" \
    DEVFLOW_INNER_URL="http://${DEVFLOW_SERVICE:-app}:${DEVFLOW_APP_PORT}" bash .devflow/verify.sh
  local rc=$?
  echo "::endgroup::"
  return $rc
}

# 失敗：先採證（回滾會重建容器，候選版本的 log 就不在了），印出遮罩後的 log 尾端，再回滾。
fail() {
  echo "::error::devflow: ${DEVFLOW_APP} ${SHA:0:12} $1" >&2
  capture candidate
  local f
  for f in .devflow/diagnostics/candidate/logs/*.log; do
    [[ -f $f ]] || continue
    echo "--- ${f##*/}（最後 80 行，已遮罩）" >&2
    tail -n 80 "$f" >&2
  done
  if docker image inspect "${REPO_IMAGE}:live" >/dev/null 2>&1; then
    echo "devflow: rolling back to ${REPO_IMAGE}:live" >&2
    if DEVFLOW_IMAGE="${REPO_IMAGE}:live" "${COMPOSE[@]}" up -d --no-build --remove-orphans && healthy; then
      echo "devflow: rollback healthy" >&2
    else
      echo "::error::devflow: rollback also unhealthy" >&2
    fi
    capture rollback
  else
    echo "::error::devflow: no ${REPO_IMAGE}:live to roll back to" >&2
  fi
  exit 1
}

echo "::group::build ${DEVFLOW_IMAGE}"
if ! "${COMPOSE[@]}" build --pull; then
  echo "::endgroup::"
  # build 不動到執行中的容器，不需回滾；仍記下目前（舊版）狀態。
  echo "::error::devflow: ${DEVFLOW_APP} ${SHA:0:12} build failed; running version unchanged" >&2
  capture build-failed
  exit 1
fi
echo "::endgroup::"

echo "::group::up"
up_ok=true
"${COMPOSE[@]}" up -d --remove-orphans || up_ok=false
echo "::endgroup::"
$up_ok || fail "up failed"
healthy || fail "failed health check"
verify || fail "failed verify"

docker tag "${DEVFLOW_IMAGE}" "${REPO_IMAGE}:live"
echo "devflow: ${DEVFLOW_APP} ${SHA:0:12} healthy + verified on 127.0.0.1:${DEVFLOW_PORT}${DEVFLOW_HEALTH_PATH:-}"
# 保留 live 與最近 3 個版本
docker images "${REPO_IMAGE}" --format '{{.Tag}}' | grep -vx live | tail -n +4 \
  | xargs -r -I{} docker rmi "${REPO_IMAGE}:{}" >/dev/null 2>&1 || true
