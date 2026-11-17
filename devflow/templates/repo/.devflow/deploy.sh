#!/usr/bin/env bash
# devflow 部署：在 Oracle VM self-hosted runner（rootless Docker）上執行。
# build → up → 健康檢查 → verify（只跑 smoke 情境：唯讀，對正式服務）。up / 健康 / verify 失敗時先採證
# （capture.sh：狀態、退出原因、本次部署開始後的 log，已遮罩）再回滾到上一個成功的 release；採證寫在
# .devflow/diagnostics/，由 workflow 上傳。驗收報告寫在 .devflow/evidence/（kind deploy）。
# release = 部署成功的確切映像 ID + 當時的 deploy/compose.yml、deploy/.env、.devflow/config.env。
# 每個 release 寫成 $DEVFLOW_LIVE_DIR.d/<sha>-<時間>（0700），寫完才把 $DEVFLOW_LIVE_DIR 這個 symlink
# 原子換過去（預設 runner 使用者的 ~/.devflow-live/<app>）；回滾只讀這一份，不看 :live tag，所以不會
# 新版 compose 配舊映像。只換回映像不夠：新版改壞的 compose（command、port、env）會讓舊映像也起不來。
# :live tag 只供保留映像與沒有 release 存檔的舊部署使用。資料（volume、migration）不在 release 內，不會回復。
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
LIVE="${DEVFLOW_LIVE_DIR:-$HOME/.devflow-live/$DEVFLOW_APP}"
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

# 部署後驗收：對剛上線的服務只跑 smoke 情境（唯讀、不 seed、不寫入；完整情境已在 PR CI 與整合驗證跑過）。
# 沒有 .devflow/verify.sh = 失敗，不能略過。
verify() {
  if [[ ! -f .devflow/verify.sh ]]; then
    echo "::error::devflow: .devflow/verify.sh 不存在；部署後驗收不能略過" >&2
    return 1
  fi
  local rc=0
  echo "::group::verify (smoke)"
  DEVFLOW_KIND=deploy DEVFLOW_SMOKE=1 DEVFLOW_ONLY='' DEVFLOW_HEAD_SHA="$SHA" \
    DEVFLOW_BASE_URL="http://127.0.0.1:${DEVFLOW_PORT}" DEVFLOW_NET="${PROJECT}_default" \
    DEVFLOW_INNER_URL="http://${PROJECT}-${DEVFLOW_SERVICE:-app}-1:${DEVFLOW_APP_PORT}" bash .devflow/verify.sh || rc=$?
  echo "::endgroup::"
  return "$rc"
}

# 失敗：先採證（回滾會重建容器，候選版本的 log 就不在了），印出遮罩後的 log 尾端，再回滾。
fail() {
  echo "::error::devflow: ${DEVFLOW_APP} ${SHA:0:12} $1" >&2
  capture candidate || true   # 採證失敗也要回滾
  local f
  for f in .devflow/diagnostics/candidate/logs/*.log; do
    [[ -f $f ]] || continue
    echo "--- ${f##*/}（最後 80 行，已遮罩）" >&2
    tail -n 80 "$f" >&2
  done
  local img=""
  if [[ -f $LIVE/image ]]; then img=$(<"$LIVE/image"); fi
  if [[ -n $img ]] && docker image inspect "$img" >/dev/null 2>&1; then
    echo "devflow: rolling back to release $(basename "$(readlink "$LIVE")") (image ${img#sha256:})" >&2
    if rollback_release "$img" && healthy_release; then
      echo "devflow: rollback healthy" >&2
    else
      echo "::error::devflow: rollback also unhealthy" >&2
    fi
    capture rollback || true
  elif docker image inspect "${REPO_IMAGE}:live" >/dev/null 2>&1; then
    # 沒有 release 存檔（新版 deploy.sh 還沒成功部署過）：只換回映像，用目前的 compose。
    echo "devflow: no release snapshot; rolling back the image only to ${REPO_IMAGE}:live" >&2
    if DEVFLOW_IMAGE="${REPO_IMAGE}:live" "${COMPOSE[@]}" up -d --no-build --remove-orphans && healthy; then
      echo "devflow: rollback healthy" >&2
    else
      echo "::error::devflow: rollback also unhealthy" >&2
    fi
    capture rollback || true
  else
    echo "::error::devflow: no release or ${REPO_IMAGE}:live to roll back to" >&2
  fi
  exit 1
}

# 上一個成功的 release：它的映像 ID（重新標成 :live）配它的 compose、.env 與 config.env。
rollback_release() {
  docker tag "$1" "${REPO_IMAGE}:live"
  cp "$LIVE/.env" deploy/.env
  (set -a; . "$LIVE/config.env"; set +a
    DEVFLOW_IMAGE="${REPO_IMAGE}:live" docker compose -p "$PROJECT" -f "$LIVE/compose.yml" --project-directory deploy \
      up -d --no-build --remove-orphans)
}

# 用那個 release 的 config.env（埠、健康路徑）檢查。
healthy_release() {
  (set -a; . "$LIVE/config.env"; set +a
    COMPOSE=(docker compose -p "$PROJECT" -f "$LIVE/compose.yml" --project-directory deploy); healthy)
}

# 發布成功的 release：整組寫進新目錄，再用 rename 一步把 $LIVE 指過去；中斷時 $LIVE 仍指向完整的舊 release。
save_release() {
  local id dir
  id=$(docker image inspect "${DEVFLOW_IMAGE}" --format '{{.Id}}')
  dir="$LIVE.d/${SHA:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mkdir -p "$LIVE.d"
  (umask 077; mkdir "$dir")
  cp deploy/compose.yml deploy/.env .devflow/config.env "$dir/"
  echo "$id" >"$dir/image"
  ln -sfn "$dir" "$LIVE.new"
  mv -Tf "$LIVE.new" "$LIVE"
  find "$LIVE.d" -mindepth 1 -maxdepth 1 ! -path "$dir" -exec rm -rf {} +
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

save_release
docker tag "${DEVFLOW_IMAGE}" "${REPO_IMAGE}:live"
echo "devflow: ${DEVFLOW_APP} ${SHA:0:12} healthy + verified on 127.0.0.1:${DEVFLOW_PORT}${DEVFLOW_HEALTH_PATH:-}"
# 保留 live 與最近 3 個版本
docker images "${REPO_IMAGE}" --format '{{.Tag}}' | grep -vx live | tail -n +4 \
  | xargs -r -I{} docker rmi "${REPO_IMAGE}:{}" >/dev/null 2>&1 || true
