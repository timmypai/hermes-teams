#!/usr/bin/env bash
# devflow 驗收情境：EPIC 每條驗收（AC-n）的每個情境（Sm）都是可執行的 Given / When / Then，
# 對真的跑起來的系統執行；證據、報告 acceptance.json 與對照表 acceptance-matrix.md 寫到 $DEVFLOW_EVIDENCE。
# PR CI（up.sh 起服務）、base 驗證（verify-base.sh）、整合驗證、部署後（deploy.sh，只跑 smoke）與 repro 都跑這支：
# 先跑完全部情境，再由 acceptance.py build 產生報告，最後以 acceptance.py gate 的結果當 exit code
# （失敗 = PR 的 devflow-ci 紅燈 / 部署回滾）。證據上傳成 Actions artifact `devflow-evidence-*`。
# 環境：
#   DEVFLOW_BASE_URL   從執行機器連服務（curl 用）；DEVFLOW_SERVICE 空（沒有服務）時不需要
#   DEVFLOW_NET        服務所在的 Docker network（瀏覽器容器加入它）
#   DEVFLOW_INNER_URL  在該 network 內連服務（Playwright 用）
#   DEVFLOW_IMAGE      本次建出的映像（shell 情境可 docker run 它；up.sh / deploy.sh 設定）
#   DEVFLOW_EVIDENCE   證據目錄（預設 .devflow/evidence）
#   DEVFLOW_KIND       報告種類 pr|base|integration|push|deploy|repro|local（預設 local）；base 只產報告不 gate
#   DEVFLOW_ONLY       只跑這些情境：完整 id（t_ab12/AC-1.S1）或整條 AC（t_ab12/AC-1），空白或逗號分隔；空 = 全部
#   DEVFLOW_SMOKE      1 = 只跑標了 smoke 的情境（部署後；smoke 只能是唯讀、不 seed、不寫入的情境）
#   DEVFLOW_TRACE      非空 = UI 情境全程錄 Playwright trace（預設只在失敗時保留）
#   PR_BODY            有值時 gate 另外要求 `Covers:` 列出的情境都在報告裡
# health 是前置條件，不是情境：失敗就不跑情境，報告記 health failed（gate 失敗）。
set -euo pipefail
cd "$(dirname "$0")/.."
. .devflow/config.env
export DEVFLOW_EVIDENCE="${DEVFLOW_EVIDENCE:-.devflow/evidence}"
E="$DEVFLOW_EVIDENCE"
export DEVFLOW_EVENTS="$E/scenario-events.jsonl"
kind=${DEVFLOW_KIND:-local}
rm -rf "$E/logs"
mkdir -p "$E/logs" "$E/shots"
rm -f "$E/playwright-report.json" "$E/acceptance.json" "$E/acceptance-matrix.md"
: >"$DEVFLOW_EVENTS"

DEVFLOW_ONLY=${DEVFLOW_ONLY:-}
read -ra only <<<"${DEVFLOW_ONLY//,/ }"
for x in "${only[@]}"; do
  if [[ ! $x =~ ^t_[0-9A-Za-z]+/AC-[0-9]+(\.S[0-9]+)?$ ]]; then
    echo "DEVFLOW_ONLY: '$x' 不是 t_xxx/AC-n 或 t_xxx/AC-n.Sm" >&2; exit 2
  fi
done
export DEVFLOW_ONLY="${only[*]}"
export DEVFLOW_SMOKE="${DEVFLOW_SMOKE:-}"
problems=()
work=$(mktemp -d)
trap 'rm -rf "$work" 2>/dev/null || true' EXIT

# --- health（前置條件）---
health=none
if [[ -n ${DEVFLOW_SERVICE:-} ]]; then
  : "${DEVFLOW_BASE_URL:?run through .devflow/up.sh (CI) or .devflow/deploy.sh (deploy)}"
  hurl="${DEVFLOW_BASE_URL}${DEVFLOW_HEALTH_PATH:-/}"
  if { echo "\$ curl -fsS -i $hurl"; curl -fsS -i --max-time 10 "$hurl"; } >"$E/logs/health.txt" 2>&1; then
    health=passed; echo "PASS health"
  else
    health=failed; echo "FAIL health（前置條件失敗，情境不跑）"; cat "$E/logs/health.txt"
  fi
fi

# ev <id> <smoke true|false> <phase> <title> <status> [error]：經 acceptance.py 記一筆事件（唯一的 JSON 寫入者）。
ev() {
  local flag=()
  if [[ $2 == true ]]; then flag=(--smoke); fi
  python3 .devflow/acceptance.py event "--id=$1" --runner=shell "--phase=$3" "--title=$4" "--status=$5" \
    "--error=${6:-}" "${flag[@]}"
}

# scenario <id> <說明> <函式前綴> --given 文字 --when 文字 --then 文字 [--smoke]
# 依序執行 <前綴>_given / _when / _then（各在自己的 subshell、set -e），輸出存到 $E/logs/<id>.<phase>.txt。
# 第一個失敗的 phase 之後的 phase 記 not-run；缺函式或缺文字 = structure。失敗不中斷 verify.sh，最後由 gate 判定。
# $W 是這個情境專用的暫存目錄（phase 之間用檔案傳資料）。
scenario() {
  local id=${1-} what=${2-} fn=${3-} g='' w='' t='' smoke=false bad=''
  shift "$(($# < 3 ? $# : 3))"
  while (($#)); do
    case $1 in
      --smoke) smoke=true; shift; continue ;;
      --given | --when | --then) ;;
      *) bad+="不認得的參數 '$1'; "; shift; continue ;;
    esac
    if (($# < 2)); then bad+="$1 缺少文字; "; break; fi
    case $1 in
      --given) g=$2 ;;
      --when) w=$2 ;;
      *) t=$2 ;;
    esac
    shift 2
  done
  if [[ ! $id =~ ^t_[0-9A-Za-z]+/AC-[0-9]+\.S[0-9]+$ ]]; then
    problems+=("scenario '$id': id 要像 t_ab12/AC-1.S1")
    echo "FAIL $id $what（id 格式錯）"; return 0
  fi
  if [[ $DEVFLOW_SMOKE == 1 && $smoke != true ]]; then return 0; fi
  if ((${#only[@]})); then
    local x hit=false
    for x in "${only[@]}"; do
      if [[ $id == "$x" || $id == "$x".* ]]; then hit=true; fi
    done
    if ! $hit; then return 0; fi
  fi
  if [[ $health == failed ]]; then echo "SKIP $id $what（health 失敗）"; return 0; fi

  local safe=${id//\//__} phase title missing=() why=$bad
  for phase in given when 'then'; do
    if ! declare -F "${fn}_$phase" >/dev/null; then missing+=("${fn}_$phase"); fi
  done
  if ((${#missing[@]})); then why+="缺少函式 ${missing[*]}; "; fi
  if [[ -z $g || -z $w || -z $t ]]; then why+="--given / --when / --then 都要有文字; "; fi
  if [[ -n $why ]]; then
    ev "$id" "$smoke" given "$g" not-run "structure: $why"
    ev "$id" "$smoke" when "$w" not-run
    ev "$id" "$smoke" 'then' "$t" not-run
    echo "FAIL $id $what（結構錯：$why）"; return 0
  fi

  local failed=false rc log
  W="$work/$safe"; export W
  mkdir -p "$W"
  for phase in given when 'then'; do
    case $phase in
      given) title=$g ;;
      when) title=$w ;;
      *) title=$t ;;
    esac
    if $failed; then ev "$id" "$smoke" "$phase" "$title" not-run; continue; fi
    log="$E/logs/$safe.$phase.txt"
    set +e
    (set -e; "${fn}_$phase") >"$log" 2>&1 </dev/null
    rc=$?
    set -e
    if ((rc == 0)); then
      ev "$id" "$smoke" "$phase" "$title" passed
    else
      failed=true
      ev "$id" "$smoke" "$phase" "$title" failed "exit $rc: $(tail -c 1500 "$log")"
      echo "FAIL $id $what（$phase: $title）"
      tail -n 20 "$log" | sed 's/^/    /'
    fi
  done
  if ! $failed; then echo "PASS $id $what"; fi
}

# --- 依 EPIC 驗收新增情境（cli / lib 驗收；每個 Sm 一組三個函式 + 一行 scenario）---
# ui / api 驗收寫在 .devflow/ui/ac.spec.ts（helper：.devflow/ui/ac.ts）。這裡的範例：
#   ac3_s1_given() { seed 2; [[ $(count) == 2 ]]; }   # Given 以前置狀態的斷言結束
#   ac3_s1_when()  { docker run --rm "$DEVFLOW_IMAGE" export --csv >"$W/out.csv"; }
#   ac3_s1_then()  { [[ $(head -1 "$W/out.csv") == id,amount ]]; }
#   scenario t_ab12/AC-3.S1 "匯出 CSV" ac3_s1 --given "資料庫有 2 筆訂單" --when "執行 export --csv" --then "第一列是 id,amount"
# --given / --when / --then 的文字要與 EPIC 的 Given / When / Then 完全相同。
# --smoke 只給唯讀情境（不 seed、不寫入）：部署後會對正式服務跑。

# devflow: end of scenarios（以下勿改）

# ui / api 情境：有 .devflow/ui（devflow contract --ui）就在 Playwright 官方映像內跑。
pw=()
if [[ -f .devflow/ui/package.json ]]; then
  if [[ $health == failed ]]; then
    echo "SKIP Playwright 情境（health 失敗）"
  else
    set +e
    bash .devflow/playwright.sh
    prc=$?
    set -e
    pw=(--playwright-exit "$prc")
  fi
fi

args=(build "--kind=$kind" "--health=$health" "${pw[@]}")
for p in "${problems[@]}"; do args+=("--problem=$p"); done
python3 .devflow/acceptance.py "${args[@]}"
if [[ $kind == base ]]; then
  echo "base run：只產報告，判定交給 acceptance.py classify-base"; exit 0
fi
python3 .devflow/acceptance.py gate
