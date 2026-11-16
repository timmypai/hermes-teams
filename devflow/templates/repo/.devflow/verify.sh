#!/usr/bin/env bash
# devflow 驗收 journeys：像使用者一樣操作服務，EPIC 每條驗收（AC-n）一段，證據存到 $DEVFLOW_EVIDENCE。
# PR CI（.devflow/up.sh 起服務）與部署後（.devflow/deploy.sh，Oracle VM runner）都跑這支：
# 失敗 = PR 的 devflow-ci 紅燈 / 部署回滾。證據上傳成 Actions artifact `devflow-evidence-*`。
# 環境：
#   DEVFLOW_BASE_URL   從執行機器連服務（curl 用）
#   DEVFLOW_NET        服務所在的 Docker network（瀏覽器容器加入它）
#   DEVFLOW_INNER_URL  在該 network 內連服務（Playwright 用）
#   DEVFLOW_EVIDENCE   證據目錄（預設 .devflow/evidence）
#   DEVFLOW_ONLY       只跑這些 AC（空白或逗號分隔，如 "AC-2 AC-5"）；空 = 全部。health 一律跑。repro workflow 用
#   DEVFLOW_TRACE      非空 = UI journeys 全程錄 Playwright trace（預設只在失敗時保留）
set -euo pipefail
cd "$(dirname "$0")/.."
. .devflow/config.env
export DEVFLOW_EVIDENCE="${DEVFLOW_EVIDENCE:-.devflow/evidence}"
E="$DEVFLOW_EVIDENCE"; mkdir -p "$E"
: "${DEVFLOW_BASE_URL:?run through .devflow/up.sh (CI) or .devflow/deploy.sh (deploy)}"

DEVFLOW_ONLY=${DEVFLOW_ONLY:-}
read -ra only <<<"${DEVFLOW_ONLY//,/ }"
for id in "${only[@]}"; do
  [[ $id =~ ^AC-[0-9]+$ ]] || { echo "DEVFLOW_ONLY: '$id' 不是 AC-n" >&2; exit 2; }
done
export DEVFLOW_ONLY="${only[*]}"

# journey <AC-id> <說明> <指令…>：執行指令，輸出存成證據 $E/<AC-id>.txt；失敗就印出並回傳非 0。
journey() {
  local ac=$1 what=$2; shift 2
  if [[ -n $DEVFLOW_ONLY && $ac != health && " $DEVFLOW_ONLY " != *" $ac "* ]]; then
    echo "SKIP $ac $what"; return 0
  fi
  if { echo "# $ac $what"; echo "\$ $*"; "$@"; } >"$E/$ac.txt" 2>&1; then
    echo "PASS $ac $what"
  else
    echo "FAIL $ac $what"; cat "$E/$ac.txt"; return 1
  fi
}

# --- 依 EPIC 驗收新增 journey（一條 AC 一行或一段）；保留 health ---
journey health "服務可用" curl -fsS -i "${DEVFLOW_BASE_URL}${DEVFLOW_HEALTH_PATH:-/}"

# UI journeys：有 .devflow/ui（devflow contract --ui）就在 Playwright 官方映像內跑，截圖存成 $E/AC-n.png。
if [[ -f .devflow/ui/package.json ]]; then bash .devflow/playwright.sh; fi
