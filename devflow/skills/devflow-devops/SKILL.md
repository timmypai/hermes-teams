---
name: devflow-devops
description: "DevOps: onboard repos with the standard delivery contract, ship exact-head approved PRs via devflow ship merge, operate production UI via operate.yml, handle deploy failures."
version: 3.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, devops, cicd, github-actions, docker, deploy]
    related_skills: [devflow, diagnosing-bugs]
---

# devflow-devops｜接入、出貨、正式操作、線上診斷

部署與正式畫面操作一律透過 GitHub Actions self-hosted runner；Oracle 容器即時唯讀診斷只走專用 `/opt/data/devflow/diagnose/oracle-debug` 入口。

## 1. Repo 接入（`[OPS]` 卡）
1. `devflow repo add OWNER/REPO`（若尚未接入）；在 worktree 跑 `devflow contract <slug> .`（有畫面加 `--ui`，還原公版加 `--sync-standard`）。
2. 所有 repo 遵循公版 `.devflow/deploy.sh`、`capture.sh`、`redact.py`、`.github/workflows/deploy.yml`；專案差異只放 `.devflow/config.env`、`Dockerfile`、`deploy/compose.yml`、`.devflow/deploy-build.sh`（只 build `$DEVFLOW_IMAGE`）。不部署設 `DEVFLOW_DEPLOY=false`；merge 不自動部署設 `DEVFLOW_DEPLOY_MANUAL_ONLY=true`。
3. 跑 `devflow repo doctor <slug>` 依結果修正（`contract-files`、`port` 由本卡 PR 修；`runner-request` 主機端失敗就 `kanban_block(kind="capability")`）。
   - 常態／已開放 review：用 `devflow pr submit` 送 PR，再 `kanban_request_review(reviewer="reviewer", ...)`，絕不自己 `kanban_complete`。
   - 若 EPIC 設 `Stop-After: implement`（尚未開放推分支／PR）：commit 並測過後改跑 `devflow checkpoint <slug> <task-id> --dir .`，再用輸出的 `block_reason` 呼叫 `kanban_block(kind="dependency", reason=...)` 等待 `epic advance`。
   - 若 EPIC 設 `Stop-After: ci`（尚未開放 review）：用 `devflow pr submit` 開 PR，待 CI 綠後執行 `devflow checkpoint <slug> <task-id> --dir .` 再 `kanban_block(kind="dependency", reason=...)`（不要 `kanban_request_review`）。

## 2. 出貨（`[SHIP]` 卡，主 checkout）
1. 從 parent 的 metadata 取 `pr_number`，body 找 `EPIC: t_xxx`。parent `[ADV]` 的 `verdict` 是 `revise` → 不合併，`kanban_complete(summary="未出貨：advisor 建議調整", metadata={"shipped": false, "reason": "advisor 建議調整"})`。沒有 PR parent 的 `[SHIP] 整合驗收` 直接做第 4、5 步。
2. `gh pr view N --json state,mergeable,headRefOid,statusCheckRollup,mergeCommit`；已 merged 跳第 4 步；`devflow-ci` 紅或有衝突見 §4。
3. 執行 `devflow ship merge <slug> N`（不要直接 `gh pr merge`），依結束碼：
   - `0`：已合併，記下 `merged_sha`、`deploys`、`deploy_manual_only`。
   - `3`：等 `[ADV]`（`wait=dependency` → `kanban_link(parent_id=<advisor>, child_id=<本卡>)` 後 `kanban_block(kind="dependency")`；`wait=retry` → 依 `policy.advisor_retry_seconds`／`advisor_max_retries` 重跑）。
   - `4`：advisor `revise` 或 `deterministic_fail`（越界、檔數超限、偏離公版）→ 以 `metadata={"shipped": false, "reason": "<advisor 或決定性檢查失敗原因>"}` 完成本卡，交 orchestrator。
   - `5`：共用部署通道未就緒 → 照輸出的修法做一次（`devflow runner request <repo>`／`devflow repo add <repo>`），仍是 5 就 `kanban_block(kind="capability", reason=<輸出的 question 原文>)`；不自創專用帳號、獨立 Docker 或其他部署方式，也不開 `[ADJ]`。
   - `6`：改動受保護檔且缺 exact-head 人類核准（`needs_human_approval=true`）→ `kanban_block(kind="needs_input", reason=<輸出的 question 原文>)`；核准流程見 `skill_view devflow-chat`。
   - `7`：EPIC 正等使用者定錨計畫風險（`plan_risk_blocked=true`）→ `kanban_block(kind="dependency", reason=<輸出的 hold_reason 原文>)`，解鎖後重跑。
   - `1`：PR 被改就重跑；reviewer 沒核准目前 head 就 `kanban_complete(summary="未出貨：reviewer 未核准目前 head", metadata={"shipped": false, "reason": "reviewer 未核准目前 head"})`，由 orchestrator 安排重審。
4. **追部署**（`deploys=true`）：`deploy_manual_only=true` 時不空等 push run，只在 EPIC `## 使用者決定` 明確授權時 `gh workflow run deploy.yml -R <OWNER/REPO> --ref <default>`，否則 `needs_input` 問誰觸發；`false` 時追該 commit 的 run（`gh run list --workflow deploy.yml --commit <sha>` → `gh run watch <id> --exit-status`；失敗會先採證再自動回滾）。
5. **整合驗收與同步**：`gh workflow run ci.yml -R <OWNER/REPO> --ref <default>`，確認 `headSha` 等於 default branch 最新 commit 且該 commit 最新部署成功並整合 run 綠；再 `devflow repo sync <slug>`，最後 `kanban_complete`（metadata 放 `merged_sha`、`deploy_run_url`、`integration_run_url`、`verify`）。

## 3. 正式服務畫面操作
容器連不到主機 `127.0.0.1:<port>`，一律 `devflow operate <slug> <op> --arg k=v`（只跑 default branch 上已存在的腳本），並用 `vision_analyze` 看截圖確認。缺 `DEVFLOW_OPS_ENV` secret 時用「請你設好後回覆『完成』」格式 `capability` block。**禁止在 `[SHIP]` 為補腳本而連開 `[DEV]`／`[OPS]` 串回自己上游**，缺口寫進 `followups` 交 orchestrator。

## 4. 失敗處理
- **程式碼、測試或部署程式問題**：建一張 `[DEV] 修正 PR #N：<白話原因>`（**必設 `assignee="developer"`、`workspace_kind="worktree"`、`workspace_path`=原卡 worktree、`goal_mode=true`、`completion_contract` 依 `devflow repo list`**；body 必含固定 `## 本卡完成`、`覆蓋：AC-n`、沿用原卡的 `Allowed-Scope:` 與 `EPIC: t_xxx`，不把 run URL 或內部技術細節塞進 body，改用 `kanban_comment` 在新卡附錯誤摘要與 run URL）→ 接一張新 `[SHIP]` → `kanban_link(新 SHIP, EPIC)` → 本卡 `kanban_complete(summary="未出貨：<原因>", metadata={"shipped": false, "reason": "<原因>", "created_cards": [...]})`。不要把新卡 link 回已開始執行的原 `[SHIP]`（事後補丁卡上限 2 張）。
- **runner offline、主機元件缺件**：先 `devflow repo doctor`／`devflow runner request`；容器內修不了就 `capability` block，不連開補診斷 PR。
- **`default branch tip only` 失敗**：不是程式問題，追最新 commit 的 deploy run（沒有就 `gh workflow run deploy.yml --ref <default>`），不要 rerun 舊 run。

## 5. Oracle 容器線上除錯
用 terminal 執行 `/opt/data/devflow/diagnose/oracle-debug list` 讀取即時容器清單，依序用 `status <容器>` 確認映像、部署 commit 與健康狀態，再用 `top`、`stats`、`logs` 或 `exec <容器> -- <命令> [參數]` 定位問題（不改動服務狀態或正式資料，修補仍走 CI/CD）。報告附容器名稱、實際版本、命令退出碼與遮罩後證據；正式容器要求短時效授權時向操作者請領 grant。
