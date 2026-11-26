---
name: devflow-orchestrate
description: "Orchestrator: plan an [EPIC] by Mode (deliver/investigate/review), run check plan before waiting, verify acceptance, and handle [ADJ]."
version: 3.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, orchestrator, kanban, planning]
    related_skills: [devflow, to-tickets]
---

# devflow-orchestrate｜拆解、串接、驗收、調整

先 `kanban_show` 自己的卡：EPIC 無 parent → **A. 規劃**；EPIC 的 parent 全完成 → **B. 驗收**；`[ADJ]` → **C. 調整**。

## 看板操作（`devflow-ops`）
worker 內只能用 `kanban_*`。需要 edit／link／unlink／archive／assign／resume 時，在自己的卡留一則含 ` ```devflow-ops ` JSON 陣列的 `kanban_comment`，由 tick 依序執行（第一個失敗即停；先 `link` 新卡再 `unlink`／`archive` 舊卡；`edit` 改 `[DEV]`／`[OPS]` 會就地跑卡片規格檢查，不合規當場拒收不寫入）：
```devflow-ops
[{"op": "edit", "task": "t_x", "body": "..."}, {"op": "link", "parent": "t_new", "child": "t_c"},
 {"op": "unlink", "parent": "t_old", "child": "t_c"}, {"op": "archive", "task": "t_old"},
 {"op": "assign", "task": "t_x", "to": "developer"}, {"op": "resume", "task": "t_x", "note": "..."}]
```

## A. 規劃
1. `devflow repo sync <slug>`，讀 README、`.devflow/`、`.github/workflows/`，確認 EPIC 的 `Mode:`（沒寫就是 `deliver`）與選填 `Pipeline:`／`Start-At:`／`Stop-After:`（例如只想寫碼到 CI 為止可設 `Stop-After: ci`，合法略過 `[SHIP]`；之後用 `devflow epic advance` 接續）。一旦 EPIC 已授權進入該階段，直接規劃並推進，不重問使用者是否只做本機修補。
2. **依模式規劃**
   - **`investigate`（線上查真因）**：不寫 Given/When/Then 情境、不建 `[SHIP]`、**禁止開 `[DEV]`／`[OPS]`**。先確認取證入口是否可達（repo 有無唯讀診斷腳本或 workflow）；有就建 `[RES]` 指派具該入口權限的 `devops`（`dir`）或 `researcher`（`scratch`）；完全沒有入口就直接 `kanban_block(kind="capability")` 說明缺哪個入口，不空轉。
   - **`review`（獨立審查既有 PR）**：不假造情境；安排審查與條件式 `[SHIP]`；禁止開 `[DEV]`／`[OPS]`。
   - **`deliver`（交付）**：
     - 每條 AC 要有編號與類型；非 `[manual]` AC 要有 `<EPIC>/AC-n.Sm` 的 Given/When/Then 情境（只讀不寫的標 `[smoke]`）。**除非 repo 的 `.devflow/scenarios.json` 已經有本 EPIC 的情境，否則規劃當下就把情境寫進 EPIC**。改寫只能更精確地說使用者要的結果，嚴禁加稽核、白名單、前後快照、自訂審查等使用者沒要的門檻；「不要動其他服務」就是一條可觀察結果，不是一套隔離系統。
     - 終點反推一次盤點：`devflow repo doctor <slug>`（`contract-files`／`port` 有缺口就排前置 `[OPS]` 與 `[SHIP]`；留意 `agents_md_warnings`）與 `gh secret list -R <OWNER/REPO>`（需要 UI 操作腳本就在初始 `[DEV]` 一併規劃，不留給 `[SHIP]` 事後補）。
3. **切卡**：`[RES]` 只在有實際未知時建；`[ARCH]` 只在有跨模組或難回頭決策時建；每張 `[DEV]`／`[OPS]` 一個 PR、後面接一張 `[SHIP]`（`deliver` 且要部署）；依賴前一個 PR 程式碼的卡，parent 指向前一張 `[SHIP]`；互不相干的卡並行。每條 AC 至少一張卡負責。
4. **`[DEV]`／`[OPS]` 建卡規格**（建卡前可跑 `devflow check card --board <slug> --title "..." --assignee ... --workspace ... [--parent <ARCH_ID>] --body "..."`）：必設 `goal_mode=true`、`workspace_kind="worktree"`、`contract=yes` 時設 `completion_contract="OWNER/REPO"`，body ≤ 600 字；若上游有已完成 `[ARCH]`，`Allowed-Scope:` 必須完整涵蓋 `[ARCH]` metadata 的 `target_files`：
   ```
   ## 目標
   <1-2 句白話：達成什麼使用者看得到的結果>
   ## 本卡完成
   完成條件：PR 已開、exact-head CI 綠。審查在本卡自己的 review 欄進行，不是 [SHIP] 的事；合併與部署由 [SHIP] 負責。
   ## 驗收
   覆蓋：AC-1, AC-3
   Allowed-Scope: <具體目錄或模組 glob，必填，不得填 * 或 **>
   Out-of-Scope: <禁止碰的路徑 glob，EPIC ## 不能碰 會自動繼承>
   Non-Goals: <1-2 項本次明確不做的鄰近事>
   Verify: <針對性快測指令，選填>
   EPIC: t_xxxx
   ```
   `## 本卡完成` 一字不改照抄；PR 卡不可只覆蓋 `[manual]` AC；body 是規格不是紀錄，PR 號、SHA、經過寫進 `kanban_comment`。`goal_mode` 建卡後改不了，漏加只能作廢重建。
5. 把每條路徑的最後一張卡 `kanban_link(parent_id=<leaf>, child_id=<EPIC>)`。
6. **自查（0 錯才准進等待）**：跑 `devflow check plan <slug> <EPIC>`。
   - `actionable_rules` 必須當場修到 `[]` 並再跑一次確認。
   - `gating_risks` 出現 `beyond_said`／`scope_creep`／`irreversible` 時，**當場刪減多餘卡片並重跑**（先確認自己沒有多加使用者沒要的需求），或 `needs_input` 請使用者定錨；不放行給 developer 寫無效程式。
7. `kanban_comment(EPIC, 計畫摘要：卡片清單、順序、AC 對應)`，再 `kanban_block(kind="dependency", reason="等待子卡完成：t_a, t_b…")`。

## B. 驗收（EPIC 被喚醒）
- **`investigate`**：核對子卡真實證據與限制；足夠就 `kanban_complete`，不足就誠實回報「未通過」或 `capability`，不拿舊場次或推測替代。
- **`review`**：核對獨立審查的 exact-head GitHub review／merge 讀回證據（含 `user/login`、完整 head SHA、review 連結與實際結論，不採信未讀回的自稱），再據實 `kanban_complete` 或回報未通過。
- **`deliver`**：一律跑 `devflow check acceptance`（不可跳過）：
  1. 收集本 EPIC 所有已合併 PR 號，跑 `devflow check acceptance <slug> <EPIC> --pr <N> [--pr <M> ...] --json`。
  2. 結束碼 `0` → `kanban_complete`（白話 summary；`metadata` 放 `prs`、`deploy_runs`、`integration_run`、`acceptance`、`followups`）。
  3. 結束碼 `3`（只剩 `UNJUDGED`）→ 不建補救卡，`kanban_block(kind="needs_input", reason="Decisions API 無法使用，AC-x 未判定；恢復後 devflow resume 重跑驗收")`。
  4. 結束碼 `1`（FAIL）→ **確定性檢查的 FAIL 不可自行改判 PASS**。缺整合或部署 run → 建 `[SHIP] 整合驗收 <EPIC>`（devops、`dir`，body 寫 `EPIC: <id>`）；情境失敗、缺情境或步驟文字不符 → 補 `[DEV]` 卡（**必帶 `goal_mode=true`、固定 `## 本卡完成`、`Allowed-Scope:`、`EPIC:`**）後接 `[SHIP]`，連成 EPIC 的 parent，再 `kanban_block(kind="dependency")`。

## C. 調整（`[ADJ]` 卡）
- 讀 body 指到的卡與 EPIC（`kanban_show`；`hermes kanban --board <slug> log <id>` 唯讀可用）；`[ADJ]` 絕不與要修的卡 link（parent 或 child 都不行）。
- 改卡、換人、封存、移出 `triage`、重跑一律走 `devflow-ops`。要建接續卡或替代卡時，**必帶 `goal_mode=true`、固定 `## 本卡完成`、`Allowed-Scope:`、`EPIC:`**，先 `link` 新卡、再 `unlink`／`archive` 舊卡；不要為此 `kanban_block`。
- 根因是 Hermes 原生限制時，在 summary 寫明限制與建議，不建修 Hermes 的卡。同類根因重複出現就把修正放進結構：repo 層 → `[DEV]`／`[OPS]` 改 `.devflow/` 或 CI；devflow 本身 → 寫進 `followups`。
- **熔斷停損**：同一張卡已有 ≥ 3 張 `[ADJ]`、review 退件已達上限、或補診斷一輪後仍不知根因時，**不要再開下一輪**；直接在本 `[ADJ]` `kanban_block(kind="needs_input")`，依 `devflow` 的問題格式給使用者 A／B 選項與代價。
- 完成時 `kanban_complete(summary=..., metadata={"root_cause": "<精簡代碼>", "fix": "..."})`。

## 原則
不輪詢、不 sleep，一律用 parent + dependency block；可逆的事自己決定；只有產品方向、付費、破壞性資料操作或達熔斷上限才 `needs_input`；同因失敗兩次就換策略。
