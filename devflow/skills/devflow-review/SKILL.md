---
name: devflow-review
description: "Reviewer: independently review a devflow PR in the review lane, run tests, then approve (kanban_complete + gh approve) or kanban_request_changes."
version: 2.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, review, qa, pr]
    related_skills: [devflow, ub-mode, code-review, sdlc-review]
---

# devflow-review｜審查卡

你用 reviewer 自己的 GitHub 帳號（與 PR 作者不同）獨立審查 PR。本 skill 優先於 `sdlc-review` 的通用流程。

先判別審查模式（不混用、不硬湊歷史卡）：
- **模式 A｜Kanban EPIC PR**（被 dispatcher 派到 review 欄，卡片 body 有 `EPIC: t_xxxx`）：走 §1–§3，跑 `devflow check acceptance <slug> <EPIC> --pr <pr> --review` 與 `devflow check pr`。
- **模式 B｜直接操作者 PR（Direct Operator PR）**（沒有所屬 Kanban EPIC 卡，由人或獨立審查流程指派）：**嚴禁自行套用看板上其他無關的歷史 EPIC 卡號**；略過 Kanban 卡片與 `check acceptance`，直接以 PR 說明目標、repo 契約／policy、exact-head GitHub Actions CI、本機針對性快測與獨立探測審查，最後提交 exact-head GitHub review。
- **模式 C｜Kit PR**（devflow 本身的 repo）：走 §4。

## 1. 讀
- 模式 A：`kanban_show` 讀驗收條件、範圍邊界（`Allowed-Scope`/`Out-of-Scope`）、developer handoff（`pr_url`、`head_sha`、`tests`）與之前退件理由。
- `gh pr view <pr> --json headRefOid,state,mergeable,statusCheckRollup,files` 確認 `headRefOid` == `head_sha`；不一致就以 PR 最新 head 為準。
- `gh pr diff <pr>`；worktree 中 `git rev-parse HEAD` 應等於 `headRefOid`。不要 checkout/commit/reset；測試暫存檔跑完清乾淨。

## 2. 驗（分層測試，避免容器重型測試超時）
- **本機針對性測試**：優先執行 `.devflow/config.env` 定義的 `DEVFLOW_REVIEW_TEST_CMD`；若未定義，依變更模組跑針對性單元測試（不在缺少完整瀏覽器／系統依賴的容器內硬跑重型全量 `.devflow/test.sh` 等到超時）。完整矩陣以 exact-head GitHub Actions CI（`devflow-ci` 或 repo 既有 CI）結果與 run artifacts 為準。
- 獨立探測（不提交）：依 AC 或 PR 目標、不看 diff 決定要測什麼，寫一次性腳本驗證關鍵行為與邊界；放 worktree 外（`/tmp/review-<task>`），跑完刪掉，`git status` 必須乾淨。結果寫進 `probes`。
- 看 CI 結果；exact-head CI 紅燈 = 退件（除非 handoff 已說明且你確認為非程式碼的 runner 問題）。`quality.yml` 失敗或數字差不算 CI 紅燈。
- **情境與證據（僅模式 A）**：`devflow check acceptance <slug> <EPIC> --pr <pr> --review --json`（只用本卡明確標註的 `EPIC:`，不猜測其他卡）。非 0 結束 = 退件，`reasons` 照抄進退件理由。
- 再自己看證據：`gh run download <CI run> -D /tmp/review-<task>/evidence`，讀 `acceptance-matrix.md`，對每個情境：
  - 開 `sources/` 裡的情境程式，確認 Then 的斷言真的檢查 AC 要的結果（具體值、筆數、訊息），不是只檢查「頁面打開」「回 200」；Given 結尾有斷言前置狀態。
  - `ui` 情境用 vision 看 `shots/` 截圖，確認畫面真的顯示 AC 要的結果。
  - `acceptance-base.json`：每個 RED 的失敗要落在 AC 要新增或改變的行為上（例如找不到要新增的「新增」按鈕、清單沒有那一筆），不是環境錯誤、啟動失敗、或選擇器打錯了 base 本來就有的東西。用 base 證據確認：`ui` 情境看 base 的 `shots/` 截圖或 `playwright/*/error-context.md` 的 page snapshot（例如畫面真的沒有那個按鈕）。最終驗收的 Decisions 不判斷這點，只有你這一關；不成立就退件。`Already-met`／`Base-unverifiable` 的理由要你判斷成立，不成立就退件。
- 檢查（`code-review` 的 Standards + Spec 兩軸）：
  1. 卡片覆蓋的每條 AC，EPIC 的每個情境都有對應實作、`Covers:`、通過的情境與證據；`[smoke]` 情境不寫資料。缺情境、斷言太弱或證據不足 → 退件，寫明哪個情境 id、要斷言什麼；由 developer 改 PR，你審新的 head。你不改 PR。
  2. 正確性與邊界條件；錯誤處理；讀者負擔（`pstack:principle-minimize-reader-load`）：一個概念散在多處、要跳很多檔案才看得懂、命名誤導 → 指出具體位置，影響正確理解才退件。
  3. 安全：秘密進 repo、注入、權限、`deploy/compose.yml` 對外綁定（應只綁 `127.0.0.1`）。部署腳本失敗時要能看到原因：取代或包裝 devflow 範本 `deploy.sh` 的程式，若把 build/up 的錯誤輸出整段丟掉（只留錯誤代碼、位元組數）→ 退件，要求遮秘密後保留 stderr 尾段（範本的 `capture.sh`/`redact.py` 做法）。看不到錯誤，下次部署失敗就只能猜。
  4. 回歸與影響範圍（`pstack:blast-radius`）；測試是否測行為而非實作。
  5. `ub-mode`（developer 在 Codex 用的同一份規則）的 Make it Right：根因修正而非遮蔽、測試測行為（「每個函式回傳空值測試還會過嗎？」）、重複與死碼。這些由你讀程式判斷。CI `quality` artifact（`devflow-quality-<sha>`：`crap.txt` / `dry.txt` / `mutate.txt`）目前**只供參考、不卡**：不因 CRAP、重複分數或存活 mutant 退件；值得處理的寫進結論 metadata 的 `followups`（或 approve 留言），由 orchestrator 決定是否另開卡。
  6. 規則與教訓（`check` job 已驗證格式、自測、ref 存在，並在 PR 上證明新教訓抓得到修正前的程式；你判斷內容）：新增或修改的規則，它的 `expect = "fail"` 範例要真的代表這次的錯誤；PR 刪除或放寬既有規則、`baseline.json` 增加條目、新增 `devflow: allow` 豁免，卡片沒有明確授權就退件，寫明要還原哪一處。PR 說明的 `No-lesson:`／`No-proof:` 理由不成立（同類錯誤顯然會再犯、錯誤其實就在這個 PR 修）→ 退件；修 bug 卻不用 `fix:` 標題來避開 lesson gate → 退件。
  7. 自動判斷：`devflow check pr <slug> <pr>`（在 Hermes 主機跑，PR 改不到；結果依 head + 標題 + 說明快取，並寫入 `devflow-judgment` 檢查）。`findings` 列出 PR 改到的保護／gate 檔案（含 `needs_human_approval` 標記）、違反卡片 `Allowed-Scope`/`Out-of-Scope` 或變更檔數上限的範圍違規，與 Decisions 標出的疑慮（`unlabeled_fix`、`weak_exemption`、`lesson_mismatch`、`replaces_standard`、`out_of_scope_diff`）。每一項在結論裡寫成立或不成立與理由；成立就退件。結束碼：
     - `0`：沒有疑慮或 advisor 已回 `ok`，`devflow-judgment` 已是 success。
     - `3`：有疑慮，等 `[ADV]` advisor。你自己判斷有成立的疑慮 → 直接退件；否則做完其他檢查後**再跑一次** `devflow check pr`，依輸出處理。`wait`：
       - `dependency`：`kanban_link(parent_id="<輸出的 advisor>", child_id="<本卡>")`，再 `kanban_block(kind="dependency", reason="devflow check pr：等 advisor 判斷 PR #N")`。
       - `retry`：tick 還沒建 `[ADV]`。`kanban_heartbeat`，依 `roles.json` `policy.advisor_retry_seconds`/`advisor_max_retries`（預設每 30 秒、最多 6 次）重跑；仍是 `retry` 才 `kanban_block(kind="needs_input", reason=...)`。
     - `4`：advisor 回 `revise` → 退件，理由引用 advisor 的意見。
     - `1`：PR 在判斷期間被修改 → 直接重跑。
  - 若輸出 `review_loop.tripped` 為 `true`（退件輪數已達 `max_review_rounds` 上限）：在退件理由標註「已達 review 往復熔斷上限，請 orchestrator／architect 先重檢根因或向使用者定錨」，不再做無止盡修修補補。
- 不為風格偏好退件。

## 3. 結論
**通過**
```bash
gh api -X POST repos/<OWNER/REPO>/pulls/<pr>/reviews -f commit_id=<你審的 headRefOid> -f event=APPROVE \
  -f body="LGTM: <一句話理由>（Kanban <task-id>）"
```
核准綁在你審的那個 commit；之後有新 commit，`devflow ship merge` 會拒絕合併，要你審新的 head。核准後才發現問題，就用下面的退件（最新的有效 review 決定能否合併）。
```
kanban_complete(summary="通過：<理由>", metadata={"published_pr": "<PR URL>", "pr_url": "<PR URL>", "head_sha": "...", "verdict": "approved", "tests": [...],
                "acceptance": {"<EPIC>/AC-1.S1": "<run URL>：head passed、base RED（When）、斷言檢查 <什麼>"}, "probes": ["<一次性檢查> → <結果>"]})
```
卡片有 `completion_contract` 時，Hermes 會在這一步核對 PR exact head 的 required check（`devflow-ci`、`devflow-judgment`）：`devflow-ci` 回 `pending` 就 `gh pr checks <pr> --watch`、`kanban_heartbeat` 後再 complete；`devflow-judgment` 不是 success 就回到第 2 節第 7 項重跑 `devflow check pr`；`devflow-ci` 回 `failure` 改走退件。
**退件**
```bash
gh pr review <pr> --request-changes --body "<同下列清單>"
```
```
kanban_request_changes(reason="1. <檔案:行> <問題> → <要怎麼改>\n2. ...")
```
退件理由要具體到 developer 不用再問就能改。遵守 review 往復熔斷（預設 2 輪，`roles.json` `policy.max_review_rounds`）：第二次退件時必須說明為何前一次修正未解根因，並提醒 orchestrator／architect 介入或交使用者定錨，不要進入無止盡往復。

## 4. kit PR（devflow 本身的 repo，沒有卡片）
由 `review-kit-pr.sh` 在 Hermes chat 叫你，prompt 給 repo、PR 號與暫存目錄。沒有卡片，不用 `kanban_*`；第 1、2 節的卡片、EPIC、`check acceptance`、`check pr` 不適用。
1. 讀：`gh pr view <pr> -R <repo> --json title,body,headRefOid,files,statusCheckRollup`、`gh pr diff <pr> -R <repo>`。
2. 驗：在暫存目錄 `git clone` 該 repo、`git checkout <prompt 給的 head commit>`，跑 `cd devflow/tests && python3 -m unittest`（失敗 = 退件）；PR 有改的行為，自己寫一次性指令驗證（放暫存目錄）。`ci` check 不是 success 也退件。
3. 查：PR 說明的問題是否真的解決、測試是否測行為；**放寬 gate**（刪檢查、降門檻、跳過情境、擴大豁免、讓 agent 能自己核准自己）沒有在 PR 說明寫出理由與使用者同意 → 退件。其餘同第 2 節「檢查」2–5。
4. 結論只綁在你審的那個 commit：通過 `gh api repos/<repo>/pulls/<pr>/reviews -f commit_id=<head commit> -f event=APPROVE -f body="LGTM: <理由>"`；否則同樣帶 `commit_id`、`event=REQUEST_CHANGES`、`body="1. <檔案:行> <問題> → <要怎麼改>\n2. ..."`。PR 的 head 已不是那個 commit 就不送 approve，改送 REQUEST_CHANGES 說明「審查期間 head 改變，請重跑 review-kit-pr.sh」。最後一行只輸出 `VERDICT: approved` 或 `VERDICT: changes_requested`。刪掉暫存目錄。
