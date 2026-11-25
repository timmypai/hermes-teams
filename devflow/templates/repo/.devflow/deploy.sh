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

_deploy_flag="${DEVFLOW_DEPLOY:-true}"
_deploy_flag="${_deploy_flag#"${_deploy_flag%%[![:space:]]*}"}"
_deploy_flag="${_deploy_flag%"${_deploy_flag##*[![:space:]]}"}"
if [[ "${_deploy_flag,,}" == "false" ]]; then
  echo "DEVFLOW_DEPLOY=false; skip deploy"; exit 0
fi

: "${DEVFLOW_APP:?}" "${DEVFLOW_PORT:?}"
SHA="${GITHUB_SHA:-$(git rev-parse HEAD)}"
REPO_IMAGE="devflow/${DEVFLOW_APP}"
export DEVFLOW_IMAGE="${REPO_IMAGE}:${SHA:0:12}"
export DEVFLOW_SOURCE_SHA="$SHA"
export DEVFLOW_RELEASE_DIR="$PWD"
PROJECT="devflow-${DEVFLOW_APP}"
COMPOSE=(docker compose -p "$PROJECT" -f deploy/compose.yml)
LIVE="${DEVFLOW_LIVE_DIR:-$HOME/.devflow-live/$DEVFLOW_APP}"
if [[ -n "${DEVFLOW_LOCK_FILE:-}" ]]; then
  (umask 077; mkdir -p "$(dirname "$DEVFLOW_LOCK_FILE")"; touch "$DEVFLOW_LOCK_FILE")
  exec 9>"$DEVFLOW_LOCK_FILE"
  flock 9
fi
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
    local rb_sha
    rb_sha=$(release_source_sha "$img")
    if rollback_release "$img" "$rb_sha" && healthy_release; then
      echo "devflow: rollback healthy" >&2
      if [[ -x .devflow/deploy-post.sh ]]; then
        DEVFLOW_DEPLOY_PHASE=rollback DEVFLOW_SOURCE_SHA="$rb_sha" bash .devflow/deploy-post.sh || true
      fi
    else
      echo "::error::devflow: rollback also unhealthy" >&2
    fi
    capture rollback || true
  elif docker image inspect "${REPO_IMAGE}:live" >/dev/null 2>&1; then
    # 沒有 release 存檔（首次切換至公版 deploy.sh）：換回映像並使用切換前的 source SHA（絕不沿用失敗候選版 SHA）。
    local pre_sha=""
    if [[ -f $LIVE.d/pre-cutover.source_sha ]]; then
      pre_sha=$(<"$LIVE.d/pre-cutover.source_sha")
    else
      pre_sha=$(release_source_sha "${REPO_IMAGE}:live")
    fi
    echo "devflow: no release snapshot; rolling back the image only to ${REPO_IMAGE}:live" >&2
    if DEVFLOW_IMAGE="${REPO_IMAGE}:live" DEVFLOW_SOURCE_SHA="$pre_sha" \
        "${COMPOSE[@]}" up -d --no-build --remove-orphans && healthy; then
      echo "devflow: rollback healthy" >&2
      if [[ -x .devflow/deploy-post.sh ]]; then
        DEVFLOW_DEPLOY_PHASE=rollback DEVFLOW_SOURCE_SHA="$pre_sha" \
          bash .devflow/deploy-post.sh || true
      fi
    else
      echo "::error::devflow: rollback also unhealthy" >&2
    fi
    capture rollback || true
  else
    echo "::error::devflow: no release or ${REPO_IMAGE}:live to roll back to" >&2
  fi
  exit 1
}

# 解析既有 release 的真實 source SHA（絕不回退成失敗候選版的 DEVFLOW_SOURCE_SHA）。
release_source_sha() {
  local img="${1:-}" sha=""
  if [[ -f $LIVE/source_sha ]]; then
    sha=$(<"$LIVE/source_sha")
  fi
  if [[ -z "$sha" && -n "$img" ]]; then
    sha=$(docker image inspect "$img" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)
    [[ "$sha" == "<no value>" ]] && sha=""
  fi
  if [[ -z "$sha" && -n "$img" ]]; then
    sha=$(docker image inspect "$img" --format '{{index .Config.Labels "io.devflow.source"}}' 2>/dev/null || true)
    [[ "$sha" == "<no value>" ]] && sha=""
  fi
  printf '%s' "$sha"
}

# 上一個成功的 release：它的映像 ID（重新標成 :live）配它的 compose、.env、config.env 與 source_sha。
rollback_release() {
  local prev_sha="${2:-}"
  if [[ -z "$prev_sha" ]]; then
    prev_sha=$(release_source_sha "$1")
  fi
  docker tag "$1" "${REPO_IMAGE}:live"
  cp "$LIVE/.env" deploy/.env
  (set -a; . "$LIVE/config.env"; set +a
    DEVFLOW_IMAGE="${REPO_IMAGE}:live" DEVFLOW_RELEASE_DIR="$LIVE" \
      DEVFLOW_SOURCE_SHA="$prev_sha" \
      docker compose -p "$PROJECT" -f "$LIVE/compose.yml" --project-directory deploy \
      up -d --no-build --remove-orphans)
}

# 用那個 release 的 config.env（埠、健康路徑）檢查。
healthy_release() {
  (set -a; . "$LIVE/config.env"; set +a
    COMPOSE=(docker compose -p "$PROJECT" -f "$LIVE/compose.yml" --project-directory deploy); healthy)
}

# 發布成功的 release：整組寫進新目錄，再用 rename 一步把 $LIVE 指過去；中斷時 $LIVE 仍指向完整的舊 release。
save_release() {
  local id dir rel
  local -a _rel_files=()
  id=$(docker image inspect "${DEVFLOW_IMAGE}" --format '{{.Id}}')
  dir="$LIVE.d/${SHA:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mkdir -p "$LIVE.d"
  (umask 077; mkdir "$dir")
  cp deploy/compose.yml deploy/.env .devflow/config.env "$dir/"
  if [[ -n "${DEVFLOW_RELEASE_FILES:-}" ]]; then
    IFS=',' read -r -a _rel_files <<<"$DEVFLOW_RELEASE_FILES"
    for rel in "${_rel_files[@]}"; do
      rel="${rel#"${rel%%[![:space:]]*}"}"
      rel="${rel%"${rel##*[![:space:]]}"}"
      [[ -n "$rel" && -f "$rel" ]] || continue
      mkdir -p "$dir/$(dirname "$rel")"
      cp "$rel" "$dir/$rel"
    done
  fi
  echo "$id" >"$dir/image"
  echo "$SHA" >"$dir/source_sha"
  ln -sfn "$dir" "$LIVE.new"
  mv -Tf "$LIVE.new" "$LIVE"
  find "$LIVE.d" -mindepth 1 -maxdepth 1 ! -path "$dir" -exec rm -rf {} +
}

echo "::group::build ${DEVFLOW_IMAGE}"
build_ok=true
if [[ -x .devflow/deploy-build.sh ]]; then
  bash .devflow/deploy-build.sh || build_ok=false
else
  "${COMPOSE[@]}" build --pull || build_ok=false
fi
if ! $build_ok; then
  echo "::endgroup::"
  # build 不動到執行中的容器，不需回滾；仍記下目前（舊版）狀態。
  echo "::error::devflow: ${DEVFLOW_APP} ${SHA:0:12} build failed; running version unchanged" >&2
  capture build-failed
  exit 1
fi
echo "::endgroup::"

# 首次切換至公版 deploy.sh（尚未有 release 快照也尚未有 :live tag）：
# 若同名 compose 專案已有執行中容器，先將其現行映像標為 :live 並記下 source SHA，失敗時可回滾。
if [[ ! -f $LIVE/image ]] && ! docker image inspect "${REPO_IMAGE}:live" >/dev/null 2>&1; then
  existing_cid=$(docker ps -q \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=${DEVFLOW_SERVICE:-app}" | head -n 1 || true)
  if [[ -n "$existing_cid" ]]; then
    existing_img=$(docker inspect --format '{{.Image}}' "$existing_cid" 2>/dev/null || true)
    existing_sha=$(docker inspect --format '{{index .Config.Labels "io.devflow.source"}}' "$existing_cid" 2>/dev/null || true)
    if [[ -n "$existing_img" ]]; then
      docker tag "$existing_img" "${REPO_IMAGE}:live"
      if [[ -n "$existing_sha" ]]; then
        mkdir -p "$LIVE.d"
        echo "$existing_sha" >"$LIVE.d/pre-cutover.source_sha"
      fi
      echo "devflow: seeded ${REPO_IMAGE}:live from running container ${existing_cid:0:12}"
    fi
  fi
fi

echo "::group::up"
up_ok=true
"${COMPOSE[@]}" up -d --remove-orphans || up_ok=false
echo "::endgroup::"
$up_ok || fail "up failed"
healthy || fail "failed health check"
verify || fail "failed verify"

save_release
docker tag "${DEVFLOW_IMAGE}" "${REPO_IMAGE}:live"
if [[ -x .devflow/deploy-post.sh ]]; then
  DEVFLOW_DEPLOY_PHASE=deploy bash .devflow/deploy-post.sh
fi
echo "devflow: ${DEVFLOW_APP} ${SHA:0:12} healthy + verified on 127.0.0.1:${DEVFLOW_PORT}${DEVFLOW_HEALTH_PATH:-}"
# 保留 live 與最近 3 個版本
docker images "${REPO_IMAGE}" --format '{{.Tag}}' | grep -vx live | tail -n +4 \
  | xargs -r -I{} docker rmi "${REPO_IMAGE}:{}" >/dev/null 2>&1 || true
