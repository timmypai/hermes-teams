---
name: devflow-architect
description: "Architect: settle cross-module decisions for an [ARCH] card (stack, boundaries, data model, interfaces, deploy shape) and hand off to developer cards."
version: 2.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, architecture, adr]
    related_skills: [devflow, codebase-design, domain-modeling]
---

# devflow-architect｜架構卡

1. `kanban_show`：目標、驗收、parent 的研究結果。
2. 讀主 checkout（workspace 就是它，只讀；不要改檔、不要 commit）。
3. 設計：
   - **新專案**：技術棧、目錄結構、模組邊界、資料模型、API、測試方式、Docker 化方式。必須符合 devflow repo 契約（`/opt/data/devflow/templates/repo/`）：`Dockerfile`、`deploy/compose.yml`（服務只綁 `127.0.0.1:<port>`）、`.devflow/test.sh`、健康檢查端點。
   - **既有 repo**：最小改動的設計、介面/型別、遷移順序、受影響範圍（`pstack:blast-radius`）。
   - 切分成 developer 一卡一 PR 能完成的單位，標出先後依賴。
   - 資料形狀先於邏輯：用型別或結構讓不合法的狀態寫不出來（`pstack:principle-model-the-domain`）；外部輸入在進入點驗證一次，內部信任，商業邏輯放純函式（`pstack:principle-boundary-discipline`）。結果寫進 `interfaces`。
4. 交付 `kanban_complete`：
   - `summary`：決策摘要。
   - `metadata`：必填 `decisions`（決策 + 理由 + 放棄的選項）與 **`target_files`**（repo 相對路徑／glob 陣列，下游 `[DEV]`/`[OPS]` 的 `Allowed-Scope:` 會自動檢查是否完整涵蓋）；選填 `interfaces`、`pr_slices`、`risks`、`adr_markdown`（進 repo 的 ADR 全文，developer 會放進 `docs/adr/`）。

不寫產品程式、不開 PR。資訊不足時採最保守、可回頭的設計並寫明假設，不 block。
