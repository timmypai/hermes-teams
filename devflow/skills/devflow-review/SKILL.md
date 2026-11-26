---
name: devflow-review
description: "Reviewer: independently verify PR scope, correctness, security, exact-head CI and acceptance evidence, then exact-head APPROVE or request_changes."
version: 3.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, review, qa, pr]
    related_skills: [devflow, code-review, diagnosing-bugs]
---

# devflow-review｜獨立審查與 QA

用 reviewer 專用 GitHub 帳號獨立把關；不代寫實作、不合併、不部署。

## 先判別模式（不硬湊無關歷史卡）
- **A｜Kanban EPIC PR**（卡片 body 有 `EPIC: t_xxxx`）：走 §1–§3。
- **B｜直接操作者 PR**（沒有所屬 EPIC）：**嚴禁套用看板上其他 EPIC**；略過 `check acceptance`，直接依 PR 目標、repo 契約、exact-head CI、本機快測與獨立探測審查，並提交 exact-head review。
- **C｜Kit PR**（devflow 本身的 repo）：走 §4。

## 1. 讀
- 模式 A：`kanban_show` 讀驗收、`Allowed-Scope`／`Out-of-Scope`／`Non-Goals`、developer handoff 與前輪退件理由。
- `gh pr view <pr> --json headRefOid,state,mergeable,statusCheckRollup,files` 與 `gh pr diff <pr>`；以最新 `headRefOid` 為準，不 checkout、不 commit、不 reset，測試暫存檔跑完清乾淨。

## 2. 驗
1. **本機針對性測試**：優先跑 `.devflow/config.env` 的 `DEVFLOW_REVIEW_TEST_CMD`，否則只跑受影響模組的單測（不在缺依賴的容器硬跑重型全量測試至逾時）；完整矩陣以 exact-head CI 為準，CI 紅燈就退件（非程式碼的 runner 問題除外）。
2. **獨立探測**：依 AC 或 PR 目標寫一次性腳本驗關鍵行為與邊界，放 `/tmp/review-<task>`，跑完刪掉，結果寫進 `probes`。
3. **自動判斷（模式 A）**：`devflow check pr <slug> <pr>`：
   - `0`：通過（`devflow-judgment` 已 success）。
   - `4`：`deterministic_fail`（越界、檔數超限、偏離公版）或 advisor `revise` → **直接退件**，引用輸出的 `findings`。
   - `3`：有疑慮等 `[ADV]`。`wait=dependency` → `kanban_link(parent_id=<輸出的 advisor>, child_id=<本卡>)` 後 `kanban_block(kind="dependency")`；`wait=retry` → `kanban_heartbeat` 並依 `policy.advisor_retry_seconds`／`advisor_max_retries` 重跑，仍 retry 才 `needs_input`。
   - `1`：PR 在判斷期間被改 → 重跑。
   - 輸出 `review_loop.tripped=true` 時，在退件理由寫明「已達 review 往復熔斷上限，請 orchestrator／architect 先重檢根因或請使用者定錨」。
4. **驗收證據（模式 A）**：`devflow check acceptance <slug> <EPIC> --pr <pr> --review --json`（非 0 就退件，`reasons` 照抄）；再下載 CI 證據確認 Then 斷言查的是具體結果（不是「頁面打開」「回 200」）、截圖符合 AC、`acceptance-base.json` 的 RED 落在目標行為而非環境錯誤。`Already-met`／`Base-unverifiable` 理由要你判斷成立。
5. **審查重點**：範圍對齊（無夾帶重構或無關功能）、正確性與邊界、安全（無秘密進 repo、無對外綁定、部署失敗保留 stderr）、規則與教訓（未擅自放寬 gate、`No-lesson`／`No-proof` 理由成立）、測試測行為不測實作。CI `quality` 報告只供參考、不為其退件。不為風格偏好退件。

## 3. 結論（綁定你審的 commit）
- **通過**：
  ```bash
  gh api -X POST repos/<OWNER/REPO>/pulls/<pr>/reviews -f commit_id=<headRefOid> -f event=APPROVE \
    -f body="LGTM: <一句話理由>（Kanban <task-id>）"
  ```
  再 `kanban_complete(summary="通過：<理由>", metadata={"published_pr": "<PR URL>", "pr_url": "<PR URL>", "head_sha": "...", "verdict": "approved", "tests": [...], "probes": [...]})`。之後有新 commit，`devflow ship merge` 會拒絕合併，需審新 head。
- **退件**：`gh pr review <pr> --request-changes --body "<具體檔案:行與改法>"`，再 `kanban_request_changes(reason="1. <檔案:行> <問題> → <改法>\n2. ...")`。第 2 次退件要說明前一次修正為何沒解根因，並提醒熔斷介入。

## 4. Kit PR（devflow 本身，沒有卡片）
由 `review-kit-pr.sh` 呼叫。1) `gh pr view`／`gh pr diff`；2) 在暫存目錄 checkout 指定 head commit，跑 `cd devflow/tests && python3 -m unittest`（失敗即退件），並對有改的行為寫一次性驗證；`ci` 檢查非 success 也退件；3) 放寬 gate（刪檢查、降門檻、跳過情境、擴大豁免）沒有寫理由與使用者同意就退件；4) 結論帶 `commit_id` 送 `APPROVE` 或 `REQUEST_CHANGES`（head 已變動改送 `REQUEST_CHANGES`），最後一行只輸出 `VERDICT: approved` 或 `VERDICT: changes_requested`，並刪掉暫存目錄。
