#!/usr/bin/env bash
# PR CI：用與部署相同的 deploy/compose.yml 起一份服務，等健康後把 verify.sh 需要的環境寫進 $GITHUB_ENV。
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . .devflow/config.env; set +a
export DEVFLOW_IMAGE="devflow-ci/${DEVFLOW_APP}:${GITHUB_SHA:-local}"
project="ci-${DEVFLOW_APP}"
compose=(docker compose -p "$project" -f deploy/compose.yml)
touch deploy/.env
"${compose[@]}" up -d --build
url="http://127.0.0.1:${DEVFLOW_PORT}"
for _ in $(seq 60); do
  curl -fsS -o /dev/null "${url}${DEVFLOW_HEALTH_PATH:-/}" && break
  sleep 2
done
if ! curl -fsS -o /dev/null "${url}${DEVFLOW_HEALTH_PATH:-/}"; then
  "${compose[@]}" logs --tail 80; exit 1
fi
{
  echo "DEVFLOW_BASE_URL=${url}"
  echo "DEVFLOW_NET=${project}_default"
  echo "DEVFLOW_INNER_URL=http://${DEVFLOW_SERVICE:-app}:${DEVFLOW_APP_PORT}"
} >> "${GITHUB_ENV:-/dev/stdout}"
