#!/usr/bin/env bash
# 在 Playwright 官方映像內跑 .devflow/ui 的 UI journeys（主機不必裝 Node 或瀏覽器）。
# 映像版本 = .devflow/ui/package.json 釘死的 @playwright/test 版本。截圖與報告寫到 $DEVFLOW_EVIDENCE。
# DEVFLOW_ONLY（verify.sh 已驗證格式，如 "AC-2 AC-5"）只跑標題含這些 AC 的 test；DEVFLOW_TRACE 非空 = 全程 trace。
set -euo pipefail
cd "$(dirname "$0")/.."
E=$(realpath -m "${DEVFLOW_EVIDENCE:-.devflow/evidence}"); mkdir -p "$E"
v=$(grep -oE '"@playwright/test": *"[0-9]+\.[0-9]+\.[0-9]+"' .devflow/ui/package.json | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)
: "${v:?pin an exact @playwright/test version (x.y.z) in .devflow/ui/package.json}"
args=()
if [[ -n ${DEVFLOW_ONLY:-} ]]; then
  [[ $DEVFLOW_ONLY =~ ^AC-[0-9]+( AC-[0-9]+)*$ ]] || { echo "DEVFLOW_ONLY: want 'AC-n AC-m'" >&2; exit 2; }
  args+=(--grep "(^|\\s)(${DEVFLOW_ONLY// /|})\\b" --pass-with-no-tests)
fi
[[ -z ${DEVFLOW_TRACE:-} ]] || args+=(--trace on)
docker run --rm --ipc=host --network "${DEVFLOW_NET:?}" \
  -e BASE_URL="${DEVFLOW_INNER_URL:?}" -e CI=1 \
  -v "$PWD/.devflow/ui:/ui" -v /ui/node_modules -v "$E:/evidence" -w /ui \
  "mcr.microsoft.com/playwright:v${v}-noble" \
  bash -c 'if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi >/dev/null && npx playwright test "$@"' \
  playwright "${args[@]}"
