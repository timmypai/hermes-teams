---
name: devflow-orchestrate
description: "Orchestrator: plan an [EPIC] into linked devflow cards, wait via dependency block, then verify and report; also handle [ADJ] cards."
version: 2.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, orchestrator, kanban, planning]
    related_skills: [devflow, to-tickets]
---

# devflow-orchestrate｜拆解、串接、驗收

先 `kanban_show` 自己的卡。判斷階段：
- `[EPIC]` 且還沒有 parent 卡 → **A. 規劃**
- `[EPIC]` 且 parent 卡都完成（你被喚醒）→ **B. 驗收**
- `[ADJ]` → **C. 調整**

## 看板操作（worker 限制）
你在 dispatcher 派的 worker 裡跑，Hermes 只允許用 `kanban_*` 工具改看板：可以 create、link、comment、block、complete；`hermes kanban ...` CLI 的寫入（edit、archive、unlink、assign、unblock）會被擋，不要試著繞過。需要這些操作時，在**自己的卡**（EPIC 或 ADJ）留一則含 `devflow-ops` 區塊的 `kanban_comment`：
````
```devflow-ops
[{"op": "edit", "task": "t_x", "body": "..."},
 {"op": "unlink", "parent": "t_old", "child": "t_epic"},
 {"op": "link", "parent": "t_new", "child": "t_child"},
 {"op": "archive", "task": "t_old"},
 {"op": "assign", "task": "t_x", "to": "developer"},
 {"op": "resume", "task": "t_x", "note": "修正了…，請重跑"}]
```
````
dispatcher 下一個 tick（約 1 分鐘）由 `devflow tick` 依序執行，結果回留言在同一張卡。`edit` 可給 `title`、`body`；`resume` 用在 blocked / 卡住的卡（會解除 PR 卡的 24h 重派保護、重設失敗計數）。發完就照流程繼續（block 或 complete），不用等結果。


## A. 規劃
1. `terminal: /opt/data/devflow/bin/devflow repo sync <slug>`，讀 README、目錄結構、`.devflow/`、`.github/workflows/`。EPIC 的 `## 驗收` 沒有 `AC-1`、`AC-2`… 編號時，先把每條改寫成可觀察的結果並編號，用 `devflow-ops` 的 `edit` 改 EPIC body（原文保留在「使用者原話」）；子卡照新編號寫。
2. 判斷 repo 狀態，決定第一段：
   | 狀態 | 第一段卡片 |
   |---|---|
   | 空 repo（新專案） | `[ARCH]` → `[DEV] 建立骨架與 devflow 契約` → `[SHIP]` |
   | 既有 repo、沒有 `.devflow/` 或 workflow，且需要部署 | `[OPS] 接入 devflow CI/CD` → `[SHIP]` |
   | 已接入 | 直接進功能卡 |
   devflow repo 契約 = `/opt/data/devflow/templates/repo/`（`.devflow/test.sh`、`verify.sh`、`up.sh`、`playwright.sh`、`deploy.sh`、`capture.sh`、`redact.py`、`config.env`、`deploy/compose.yml`、`.github/workflows/ci.yml`、`deploy.yml`、`repro.yml`）。已接入但缺 `verify.sh` 或 `ci.yml` 沒有 `verify` job（`devflow contract <slug> <主 checkout>` 會列出差異）→ 第一段加 `[OPS] 升級 devflow 契約（verify）`；缺 `capture.sh` / `repro.yml` 且這個 EPIC 是除錯或維護 → 同樣先加 `[OPS] 升級 devflow 契約（診斷）`。
3. 先寫吞吐檢查（`pstack` feature playbook）：哪些步驟必須先做、哪些可並行、哪裡會同時寫同一份檔案（先拆開，真的要共用才串行）、最小可行的切法。然後切卡（`pstack:principle-sequence-verifiable-units`）：
   - `[RES]` 只在有實際未知時建；`[ARCH]` 只在有跨模組或難回頭決策時建。
   - `[DEV]` 一卡一 PR，能獨立合併；每張 `[DEV]`/`[OPS]` 後面接一張 `[SHIP]`（parent = 該卡）。
   - 依賴前一個 PR 程式碼的 `[DEV]`，parent 要指向前一張 `[SHIP]`（合併後 worktree 才會基於新 main）。互不相干的 `[DEV]` 並行。
   - 不需要部署的工作（純研究、文件）不用 `[SHIP]`。
   - 每條 AC 至少一張卡負責；卡片 body 寫「覆蓋：AC-1, AC-3」，`## 驗收` 照抄那幾條 AC（可加這張卡自己的技術驗收）。
   - 卡片只寫目標與驗收，不寫實作細節；做法交給 architect / developer。
4. 建卡：
   ```
   kanban_create(title="[DEV] ...", assignee="developer", workspace_kind="worktree",
                 parents=[...], body="## 目標\n...\n## 驗收\n- ...\n## 提示\n- 相關檔案...\n- EPIC: t_xxxx",
                 skills=[選用，例如 "tdd"])
   ```
   workspace：`[RES]`/`[ADV]` scratch、`[ARCH]`/`[SHIP]` dir、`[DEV]`/`[OPS]` worktree（見 `devflow` 表）。`[DEV]`/`[OPS]` 不給 `workspace_path`（只有接續舊卡 worktree 時才給）：建卡回應裡的 `workspace_path` 會先顯示主 checkout，dispatcher claim 時才換成 `.worktrees/<task-id>`，這是正常的，不要自己 `git worktree add`。`[DEV]`/`[OPS]` 在 `devflow repo list` 顯示 `contract=yes` 時加 `completion_contract="OWNER/REPO"`，否則不設（見 `devflow` 推進規則）。
5. 把每條路徑的最後一張卡（通常是 `[SHIP]`）連成 EPIC 的 parent：`kanban_link(parent_id=<leaf>, child_id=<EPIC>)`。
6. 自查：`terminal: devflow check plan <slug> <EPIC>`（唯讀）。`findings` 裡的規則問題（assignee、workspace、contract、缺 `[SHIP]`、AC 沒人負責、沒接到 EPIC）自己修掉再跑一次；`Decisions …` 開頭的是風險判斷，不用自己處理。
7. `kanban_comment(EPIC, 計畫摘要：卡片清單、順序、AC 對應)`，然後 `kanban_block(kind="dependency", reason="等待子卡完成：t_a, t_b…")`。EPIC 等待期間，dispatcher tick 會在計畫有變動時跑 `devflow check plan`：有疑慮就自動建 `[ADV]`（Opus 5.5）並在 EPIC 留言；含不可回復操作時 `[ADV]` 會成為各 `[SHIP]` 的 parent。你不用等它。
8. 想主動請 advisor 看（目標有多種解讀、架構取捨）：`kanban_create(title="[ADV] ...", assignee="default", workspace_kind="scratch", model="claude-opus-5.5", provider="copilot", body="EPIC: <id>\n<要它判斷什麼>")`，不加 parent、不連到 EPIC。

## B. 驗收
1. 讀所有 parent 的 summary/metadata：PR、`deploy_run_id`、部署後 verify 結果、`acceptance`、`followups`。
2. 逐條判定 AC：`terminal: devflow check acceptance <slug> <EPIC> --run <deploy_run_id> [--run <PR CI run>] --json`（會下載 Actions artifact 的證據：journey 輸出、Playwright 截圖、報告，交 Decisions API 逐條判斷）。FAIL 的 AC 自己看證據：真的沒做到或缺證據 → 補卡（缺 journey → `[DEV]` 補 verify journey），連成 EPIC parent，再 `kanban_block(kind="dependency")`；證據其實已足夠（判斷偏嚴）→ 記下是哪份證據、為什麼足夠，當 PASS。
3. 全部 PASS → `kanban_complete`：summary 用繁中寫給使用者（做了什麼、PR 連結、部署結果/網址、每條 AC 的結果與證據、已知限制）；metadata 放 `prs`、`deploy_runs`、`acceptance`（`[{ac, verdict, probability, evidence}]`）、`followups`。

## C. 調整（`[ADJ]` 卡：Advisor 建議或失敗卡處理）
- 讀 body 指到的 EPIC / 卡片：`kanban_show`；失敗原因看卡片 runs 與留言（`terminal: hermes kanban --board <slug> log <id>` 是唯讀，可用）。
- 接受建議：新卡照 A.4 建並 `kanban_link` 到 EPIC；要作廢、改寫、換人或解除依賴的舊卡，用 `devflow-ops`（見「看板操作」）。卡型只有 `[RES]` `[ARCH]` `[DEV]` `[OPS]` `[SHIP]`，review 是 `[DEV]`/`[OPS]` 的 review 欄，不建 review 卡。
- `[ADJ]` 卡不要跟要修的卡 link（不論 parent 或 child），否則互等。
- 執行失敗（gave_up / crash）：找出根因並修掉（設定、卡片 body、換 assignee、拆小）；能用一次真實小測試證明修好就做。
- 根因是 Hermes 原生限制時：照 README「Hermes 原生限制與對應」處理；沒有對應就 `kanban_complete` 寫明限制與建議，不建修 Hermes 程式或 image 的卡。
- 卡片本身卡死（例如在不能設 required checks 的 repo 上設了 PR `completion_contract`，完成被 Hermes gate 拒絕；這類設定建立後不能改）：建替代卡（同 assignee、同 `workspace_kind`，worktree 用 `workspace_path`=舊卡 worktree，不設 contract），body 寫「接續 t_old：已完成什麼、剩什麼」；用 `devflow-ops`：對舊卡每個 child `link <新卡> <child>`、`unlink <舊卡> <child>`，最後 `archive <舊卡>`。
- 要重跑的卡：`devflow-ops` 的 `resume`。不要為此 `kanban_block`。
- 同一類根因已在之前的 `[ADJ]` 出現過（看 EPIC 留言與舊 `[ADJ]` 的 `root_cause`）：把修正放進結構，不只改這張卡（`pstack:principle-encode-lessons-in-structure`）。repo 層面 → 建 `[DEV]`/`[OPS]` 改 `.devflow/` 或 CI；devflow 本身（skill、範本、`devflow` CLI）→ 寫進 `followups`，由使用者決定是否改 kit。
- 不接受：summary 寫理由。最後 `kanban_complete(summary=..., metadata={"root_cause": ..., "fix": ...})`。

## 原則
- 不輪詢、不 sleep 等待；一律用 parent + dependency block。
- 不設次數或時間上限；同因失敗兩次就換策略（`pstack:principle-attack-the-premise`）。
- 只有產品方向、付費、破壞性資料操作才 `kanban_block(kind="needs_input")`；Hermes 會問使用者。
