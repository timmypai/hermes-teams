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
- 看證據：`gh run download <CI run> -D /tmp/review-<task>/evidence`，每條 AC 對到 `verify.sh` 的 journey 輸出或 Playwright 截圖（用 vision 看圖，確認畫面真的顯示 AC 要的結果）。
- 檢查（`code-review` 的 Standards + Spec 兩軸）：
  1. 每條 AC 都有對應實作、journey 與證據。缺 journey 或證據不足 → 退件，寫明要補哪條 AC 的 journey、要斷言什麼；由 developer 改 PR，你審新的 head。你不改 PR。
  2. 正確性與邊界條件；錯誤處理；讀者負擔（`pstack:principle-minimize-reader-load`）：一個概念散在多處、要跳很多檔案才看得懂、命名誤導 → 指出具體位置，影響正確理解才退件。
  3. 安全：秘密進 repo、注入、權限、`deploy/compose.yml` 對外綁定（應只綁 `127.0.0.1`）。
  4. 回歸與影響範圍（`pstack:blast-radius`）；測試是否測行為而非實作。
  5. `ub-mode`（developer 在 Codex 用的同一份規則）的 Make it Right：根因修正而非遮蔽、測試測行為（「每個函式回傳空值測試還會過嗎？」）、重複與死碼。這些由你讀程式判斷。CI `quality` artifact（`devflow-quality-<sha>`：`crap.txt` / `dry.txt` / `mutate.txt`）目前**只供參考、不卡**：不因 CRAP、重複分數或存活 mutant 退件；值得處理的寫進結論 metadata 的 `followups`（或 approve 留言），由 orchestrator 決定是否另開卡。
  6. 規則與教訓（`check` job 已驗證格式、自測與 ref 存在；你判斷內容）：新增或修改的規則，它的 `expect = "fail"` 範例要真的代表這次的錯誤；PR 刪除或放寬既有規則、`baseline.json` 增加條目、新增 `devflow: allow` 豁免，卡片沒有明確授權就退件，寫明要還原哪一處。
- 不為風格偏好退件。

## 3. 結論
**通過**
```bash
gh pr review <pr> --approve --body "LGTM: <一句話理由>（Kanban <task-id>）"
```
```
kanban_complete(summary="通過：<理由>", metadata={"published_pr": "<PR URL>", "pr_url": "<PR URL>", "head_sha": "...", "verdict": "approved", "tests": [...],
                "acceptance": {"AC-1": "<證據：journey/截圖/run>"}, "probes": ["<一次性檢查> → <結果>"]})
```
卡片有 `completion_contract` 時，Hermes 會在這一步核對 PR exact head 的 required check（`devflow-ci`）：回 `pending` 就 `gh pr checks <pr> --watch`、`kanban_heartbeat` 後再 complete；回 `failure` 改走退件。
**退件**
```bash
gh pr review <pr> --request-changes --body "<同下列清單>"
```
```
kanban_request_changes(reason="1. <檔案:行> <問題> → <要怎麼改>\n2. ...")
```
退件理由要具體到 developer 不用再問就能改。沒有次數上限；同一問題第二次退件時，說明為什麼前一次修正沒解決，並給更直接的做法。
