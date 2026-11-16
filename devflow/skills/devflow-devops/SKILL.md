---
name: devflow-devops
description: "DevOps: onboard repos (board, contract, runner), ship approved PRs (merge → GitHub Actions deploy to Oracle VM → health → sync), and fix CI/CD."
version: 2.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, devops, cicd, github-actions, docker, deploy]
    related_skills: [devflow, wizard, diagnosing-bugs]
---

# devflow-devops｜接入、出貨、維運

## 1. 架構
```
PR ──ci.yml (GitHub-hosted ubuntu-latest)
      ├─ test:   .devflow/test.sh + docker build
      ├─ verify: .devflow/up.sh（compose 起服務）→ .devflow/verify.sh（AC journeys；UI 用 Playwright 容器）→ artifact devflow-evidence-pr-<sha>
      │          失敗時 capture.sh → artifact devflow-diagnostics-pr-<sha>（7 天）
      └─ devflow-ci: 彙總（required check / Hermes contract）
main push ──deploy.yml (runs-on: [self-hosted, devflow] = Oracle VM 主機 devflow 使用者, rootless Docker)
            └─ .devflow/deploy.sh: compose build → up -d → health → verify.sh（部署後驗收）
               → up / health / verify 失敗：先 capture.sh 採證，再自動回滾到上一版；build 失敗不動執行中版本
               → artifact devflow-evidence-deploy-<sha>、devflow-diagnostics-deploy-<sha>（7 天）
workflow_dispatch ──repro.yml (GitHub-hosted)：只跑指定 AC、全程 trace、採證 → artifact devflow-repro-<run>（developer 除錯用）
```
- 每個 repo 一個 runner（`devflow-runner@<owner>__<repo>`），由 `devflow runner request` 透過主機的 path unit 自動註冊。
- 服務只綁 `127.0.0.1:<DEVFLOW_PORT>`；對外公開（Cloudflare 等）需使用者另外決定。
- App 秘密：GitHub Secret `DEVFLOW_APP_ENV`（dotenv 內容）→ 部署時寫成 `deploy/.env`。

## 2. repo 契約（`/opt/data/devflow/templates/repo/`）
| 檔案 | 用途 |
|---|---|
| `.devflow/config.env` | `DEVFLOW_APP`、`DEVFLOW_PORT`（主機埠）、`DEVFLOW_APP_PORT`（容器埠）、`DEVFLOW_SERVICE`（compose 服務名）、`DEVFLOW_HEALTH_PATH`、`DEVFLOW_DEPLOY` |
| `.devflow/test.sh` | 測試入口（CI 與 Bot 共用） |
| `.devflow/verify.sh` | 驗收 journeys（一條 AC 一個），PR CI 與部署後都跑，證據寫到 `.devflow/evidence/` |
| `.devflow/up.sh` | CI 用：以 `deploy/compose.yml` 起服務並提供 verify 環境 |
| `.devflow/playwright.sh` + `.devflow/ui/`（`--ui`） | UI journeys：在 Playwright 官方映像內跑，截圖 `AC-n.png` |
| `.devflow/deploy.sh` | 部署 + 健康檢查 + verify + 失敗採證 + 回滾（通常不需改） |
| `.devflow/capture.sh`、`.devflow/redact.py` | 唯讀採證：容器清單、`State`（退出碼、OOM、健康、重啟次數）、限時 log、健康探測、主機磁碟/記憶體；以 `deploy/.env` 的值與常見樣式遮罩秘密，遮罩失敗就刪 log |
| `deploy/compose.yml` | 服務定義；`image: ${DEVFLOW_IMAGE}` + `build` |
| `.github/workflows/ci.yml`、`deploy.yml`、`repro.yml` | CI / CD（都上傳證據 artifact）/ 除錯重現 |

在 worktree 套用：`devflow contract <slug> .`（只補缺的檔，不覆蓋；會填好 app 名稱與分配的埠；列出與目前範本不同的檔案，舊 repo 升級時對照範本手動合併，保留專案改動）。專案自己要有 `Dockerfile`。不需要部署的 repo 設 `DEVFLOW_DEPLOY=false`。repo 還沒有可腳本化的驗證方式時，用 `pstack:create-verification-skill` 的方法盤點，產物寫成 `.devflow/verify.sh`／`.devflow/ui/`。

## 3. 接入 repo（`[OPS]` 卡，或 Hermes 直接要求）
1. `devflow repo add OWNER/REPO`（若尚未）：建 board、clone、設定 squash/自動刪分支、讓 builder 與 reviewer 都有 push 權限、分配埠、自動申請 self-hosted runner。
2. 在 worktree：`devflow contract <slug> .`，依專案調整 `test.sh`、`Dockerfile`、`compose.yml`（容器埠、volume）。改部署相關腳本時，重跑一次或中途失敗後再跑都要得到同樣狀態（`pstack:principle-make-operations-idempotent`）。
3. `devflow runner status OWNER/REPO` 應看到 github runner `online`；沒有就 `devflow runner request OWNER/REPO`（約 1 分鐘）。
4. 走 PR（同 `devflow-develop` §5–6，`reviewer="reviewer"`）。

## 4. 出貨（`[SHIP]` 卡，workspace = 主 checkout）
1. 從 parent 卡 metadata 取 `pr_url`／`pr_number`；body 找 `EPIC: t_xxx`。parent 有 `[ADV]` 且其 metadata `verdict` 是 `revise` → 不 merge，`kanban_complete(summary="未出貨：advisor 建議調整，見 <ADV> / <ADJ>", metadata={"shipped": false})`，由 orchestrator 依 `[ADJ]` 處理。
2. `gh pr view N --json state,mergeable,headRefOid,statusCheckRollup,mergeCommit`
   - 已 merged → 跳到 4。
   - CI 紅或有衝突 → 見 §5。
3. `gh pr merge N --squash`；取得 `mergeCommit.oid`。
4. 有 `deploy.yml` 且 `DEVFLOW_DEPLOY` 不是 false：先 `devflow runner status <repo>` 確認 runner online（不在就 `devflow runner request`），再追部署：
   ```bash
   gh run list --workflow deploy.yml --commit <sha> --json databaseId,status,conclusion,url   # 剛 merge 可能要等幾秒才出現
   gh run watch <id> --exit-status
   ```
   等待期間偶爾 `kanban_heartbeat`。部署 run 內含部署後 `verify.sh`；失敗會自動回滾（照 §5）。
5. `devflow repo sync <slug>`；`devflow repo list` 顯示 `contract=no` 時跑 `devflow repo protect <slug>`（default branch 已有 `devflow-ci` 就會設成 required check，之後的 PR 卡改用 contract；GitHub Free 的 private repo 會回報不支援，維持 local-only，不要處理）。
6. `kanban_complete(summary="已合併並部署 <sha 前 7 碼>，部署後 verify 通過", metadata={"merged_sha", "deploy_run_id", "deploy_run_url", "verify": "<verify.sh 各 journey 結果>", "evidence_artifact": "devflow-evidence-deploy-<sha>"})`。summary 裡的數字（耗時、回應時間、容量）要說明怎麼量的、和什麼比（`pstack:principle-explain-the-number`）。

## 5. 失敗處理（不 block，改建卡）
| 狀況 | 做法 |
|---|---|
| CI 紅（程式碼）、merge 衝突、部署失敗且是程式碼問題 | 建 `[DEV] 修正 PR #N：<原因>`（assignee developer、`workspace_kind="worktree"`、`workspace_path`=原 DEV 卡的 worktree、`completion_contract` 照 `devflow repo list`、body 附錯誤摘要與 run URL、`EPIC: ...`）→ 建新 `[SHIP]`（parent = 修正卡）→ `kanban_link(parent_id=<新 SHIP>, child_id=<EPIC>)` → 本卡 `kanban_complete(summary="未出貨：<原因>；改由 t_x → t_y", created_cards=[...])` |
| runner offline / 主機 Docker 問題 | `devflow runner request` 重註冊；`devflow runner status`；修好後 `gh run rerun <id>` |
| 部署 health、verify 或 up 失敗 | deploy.sh 已先採證再回滾。`gh run view <id> --log-failed` 看哪一步、哪條 AC；`gh run download <id> -n devflow-diagnostics-deploy-<sha> -D /tmp/diag-<id>` 讀 `candidate/`（失敗版本的 `state.jsonl`、`logs/`、`health.json`、`host.txt`）與 `rollback/`（回滾後狀態）。判定是程式問題 → 照第一列建修正卡，body 附 run URL 與關鍵證據（不貼整段 log）；主機資源或 runner 問題 → 照第二列 |
| build 失敗 | 執行中版本不受影響（`build-failed/` 記錄當下狀態）；看 `--log-failed` 的建置輸出，照第一列處理 |
| 需要使用者決定（公開網域、付費、刪資料） | 才 `kanban_block(kind="needs_input")` |
| 同一類失敗第二次出現（不同卡、同一原因） | 除了照上面處理，把教訓放進結構而不是只寫提醒（`pstack:principle-encode-lessons-in-structure`）：該 repo 的問題 → 修正卡改 `.devflow/` 腳本或 CI；所有 repo 都會遇到 → summary `followups` 寫明要改 devflow 範本或 `devflow` CLI 的哪裡 |

## 6. 不做
- 不在 Hermes 容器內跑 Docker 或 ssh 主機；主機操作只透過 Actions runner 與 `devflow runner request`。
- 不把秘密寫進 repo、卡片或 log。
