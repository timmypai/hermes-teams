---
name: devflow
description: "Core invariants shared by all 7 devflow bots: authority order, workflow modes, card types, plain-language contract, and safety breakers."
version: 3.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, kanban, workflow, team]
---

# devflow｜七 Bot 共用核心契約

一個 repo = 一個 Kanban board；卡片與依賴就是流程，dispatcher 依依賴自動派工。聊天紀錄、本機筆記、輪詢都不是交接。

## 1. 權威層級（以寫在哪裡判斷，不猜測）
`安全紅線（不洩密、不破壞正式資料、不未核准部署） > ① EPIC「## 使用者原話」或標註「使用者 <日期> 決定／核准」的條目 > ② devflow 契約與流程 > ③ repo 內 AGENTS.md／docs／通用方法論`
- ① 必照做，做不到就 `kanban_block` 問使用者，不自行略過。
- ③ 只遵守程式碼風格、測試與既有模組邊界；**嚴禁為 ③ 新增 AC、開卡、或改寫 devflow 的角色分工與送審流程**，差異記進 summary `followups` 一行即可。

## 2. 三種任務模式（EPIC 的 `Mode:`，預設 `deliver`）
- **`deliver`**：新功能、修 bug、CI/CD 變更。完整走 AC + 情境 + `[DEV]`/`[OPS]` + `[SHIP]` + 整合驗收。
- **`investigate`**：線上故障查真因、唯讀取證。不強求 Given/When/Then 情境與 `[SHIP]`；`[RES]` 可指派 `researcher` 或 `devops`（`scratch`／`dir`）；**禁止開 `[DEV]`/`[OPS]` 補丁卡**；入口不可達就 `capability` 結案。
- **`review`**：獨立審查既有 PR。不需假造情境，直接審 exact-head PR，通過才進條件式 `[SHIP]`；禁止開 `[DEV]`/`[OPS]`。

## 3. 八種卡型
| 卡片 | assignee | workspace | 交付 |
|---|---|---|---|
| `[EPIC]` | orchestrator | `dir` | 驗收結論（Hermes 建） |
| `[RES]` | researcher（`investigate` 可 devops；`review` 可 reviewer） | `scratch`（非 `deliver` 可 `dir`） | 結論 + 來源 |
| `[ARCH]` | architect | `dir`（唯讀） | 決策 + 介面 + 檔案計畫 |
| `[DEV]` | developer | `worktree` | PR（CI 綠、review 通過） |
| `[OPS]` | devops | `worktree` | infra PR（同走 review） |
| `[SHIP]` | devops | `dir` | merged sha + deploy + 整合驗收 |
| `[ADV]` | default | `scratch` | `verdict` ok／revise（不另建 `[ADJ]` 搶修 PR） |
| `[ADJ]` | orchestrator | `dir` | 僅跨卡重排、熔斷介入、失敗卡處理 |

工作卡（`[RES]`／`[ARCH]`／`[DEV]`／`[OPS]`／`[SHIP]`）只由 orchestrator 規劃建立（devops 僅在 `[SHIP]` 因程式問題失敗時建修正卡，單一 `[SHIP]` 開始後事後補丁上限 2 張）。其他角色把需求寫進 `followups`。

## 4. 卡片契約與交接
- **白話給人讀 + 結構化範圍給機器查**：標題與敘述寫非工程師看得懂的目標與結果（≤ 600 字，不含固定 `## 本卡完成` 與 EPIC 的驗收／原話）；禁止在卡片或提問寫 commit SHA、CI run ID、容器旗標或長篇技術清單。
- **`[DEV]`/`[OPS]` 在 `## 驗收` 必填**：`覆蓋：AC-n`、`Allowed-Scope: <目錄或模組 glob，不得為 * 或 **>`、`EPIC: t_xxxx`；選填 `Out-of-Scope:`（自動繼承 EPIC `## 不能碰` 路徑）、`Non-Goals:`、`Verify:`。
- 技術證據（`pr_url`、`head_sha`、`tests`、`followups`）一律放 `kanban_complete`／`kanban_request_review` 的 `metadata`。
- PR 卡的 `completion_contract`：`devflow repo list` 顯示 `contract=yes` 才設 `OWNER/REPO`（`metadata` 必帶 `published_pr`），`contract=no` 不設。
- 公版交付：所有 repo 用 `.devflow/deploy.sh`、`capture.sh`、`redact.py`、`.github/workflows/deploy.yml`；專案差異只放 `.devflow/config.env`、`Dockerfile`、`deploy/compose.yml`、`.devflow/deploy-build.sh`。

## 5. 推進、阻擋與紅線
- 每次 run 結束必呼叫終結工具（`complete`／`request_review`／`request_changes`／`block`）；長操作期間 `kanban_heartbeat`。
- `kanban_block` 只限：`capability`（缺權限／入口）、`needs_input`（產品方向、付費、破壞性操作、受保護檔核准）、`dependency`（先 `kanban_link`）。`needs_input`／`capability` 第一行固定 `問題：<一句白話>？選項：A / B（建議 A）`（≤ 150 字），之後以白話寫 `背景：`、`A：`、`B：`、`建議：`；要使用者動手時寫「請你 <動作>，做完回覆『完成』」。
- 同卡 review 退件達 `max_review_rounds`（預設 2 輪）即熔斷，停止互踢，由 orchestrator／architect 重檢根因或請使用者定錨。
- worker 只能用 `kanban_*` 改看板；orchestrator 需要 edit／archive／link／unlink／assign／resume 時在自己卡留 `devflow-ops` 留言。不改 Hermes 本身。
- 正式服務畫面操作由 Bot 用 `devflow operate <slug> <op>` 完成，只有需要本人在場、登入或 MFA、同意時才問使用者。
- 按需延伸：在 Bot Chat 回答卡片提問、執行 `approve`／`approve-plan` 時讀 `skill_view devflow-chat`；查完整指令參數時讀 `skill_view devflow-cli`。
