#!/usr/bin/env bash
# CI：用與部署相同的 deploy/compose.yml 起一份服務，等健康，把 verify.sh 需要的環境寫進 $GITHUB_ENV
# （本機沒有 GITHUB_ENV 時印到 stdout）。環境先寫出再起服務：起不來時 verify.sh 仍能把 health failed 記進報告。
# DEVFLOW_SERVICE 空 = 沒有服務（CLI / 函式庫）：不起 compose，health 記 none；有 Dockerfile 就 build 成
# $DEVFLOW_IMAGE 給 shell 情境 docker run。宣告了服務卻沒有 Dockerfile 直接失敗（不再靜默略過驗收）。
# 可覆寫（verify-base.sh 用，避免與 head 那份撞名、撞埠）：DEVFLOW_IMAGE、DEVFLOW_PROJECT、DEVFLOW_UP_PORT。
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . .devflow/config.env; set +a
export DEVFLOW_IMAGE="${DEVFLOW_IMAGE:-devflow-ci/${DEVFLOW_APP}:${GITHUB_SHA:-local}}"
project=${DEVFLOW_PROJECT:-ci-${DEVFLOW_APP}}
port=${DEVFLOW_UP_PORT:-$DEVFLOW_PORT}
out=${GITHUB_ENV:-/dev/stdout}

if [[ -z ${DEVFLOW_SERVICE:-} ]]; then
  if [[ ! -f Dockerfile ]]; then
    printf '%s\n' DEVFLOW_IMAGE= DEVFLOW_BASE_URL= DEVFLOW_NET= DEVFLOW_INNER_URL= >>"$out"
    echo "devflow: DEVFLOW_SERVICE 空、沒有 Dockerfile：不起服務，只跑 shell 情境" >&2
    exit 0
  fi
  printf '%s\n' "DEVFLOW_IMAGE=${DEVFLOW_IMAGE}" DEVFLOW_BASE_URL= DEVFLOW_NET= DEVFLOW_INNER_URL= >>"$out"
  docker build -t "$DEVFLOW_IMAGE" . >&2
  echo "devflow: DEVFLOW_SERVICE 空：不起服務；shell 情境可用 ${DEVFLOW_IMAGE}" >&2
  exit 0
fi

url="http://127.0.0.1:${port}"
{
  echo "DEVFLOW_IMAGE=${DEVFLOW_IMAGE}"
  echo "DEVFLOW_BASE_URL=${url}"
  echo "DEVFLOW_NET=${project}_default"
  # 用 compose 容器名（<project>-<service>-1）而不是服務名：服務名 app 是公開 TLD，Chromium 的 HSTS preload 會強制改走 HTTPS。
  echo "DEVFLOW_INNER_URL=http://${project}-${DEVFLOW_SERVICE}-1:${DEVFLOW_APP_PORT}"
} >>"$out"
if [[ ! -f Dockerfile ]]; then
  echo "::error::devflow: config.env 宣告了服務 DEVFLOW_SERVICE=${DEVFLOW_SERVICE}，但 repo 根目錄沒有 Dockerfile；沒有服務的 repo 請設 DEVFLOW_SERVICE=" >&2
  exit 1
fi
compose=(docker compose -p "$project" -f deploy/compose.yml)
touch deploy/.env
DEVFLOW_PORT=$port "${compose[@]}" up -d --build >&2
for _ in $(seq 60); do
  curl -fsS -o /dev/null --max-time 5 "${url}${DEVFLOW_HEALTH_PATH:-/}" && break
  sleep 2
done
if ! curl -fsS -o /dev/null --max-time 5 "${url}${DEVFLOW_HEALTH_PATH:-/}"; then
  DEVFLOW_PORT=$port "${compose[@]}" logs --tail 80 >&2
  echo "::error::devflow: ${DEVFLOW_SERVICE} 沒有通過健康檢查 ${url}${DEVFLOW_HEALTH_PATH:-/}" >&2
  exit 1
fi
