#!/usr/bin/env bash
# devflow 必過檢查：規則自測、教訓紀錄、規則掃描（CI 的 check job；開發中也可直接跑）。
#   bash .devflow/check.sh                    檢查
#   bash .devflow/check.sh --write-baseline   接入既有 repo 時記錄舊違規（之後只擋新增）
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 .devflow/check.py "$@"
