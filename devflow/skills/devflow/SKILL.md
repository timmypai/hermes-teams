---
name: devflow
description: "Use for any devflow Kanban card: roles, card types, workspaces, handoff and blocking rules shared by all 7 bots."
version: 2.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, kanban, workflow, team]
---

# devflow｜七 Bot 共用開發流程

一個 repo = 一個 Kanban board。卡片與它的依賴就是流程；dispatcher 依依賴自動派工。聊天紀錄、檔案交接、輪詢都不是流程的一部分。

## 流程

```
使用者 ⇄ Hermes（語音/文字）：目標 + 編號驗收 AC-1..n（語音確認一次）
   └─ [EPIC] → orchestrator：sync repo、拆卡（每張標「覆蓋：AC-n」）、最後的卡設為 EPIC 的 parent、block(dependency)
        │     └─ devflow check plan（dispatcher tick 自動）：規則 + Decisions API 風險 → 有疑慮自動建 [ADV]
        ├─ [RES]  researcher   （有未知才建，可並行）
        ├─ [ARCH] architect    （有跨模組/難回頭決策才建）
        ├─ [DEV]  developer    → 實作 + 每條 AC 一個 verify journey → PR + CI（test + verify + 證據 artifact）綠
        │     → request_review → reviewer（獨立探測 + 看證據）→ 通過才 done
        │     └─ [SHIP] devops → merge → deploy workflow → health + 部署後 verify（失敗回滾）
        ├─ [ADV]  hermes(default) advisor：判斷疑慮；只有「含不可回復操作」時擋 [SHIP]
        └─ 全部完成 → EPIC 回到 orchestrator → devflow check acceptance（AC × 證據逐條判定）
              → 全 PASS 才 complete → Hermes 收到通知回報使用者
```

## Board 與工作區

| 項目 | 值 |
|---|---|
| board slug | repo 名稱（小寫 kebab），`devflow repo list` 可查 |
| 主 checkout | `/opt/data/kanban/boards/<slug>/repo`（board default_workdir，保持在 main） |
| 開發 worktree | `<repo>/.worktrees/<task-id>`，分支 `wt/<task-id>`（dispatcher 自動建立） |

| 卡片 | assignee | workspace_kind | 交付 |
|---|---|---|---|
| `[EPIC]` | orchestrator | `dir` | 驗收結論 |
| `[RES]` | researcher | `scratch` | 結論 + 來源 |
| `[ARCH]` | architect | `dir`（只讀） | 決策 + 介面 + 檔案計畫 |
| `[DEV]` | developer | `worktree` | PR（CI 綠、review 通過） |
| `[OPS]` | devops | `worktree` | infra PR（workflow/Docker），同樣走 review |
| `[SHIP]` | devops | `dir` | merged sha + deploy run + health |
| `[ADV]` | default（Opus 5.5） | `scratch` | 目標對齊意見 + `verdict` ok/revise |
| `[ADJ]` | orchestrator | `dir` | 依 advisor 建議或失敗卡調整計畫 |

## 交接規則
- 卡片 body = 規格：**目標 / 驗收 / 提示（相關檔案、限制）**。三段就夠。EPIC 的驗收是編號 `AC-n`（可觀察的結果）；子卡在驗收寫「覆蓋：AC-n」。
- 證據：每條 AC 由 repo 的 `.devflow/verify.sh` journey（UI 用 Playwright 截圖）在 PR CI 與部署後各跑一次，證據存 GitHub Actions artifact；卡片 metadata 只放 run id / URL，不貼長 log。
- 交付寫在 `kanban_complete`（或 `kanban_request_review`）：`summary` 1–3 句；`metadata` 放結構化事實（`pr_url`、`branch`、`head_sha`、`tests`、`decisions`、`findings`…）。下游卡自動在 context 看到 parent 的 summary/metadata。
- 不寫額外交接文件；需要給人看的長內容用 `artifacts` 附檔。
- 外部 skill 說的 "issue tracker / tickets / issues" 一律指這個 board 的 Kanban 卡；"PR" 指 GitHub PR。

## 推進規則（不設卡控）
- 可逆的事自己決定並記在 summary，不停下來問。
- review 退件用 `kanban_request_changes`，不是 block；沒有次數上限。同一原因再失敗時換做法（`pstack:principle-attack-the-premise`），不要原樣重試。
- 只有三種情況才 `kanban_block`：缺憑證/權限（`capability`）、產品方向或付費/破壞性資料操作需要使用者決定（`needs_input`）、在等其他卡（`dependency`，需先 `kanban_link` 該卡為 parent）。block 時 reason 寫成使用者能直接回答的一句話。
- 卡片範圍外的發現：寫進 summary/metadata `followups`，由 orchestrator 決定是否建卡。
- 長時間操作（CI、部署）期間呼叫 `kanban_heartbeat`。
- 每次 run 結束前一定要呼叫一個終結工具（complete / request_review / request_changes / block）。
- PR 卡（`[DEV]`/`[OPS]`）的 `completion_contract` 看 `devflow repo list`：`contract=yes` → 建卡設 `completion_contract="OWNER/REPO"`，Hermes 會等 PR exact head 的 required check `devflow-ci` 全綠才讓卡完成；`contract=no`（還沒有 `devflow-ci`，或 GitHub Free 的 private repo 不能設 required checks）→ 不設，CI 由 reviewer 與 devops 用 `gh` 核對。其他卡都不設。
- 有 contract 的卡，`kanban_request_review` / `kanban_complete` 的 metadata 一定帶 `published_pr`（PR URL）。
- worker 只能用 `kanban_*` 工具改看板（Hermes 原生限制）：`hermes kanban` CLI 的寫入（edit、archive、unlink、assign、unblock）在 worker 內一律被擋，不要繞過。orchestrator 需要時在自己的卡留 `devflow-ops` 留言（見 `devflow-orchestrate`），由 dispatcher tick 執行；其他 Bot 需要時寫進 summary/`followups` 交 orchestrator。
- 不改 Hermes 本身（`/opt/hermes`、容器 image、Hermes 原始碼）。碰到 Hermes 原生限制，用 devflow 已有的對應（見 `/opt/data/devflow/README.md`「Hermes 原生限制與對應」）；沒有對應就在 summary/`followups` 寫清楚，交 Hermes 回報使用者，不建修 Hermes 的卡。

## 身分與憑證
- GitHub：reviewer 用自己的帳號；其他 Bot 共用 builder 帳號（`GH_CONFIG_DIR` 已設定）。`git push` 走 `gh auth git-credential`。
- 不輸出 token、`.env`、`auth.json`、cookie、私鑰。應用程式秘密放 GitHub Secrets。
- 部署只透過 GitHub Actions（merge 到 main 觸發 deploy workflow 跑在 Oracle VM self-hosted runner）。

## devflow CLI（`/opt/data/devflow/bin/devflow`）
| 指令 | 用途 |
|---|---|
| `devflow repo list` | 已接入的 repo / board |
| `devflow repo add OWNER/REPO [--create] [--public] [--no-runner]` | 建 board + clone + 設定 builder/reviewer 權限 + 分配埠 + 申請 runner（`--create` 先建 GitHub repo） |
| `devflow repo sync SLUG` | 主 checkout fetch + 快轉到 origin 預設分支 |
| `devflow repo path SLUG` | 印出主 checkout 路徑 |
| `devflow contract SLUG [DIR] [--ui]` | 把 devflow repo 契約檔（CI/CD、deploy、test、verify；`--ui` 加 Playwright）補進 DIR，不覆蓋既有檔，列出與範本不同的檔 |
| `devflow check plan SLUG EPIC [--advise]` | 檢查計畫（唯讀）：卡型/assignee/workspace/contract/SHIP/parent 鏈/AC 覆蓋 + Decisions API 風險；`--advise` 有新疑慮時建 `[ADV]`（dispatcher tick 會自動跑） |
| `devflow tick SLUG` | 看板自動化（由 `on_kanban_dispatch_tick` hook 觸發）：執行 orchestrator 的 `devflow-ops`、計畫變動時跑 check plan |
| `devflow check acceptance SLUG EPIC --run ID... [--json]` | 下載 run 的證據 artifact，以 Decisions API 逐條判定 AC PASS/FAIL |
| `devflow runner request OWNER/REPO` | 為 repo 註冊 Oracle VM self-hosted runner（需 repo admin） |
| `devflow runner status OWNER/REPO` | 查 runner 狀態 |
| `devflow resume SLUG TASK [--note TEXT]` | 只限 Hermes 聊天：重跑 blocked/卡住的卡（解除已開 PR 卡的 24h 重派保護、重設失敗計數） |
| `devflow repo protect SLUG` | default branch 有 `devflow-ci` 時設為 required check，開啟 PR contract（冪等） |

## 外部技能
- `pstack:*` 與 mattpocock 技能是方法論，依角色已分配好；用 `skills_list` / `skill_view` 讀。
- 技能提到 Cursor subagent / Task 時，用 `delegate_task`（若可用）或自己依序完成。不要執行 `setup-pstack`、`setup-matt-pocock-skills`。
