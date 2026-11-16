#!/usr/bin/env bash
# 在 Playwright 官方映像內跑 .devflow/ui 的 ui / api 情境（主機不必裝 Node 或瀏覽器）。
# 映像版本 = .devflow/ui/package.json 釘死的 @playwright/test 版本。事件、截圖與 JSON 報告寫到 $DEVFLOW_EVIDENCE
# （容器內 /evidence；事件檔與 verify.sh 的 shell 情境共用 scenario-events.jsonl）。
# DEVFLOW_ONLY（完整 id 或整條 AC）/ DEVFLOW_SMOKE=1 轉成 --grep，篩掉的情境不會出現在報告。
# 有篩選時 0 個符合不算錯（例如只篩 shell 情境）：該跑的情境有沒有跑，由 acceptance.py gate 依 `Covers:` 檢查；
# 沒篩選時 0 個 test 仍是錯。DEVFLOW_TRACE 非空 = 全程 trace。
set -euo pipefail
cd "$(dirname "$0")/.."
E=$(realpath -m "${DEVFLOW_EVIDENCE:-.devflow/evidence}")
mkdir -p "$E/shots" .devflow/ui/node_modules   # node_modules：匿名 volume 的掛載點，主機先建才不是 root 的
touch "$E/scenario-events.jsonl"
v=$(grep -oE '"@playwright/test": *"[0-9]+\.[0-9]+\.[0-9]+"' .devflow/ui/package.json | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)
: "${v:?pin an exact @playwright/test version (x.y.z) in .devflow/ui/package.json}"

# Playwright 以 test 的完整標題路徑（含檔名，空白連接）比對 --grep；標題是 "<id> <說明>[ @smoke]"。
only=${DEVFLOW_ONLY:-}
read -ra entries <<<"${only//,/ }"
ids=''
for x in "${entries[@]}"; do
  if [[ ! $x =~ ^t_[0-9A-Za-z]+/AC-[0-9]+(\.S[0-9]+)?$ ]]; then
    echo "DEVFLOW_ONLY: '$x' 不是 t_xxx/AC-n 或 t_xxx/AC-n.Sm" >&2; exit 2
  fi
  x=${x//./[.]}
  [[ $x == *"[.]S"* ]] || x+='[.]S[0-9]+'
  ids+="${ids:+|}$x"
done
grep=''
if [[ -n $ids ]]; then grep+="(?=.*(^| )($ids)( |\$))"; fi
if [[ ${DEVFLOW_SMOKE:-} == 1 ]]; then grep+='(?=.* @smoke( |$))'; fi
args=()
if [[ -n $grep ]]; then args+=(--grep "^$grep" --pass-with-no-tests); fi
[[ -z ${DEVFLOW_TRACE:-} ]] || args+=(--trace on)
# 容器以 root 跑：結束前把 /evidence 與 npm 可能新建的 package-lock.json 交還主機使用者（不論測試成敗），
# self-hosted runner 下次才清得掉。交還給「掛載點在容器內看到的擁有者」（主機上建的目錄）：rootful Docker
# 下是主機 uid；rootless Docker 下容器 root 已對應主機使用者，看到的是 0。不能傳主機的 $(id -u) 進去，
# rootless 下容器 uid 1008 會對應到 subuid 範圍的另一個主機 uid，runner 反而寫不了自己的證據目錄。
docker run --rm --ipc=host --network "${DEVFLOW_NET:?}" \
  -e BASE_URL="${DEVFLOW_INNER_URL:?}" -e CI=1 -e DEVFLOW_EVENTS=/evidence/scenario-events.jsonl \
  -v "$PWD/.devflow/ui:/ui" -v /ui/node_modules -v "$E:/evidence" -w /ui \
  "mcr.microsoft.com/playwright:v${v}-noble" \
  bash -c 'if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi >/dev/null && npx playwright test "$@"
    rc=$?; owner=$(stat -c %u:%g /evidence); chown -R "$owner" /evidence
    [ ! -f package-lock.json ] || chown "$(stat -c %u:%g /ui)" package-lock.json; exit $rc' \
  playwright "${args[@]}"
