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
dispatcher 下一個 tick（約 1 分鐘）由 `devflow tick` 依序執行，結果回留言在同一張卡；第一個操作失敗就停止，後面的不執行（結果列出），修正原因後把失敗與未執行的操作貼成新的 `devflow-ops` 區塊。所以有依賴的操作要照安全順序排：先 `link` 新卡，再 `unlink`、`archive` 舊卡。`edit` 可給 `title`、`body`；`resume` 用在 blocked / 卡住的卡（會解除 PR 卡的 24h 重派保護、重設失敗計數）。發完就照流程繼續（block 或 complete），不用等結果。


## A. 規劃
1. `terminal: /opt/data/devflow/bin/devflow repo sync <slug>`，讀 README、目錄結構、`.devflow/`、`.github/workflows/`。EPIC 的 `## 驗收` 沒有 `AC-1`、`AC-2`… 編號或類型標記（`[ui]`/`[api]`/`[cli]`/`[lib]`/`[manual]`）時，先把每條改寫成可觀察的結果、編號並標類型。
   然後把每條非 `[manual]` AC 展開成情境（讀過 repo 才寫得出具體資料與畫面文字）：至少一個正常情境；有輸入的 AC 再加一個錯誤或邊界情境；只讀、不寫資料的情境標 `[smoke]`（部署後只跑這些）。格式固定：
   ```
   - AC-1 [ui] 使用者可新增待辦，重新整理後仍存在
     - S1 新增後重新整理仍存在
       - Given 已登入、清單為空
       - When 輸入「買牛奶」並按「新增」
       - Then 清單只有「買牛奶」，重新整理後仍在
     - S2 [smoke] 待辦頁可開啟並顯示新增表單
       - Given 服務已啟動
       - When 開啟待辦頁
       - Then 看到標題「我的待辦」與「新增」按鈕
   ```
   Given 寫要先成立的狀態（developer 會在 Given 結尾斷言它），When 只寫一個操作，Then 寫使用者或呼叫端看得到的結果與具體值。情境完整 id 是 `<EPIC id>/AC-n.Sm`；Given/When/Then 文字就是測試步驟名稱（若 repo 已維護 `.devflow/scenarios.json`，`check plan` 與 `check acceptance` 會自動從 `.devflow/scenarios.json` 載入情境步驟，EPIC 卡片上只需保留精簡白話 `AC-n` 列表即可；若寫在 EPIC 上也一律用白話描述看得到的動作與結果，不寫 SHA、路徑或內部參數）。用 `devflow-ops` 的 `edit` 改 EPIC body（原文保留在「使用者原話」）；子卡照新編號寫。
   改寫與展開只能**更精確地說使用者要的結果**，不能加使用者沒要求的機制：稽核紀錄、白名單、前後快照、准入檢查、自訂審查政策、專屬帳號或通道都不是 AC，也不寫進「提示」當限制（`check plan` 會把 EPIC 對照「使用者原話」，多出來的列為 `beyond_said`）。「不要動其他服務」就是一條可觀察的結果（例如部署前後其他容器的 ID 與啟動時間不變），不是要建一套隔離系統。使用者明確要求或核准的限制，在「提示」寫成「使用者 <日期> 決定：…」：worker 會把它當成高於 devflow 流程的限制照做（`devflow` skill「誰定流程」），沒有這個標記的提示不能改變流程。
   部署後的服務只綁主機 `127.0.0.1:<port>`；使用者打開它的方式固定是 `devflow repo access <slug>` 的 SSH tunnel（README「部署」）。`[ui]` 情境的 Given 寫「服務已部署」即可，不寫「經核准的安全入口」，也不要求公開網域或 HTTPS；部署後 smoke 在主機上連同一個位址，等同使用者經 tunnel 看到的畫面。不要為入口問使用者。
2. 判斷 repo 狀態，決定第一段：
   | 狀態 | 第一段卡片 |
   |---|---|
   | 空 repo（新專案） | `[ARCH]` → `[DEV] 建立骨架與 devflow 契約` → `[SHIP]` |
   | 既有 repo、沒有 `.devflow/` 或 workflow，且需要部署 | `[OPS] 接入 devflow CI/CD` → `[SHIP]` |
   | 已接入 | 直接進功能卡 |
   devflow repo 契約 = `/opt/data/devflow/templates/repo/`（`.devflow/test.sh`、`check.sh`、`check.py`、`rules/`、`verify.sh`、`verify-base.sh`、`acceptance.py`、`up.sh`、`playwright.sh`、`deploy.sh`、`capture.sh`、`redact.py`、`quality.sh`、`config.env`、`deploy/compose.yml`、`.github/workflows/ci.yml`、`deploy.yml`、`repro.yml`、`quality.yml`）。已接入但缺 `acceptance.py`／`verify-base.sh`、或 `ci.yml` 沒有 `verify`／`verify-base`／`check` job（`devflow contract <slug> <主 checkout>` 會列出差異）→ 第一段加 `[OPS] 升級 devflow 契約`（body 寫明：合併 `ci.yml` 的 `check`、`verify-base` job 並列入 `devflow-ci` 的 needs、把舊的 `journey` 改寫成 `scenario`、`check.sh --write-baseline` 收舊違規）；缺 `capture.sh` / `repro.yml` / `quality.yml`（`devflow contract` 會直接補上缺的檔）→ 同一張或另一張 `[OPS] 升級 devflow 契約` 補齊，不擋功能卡並行。
   `[ADJ]` 找到根因、且同類錯誤可能再犯時，修正卡的 `## 驗收` 要寫「`fix:` PR，新增教訓 `.devflow/lessons/L-NNNN.toml`（PR 說明 `Lesson: L-NNNN`），優先以規則或測試擋住」。CI `check` job 會擋沒有 `Lesson:`／`No-lesson:` 的 fix PR、證明新教訓在修正前的程式上會失敗，並要求同一根因再出現時更新原本的教訓。
3. 先寫吞吐檢查（`pstack` feature playbook）：哪些步驟必須先做、哪些可並行、哪裡會同時寫同一份檔案（先拆開，真的要共用才串行）、最小可行的切法。然後切卡（`pstack:principle-sequence-verifiable-units`）：
   - `[RES]` 只在有實際未知時建；`[ARCH]` 只在有跨模組或難回頭決策時建。
   - `[DEV]` 一卡一 PR，能獨立合併；每張 `[DEV]`/`[OPS]` 後面接一張 `[SHIP]`（parent = 該卡）。
   - 依賴前一個 PR 程式碼的 `[DEV]`，parent 要指向前一張 `[SHIP]`（合併後 worktree 才會基於新 main）。互不相干的 `[DEV]` 並行。
   - 不需要部署的工作（純研究、文件）不用 `[SHIP]`。
   - 有 `[SHIP]` 時先做**終點反推的端到端前置盤點**（一次查清，禁止留到 `[SHIP]` 才逐一撞牆打地鼠）：
     1. 跑 `terminal: devflow repo doctor <slug>` 看 `deploy`：`contract-files` 或 `port` 沒過 → 加一張 `[OPS]` 用 PR 修，後面接它自己的 `[SHIP]`；其他 `[DEV]` 的 parent 指向這張 `[SHIP]`，讓每張 `[SHIP]` 都在這張 `[OPS]` 下游。`[OPS]` body 寫出這些項目 id 與 doctor 給的修法（`check plan` 會檢查）。`runner-online`／`runner-request`／`fork-approval` 沒過不用建卡：`[SHIP]` 合併前會擋下並問使用者。
     2. 跑 `terminal: gh secret list -R <OWNER/REPO>`（只看名稱，不讀值）並對照各 AC 的最終驗證步驟：若部署或正式環境操作需要特定腳本（如 `.devflow/ui/ops/<op>.spec.ts`），檢查 repo 是否已存在該腳本；還沒有就**在初始規劃一併排進 `[DEV]` 卡**，不要留給 `[SHIP]` 事後發現才連開補丁 PR。不設計新的部署方式或安全門檻。
   - 每條 AC 至少一張卡負責；卡片 `## 驗收` 第一行寫「覆蓋：AC-1, AC-3」，只寫編號，不抄 AC 原文（全文在 EPIC，worker 用 `kanban_show` 讀得到；照抄的行 tick 會自動刪掉、留編號）。PR 卡（`[DEV]`/`[OPS]`）覆蓋的 AC 不可全為 `[manual]`（0 個可執行情境會導致 `check acceptance` 的 `Covers:` 核對失敗）。下面最多補 1–2 句**給人判讀的白話完成結果**，不列技術實作清單、不設 `## 提示` 技術垃圾桶。
   - **卡片是給人看及判讀的**：標題與內容只寫白話目標與結果，禁止塞入 commit SHA、run ID、檔案路徑、函數/參數名或容器旗標；技術做法交給 architect / developer 在 repo 內完成。
4. 建卡：
   ```
   kanban_create(title="[DEV] <白話標題：做什麼事>", assignee="developer", workspace_kind="worktree", goal_mode=true,
                 parents=[...], body="## 目標\n<1-2 句白話：達成什麼使用者看得到的結果>\n## 本卡完成\n完成條件：PR 已開、exact-head CI 綠。審查在本卡自己的 review 欄進行，不是 [SHIP] 的事；合併與部署由 [SHIP] 負責。\n## 驗收\n覆蓋：AC-1, AC-3\nEPIC: t_xxxx",
                 skills=[選用，例如 "tdd"])
   ```
   `[DEV]`/`[OPS]` 一定加 `goal_mode=true`：developer 停下卻沒呼叫終結工具（例如「等 CI」）時，Hermes 會再給一輪要它繼續，而不是記 crash 並扣住卡片。送審時 Hermes 的 goal judge 只讀 title+body 的前 2000 字，判斷「本卡完成」是否已達成。所以：
   - `## 本卡完成` 一字不改照抄上面那句，緊接在 `## 目標` 後面。不要加「已交 review」「reviewer 已接手」「已批准」「已部署」，也不要寫送審工具名稱：judge 不知道是哪個工具在問，這些在送審時都還沒發生，會永遠拒絕送審（teams-transcription t_6d121ad4；2026-11-19 實測）。改過的文字 dispatcher tick 會自動改回，並在 EPIC 留言。
   - **body 只寫人話且極短**：整份 ≤ 600 字（`check plan` 檢查，不含 `[DEV]`/`[OPS]` 的 `## 本卡完成` 與 EPIC 的 `## 驗收`／`## 使用者原話`／`## 使用者決定`）。`## 目標` 1–2 句白話；**不寫 `## 提示` 技術細節**（不抄 repo 架構、SHA、run ID、socket、seccomp、禁止條文或 schema 步驟；做法由 developer / devops 讀 repo 決定）。
   - body 是規格，不是紀錄：PR 號、head SHA、run id、review id、每一輪試過什麼、誰接手，寫進 `kanban_comment`，body 最多放一行「經過見留言」。改卡（`[ADJ]` 或 `devflow-ops edit`）是**改寫**成目前的規格，不是在後面疊加新段落。
   goal_mode 建卡後改不了，漏加只能作廢重建。
   workspace：`[RES]`/`[ADV]` scratch、`[ARCH]`/`[SHIP]` dir、`[DEV]`/`[OPS]` worktree（見 `devflow` 表）。`[DEV]`/`[OPS]` 不給 `workspace_path`（只有接續舊卡 worktree 時才給）：建卡回應裡的 `workspace_path` 會先顯示主 checkout，dispatcher claim 時才換成 `.worktrees/<task-id>`，這是正常的，不要自己 `git worktree add`。`[DEV]`/`[OPS]` 在 `devflow repo list` 顯示 `contract=yes` 時加 `completion_contract="OWNER/REPO"`，否則不設（見 `devflow` 推進規則）。
5. 把每條路徑的最後一張卡（通常是 `[SHIP]`）連成 EPIC 的 parent：`kanban_link(parent_id=<leaf>, child_id=<EPIC>)`。
6. 自查（必做，0 錯誤才准進入等待）：`terminal: devflow check plan <slug> <EPIC>`（唯讀）。`actionable_rules`（或 `findings` 裡的確定性規則問題：assignee、workspace、contract、缺 `[SHIP]`、PR 卡只覆蓋 `[manual]` AC、AC 沒人負責、AC 沒類型、缺情境或 Given/When/Then、沒接到 EPIC、越權建卡、單一 `[SHIP]` 事後補丁卡超限）**必須當場修到 `actionable_rules` 為空（`[]`）再跑一次確認**；`[manual]` 的提醒與 `Decisions …` 風險是給 advisor 判斷的，不用自己處理。
7. `kanban_comment(EPIC, 計畫摘要：卡片清單、順序、AC 對應)`，然後 `kanban_block(kind="dependency", reason="等待子卡完成：t_a, t_b…")`。EPIC 等待期間，dispatcher tick 會在計畫有變動時先原地補齊遺漏的 `goal_mode` 與固定 `## 本卡完成`，再跑 `devflow check plan`：若有未解決的確定性規則錯誤，tick 會建立／通知 `[ADJ] 計畫規則修正 <EPIC>` 立刻喚醒你修正；若有顧問／風險疑慮則自動建 `[ADV]`（Opus 5.5），且當疑慮包含不可回復操作（`irreversible`）或超出範圍（`beyond_said`、`scope_creep`）時，`[ADV]` 會自動成為各待出貨 `[SHIP]` 的 parent 擋下出貨。
8. 想主動請 advisor 看（目標有多種解讀、架構取捨）：`kanban_create(title="[ADV] ...", assignee="default", workspace_kind="scratch", model="claude-opus-5.5", provider="copilot", body="EPIC: <id>\n<要它判斷什麼>")`，不加 parent、不連到 EPIC。

## B. 驗收
1. 讀所有 parent 的 summary/metadata：PR 號、`merged_sha`、部署與整合 run、`acceptance`、`followups`。這些只用來找出 PR 號；證據一律由下一步向 GitHub 核對。
2. 逐條判定 AC：`terminal: devflow check acceptance <slug> <EPIC> --pr <N> [--pr <M> ...] --json`（列出本 EPIC 所有已合併的 PR）。它會：
   - 由 GitHub 找每個 PR head 的 `ci.yml` run（`pull_request`、最新 attempt、成功），核對 `acceptance.json` 的 repo／SHA／run／attempt，以及 base 紅綠判定；
   - 找 default branch 最新 commit D 的部署 run（`push` 或 `workflow_dispatch` 中最新的一次，必須完成且成功；還在跑、失敗或沒有都算 FAIL，較舊的成功部署不算）與 D 上的整合 run（`ci.yml` `workflow_dispatch`），確認每個 PR 的合併 commit 都在 D 之內、全部情境在 D 通過、`[smoke]` 情境在部署後通過；
   - 情境的 Given/When/Then 文字必須和目前 EPIC 一致（EPIC 改過就要重跑）；
   - 上面任一項不符，該 AC 直接 FAIL（`reasons` 寫原因）；全部符合才對每條 AC 呼叫一次 Decisions（證據是否顯示達成、斷言是否足以證明）。`[manual]` AC 只有 Decisions，可用 `--evidence <檔>` 附證據。Decisions API 無法使用時，該 AC 判 `UNJUDGED`（證據已全部通過，只缺判斷），不是 FAIL。
   結束碼：`0` 全部 PASS；`1` 有 FAIL 或其他問題；`3` 只剩 `UNJUDGED`。
   `3` 的處理：不建任何補救卡（沒有找到缺陷）。`kanban_block(kind="needs_input", reason="Decisions API 無法使用，AC-x 未判定；API 恢復後 devflow resume 重跑驗收")`，Hermes 會轉告使用者。
   FAIL 的處理（依 `reasons`；同時有 `UNJUDGED` 時只處理 FAIL）：缺整合 run、整合 run 不在部署 SHA → 建 `[SHIP] 整合驗收 <EPIC>`（devops、dir、不設 contract，body 寫「`EPIC: <id>`；在目前部署 SHA 跑 ci.yml 整合 run，見 devflow-devops §4 第 5 步」），連成 EPIC parent；最新 commit 的部署 run 還在跑 → 不建卡，`kanban_heartbeat` 後等幾分鐘重跑本步；最新 commit 的部署失敗或沒有部署 run → 同樣建 `[SHIP] 整合驗收 <EPIC>`，body 多寫「最新 commit <sha> 的部署 <run URL 或『沒有』>：先依 devflow-devops §6 處理部署，再做第 5 步」；情境沒做到、缺情境、base 不是紅燈、步驟文字和 EPIC 不一致 → 補 `[DEV]` 卡（body 寫情境 id、哪一段失敗、run URL），後接 `[SHIP]`，連成 EPIC parent。之後 `kanban_block(kind="dependency")`。只有 Decisions 偏嚴（確定性檢查全過、證據確實足夠）時，才可記下是哪份證據、為什麼足夠，當 PASS；確定性檢查的 FAIL 不能自行改判。
3. 全部 PASS → `kanban_complete`：summary 用繁中寫給使用者（做了什麼、PR 連結、部署結果/網址、每條 AC 的結果與一句判斷依據（哪個情境、改動前是否紅燈、截圖看到什麼）、已知限制）；metadata 放 `prs`、`deploy_runs`、`integration_run`、`acceptance`（`check acceptance` 的 `criteria`）、`followups`。

## C. 調整（`[ADJ]` 卡：Advisor 建議或失敗卡處理）
- 讀 body 指到的 EPIC / 卡片：`kanban_show`；失敗原因看卡片 runs 與留言（`terminal: hermes kanban --board <slug> log <id>` 是唯讀，可用）。
- 接受建議：新卡照 A.4 建並 `kanban_link` 到 EPIC；要作廢、改寫、換人或解除依賴的舊卡，用 `devflow-ops`（見「看板操作」）。卡型只有 `[RES]` `[ARCH]` `[DEV]` `[OPS]` `[SHIP]`，review 是 `[DEV]`/`[OPS]` 的 review 欄，不建 review 卡。
- `[ADJ]` 卡不要跟要修的卡 link（不論 parent 或 child），否則互等。
- 執行失敗（gave_up / crash）：找出根因並修掉（設定、卡片 body、換 assignee、拆小）；能用一次真實小測試證明修好就做。
- 根因是 Hermes 原生限制時：照 README「Hermes 原生限制與對應」處理；沒有對應就 `kanban_complete` 寫明限制與建議，不建修 Hermes 程式或 image 的卡，也不在 `/opt/data/projects/<slug>` 以外建工作區（`check plan` 會列為問題）。使用者同意「處理這個問題」不等於同意改 Hermes。
- 標題 `[ADJ] 流程阻塞 t_x` 是 dispatcher tick 自動建的：worker 用 needs_input/capability 問了不是使用者能決定的事（工具、權限、卡片寫法、交接卡住），或有張沒有 block 事件的 `triage` 卡擋住下游未完成卡片。照上面找根因修掉，最後用 `devflow-ops` 的 `resume`（note 寫明改了什麼、worker 下一步做什麼；對 `triage` 卡會移出 `triage` 進入 `ready`/`todo`）。真的要使用者決定時才在本卡 `kanban_block(kind="needs_input")`。本卡還沒完成時，同一張卡再被擋，tick 不另建 `[ADJ]`，會把新問題留言到本卡：一併處理。
- `[DEV]`/`[OPS]` 沒送審就 done（runs 沒有 `review_requested`，PR 沒有獨立 review）：Hermes 不能把 done 的卡移回 review 欄，`resume` 也不行。建一張接續卡：同標題前綴、同 assignee、`goal_mode=true`、`workspace_path`=舊卡的 worktree、parents 同舊卡，body `## 目標` 寫「接續 t_old：PR #N 已開、CI 綠，修正已完成；確認 exact head 後送審」，`## 本卡完成` 照固定文字。用 `devflow-ops`：`link <接續卡> <舊卡每個 child>`、`unlink <舊卡> <child>`。不另建審查卡或新 `[SHIP]`。
- 卡片本身卡死（例如在不能設 required checks 的 repo 上設了 PR `completion_contract`，完成被 Hermes gate 拒絕；這類設定建立後不能改）：建替代卡（同 assignee、同 `workspace_kind`，worktree 用 `workspace_path`=舊卡 worktree，不設 contract），body 寫「接續 t_old：已完成什麼、剩什麼」；用 `devflow-ops`：對舊卡每個 child `link <新卡> <child>`、`unlink <舊卡> <child>`，最後 `archive <舊卡>`。
- 要重跑的卡：`devflow-ops` 的 `resume`。不要為此 `kanban_block`。
- 同一類根因已在之前的 `[ADJ]` 出現過（看 EPIC 留言與舊 `[ADJ]` 的 `root_cause`）：把修正放進結構，不只改這張卡（`pstack:principle-encode-lessons-in-structure`）。repo 層面 → 建 `[DEV]`/`[OPS]` 改 `.devflow/` 或 CI；devflow 本身（skill、範本、`devflow` CLI）→ 寫進 `followups`，由使用者決定是否改 kit。
- 同一張卡已有 3 張以上 `[ADJ]` 明確處理它（標題 `流程阻塞`／`處理失敗卡 t_x`，或 body 一行 `處理：t_x`，含本卡；`check plan` 會列出），或同一個問題已經開過一輪「補診斷／補證據」的修正 PR 仍不知道根因：不要再開下一輪。在本 `[ADJ]` `kanban_block(kind="needs_input")`，reason 照 `devflow` skill 的問題格式寫給使用者：卡在哪（一句話＋關鍵證據）、試過什麼、A/B 選項與代價（例如「請你在 VM 執行 <唯讀指令> 回報輸出」「改回 devflow 標準部署」「縮小這條 AC」）。`[ADJ]` 的提問不會被 tick 轉成流程問題，會直接到使用者（teams-transcription 2026-11-18：主機沒裝 buildx，連開三輪診斷卡）。tick 在一張卡已有 2 張 `[ADJ]` 後，第 3 次被擋就直接問使用者。
- 不接受：summary 寫理由。最後 `kanban_complete(summary=..., metadata={"root_cause": ..., "fix": ...})`。

## 原則
- 不輪詢、不 sleep 等待；一律用 parent + dependency block。
- 不設固定的次數或時間上限；同因失敗兩次就換策略（`pstack:principle-attack-the-premise`），同一段路繞第三次就照「C. 調整」把選擇交給使用者。
- 只有產品方向、付費、破壞性資料操作，或上一點「繞第三次」要使用者選路時，才 `kanban_block(kind="needs_input")`；Hermes 會問使用者。
