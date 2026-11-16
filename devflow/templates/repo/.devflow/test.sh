#!/usr/bin/env bash
# devflow 測試入口：CI 與 Bot 都跑這支。依專案改成實際指令。
# CI 的 lesson gate（check.py --pr）證明 test 類教訓時會設：
#   DEVFLOW_TEST_ONLY  只跑這個測試檔
#   DEVFLOW_JUNIT      把逐條測試結果寫成 JUnit XML 到這個路徑（沒寫 = 無法證明）
# pytest 已支援；其他技術棧要支援時照同樣方式加（例如 gotestsum --junitfile、jest-junit）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f package.json ]]; then
  if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
  npm test --if-present
elif [[ -f pyproject.toml || -f requirements.txt ]]; then
  python3 -m venv .venv-test
  . .venv-test/bin/activate
  pip install -q --upgrade pip
  if [[ -f requirements.txt ]]; then pip install -q -r requirements.txt; fi
  if [[ -f pyproject.toml ]]; then pip install -q -e '.[test]' 2>/dev/null || pip install -q -e .; fi
  pip install -q pytest
  args=(-q)
  if [[ -n ${DEVFLOW_TEST_ONLY:-} ]]; then args+=("$DEVFLOW_TEST_ONLY"); fi
  if [[ -n ${DEVFLOW_JUNIT:-} ]]; then args+=("--junitxml=$DEVFLOW_JUNIT"); fi
  pytest "${args[@]}"
elif [[ -f go.mod ]]; then
  go test ./...
elif [[ -f Cargo.toml ]]; then
  cargo test
else
  echo "devflow: no test command configured; edit .devflow/test.sh" >&2
fi
