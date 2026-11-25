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
      ├─ verify: .devflow/up.sh（compose 起服務；DEVFLOW_SERVICE 空 = 不起服務）→ .devflow/verify.sh（全部 Given/When/Then 情境；UI/API 用 Playwright 容器）
      │          → acceptance.py gate（Covers: 都在且通過、不得 skip）→ artifact devflow-evidence-pr-<head sha>-<attempt>
      │          失敗時 capture.sh → artifact devflow-diagnostics-pr-<sha>（7 天）
      ├─ verify-base: .devflow/verify-base.sh（PR base + head 的 .devflow/，只跑 Covers:，必須在 When/Then 紅燈）→ artifact devflow-evidence-base-<head sha>-<attempt>
      └─ devflow-ci: 彙總（required check / Hermes contract）
main push ──deploy.yml (runs-on: [self-hosted, devflow] = Oracle VM 主機 devflow 使用者, rootless Docker)
            └─ default branch tip only：不是 default branch 最新 commit 就在寫入秘密前失敗（擋分支 dispatch 與重跑舊 run 降版）
            └─ .devflow/deploy.sh: compose build → up -d → health → verify.sh DEVFLOW_SMOKE=1（只跑唯讀 [smoke] 情境）
               → up / health / verify 失敗（含缺 verify.sh）：先 capture.sh 採證，再自動回滾到上一個成功的 release（確切映像 ID + 當時的 compose/.env/config.env，原子切換；不看 :live tag；資料不回復）；build 失敗不動執行中版本
               → artifact devflow-evidence-deploy-<sha>-<attempt>、devflow-diagnostics-deploy-<sha>（7 天）
workflow_dispatch ──ci.yml --ref <default>：整合驗收，在部署的 SHA 跑全部情境（一次性環境）→ artifact devflow-evidence-integration-<sha>-<attempt>
workflow_dispatch ──repro.yml (GitHub-hosted)：只跑指定情境、全程 trace、採證 → artifact devflow-repro-<run>（developer 除錯用）
workflow_dispatch ──operate.yml (self-hosted, default branch tip only)：`devflow operate` 對正式服務跑一個審查過的 UI 操作腳本 → artifact devflow-operate-<run>-<attempt>（§5）
```
- 每個 repo 一個 runner（`devflow-runner@<owner>__<repo>`），由 `devflow runner request` 透過主機的 path unit 自動註冊。
- 服務只綁 `127.0.0.1:<DEVFLOW_PORT>`；對外公開（Cloudflare 等）需使用者另外決定。
- App 秘密：GitHub Secret `DEVFLOW_APP_ENV`（dotenv 內容）→ 部署時寫成 `deploy/.env`。

## 2. repo 契約（`/opt/data/devflow/templates/repo/`）
| 檔案 | 用途 |
|---|---|
| `.devflow/config.env` | `DEVFLOW_APP`、`DEVFLOW_PORT`（主機埠）、`DEVFLOW_APP_PORT`（容器埠）、`DEVFLOW_SERVICE`（compose 服務名）、`DEVFLOW_HEALTH_PATH`、`DEVFLOW_DEPLOY` |
| `.devflow/test.sh` | 測試入口（CI 與 Bot 共用） |
| `.devflow/verify.sh` | 驗收情境（shell `scenario`：cli/lib；有 `.devflow/ui` 就接著跑 Playwright 情境），PR CI、整合驗收、部署後（smoke）都跑；證據與 `acceptance.json` 寫到 `.devflow/evidence/` |
| `.devflow/acceptance.py` | 情境事件 → `acceptance.json`／`acceptance-matrix.md`；`gate`（head）與 `classify-base`（base 紅綠判定） |
| `.devflow/verify-base.sh` | PR 的 base 紅綠證明（ci.yml `verify-base` job） |
| `.devflow/up.sh` | CI 用：以 `deploy/compose.yml` 起服務並提供 verify 環境 |
| `.devflow/playwright.sh` + `.devflow/ui/`（`--ui`） | UI／API 情境：在 Playwright 官方映像內跑 `ac.ts` 的 `scenario()`，`ui` 情境截圖到 `shots/` |
| `.devflow/operate.sh` + `.devflow/ui/ops/<op>.spec.ts` | Hermes 自己操作正式服務的畫面（`operate.yml`，§5）；範例 `ops/look.spec.ts`（唯讀：開一頁、截圖、回報文字） |
| `.devflow/deploy.sh` | 部署 + 健康檢查 + verify + 失敗採證 + 回滾（通常不需改） |
| `.devflow/capture.sh`、`.devflow/redact.py` | 唯讀採證：容器清單、`State`（退出碼、OOM、健康、重啟次數）、限時 log、健康探測、主機磁碟/記憶體；以 `deploy/.env` 的值與常見樣式遮罩秘密，遮罩失敗就刪 log |
| `deploy/compose.yml` | 服務定義；`image: ${DEVFLOW_IMAGE}` + `build` |
| `.devflow/check.sh`、`check.py`、`rules/*.toml`、`lessons/` | 必過的規則與教訓檢查（CI `check` job，在 `devflow-ci` 的 needs）：每條規則先用自己的錯誤範例自測，再掃描追蹤中的檔案；豁免必須引用存在的教訓 |
| `.github/workflows/ci.yml`、`deploy.yml`、`repro.yml`、`operate.yml`、`quality.yml` | CI（`test`／`check`／`verify`／`verify-base` → `devflow-ci`；`workflow_dispatch` = 整合驗收）/ CD（smoke，都上傳證據 artifact）/ 除錯重現 / 正式服務 UI 操作 / ub-mode 指標（不擋） |

在 worktree 套用：`devflow contract <slug> .`（只補缺的檔，不覆蓋）；若要強制對齊或還原公版交付腳本（`.devflow/deploy.sh`、`capture.sh`、`redact.py`、`.github/workflows/deploy.yml`）執行 `devflow contract <slug> . --sync-standard`。所有 repo 一律遵循公版 `deploy.sh`（build → up → health → smoke → 失敗採證 → 精確 release 回滾），禁止用自寫腳本取代；若建置映像需自備 build contexts（例如離線 npm/wheels），只放在可執行的 `.devflow/deploy-build.sh`（公版 `deploy.sh` 會自動呼叫並保留完整輸出），需保留手動觸發部署設 `DEVFLOW_DEPLOY_MANUAL_ONLY=true`，需隨 release 保存的檔案（如 seccomp JSON）寫在 `DEVFLOW_RELEASE_FILES`。有服務的專案要有 `Dockerfile`；沒有服務設 `DEVFLOW_SERVICE=`；不部署設 `DEVFLOW_DEPLOY=false`。

## 3. 接入 repo（`[OPS]` 卡，或 Hermes 直接要求）
1. `devflow repo add OWNER/REPO`（若尚未）：建 board、clone、設定 squash/自動刪分支、讓 builder 與 reviewer 都有 push 權限、分配埠、自動申請 self-hosted runner。
2. 在 worktree：`devflow contract <slug> .`，依專案調整 `test.sh`、`Dockerfile`、`compose.yml`（容器埠、volume）。既有 repo 第一次接入規則檢查：`bash .devflow/check.sh --write-baseline` 把現有違規記成舊債（之後只擋新增），commit `.devflow/rules/baseline.json`；不要用 baseline 收新寫的程式。專案自己的模組邊界規則照 `.devflow/rules/*.toml` 的格式加（附 `expect = "fail"` 與 `"pass"` 範例）。改部署相關腳本時，重跑一次或中途失敗後再跑都要得到同樣狀態（`pstack:principle-make-operations-idempotent`）。
3. `devflow repo doctor <slug>` 讀回接入結果（`repo add` 的輸出也有同一份 `deploy`）：每個失敗項目照 `detail` 的修法處理；`contract-files`、`port` 由這張卡的 PR 修；`ci-required` 在契約合併後由 `[SHIP]` 跑 `devflow repo protect`；`runner-online` 沒有 runner → `devflow runner request <repo>`；`runner-request` 失敗（主機沒處理申請）→ `kanban_block(kind="capability")`，reason 第一行用 `問題：<repo> 的 runner 申請主機沒處理，要由你在 VM 檢查 devflow-runner-requests 服務後回覆嗎？選項：A 我檢查後回覆 / B 先不部署（建議 A）`。部署通道只有共用 runner + 契約 `deploy.yml`，不提出其他方式。
4. 走 PR（同 `devflow-develop` §5–6，`reviewer="reviewer"`）。`[OPS]` 卡只用 `kanban_request_review` 交出，不自己 `kanban_complete`：「本卡完成」的完成條件是送審的條件，審查在這張卡的 review 欄；`[SHIP]` 只合併已核准的 PR，不審查、不另建審查卡。

## 4. 出貨（`[SHIP]` 卡，workspace = 主 checkout）
1. 從 parent 卡 metadata 取 `pr_url`／`pr_number`；body 找 `EPIC: t_xxx`。parent 有 `[ADV]` 且其 metadata `verdict` 是 `revise` → 不 merge，`kanban_complete(summary="未出貨：advisor 建議調整，見 <ADV> / <ADJ>", metadata={"shipped": false})`，由 orchestrator 依 `[ADJ]` 處理。沒有 PR parent 的 `[SHIP] 整合驗收` 卡：直接做第 5、6 步。
2. `gh pr view N --json state,mergeable,headRefOid,statusCheckRollup,mergeCommit`
   - 已 merged → 跳到 4。
   - `devflow-ci` 紅或有衝突 → 見 §6。`devflow-judgment` 不是 success 不算 CI 紅：直接做第 3 步。
3. `devflow ship merge <slug> N`（不要直接 `gh pr merge`）。它先確認 reviewer 帳號最新的有效 review 是 APPROVED 且核准的就是目前 head，再重新判斷 PR 目前的 head + 標題 + 說明（review 後說明被改也擋得到），寫入 `devflow-judgment`，然後依 `.devflow/config.env` 的 `DEVFLOW_MERGE_METHOD`（`squash`／`merge`／`rebase`，預設 `squash`）合併那個 head。結束碼：
   - `0`：已合併，輸出 `merged_sha` 與 `merge_method`。
   - `3`：有疑慮，等 `[ADV]` advisor 結論（Decisions API 無法判斷時也是這樣：advisor 代替 Decisions 判斷）。看輸出的 `wait`：`dependency` → `kanban_link(parent_id="<輸出的 advisor>", child_id="<本卡>")`，再 `kanban_block(kind="dependency", reason="devflow ship merge：等 advisor 判斷 PR #N")`，advisor 完成後本卡自動回來，從這一步重跑；`retry` → tick 還沒建 `[ADV]`：`kanban_heartbeat`，依 `roles.json` `policy.advisor_retry_seconds`/`advisor_max_retries`（預設每 30 秒、最多 6 次）重跑，仍是 `retry` 才 `kanban_block(kind="needs_input", reason=...)`。
   - `4`：advisor 回 `revise`（或沒寫 verdict）→ 照第 1 步「未出貨」完成本卡。
   - `5`：共用部署通道沒就緒（輸出 `deploy_channel` 列出失敗項目與修法）。先照修法做一次：`runner-online` 沒有 runner → `devflow runner request <repo>`，2 分鐘後重跑；`fork-approval` → `devflow repo add <repo>`（冪等）後重跑。仍是 `5`，或是 `runner-request`（主機沒處理申請，容器內修不了）→ `kanban_block(kind="capability", reason=<輸出的 question 原文>)`，等使用者回覆後從這一步重跑。不要自己設計專用帳號、獨立 Docker、job 前准入或其他部署方式，也不要開 `[ADJ]`。
   - `6`：PR 改動了受保護檔案且尚未取得本次 exact-head 人類核准（輸出 `needs_human_approval=true`、`question`、`protected_changes`、`head_sha`）。直接 `kanban_block(kind="needs_input", reason=<輸出的 question 原文>)`；只有使用者明確確認核准時才由 Hermes 聊天執行 `devflow approve <slug> <pr> --head <sha>`（或於 EPIC `## 使用者決定` 寫 `- 核准保護檔變更：PR #<N> <sha>`）再重跑；若使用者拒絕則照第 1 步以「未出貨」完成並退回修改。
   - `7`：所屬 EPIC 正等待使用者定錨計畫風險（輸出 `plan_risk_blocked=true`、`epic`、`reason`）。不要另提受保護檔核准問題，直接 `kanban_block(kind="dependency", reason="等待 <epic> 計畫風險由使用者定錨")`，待使用者回覆 EPIC 後由 tick 解鎖再重跑。
   - `1`：其他錯誤（PR 在判斷後被修改、merge 被拒）→ 看訊息；PR 被修改就直接重跑。訊息是 reviewer 沒核准目前 head（沒核准、核准後退件或被撤銷、只核准過舊 head）→ 不要自己核准或合併，`kanban_complete(summary="未出貨：reviewer 未核准 PR #N 目前的 head <sha 前 7 碼>", metadata={"shipped": false})`，由 orchestrator 安排重新審查。
4. 有 `deploy.yml` 且 `DEVFLOW_DEPLOY` 不是 false：追部署（第 2 步已 merged 而跳過第 3 步時，先 `devflow repo doctor <slug>`，`runner-online`／`runner-request` 沒過照第 3 步 `5` 處理）：
   ```bash
   gh run list --workflow deploy.yml --commit <sha> --json databaseId,status,conclusion,url   # 剛 merge 可能要等幾秒才出現
   gh run watch <id> --exit-status
   ```
   等待期間偶爾 `kanban_heartbeat`。部署 run 內含部署後 `verify.sh`（只跑 `[smoke]`）；失敗會自動回滾（照 §6）。
5. 整合驗收（每次出貨都做；不部署的 repo 用合併後的 SHA）：
   ```bash
   gh workflow run ci.yml -R <OWNER/REPO> --ref <default>
   gh run list -R <OWNER/REPO> --workflow ci.yml --event workflow_dispatch -L 1 --json databaseId,headSha,status,url
   gh run watch <id> -R <OWNER/REPO> --exit-status
   ```
   `headSha` 必須等於 default branch 最新 commit，而且那個 commit 最新的部署 run 成功（`gh run list --workflow deploy.yml --commit <headSha> -L 1 --json conclusion`）；不同（期間又有人合併）就等那次部署完成後再觸發一次。最終驗收只認 default branch 最新 commit 的部署：它失敗時，較舊的成功部署不算。整合 run 失敗 = 合併後有情境壞掉：照 §6 第一列建修正卡，body 附整合 run URL 與失敗情境 id。
6. `devflow repo sync <slug>`。`devflow ship merge` 合併後會在 `contract=no` 時自動跑 `repo protect`（輸出 `protect`）：default branch 已有 `devflow-ci` 就設成 required check，之後的 PR 卡改用 contract；`contract_note` 是 GitHub Free private repo 不支援時，維持 local-only，不要處理；其他 `contract_note` 寫進 summary `followups`。
7. `kanban_complete(summary="已合併並部署 <sha 前 7 碼>，部署後 smoke 與整合驗收通過", metadata={"merged_sha", "deploy_run_id", "deploy_run_url", "integration_run_url", "verify": "<smoke 與整合 run 各情境結果>", "evidence_artifact": "devflow-evidence-deploy-<sha>-<attempt>"})`。summary 裡的數字（耗時、回應時間、容量）要說明怎麼量的、和什麼比（`pstack:principle-explain-the-number`）。

## 5. 正式服務的 UI 操作由你做，不交給使用者
已部署服務的管理畫面（登入、建立或選擇資料、按下按鈕、讀出 id）你自己操作，不要請使用者開瀏覽器做。你的容器連不到服務（每個 rootless Docker 都是 `--disable-host-loopback`，`curl 127.0.0.1:<port>` 會失敗），也沒有可用的雲端瀏覽器；能連到服務的只有 VM 上的 devflow runner，所以操作都經 `operate.yml`：
```bash
devflow operate <slug> look --arg path=/login     # 唯讀看一頁：result.text + 截圖
devflow operate <slug> <op> --arg key=value       # repo 裡的 .devflow/ui/ops/<op>.spec.ts
```
- 輸出 JSON：`conclusion`、`result`（腳本寫的結果）、`images`（截圖路徑）、`failures`、`run`。用 `vision_analyze` 看截圖確認畫面真的是預期結果，不只看 `conclusion`。結束碼非 0 = 操作失敗：看 `failures` 與截圖。
- 只跑 default branch 最新 commit 上**已存在**的腳本。`[SHIP]` 卡的職責是合併、部署與執行既有正式驗證，**禁止在 `[SHIP]` 為了缺少操作腳本或新功能而自己連續建 `[DEV]`／`[OPS]` 卡串在自己上游**（避免打地鼠式無限開 PR）：若規劃時漏了必要的 `ops/<op>.spec.ts` 或功能，在 summary `followups` 寫明缺哪支腳本並完成目前已部署範圍的交接（或由 orchestrator 在規劃／調整階段統一排卡）。
- 秘密（管理密碼等）在 repo secret `DEVFLOW_OPS_ENV`（dotenv）。先用 `gh secret list -R <repo>` 看名稱（不讀值）；沒有 → `kanban_block(kind="capability")`，第一行 `問題：請你在 GitHub 為 <repo> 設 secret DEVFLOW_OPS_ENV（一行 ADMIN_PASSWORD=<管理密碼>），做完回覆「完成」？選項：A 已完成 / B 暫緩（建議 A）`（使用者自己動手的題型，見 `devflow` 推進規則），不要在聊天收密碼。重跑後第一件事重查名稱；仍沒有就照實說明後再 block，題目寫明「上次回覆後仍未看到 DEVFLOW_OPS_ENV」。`--arg` 會出現在 run 的 inputs，不放秘密。
- 會寫入正式資料的操作，照卡片與 EPIC 的授權做；不可回復的操作（刪資料等）仍先 `needs_input`。
- 只有使用者本人才做得到的事才問他：本人在場、說話或聽到、明確同意、他自己帳號的登入或 MFA、只有他有的資料（例如會議要排在哪一天）。問的時候說清楚其餘步驟由你做。

## 6. 失敗處理（程式問題改建卡；只有人能修的主機或權限問題才 block）
| 狀況 | 做法 |
|---|---|
| CI 紅（程式碼）、merge 衝突、部署失敗且是程式碼問題 | 建 `[DEV] 修正 PR #N：<原因>`（assignee developer、`workspace_kind="worktree"`、`workspace_path`=原 DEV 卡的 worktree、`completion_contract` 照 `devflow repo list`、body 附錯誤摘要與 run URL、`EPIC: ...`）→ 建新 `[SHIP]`（parent = 修正卡）→ `kanban_link(parent_id=<新 SHIP>, child_id=<EPIC>)` → 本卡 `kanban_complete(summary="未出貨：<原因>；改由 t_x → t_y", created_cards=[...])`。注意：不要把新修正卡 link 回原本已開始執行的 `[SHIP]` 反覆重跑同一張 `[SHIP]`（同一張未完成 `[SHIP]` 開始執行後事後追加的 `[DEV]`/`[OPS]` 補丁卡上限為 2 張，超限時 `check plan` 會強制交 orchestrator 向使用者確認收斂） |
| runner offline / 主機 Docker 問題 | `devflow repo doctor <slug>` 看 `runner-online`／`runner-request` 的 `detail`；沒有 runner 就 `devflow runner request`。修好後部署最新版：`gh workflow run deploy.yml --ref <default>`（不要 `gh run rerun` 舊 run：期間有新合併時，它在 `default branch tip only` 失敗，不會部署）。容器內修不了（主機沒處理申請、runner 服務停了）→ `kanban_block(kind="capability")`，問題寫成誰在 VM 做什麼；不建卡、不設計其他部署方式 |
| 部署 run 在 `default branch tip only` 失敗 | 不是程式問題：這個 SHA 已不是 default branch 最新版。追最新 commit 的部署 run（沒有就用上一列的 `gh workflow run`）；要回到舊版用 revert PR |
| 部署 health、verify 或 up 失敗 | deploy.sh 已先採證再回滾。`gh run view <id> --log-failed` 看哪一步、哪個情境；`gh run download <id> -n devflow-diagnostics-deploy-<sha> -D /tmp/diag-<id>` 讀 `candidate/`（失敗版本的 `state.jsonl`、`logs/`、`health.json`、`host.txt`）與 `rollback/`（回滾後狀態）。判定是程式問題 → 照第一列建修正卡，body 附 run URL 與關鍵證據（不貼整段 log）；主機資源或 runner 問題 → 照第二列 |
| build 失敗 | 執行中版本不受影響（`build-failed/` 記錄當下狀態）；看 `--log-failed` 的建置輸出，照第一列處理。容器建置指令回報未知子命令或連不到 daemon，多半是主機缺元件 → 照第二列 |
| log 看不到錯誤原文（repo 自訂的部署腳本只留錯誤代碼或位元組數） | 不要連續開「補診斷」PR 輪。用 exit code 與失敗步驟縮小範圍；能判定是主機問題 → 照第二列 block，問題寫成「請在主機以 runner 帳號執行 <唯讀指令>，回報輸出」；是 repo 問題 → 一張小 `[DEV]`：失敗時把該步驟的 stderr 尾段（遮秘密後）印進 log，同時改回 devflow 範本 `deploy.sh` 的採證方式 |
| 需要使用者決定（公開網域、付費、刪資料） | 才 `kanban_block(kind="needs_input")` |
| 同一類失敗第二次出現（不同卡、同一原因） | 除了照上面處理，把教訓放進結構而不是只寫提醒（`pstack:principle-encode-lessons-in-structure`）：該 repo 的問題 → 修正卡改 `.devflow/` 腳本或 CI；所有 repo 都會遇到 → summary `followups` 寫明要改 devflow 範本或 `devflow` CLI 的哪裡 |

## 7. 不做
- 不在 Hermes 容器內跑 Docker 或 ssh 主機；主機操作只透過 Actions runner 與 `devflow runner request`。
- 不把秘密寫進 repo、卡片或 log。
