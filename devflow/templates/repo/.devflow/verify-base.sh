#!/usr/bin/env bash
# devflow base 驗證（ci.yml 的 verify-base job，只在 pull_request）：證明 PR 的情境在改動前的程式上會紅。
# 從 head 的 checkout 執行：git worktree 取出 BASE，蓋上 head 的 .devflow/（情境與腳本以 head 為準），
# 用另一個 compose 專案與空閒埠起服務，只跑 PR 說明 `Covers:` 列出的情境（DEVFLOW_ONLY），
# 報告寫到 head 的 .devflow/evidence-base，最後 acceptance.py classify-base：每個 covered 情境要 RED
# （Given 通過、When / Then 失敗），或 GREEN + `Already-met:`、INVALID + `Base-unverifiable:`。
# base 起不來 / health 失敗 = health failed = INVALID。PR 說明沒有 `Covers:` 時不需要證明，直接通過。
# 用法：PR_BODY=… bash .devflow/verify-base.sh <base sha>   （需要完整 git 歷史：checkout fetch-depth: 0）
set -euo pipefail
cd "$(dirname "$0")/.."
base=${1:?usage: verify-base.sh <base sha>}
head=$PWD
set -a; . .devflow/config.env; set +a
DEVFLOW_HEAD_SHA=${DEVFLOW_HEAD_SHA:-$(git rev-parse HEAD)}
export DEVFLOW_HEAD_SHA DEVFLOW_KIND=base DEVFLOW_EVIDENCE="$head/.devflow/evidence-base"

ids=$(python3 .devflow/acceptance.py covers)
if [[ -z $ids ]]; then
  echo "verify-base: PR 說明沒有 Covers:，沒有情境要在 base 上證明"
  exit 0
fi
base=$(git rev-parse --verify "${base}^{commit}")
rm -rf "$DEVFLOW_EVIDENCE"
mkdir -p "$DEVFLOW_EVIDENCE"

tmp=$(mktemp -d)
tree="$tmp/base"
project="ci-${DEVFLOW_APP}-base"
cleanup() {
  cd "$head"
  docker ps -aq --filter "label=com.docker.compose.project=$project" 2>/dev/null | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm "${project}_default" >/dev/null 2>&1 || true
  git worktree remove --force "$tree" >/dev/null 2>&1 || true
  rm -rf "$tmp" 2>/dev/null || true
  git worktree prune >/dev/null 2>&1 || true
}
trap cleanup EXIT

git worktree add --detach --quiet "$tree" "$base"
rm -rf "$tree/.devflow"
tar -C "$head" --exclude=.devflow/evidence --exclude=.devflow/evidence-base --exclude=.devflow/diagnostics \
  --exclude=node_modules -cf - .devflow | tar -C "$tree" -xf -
cd "$tree"

port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
export DEVFLOW_ONLY="$ids" DEVFLOW_SMOKE='' DEVFLOW_PROJECT="$project" DEVFLOW_UP_PORT="$port"
export DEVFLOW_IMAGE="devflow-ci/${DEVFLOW_APP}:base-${base:0:12}"
up_env="$tmp/up.env"
: >"$up_env"
echo "::group::up (base ${base:0:12})"
up_ok=true
GITHUB_ENV="$up_env" bash .devflow/up.sh || up_ok=false
echo "::endgroup::"
set -a
# shellcheck source=/dev/null
. "$up_env"
set +a

if $up_ok; then
  echo "::group::verify (base ${base:0:12}): $ids"
  bash .devflow/verify.sh || echo "verify-base: verify.sh exit $?"
  echo "::endgroup::"
else
  echo "::warning::devflow: base ${base:0:12} 起不來；covered 情境判為 INVALID" >&2
  DEVFLOW_DIAGNOSTICS="$head/.devflow/diagnostics" bash .devflow/capture.sh "$project" base || true
fi
if [[ ! -f $DEVFLOW_EVIDENCE/acceptance.json ]]; then
  python3 .devflow/acceptance.py build --kind=base --health=failed \
    "--problem=base ${base:0:12}: up.sh or verify.sh failed before a report was written"
fi
python3 .devflow/acceptance.py classify-base --base-sha "$base"
