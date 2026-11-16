#!/usr/bin/env bash
# devflow 測試入口：CI 與 Bot 都跑這支。依專案改成實際指令。
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
  pytest -q
elif [[ -f go.mod ]]; then
  go test ./...
elif [[ -f Cargo.toml ]]; then
  cargo test
else
  echo "devflow: no test command configured; edit .devflow/test.sh" >&2
fi
