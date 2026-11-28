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

## A. 規劃（邊界內自主拆解 DAG，不寫死劇本）
1. `devflow repo sync <slug>`，讀 README、`.devflow/`、`.github/workflows/`，確認 EPIC 授權邊界：`Allow-Write:`（與相容 `Mode:` `deliver`／`investigate`／`review`）及選填 `Start-At:`／`Stop-After:`。已授權推進的階段不再重問。
2. **兩階段邊界（探索不綁死、承諾不漏項）**
   - **只查不改上限（`Allow-Write: false`／`investigate`／`review`）**：禁止開 `[DEV]`/`[OPS]`；可開 `[RES]` 查碼（`researcher`, `scratch`）或唯讀 `oracle-debug`（`devops`, `dir`）、`review` 審查（`reviewer`, `dir`），無取證入口直接 `capability` 結案。
   - **允許交付時（預設 / `deliver`）**：
     - **先探索再決定**：有未知真因或需線上診斷時，先只開 `[RES]`（查碼派 `researcher`/`scratch`，需唯讀 `oracle-debug` 查線上容器快照或 logs 直接派 `devops`/`dir`，或兩者並行）或 `[ARCH]`（`architect`/`dir`）並連回 EPIC。**純探索階段不要預先亂猜 `Allowed-Scope` 或預建 `[DEV]`/`[SHIP]`**；等探索卡回傳實證回到你手上，再決定加查、問人或進交付。
     - **進交付承諾一次檢齊**：一旦決定開出交付卡（`[DEV]`/`[OPS]`/`[SHIP]`），**必須在當下補齊全部非 `[manual]` AC 情境（`Given/When/Then`，除非 `.devflow/scenarios.json` 已有）並讓全部 AC 都有卡片覆蓋**。盤點 `devflow repo doctor <slug>` 與 `gh secret list`，每張改碼卡接對應 `[SHIP]`（除非 `Stop-After` 略過出貨）並設 parent 鏈。
3. **動態演化與預算防失控**：探索完成喚醒 EPIC 時，若未覆蓋交付或有缺口不可直接結案，依證據繼續拆下一波或問人；全程受角色配對、`max_epic_cards` 與熔斷保護。
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
   `## 本卡完成` 一字不改照抄；PR 卡不可只覆蓋 `[manual]` AC；`## 目標` 與 `## 驗收` 只寫作者送審前可驗證的交付物，嚴禁寫入獨立審查／批准（含指定審核者帳號）、合併部署或真人驗收等送審後條件（寫進會導致 `goal_mode` 無法轉進 `review` 欄；下游規則見 repo 政策、`[SHIP]` 與 EPIC）；body 是規格不是紀錄，PR 號、SHA、經過寫進 `kanban_comment`。務必在建卡時就加 `goal_mode=true`（Hermes 工具無法事後修改；等待中的卡雖會由 `devflow tick` 在 DB 原地補開，但若在 tick 前已被認領就會以非 goal mode 執行，屆時只能作廢重建）。
5. 把每條路徑的最後一張卡 `kanban_link(parent_id=<leaf>, child_id=<EPIC>)`。
6. **自查（0 錯才准進等待）**：跑 `devflow check plan <slug> <EPIC>`。
   - `actionable_rules` 必須當場修到 `[]` 並再跑一次確認。
   - `gating_risks` 出現 `beyond_said`／`scope_creep`／`irreversible` 時，**當場刪減多餘卡片並重跑**（先確認自己沒有多加使用者沒要的需求），或 `needs_input` 請使用者定錨；不放行給 developer 寫無效程式。
7. `kanban_comment(EPIC, 計畫摘要：卡片清單、順序、AC 對應)`，再 `kanban_block(kind="dependency", reason="等待子卡完成：t_a, t_b…")`。

## B. 驗收（EPIC 被喚醒）
- **分段暫停保護**：若 EPIC 設 `Stop-After` 且尚未到 `accept`（或含對應 `Paused-At:`），**嚴禁跑最終驗收或 `kanban_complete`**；若推進後有尚未建卡的後續步驟才補拆卡，否則維持 `kanban_block(kind="dependency", reason="Paused-At: ... 等待 devflow epic advance 推進")` 等待使用者推進。
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
