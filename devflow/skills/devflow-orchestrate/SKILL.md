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
   Given 寫要先成立的狀態（developer 會在 Given 結尾斷言它），When 只寫一個操作，Then 寫使用者或呼叫端看得到的結果與具體值。情境完整 id 是 `<EPIC id>/AC-n.Sm`；Given/When/Then 文字就是測試步驟名稱，之後要改只能改 EPIC 並讓相關 PR 重跑。用 `devflow-ops` 的 `edit` 改 EPIC body（原文保留在「使用者原話」）；子卡照新編號寫。
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
   - 有 `[SHIP]` 時先跑 `terminal: devflow repo doctor <slug>`，看 `deploy`：`contract-files` 或 `port` 沒過 → 加一張 `[OPS]` 用 PR 修，後面接它自己的 `[SHIP]`；其他 `[DEV]` 的 parent 指向這張 `[SHIP]`，讓每張 `[SHIP]` 都在這張 `[OPS]` 下游。`[OPS]` body 寫出這些項目 id 與 doctor 給的修法（`check plan` 會檢查）。`runner-online`／`runner-request`／`fork-approval` 沒過不用建卡：`[SHIP]` 合併前會擋下並問使用者。不設計新的部署方式或安全門檻。
   - 每條 AC 至少一張卡負責；卡片 body 寫「覆蓋：AC-1, AC-3」，`## 驗收` 照抄那幾條 AC（可加這張卡自己的技術驗收）。
   - 卡片只寫目標與驗收，不寫實作細節；做法交給 architect / developer。
4. 建卡：
   ```
   kanban_create(title="[DEV] ...", assignee="developer", workspace_kind="worktree", goal_mode=true,
                 parents=[...], body="## 目標\n...\n## 本卡完成\nPR 已開、exact-head CI 綠、已交 review；合併與部署由 [SHIP] 負責。\n## 驗收\n- ...\n## 提示\n- 相關檔案...\n- EPIC: t_xxxx",
                 skills=[選用，例如 "tdd"])
   ```
   `[DEV]`/`[OPS]` 一定加 `goal_mode=true`：developer 停下卻沒呼叫終結工具（例如「等 CI」）時，Hermes 會再給一輪要它繼續，而不是記 crash 並扣住卡片。goal judge 用 title+body 判斷能否交 review，所以 body 一定要有上面的 `## 本卡完成`，否則會因「還沒部署」擋下 review。goal_mode 建卡後改不了，漏加只能作廢重建。
   workspace：`[RES]`/`[ADV]` scratch、`[ARCH]`/`[SHIP]` dir、`[DEV]`/`[OPS]` worktree（見 `devflow` 表）。`[DEV]`/`[OPS]` 不給 `workspace_path`（只有接續舊卡 worktree 時才給）：建卡回應裡的 `workspace_path` 會先顯示主 checkout，dispatcher claim 時才換成 `.worktrees/<task-id>`，這是正常的，不要自己 `git worktree add`。`[DEV]`/`[OPS]` 在 `devflow repo list` 顯示 `contract=yes` 時加 `completion_contract="OWNER/REPO"`，否則不設（見 `devflow` 推進規則）。
5. 把每條路徑的最後一張卡（通常是 `[SHIP]`）連成 EPIC 的 parent：`kanban_link(parent_id=<leaf>, child_id=<EPIC>)`。
6. 自查：`terminal: devflow check plan <slug> <EPIC>`（唯讀）。`findings` 裡的規則問題（assignee、workspace、contract、缺 `[SHIP]`、AC 沒人負責、AC 沒類型、缺情境或 Given/When/Then、沒接到 EPIC）自己修掉再跑一次；`[manual]` 的提醒是給 advisor 判斷的，不用自己處理；`Decisions …` 開頭的是風險判斷，不用自己處理。
7. `kanban_comment(EPIC, 計畫摘要：卡片清單、順序、AC 對應)`，然後 `kanban_block(kind="dependency", reason="等待子卡完成：t_a, t_b…")`。EPIC 等待期間，dispatcher tick 會在計畫有變動時跑 `devflow check plan`：有疑慮就自動建 `[ADV]`（Opus 5.5）並在 EPIC 留言；含不可回復操作時 `[ADV]` 會成為各 `[SHIP]` 的 parent。你不用等它。
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
   FAIL 的處理（依 `reasons`；同時有 `UNJUDGED` 時只處理 FAIL）：缺整合 run、整合 run 不在部署 SHA → 建 `[SHIP] 整合驗收 <EPIC>`（devops、dir、不設 contract，body 寫「`EPIC: <id>`；在目前部署 SHA 跑 ci.yml 整合 run，見 devflow-devops §4 第 5 步」），連成 EPIC parent；最新 commit 的部署 run 還在跑 → 不建卡，`kanban_heartbeat` 後等幾分鐘重跑本步；最新 commit 的部署失敗或沒有部署 run → 同樣建 `[SHIP] 整合驗收 <EPIC>`，body 多寫「最新 commit <sha> 的部署 <run URL 或『沒有』>：先依 devflow-devops §5 處理部署，再做第 5 步」；情境沒做到、缺情境、base 不是紅燈、步驟文字和 EPIC 不一致 → 補 `[DEV]` 卡（body 寫情境 id、哪一段失敗、run URL），後接 `[SHIP]`，連成 EPIC parent。之後 `kanban_block(kind="dependency")`。只有 Decisions 偏嚴（確定性檢查全過、證據確實足夠）時，才可記下是哪份證據、為什麼足夠，當 PASS；確定性檢查的 FAIL 不能自行改判。
3. 全部 PASS → `kanban_complete`：summary 用繁中寫給使用者（做了什麼、PR 連結、部署結果/網址、每條 AC 的結果與一句判斷依據（哪個情境、改動前是否紅燈、截圖看到什麼）、已知限制）；metadata 放 `prs`、`deploy_runs`、`integration_run`、`acceptance`（`check acceptance` 的 `criteria`）、`followups`。

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
