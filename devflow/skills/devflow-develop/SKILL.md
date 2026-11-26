---
name: devflow-develop
description: "Developer (Codex app-server): implement a [DEV]/[OPS] card strictly within Allowed-Scope, prove tests red then green, submit via devflow pr submit, hand off to reviewer."
version: 3.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, developer, codex, pr, ci]
    related_skills: [devflow, tdd, diagnosing-bugs]
---

# devflow-develop｜開發卡（一次做對 SOP）

cwd 是本卡專屬 worktree（分支 `wt/<task-id>`）。沙箱內 `.git` 唯讀：`git commit／push／fetch／rebase`、`gh`、`devflow pr submit` 直接以升權執行；其餘測試與建置留在沙箱內。

## 1. 讀契約、鎖範圍（先定案再改碼）
1. `kanban_show` 讀目標、`覆蓋：AC-n`、`Allowed-Scope:`、`Out-of-Scope:`、`Non-Goals:`、`Verify:`、parent 結論與退件理由；再 `kanban_show(<EPIC>)` 讀 `## 使用者原話`、`## 不能碰` 與對應情境。
2. 動手前確認：預計改哪些檔、全在 `Allowed-Scope`（或測試路徑）內、不碰 `Out-of-Scope` 與未授權保護路徑、不做 `Non-Goals`。
3. 需要時 `git fetch origin && git rebase origin/<default>`。

## 2. 先紅後綠、最小改動
- **驗收情境先寫、先紅**：覆蓋的非 `[manual]` 情境 `<EPIC>/AC-n.Sm`，Given／When／Then 文字與 EPIC **逐字一致**；Given 結尾斷言前置狀態；Then 斷言具體可觀察值；`[smoke]` 只能讀不寫；不得 skip。
  - `[ui]`／`[api]`：寫在 `.devflow/ui/*.spec.ts`（用 `ac.ts` 的 `scenario()`）。
  - `[cli]`／`[lib]`：寫在 `.devflow/verify.sh`（打建出的成品，不直接呼叫原始碼）。
  - 正式服務 UI 操作腳本：`.devflow/ui/ops/<op>.spec.ts`（參數讀 `DEVFLOW_OP_ARGS`、秘密只讀環境變數、結果寫 `DEVFLOW_OP_RESULT`）。
- **修 bug／退件／部署失敗（`fix:`／`revert:` PR）**：先用會失敗的測試重現，只交付證據支持的最小修正；同類錯誤可能再犯時新增 `.devflow/lessons/L-NNNN.toml` 並在 PR 說明寫 `Lesson: L-NNNN`（或寫至少 10 字的 `No-lesson: <理由>`）；舊教訓復發要更新原教訓。
- 新專案或 `[OPS]`：`devflow contract <slug> .`（有畫面加 `--ui`）補契約檔；偏離公版時 `--sync-standard` 還原。

## 3. 單一提交入口（前移卡控）
先做小而有序的英文 conventional commit（`<type>: <摘要> (<task-id>)`），再**一律**用：
```bash
/opt/data/devflow/bin/devflow pr submit <slug> <task-id> --dir . \
  --pr-title "<type>: <摘要>" \
  --pr-body "<白話：做了什麼、怎麼驗證>

Covers: <EPIC>/AC-n.S1
Kanban: <task-id>"
```
- `pr submit` 先跑完整 `preflight`（worktree 乾淨、範圍與檔數、保護檔、公版交付、`Covers:`／`Lesson:` 格式、`check.sh` 與針對性測試）；**任何一項失敗就不 push、不開 PR**，當場修完重跑。
- 推上後 `gh pr checks --watch`（期間偶爾 `kanban_heartbeat`）。整合層問題可用 `gh workflow run repro.yml --ref wt/<task-id> -f only="<情境id>" -f trace=true` 重現；送審前清掉除錯 log 與 `.devflow/repro.env`。
- CI 綠後，有 `Covers:` 情境就再跑 `/opt/data/devflow/bin/devflow check acceptance <slug> <EPIC> --pr <PR號> --review --json`；確定性項目全過（API 暫時不可用的 exit 3 除外）才送審，不要把確定性不符丟給 reviewer 退件。
- `ci.yml` 必須保留彙總 job `devflow-ci`，新 job 要加進它的 `needs`。

## 4. 送審交接
作者**只用 `kanban_request_review`，絕不自己 `kanban_complete`**（自己 complete 的卡不能再回 review 欄，PR 就沒人審）：
```
kanban_request_review(
  reviewer="reviewer",
  summary="<白話做了什麼與測試結果>",
  metadata={"published_pr": "<PR URL>", "pr_url": "<PR URL>", "pr_number": N, "branch": "wt/<id>",
            "head_sha": "<git rev-parse HEAD>", "tests": ["<指令> → pass"], "ci_run": "<CI run URL>",
            "followups": [...]})
```
被退件時在同一個 worktree 與 PR 修正、`pr submit` 後再送審。不 merge、不部署、不 push 到 main；範圍外發現寫進 `followups`。
