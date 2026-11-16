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

這張卡在 review 欄，cwd 是 developer 的 worktree。你用 reviewer 自己的 GitHub 帳號（與 PR 作者不同）。本 skill 優先於 `sdlc-review` 的通用流程。

## 1. 讀
- `kanban_show`：驗收條件、developer 的 handoff（`pr_url`、`head_sha`、`tests`）、之前的退件理由（是否都處理了）。
- `gh pr view <pr> --json headRefOid,state,mergeable,statusCheckRollup,files` 確認 `headRefOid` == `head_sha`；不一致就以 PR 最新 head 為準。
- `gh pr diff <pr>`；`git rev-parse HEAD` 應等於 `headRefOid`（developer 從這個 worktree push）。不要 checkout/commit/reset：退件後 developer 會回到同一個 worktree 繼續。測試產生的暫存檔跑完用 `git status` 確認並清掉。

## 2. 驗
- 跑 `.devflow/test.sh`（或 repo 慣用測試）。
- 獨立探測（不提交）：依卡片的 AC 原文、不看 diff 決定要測什麼，自己寫一次性腳本或指令驗證關鍵行為與邊界；放在 worktree 外（`/tmp/review-<task>`），跑完刪掉，`git status` 必須乾淨。結果寫進結論 metadata 的 `probes`。
- 看 CI 結果；`ci.yml`（`devflow-ci`）紅燈 = 退件（除非 handoff 已說明是 runner 問題且你確認與程式碼無關）。`quality.yml` 失敗或數字差不算 CI 紅燈。
- 情境與證據：`devflow check acceptance <slug> <EPIC> --pr <pr> --review --json`（EPIC 在卡片 body 的 `EPIC:`）。它向 GitHub 找這個 PR head 的 `ci.yml` run（最新 attempt），核對 `acceptance.json` 身分、`Covers:` 每個情境在 head 通過、步驟文字和 EPIC 一致、base 判定為紅燈（或帶豁免）。非 0 結束 = 退件，`reasons` 照抄進退件理由。
- 再自己看證據：`gh run download <CI run> -D /tmp/review-<task>/evidence`，讀 `acceptance-matrix.md`，對每個情境：
  - 開 `sources/` 裡的情境程式，確認 Then 的斷言真的檢查 AC 要的結果（具體值、筆數、訊息），不是只檢查「頁面打開」「回 200」；Given 結尾有斷言前置狀態。
  - `ui` 情境用 vision 看 `shots/` 截圖，確認畫面真的顯示 AC 要的結果。
  - `acceptance-base.json`：RED 的失敗段落與錯誤合理（例如找不到「新增」按鈕、清單沒有那一筆）；`Already-met`／`Base-unverifiable` 的理由要你判斷成立，不成立就退件。
- 檢查（`code-review` 的 Standards + Spec 兩軸）：
  1. 卡片覆蓋的每條 AC，EPIC 的每個情境都有對應實作、`Covers:`、通過的情境與證據；`[smoke]` 情境不寫資料。缺情境、斷言太弱或證據不足 → 退件，寫明哪個情境 id、要斷言什麼；由 developer 改 PR，你審新的 head。你不改 PR。
  2. 正確性與邊界條件；錯誤處理；讀者負擔（`pstack:principle-minimize-reader-load`）：一個概念散在多處、要跳很多檔案才看得懂、命名誤導 → 指出具體位置，影響正確理解才退件。
  3. 安全：秘密進 repo、注入、權限、`deploy/compose.yml` 對外綁定（應只綁 `127.0.0.1`）。
  4. 回歸與影響範圍（`pstack:blast-radius`）；測試是否測行為而非實作。
  5. `ub-mode`（developer 在 Codex 用的同一份規則）的 Make it Right：根因修正而非遮蔽、測試測行為（「每個函式回傳空值測試還會過嗎？」）、重複與死碼。這些由你讀程式判斷。CI `quality` artifact（`devflow-quality-<sha>`：`crap.txt` / `dry.txt` / `mutate.txt`）目前**只供參考、不卡**：不因 CRAP、重複分數或存活 mutant 退件；值得處理的寫進結論 metadata 的 `followups`（或 approve 留言），由 orchestrator 決定是否另開卡。
  6. 規則與教訓（`check` job 已驗證格式、自測、ref 存在，並在 PR 上證明新教訓抓得到修正前的程式；你判斷內容）：新增或修改的規則，它的 `expect = "fail"` 範例要真的代表這次的錯誤；PR 刪除或放寬既有規則、`baseline.json` 增加條目、新增 `devflow: allow` 豁免，卡片沒有明確授權就退件，寫明要還原哪一處。PR 說明的 `No-lesson:`／`No-proof:` 理由不成立（同類錯誤顯然會再犯、錯誤其實就在這個 PR 修）→ 退件；修 bug 卻不用 `fix:` 標題來避開 lesson gate → 退件。
  7. 自動判斷：`devflow check pr <slug> <pr>`（在 Hermes 主機跑，PR 改不到；結果依 head + 標題 + 說明快取，並寫入 `devflow-judgment` 檢查）。`findings` 列出 PR 改到的 gate 檔案（`check.py`、`check.sh`、`test.sh`、`rules/`、`.github/workflows/`、刪除教訓）與 Decisions 標出的疑慮（`unlabeled_fix` 修 bug 沒標 `fix:`、`weak_exemption` `No-lesson`/`No-proof`/`Already-met`/`Base-unverifiable` 理由不成立、`lesson_mismatch` 教訓沒對準這次的錯）。每一項在結論裡寫成立或不成立與理由；成立就退件。結束碼：
     - `0`：沒有疑慮或 advisor 已回 `ok`，`devflow-judgment` 已是 success。
     - `3`：有疑慮，等 `[ADV]` advisor（Decisions API 無法判斷時也是：疑慮寫「請 advisor 代為回答」，advisor 代替 Decisions 判斷）。你自己判斷有成立的疑慮 → 直接退件；否則做完其他檢查後**再跑一次** `devflow check pr`（結果有快取，不會重新判斷），依這次的輸出處理（advisor 可能已在你檢查期間完成：`0` 就照通過）。`wait`：
       - `dependency`：`kanban_link(parent_id="<輸出的 advisor>", child_id="<本卡>")`，再 `kanban_block(kind="dependency", reason="devflow check pr：等 advisor 判斷 PR #N")`。Hermes 把本卡放回 todo，advisor 完成後自動回到 review；重跑時從這一步開始。不要用沒有 kind 的 block：同一張卡連續兩次會被送去 triage。
       - `retry`：tick 還沒建 `[ADV]`。`kanban_heartbeat`，等 1 分鐘重跑，最多 10 次；仍是 `retry` 才 `kanban_block(kind="needs_input", reason=...)`。
     - `4`：advisor 回 `revise` → 退件，理由引用 advisor 的意見。
     - `1`：PR 在判斷期間被修改 → 直接重跑。
- 不為風格偏好退件。

## 3. 結論
**通過**
```bash
gh pr review <pr> --approve --body "LGTM: <一句話理由>（Kanban <task-id>）"
```
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
退件理由要具體到 developer 不用再問就能改。沒有次數上限；同一問題第二次退件時，說明為什麼前一次修正沒解決，並給更直接的做法。
