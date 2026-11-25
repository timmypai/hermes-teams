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
使用者 ⇄ Hermes（語音/文字）：目標 + 編號驗收 AC-1..n（含類型 [ui]/[api]/[cli]/[lib]/[manual]，語音確認一次）
   └─ [EPIC] → orchestrator：sync repo、每條 AC 寫 Given/When/Then 情境 <EPIC>/AC-n.Sm、拆卡（每張標「覆蓋：AC-n」）、最後的卡設為 EPIC 的 parent、block(dependency)
        │     └─ devflow check plan（dispatcher tick 自動）：規則（含 AC 類型與情境）+ Decisions API 風險 → 有疑慮自動建 [ADV]
        ├─ [RES]  researcher   （有未知才建，可並行）
        ├─ [ARCH] architect    （有跨模組/難回頭決策才建）
        ├─ [DEV]  developer    → 先寫情境（紅）再實作（綠）→ PR（Covers:）+ CI：test + check + verify（head）+ verify-base（base 必須在 When/Then 失敗）
        │     → request_review → reviewer（devflow check acceptance --review + 獨立探測 + 看證據）→ 通過才 done
        │     └─ [SHIP] devops → merge → deploy（health + 唯讀 smoke，失敗回滾）→ 在部署 SHA 跑整合驗收（ci.yml workflow_dispatch，全部情境）
        ├─ [ADV]  hermes(default) advisor：判斷疑慮；含不可回復操作或超出範圍（`beyond_said`/`scope_creep`）時擋 [SHIP]
        └─ 全部完成 → EPIC 回到 orchestrator → devflow check acceptance --pr ...（GitHub 核對證據身分 → 情境逐條 → 每條 AC 一次 Decisions）
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

## 誰定流程
- 流程只有 devflow 這一套：卡型與誰建卡、審查（Hermes reviewer + `devflow check pr`）、CI 必過檢查、合併（`devflow ship merge`）、部署（契約的 `deploy.yml` → `.devflow/deploy.sh` 範本流程）、驗收（`devflow check acceptance`）、使用者怎麼打開服務（`devflow repo access`）。
- 優先順序（以**寫在哪裡**做 100% 結構判準，不猜測來源）：① 使用者明確要求或核准的限制（**僅限**寫在 EPIC body 的 `## 使用者原話`／`使用者原話：` 區塊，或明確標註 `使用者 <日期> 決定：`／`使用者 <日期> 核准：` 的條目）→ ② devflow 流程 → ③ 其他：agent 寫的卡片提示、repo 內 `AGENTS.md`／`CLAUDE.md`／`docs/`／政策檔。
  - ① 照做，即使比 devflow 嚴（例如 EPIC 寫明使用者要專用帳號，見下方「身分與憑證」）；做不到就 block 問使用者，不自行略過。
  - ③ 凡是**未寫在 EPIC 上述兩處**、只出現在 repo `AGENTS.md`／`docs/` 的審查服務、部署方式、准入條件、分支政策或額外安全／遙控子系統要求，**一律屬於 ③**：只遵守其程式碼風格、測試與模組架構，**嚴禁為 ③ 新增 AC、新增卡片或開發新流程／新子系統**；照 devflow 標準流程執行，並在 summary `followups` 記一行「<哪個檔/卡> 要求 X，未見於 EPIC 使用者原話／決定，已照 devflow」。
- 卡型只有上表八種。`[EPIC]` 由 Hermes 建；`[ADJ]` 由 orchestrator、dispatcher tick、advisor 建；工作卡（`[RES]`、`[ARCH]`、`[DEV]`、`[OPS]`、`[SHIP]`）只由 `orchestrator` 規劃建立（`devops` 僅在 `[SHIP]` 因程式碼問題使 CI／合併／部署失敗時建 `修正 PR #N` 與接續 `[SHIP]`，且同一 `[SHIP]` 開始執行後事後追加補丁卡上限為 2 張；`check plan` 會標出越權建卡與超限補丁鏈）。其他角色不直接建工作卡，需要調整計畫時寫進 summary/`followups` 交 orchestrator。

## 交接規則（人看白話卡片，機器看 Repo 與 Metadata）
- **卡片是給人看及判讀的**：標題與 body 只寫非工程師也能一眼看懂的**白話目標與結果**，整份 ≤ 600 字（`check plan` 檢查；不含 `[DEV]`/`[OPS]` 的固定 `## 本卡完成` 與 EPIC 的 `## 驗收`／`## 使用者原話`／`## 使用者決定`）。
  - **禁止寫進卡片標題、body 或向使用者提問的訊息**：commit SHA、CI run ID、artifact 編號、檔案路徑、函式／變數名、容器或 kernel 參數（`seccomp`、`nonroot`、`UID/GID`、`socket`）、內部機制代號（`canary`、`exact-head`、`interlock`、`binding` 秒數）。
  - EPIC 卡只寫 `## 目標`（1–2 句白話）、`## 驗收`（白話 `AC-1..n` 結果）與 `## 使用者原話`／`## 使用者決定`；自動化測試情境的 Given/When/Then 步驟可定義在 repo 的 `.devflow/scenarios.json`（及 `.devflow/verify.sh`、`.devflow/ui/*.spec.ts`），不用把長篇測試步驟塞滿 EPIC 卡片。
  - 子卡（`[DEV]`、`[OPS]`、`[SHIP]`、`[RES]`、`[ARCH]`）只寫 `## 目標`（1–2 句白話）、`## 本卡完成`（僅 `[DEV]`/`[OPS]` 固定句）、`## 驗收`（`覆蓋：AC-n` 加最多 1–2 句白話完成結果與 `EPIC: t_xxxx`），**不設 `## 提示` 技術垃圾桶、不列技術實作清單**。
- 證據：每個情境在 repo 是一個 Given/When/Then 可執行情境（`[ui]`/`[api]` 用 `.devflow/ui` 的 Playwright `scenario`，`[cli]`/`[lib]` 用 `.devflow/verify.sh` 的 `scenario`；步驟定義在 repo `.devflow/scenarios.json` 或 EPIC）。PR CI（head + base 紅綠證明）、部署後（smoke）、整合驗收（部署 SHA 全部情境）各產生 `acceptance.json`，存 GitHub Actions artifact；卡片 metadata 只放 PR 號與 run URL，不貼長 log。證據身分（repo、SHA、workflow、run attempt）由 `devflow check acceptance` 向 GitHub 核對，不信卡片或報告自述。
- 交付寫在 `kanban_complete`（或 `kanban_request_review`）：`summary` 1–2 句白話結論；技術細節與結構化事實（`pr_url`、`branch`、`head_sha`、`tests`、`decisions`、`findings`…）一律放 `metadata`（JSON）。下游卡自動在 context 看到 parent 的 summary/metadata。
- 不寫額外交接文件；需要給人看的長內容用 `artifacts` 附檔。
- 外部 skill 說的 "issue tracker / tickets / issues" 一律指這個 board 的 Kanban 卡；"PR" 指 GitHub PR。

## 推進規則與熔斷上限
- **權威層級**：`安全紅線（不洩密、不破壞正式資料、不未核准部署） > 本次 EPIC 使用者原話／明確決定 > 專案 .devflow 契約與 devflow 流程 > repo 風格／通用方法論`。衝突時不自行腦補，交 `needs_input` 或記進 `followups`。
- 可逆的事自己決定並記在 summary，不停下來問。
- **Review 往復熔斷**：review 退件用 `kanban_request_changes`；同一張卡退件達上限（預設 2 輪，`roles.json` `policy.max_review_rounds`）時觸發熔斷，停止盲目互踢，由 orchestrator／architect 重檢根因或以 `needs_input` 請使用者定錨。同因再失敗必須換做法（`pstack:principle-attack-the-premise`），不要原樣重試。
- **卡片與預算上限**：單一 EPIC 工作卡數上限與 worker 回合／時間預算由 `roles.json` `policy`／`worker_config` 宣告控制，超限自動要求收斂。
- **公版交付強制對齊**：所有 repo 一律使用 devflow 公版部署與採證腳本（`.devflow/deploy.sh`、`capture.sh`、`redact.py`、`.github/workflows/deploy.yml`），禁止自寫黑箱部署包裝器；專案差異只放 `.devflow/config.env`、`Dockerfile`、`deploy/compose.yml` 與選用的 `.devflow/deploy-build.sh`（只負責 build `$DEVFLOW_IMAGE`）。偏離公版時 `repo doctor`、`preflight` 與 `check pr` 會直接確定性擋下，用 `devflow contract <slug> . --sync-standard` 還原。
- **反官僚與行動導向交接**：禁止在留言貼多段免責宣言，禁止提「要不要請另一個 Bot 檢查？」這類元問題。`kanban_block` 嚴格寫 3 段：① 確切失敗步驟與錯誤原因 ② 已查證事實 ③ 唯一具體下一步（需人動手只給一條明確動作）。
- 只有三種情況才 `kanban_block`：缺憑證/權限（`capability`）、產品方向或付費/破壞性資料操作需要使用者決定（`needs_input`）、在等其他卡（`dependency`，需先 `kanban_link` 該卡為 parent）。block 時 reason **必須是給人直接判讀的純白話**（禁止寫 commit SHA、CI run ID、artifact 編號或內部參數代號）。`needs_input` 與 `capability` 的 reason 第一行固定為 `問題：<一句白話問題>？選項：A / B（建議 A）`，150 字內（聊天通知只顯示前 160 字）；`capability` 的問題寫成誰做什麼具體動作（例「要由 Hermes 用主機 ssh 註冊 devflow 共用 runner 嗎？」），不要只寫缺什麼。第二行以後只寫簡潔白話，使用者只看這段就能決定：`背景：` 目前完成到哪、只缺哪一件事（1–2 句白話）；`A：` / `B：` 各選項的白話結果與影響；`建議：` 一句話理由。Hermes 會把它送到聊天問使用者，回覆後以留言帶回並重跑本卡。
- 要使用者**自己動手**的 `capability`（設 GitHub secret、用本人帳號 OAuth、在主機執行指令）：問題第一行寫成 `問題：請你 <動作>，做完回覆「完成」？選項：A 已完成 / B 暫緩（建議 A）`，不要寫成「要不要／同意嗎」：使用者回「同意」只表示願意做，交回卡片後重查仍缺、再 block 同一題會觸發重複阻擋並被轉進 triage。由 Bot 做、只需使用者核准的事（例如「要由 Hermes 註冊 runner 嗎？」）才用「同意 / 先不要」。
- 卡片範圍外的發現：寫進 summary/metadata `followups`，由 orchestrator 決定是否建卡。
- 長時間操作（CI、部署）期間呼叫 `kanban_heartbeat`。
- 每次 run 結束前一定要呼叫一個終結工具（complete / request_review / request_changes / block）。
- PR 卡（`[DEV]`/`[OPS]`）的 `completion_contract` 看 `devflow repo list`：`contract=yes` → 建卡設 `completion_contract="OWNER/REPO"`，Hermes 會等 PR exact head 的 required check `devflow-ci` 全綠才讓卡完成；`contract=no`（還沒有 `devflow-ci`，或 GitHub Free 的 private repo 不能設 required checks）→ 不設，CI 由 reviewer 與 devops 用 `gh` 核對。其他卡都不設。
- 有 contract 的卡，`kanban_request_review` / `kanban_complete` 的 metadata 一定帶 `published_pr`（PR URL）。
- worker 只能用 `kanban_*` 工具改看板（Hermes 原生限制）：`hermes kanban` CLI 的寫入（edit、archive、unlink、assign、unblock）在 worker 內一律被擋，不要繞過。orchestrator 需要時在自己的卡留 `devflow-ops` 留言（見 `devflow-orchestrate`），由 dispatcher tick 執行；其他 Bot 需要時寫進 summary/`followups` 交 orchestrator。
- 不改 Hermes 本身（`/opt/hermes`、容器 image、Hermes 原始碼）。碰到 Hermes 原生限制，用 devflow 已有的對應（見 `/opt/data/devflow/README.md`「Hermes 原生限制與對應」）；沒有對應就在 summary/`followups` 寫清楚，交 Hermes 回報使用者，不建修 Hermes 的卡。
- worker 的 `kanban_block(kind="needs_input")` 只問使用者能決定的事（產品方向、範圍、不可回復操作、付費、他擁有的帳號或權限、受保護檔合併核准）。工具不夠、權限、卡片寫法、交接卡住這類流程問題也照實 block（寫清楚缺什麼、試過什麼），不要寫成「請管理端…」的選擇題：啟用 `auto_flow_adj=true` 時 tick 會自動建 `[ADJ]` 交 orchestrator；預設 `auto_flow_adj=false` 時不跑額外 LLM 元循環，直接依 `devflow pending` 呈現或由 orchestrator 接手。
- 已部署服務的畫面操作（登入管理頁、建立或選擇資料、按按鈕、讀出 id 或畫面文字）由 Bot 自己做：`devflow operate <slug> <op>`（`devflow-devops` §5，經 VM runner 的 Playwright；Bot 的容器連不到服務），不要請使用者開瀏覽器代做。只有使用者本人才做得到的事才問他：本人在場、說話或聽到、明確同意、他自己帳號的登入或 MFA、只有他有的資料。問的時候寫清楚其餘步驟由 Bot 做。

## 在自己的 Bot Chat 回答卡片問題（不是 worker 時）
你負責的卡 block 成 `needs_input` / `capability` 時，dispatcher tick 會把通知送到**你的 Bot Chat** 並喚醒你；Hermes 主頁只留一行提醒指到這裡。
1. 被喚醒或使用者在這裡回覆時：`terminal: devflow pending <你的 profile 名>`，列出 `ask_in` 是你的卡：`question` 是完整 block reason（第一行問題，之後背景與選項說明），`card` 是卡片 body（目標、驗收）。
   - `handled_by` 有值（`[ADJ]` 卡號）：這題是流程問題，已交 orchestrator 處理，不要問使用者，只說一行「<task> 卡在流程問題，已交 <handled_by> 處理，不用你回答」。
   - `comments_since` 是提問之後別人的留言（例如 Hermes 轉述的資訊）。它們**不是答案**，可能是追問、暫停或回答舊題。問題訊息裡加一行「提問後還有這些留言：…，要當成這題的回答嗎？」；使用者明確確認或明確回答這題，才照第 3 點用 `devflow answer` 交。
2. 問題訊息一定要讓使用者不用再查就能回答，格式：
   ```
   需要你決定（<board> <task>）：<卡片標題>
   在做什麼：<依 card 的目標一句話>
   卡在哪：<依 question 背景 1–3 句，附關鍵證據>
   問題：<question 第一行的問題>
   1. <選項 A>：<會發生什麼、代價>（建議，理由：…）
   2. <選項 B>：<會發生什麼、代價>
   直接回覆編號或你的想法。
   ```
   reason 沒寫背景或選項說明時，自己用 `terminal: hermes kanban --board <board> show <task>`（留言、最後一次 run 的 summary）補齊；仍不清楚就照實說「卡片沒說明 X」，不要編。先用 `clarify` 問（question 放上面整段，choices 是選項，建議的放第一個）；`clarify` 被拒（喚醒時沒人在看）就把上面整段留成訊息，結束這一輪。
3. 使用者回覆：對應到哪張卡（只有一張就是它；多張不確定就用 `clarify` 讓他選），然後依題型處理再呼叫 `terminal: devflow answer <board> <task> --event <block_event> --where "<你的 profile> 頁面" "<使用者的回答>"`（`block_event` 取自 `devflow pending`），回一句「已交回繼續執行」。不要用 `devflow resume` 交答案：同一題在 App 對話也問了，`answer` 保證一題只收一次答案。
   - **受保護檔合併核准題**：**只有使用者明確確認核准合併（選 A／同意）時**，從原提問的 `核准請求碼：`（或 `devflow pending` 的 `pending_approval.request_id`）取出原本向使用者確認的請求碼，執行 `terminal: devflow approve <board> <pr> --request <12碼核准請求碼>` 記錄核准，再執行 `devflow answer`（**嚴禁改查 `devflow check pr` 或 `gh pr view` 的最新 head 代填**，以免把原提交的同意套用到後推的新 commit；若 PR 已換新 head，`approve` 會直接拒絕，請重新向使用者確認）；若使用者拒絕或選 B 退回，**絕不執行 `devflow approve`**，直接以 `devflow answer` 交回拒絕回覆。
   - **計畫風險定錨題**：**只有使用者明確選擇核准目前計畫（選 B）時**，先從 `question` 的 `計畫雜湊：` 取得 12 碼雜湊，執行 `terminal: devflow approve-plan <board> <epic> --plan-hash <12碼雜湊>` 記錄明確同意，再執行 `devflow answer`；若使用者選擇調整縮減（選 A）或拒絕，**絕不執行 `devflow approve-plan`**，直接以 `devflow answer` 交回調整指示（調整消除風險前維持暫停出貨）。
   - **要使用者自己動手的題目**（設 secret、OAuth、在主機執行指令）：只有他說做完了、或說暫緩／不做，才 `answer`；只回「同意」「好」時不要交（卡片會重跑、重查仍缺、再 block 同一題而被轉進 triage），回他「做完回覆『完成』，我再交回」並附上步驟，卡片繼續等。
4. `answer` 回 `not applied: ...`（或回覆對不上 `devflow pending` 的任何卡）：不要套到別張卡。照輸出的 status 與 `events since` 照實說明，例如「你的回覆沒有套用：這張卡在你回覆前已經 unblocked（恢復執行）」或「已封存」或「它已改問新的問題（事件 N）」；只有 `hermes kanban --board <board> show <task>` 的留言裡看得到另一則「使用者回覆（<地方>，問題事件 N）」時，才說「已在 <地方> 回答過」。然後列出 `devflow pending` 裡還在等他的題目（沒有就說沒有）。
5. 說「有沒有送到」只看 `devflow pending` 的 `chats[]`，它讀的是那個對話自己的紀錄：`woken` = 通知已進入那個對話，`replied` = 那個 bot 之後有回話。`woken` 為 false 時說「那邊還沒看到這則通知（Hermes 一次叫醒一個 bot，常晚約 1 分鐘）」，不要說「未送達」；超過 10 分鐘仍為 false 才說「那邊沒收到」，並請使用者在這裡直接回答。

## 身分與憑證
- GitHub：reviewer 用自己的帳號；其他 Bot 共用 builder 帳號（`GH_CONFIG_DIR` 已設定）。`git push` 走 `gh auth git-credential`。
- 不輸出 token、`.env`、`auth.json`、cookie、私鑰。應用程式秘密放 GitHub Secrets。
- 部署只透過 GitHub Actions（merge 到 main 觸發 deploy workflow 跑在 Oracle VM self-hosted runner）。部署通道只有這一種，所有 repo 共用；EPIC 沒寫明時不提出專用帳號、獨立 Docker daemon、job 前准入等新門檻（使用者 2026-11-17 接受 README「信任邊界」）。通道不通時照 `devflow repo doctor` 的失敗項目修；修不了就 `capability` 問使用者，不另設計部署方式。

## devflow CLI（`/opt/data/devflow/bin/devflow`）
| 指令 | 用途 |
|---|---|
| `devflow repo list` | 已接入的 repo / board |
| `devflow repo add OWNER/REPO [--create] [--public] [--no-runner]` | 建 board + clone + 設定 builder/reviewer 權限 + 分配埠 + 綁定 Hermes 專案 + 公開 repo 的 fork PR 一律要核准 + 申請 runner 並等 2 分鐘讀回 + 輸出 `deploy_ready`/`deploy`（同 doctor）（`--create` 先建 GitHub repo） |
| `devflow repo bind SLUG` | 只做專案綁定（冪等）：沿用主路徑 = clone 的專案或新建，寫入看板 `project_id` 與專案 `board_slug`；有衝突就停止不改 |
| `devflow repo doctor [SLUG]` | 唯讀檢查專案 ↔ 看板 ↔ repo 綁定與部署就緒（`deploy` 各項 id：`contract-files`、`port`、`ci-required`、`runner-online`、`runner-request`、`fork-approval`，附修法）；有問題時 exit 1 |
| `devflow repo sync SLUG` | 主 checkout fetch + 快轉到 origin 預設分支 |
| `devflow repo path SLUG` | 印出主 checkout 路徑 |
| `devflow repo access SLUG` | 使用者打開已部署服務的唯一方式：SSH tunnel 指令與網址（服務只綁主機 `127.0.0.1:<port>`）；不要為入口問使用者 |
| `devflow contract SLUG [DIR] [--ui]` | 把 devflow repo 契約檔（CI/CD、deploy、test、verify；`--ui` 加 Playwright）補進 DIR，不覆蓋既有檔，列出與範本不同的檔 |
| `devflow check plan SLUG EPIC [--advise]` | 檢查計畫（唯讀）：卡型/assignee/workspace/contract/SHIP/parent 鏈/AC 覆蓋 + Decisions API 風險；`--advise` 有新疑慮時建 `[ADV]`（dispatcher tick 會自動跑） |
| `devflow tick SLUG` | 看板自動化（由 `on_kanban_dispatch_tick` hook 觸發）：執行 orchestrator 的 `devflow-ops`、計畫變動時跑 check plan |
| `devflow check ac [--said TEXT] < AC 行` | D0：建 EPIC 前檢查 AC 草稿（類型、是否太模糊、使用者原話有沒有漏）；exit 0 通過、1 要問使用者、2 Decisions 無法使用（照樣建卡） |
| `devflow check acceptance SLUG EPIC --pr N... [--review] [--evidence FILE] [--json]` | 向 GitHub 核對 PR CI／base／部署／整合 run 的身分與情境結果，再每條 AC 一次 Decisions 判定 PASS/FAIL（API 無法使用時 `UNJUDGED`，exit 3）；`--review` 只檢查開著的 PR（reviewer 用） |
| `devflow runner request OWNER/REPO` | 為 repo 註冊 Oracle VM self-hosted runner（需 repo admin） |
| `devflow runner status OWNER/REPO` | 查 runner 狀態 |
| `devflow resume SLUG TASK [--note TEXT]` | 只限聊天（不是 worker）：重跑 blocked/卡住的卡（解除已開 PR 卡的 24h 重派保護、重設失敗計數） |
| `devflow pending [PROFILE]` | 等使用者回答的卡（board、task、question、card、`block_event`）、在哪個 Bot Chat 問（`ask_in`）、各對話紀錄是否已有通知與回覆（`chats[].woken`/`replied`） |
| `devflow answer SLUG TASK --event N --where WHERE TEXT` | 交使用者對問題事件 N 的回答（一題只收一次；卡已不在等這題時回 `not applied` 與原因），再照 `resume` 重跑 |
| `devflow check card --title TITLE [--body-file F]` | 建卡／改卡前確定性檢查卡型、assignee、workspace、固定完成條件、body 長度與技術資訊洩漏 |
| `devflow preflight SLUG [TASK] [--dir DIR]` | developer 送審前一站式確定性自檢：範圍（`Allowed-Scope`/`Out-of-Scope`）、變更檔數、保護檔 hash、PR 格式與快速測試 |
| `devflow check pr SLUG PR` | PR 判斷：保護／gate 檔變更、EPIC 目標與 diff 範圍對齊（`out_of_scope_diff`）、review 往復熔斷與 Decisions 檢查 |
| `devflow ship merge SLUG PR` | 核對 reviewer exact-head approve、judgment、受保護檔人類 exact-head 核准（必要時 exit 6）與 EPIC 計畫風險定錨（必要時 exit 7）後，依 `.devflow/config.env` 的 `DEVFLOW_MERGE_METHOD` 合併 |
| `devflow approve SLUG PR --request REQ_ID [--head FULL_SHA] [--note TEXT]` | 只限聊天（不是 worker）：於使用者明確核准後，以原提問的 `核准請求碼` 綁定原始確認提交並記錄 exact-head 核准（head 已變動即拒絕） |
| `devflow approve-plan SLUG EPIC --plan-hash HASH [--note TEXT]` | 只限聊天（不是 worker）：於使用者明確核准計畫風險（選 B）後，記錄該計畫雜湊的明確同意並解除 `[SHIP]` 暫停 |
| `devflow repo protect SLUG` | default branch 有 `devflow-ci` 時設為 required check，開啟 PR contract（冪等） |

## 外部技能
- `pstack:*` 與 mattpocock 技能是方法論，依角色已分配好；用 `skills_list` / `skill_view` 讀。
- 技能提到 Cursor subagent / Task 時，用 `delegate_task`（若可用）或自己依序完成。不要執行 `setup-pstack`、`setup-matt-pocock-skills`。
