# Hermes / devflow 閉環開發設計缺口

## 結論

目前設計主幹合理：AC、任務依賴、獨立 reviewer、驗收證據與部署恢復已串接。但部分關鍵保證仍依賴 Agent 遵守 skill，尚未成為不可跳過的程式關卡。

**不必推倒七 Bot 架構，也不需要恢復例行人工 approve。優先把「審過、驗過、已恢復」變成程式強制的不變條件。**

## 審查範圍與證據限制

- 基於本地原始碼、設定、skills 與既有測試內容的靜態審查。
- 未執行測試、攻擊驗證或線上操作；未確認 VM 所有檔案與本地完全一致。
- 下述反例均為 **[INFERENCE] 程式路徑推導**，不是已觀察到的線上事故。
- 區分實作缺口、架構限制與既定政策；不把所有改善項目都稱為新 bug。
- 下方「實作狀態」記錄已修補項目；各 GAP 原文保留審查當時的分析。
- 修補後的驗收案例是建議測試，不是本次已執行的檢查。

### 優先序

- **P1**：可能錯誤放行、破壞權限邊界、交付錯誤版本，或使流程失去恢復能力。
- **P2**：主要影響無人運轉率與外部故障恢復。

## 實作狀態（2026-11-17）

| GAP | 必要性判定 | 狀態 | 實作與驗證 |
|---|---|---|---|
| 01 | 必要 | **已修** | `host/runner-host.sh` 讀請求、寫狀態都降成資料目錄擁有者（`setpriv`），root 只註冊 runner。`devflow/tests/runner_host.sh` 在一次性 root 容器驗證；舊版會把 symlink 指向的 root 目錄改成 1000:1000 |
| 02 | 建議（P2） | **已修** | `devflow ship merge` 要求 reviewer 帳號的最新有效 review（APPROVED／CHANGES_REQUESTED／DISMISSED，COMMENTED 不算）是 APPROVED 且 `commit_id` 等於目前 head；review skill 改為帶 `commit_id` 核准。單元測試涵蓋：同 head 先核准後退件、核准被撤銷、只核准舊 head、只有其他帳號核准、只有留言 |
| 03 | 必要（部分） | **已修** | `devflow-ops` 區塊第一個失敗就停止並列出未執行操作；`resume` 取消指派後失敗會還原原 assignee（review 欄除外）；tick 狀態檔原子寫入。未做完整 workflow journal |
| 04 | 必要 | **已修** | 不可回復風險的疑慮按每張待出貨 `[SHIP]` 記錄；新 `[SHIP]` 會建新 `[ADV]`（idempotency key 含 SHIP）並 link；link 失敗不再被忽略 |
| 05 | 必要 | **已修** | 範本 `deploy.yml` 在寫入秘密前要求 `GITHUB_SHA` 等於 default branch 最新 commit；分支 dispatch 與重跑舊 run 都拒絕 |
| 06 | 驗收部分必要；回滾建議 | **已修** | 驗收只認 default branch 最新 commit 最新一次部署 run（push 或 dispatch），失敗、執行中、沒有都 FAIL。`deploy.sh` 成功時把確切映像 ID 與 compose、`.env`、`config.env` 整組寫進新的 release 目錄，再原子切換 symlink；回滾只讀這份 release（重新標成 `:live`），不看 `:live` tag，所以 tag 失敗或中斷不會造成新 compose 配舊映像；採證失敗不阻止回滾。部署情境 5、6 證明：compose 改壞時回到 v1；release 發布後 `:live` 落後時回到 v6 |
| 07 | 暫緩 | 未做 | 先驗候選版本與不可變 image 成本高 |
| 08 | 暫緩 | 未做 | 需要 Hermes 完成入口能力，目前原則不改 Hermes |
| 09 | 條件性 | 未做 | 開放不可信 repo 或貢獻者前必須處理 |
| 10 | 暫緩 | 未做 | 目前代價是手動 resume，不會錯誤放行 |

### 對原分析的更正

- GAP-01 漏列更直接的破口：舊版 `runner-host.sh:10-12` 以 root 對 controller 可控制的 `status` 路徑執行 `mkdir -p` 與 `chown`，`status` 被換成 symlink 時會改掉目標目錄的 owner。
- GAP-02 風險偏高：看板依賴已強制 review 完成才進 `[SHIP]`；實際缺口是核准後 head 或 review 狀態改變的窗口。

### 升級注意

- 既有 repo 要用 `devflow contract` 合併新的 `deploy.yml`、`deploy.sh`，主機要用 `deploy-kit.sh --host` 安裝新的 `runner-host.sh`。
- 升級後第一次失敗回滾時，若還沒有成功部署過新版 `deploy.sh`，沒有 release 存檔，會退回只換映像的舊行為。
- 正在等待的 EPIC 若已有不可回復風險疑慮，升級後下一次計畫檢查會再建一張 `[ADV]`（advised 記錄格式改為按 SHIP 記錄）。
- 直接 `gh pr merge` 不檢查 reviewer 核准；必須經 `devflow ship merge`。

## GAP-01｜Root runner 橋接器可能跟隨 controller 建立的 symlink

**優先序：P1；類型：權限邊界實作缺口。**

### 證據

- `host/runner-host.sh:21–27`：以可預測的 `.key.tmp` 寫入狀態，接著執行 `chown`、`chmod` 與 `mv`。
- `host/runner-host.sh:49`：repo/key 不符時，也會在 GitHub 註冊之前呼叫狀態寫入。
- `host/setup-host.sh:55–57`：狀態目錄由 Hermes data owner 持有。
- `host/setup-host.sh:85–93`：處理請求的 systemd service 以 root 執行。

### 失效情境與影響

[INFERENCE] controller 在可寫狀態目錄建立暫存檔 symlink，指向主機上的 root-owned 檔案；狀態發布時，root 重導向可能截斷目標，後續操作也可能改變其 owner 與 mode。

這不是已知的「共用 rootless Docker」限制，而是 controller 可控制路徑跨到主機 root 寫入的問題。

### 建議方案

1. root 不直接寫入 controller 可控制的目錄，狀態發布改為低權限身分執行。
2. 必須由 root 管理的資料，放在 root-controlled 路徑。
3. 需要跨權限發布時，使用拒絕 symlink 的安全檔案開啟與原子發布；檢查整條路徑的控制權。
4. 不把隨機暫存檔名當成完整修補。

### 驗收案例

- [ ] 在隔離環境，以 root-owned canary 作為 symlink 目標，觸發錯誤狀態發布。
- [ ] canary 的內容、owner、mode 均不改變。
- [ ] 合法 runner 請求仍能發布可讀狀態。

## GAP-02｜產品 PR 未強制要求指定 reviewer 審過目前 head

**優先序：P1；類型：核准與合併關卡缺口。**

### 證據

- `devflow/bin/devflow:362–369`：`protect_repo` 建立的 ruleset 只有 required status checks，沒有必要 PR review 規則。
- `devflow/bin/devflow:1801–1827`：`cmd_ship_merge` 核對 judgment 與 head，但不核對指定 reviewer 的 approval。
- `devflow/skills/devflow-review/SKILL.md:49`：產品 PR 使用 `gh pr review --approve`，沒有像 kit 路徑一樣明確傳入 `commit_id`。
- `devflow/skills/devflow-review/SKILL.md:70`、`review-kit-pr.sh:33–40`：kit 路徑已明確綁定核准 commit，並在合併前核對。

### 失效情境與影響

[INFERENCE] 對依此工具新接入、且沒有額外分支保護的 repo，只要 checks 通過，出貨入口未必需要獨立 reviewer approval。審查期間 head 從 A 變成 B，也存在審 A、卻在核准時指向 B 的窗口。

不能把 kit 的 exact-head 保證推論成產品 repo 也具有同樣保證。

### 建議方案

1. ruleset 要求 PR review，並使舊核准在新提交後失效。
2. reviewer 使用明確 `commit_id` 發布核准。
3. merge 入口核對指定 reviewer 的有效 `APPROVED`，且 `commit_id` 等於目前 head。
4. 保留 `--match-head-commit`；不可只相信卡片 `verdict=approved`。
5. 使用 reviewer Bot 完成核准，不增加例行人類 approval。

### 驗收案例

- [ ] 無核准時拒絕合併。
- [ ] 只有非指定 reviewer 核准時拒絕合併。
- [ ] 只有舊 head 核准時拒絕合併。
- [ ] 審查期間新增 commit，不得將舊審查結果套用到新版本。
- [ ] 指定 reviewer 核准目前 head、其他關卡成功時，可自動合併。

## GAP-03｜恢復操作缺少可重入進度，tick 狀態也不是原子寫入

**優先序：P1；類型：工作流恢復實作缺口。**

### 證據

- `devflow/bin/devflow:984–997`：`apply_comment_ops` 執行前先標記 applied，單一步驟失敗後仍繼續後續操作。
- `devflow/bin/devflow:928–943`：`resume` 不接受沒有 assignee 的卡，但恢復過程會先取消 assignee，再重新指派。
- `devflow/bin/devflow:1014–1016`：完成的卡片執行操作後會加入 settled。
- `devflow/bin/devflow:1039–1065`：hook 與直接 tick 都解析共用 JSON；最後用 `write_text` 直接覆寫。

### 失效情境與影響

[INFERENCE]

- `link 新依賴` 失敗，後續仍執行 `unlink 舊依賴`，可能使任務在前置條件尚未建立時變成可執行。
- 取消 assignee 成功、重新指派失敗，會留下再次 resume 不接受的狀態。
- tick 狀態檔截斷後程序中止，留下不完整 JSON，可能讓全部看板的 tick 工作無法載入狀態。

### 建議方案

1. 相依操作遇錯即停，不能把整個區塊提前標成已完成。
2. 以操作 ID 與步驟索引保存成功進度；失敗步驟保留 pending。
3. `resume` 在取消指派前持久化原 assignee、目標 lane 與恢復階段，重入時繼續未完成步驟。
4. tick 在既有鎖內以暫存檔寫入完整狀態，再 atomic replace；保留最後有效 checkpoint。
5. 不可把 JSON 解析失敗直接當成空狀態，否則可能重播已完成副作用。

### 驗收案例

- [ ] 第一個 link 失敗時，不執行後續 unlink/archive。
- [ ] assign 失敗後，下一輪恢復原 assignee，且只派工一次。
- [ ] 每個副作用後中斷，重入都不破壞依賴或重複操作。
- [ ] 暫存檔寫入中或 replace 前中斷，舊 checkpoint 仍可解析。

## GAP-04｜計畫變更後，舊 Advisor 去重可能漏擋新 SHIP

**優先序：P1；類型：風險關卡與計畫版本綁定缺口。**

### 證據

- `devflow/bin/devflow:824–826`：依 findings 文字過濾已交過 Advisor 的問題。
- `devflow/bin/devflow:789`：ADV idempotency key 由 EPIC 與 findings 組成，不含計畫版本。
- `devflow/bin/devflow:799–802`：只有開 ADV 時，才為當時已有的 SHIP 加上依賴；link 失敗未強制中止。
- `devflow/bin/devflow:1023–1028`：計畫 fingerprint 改變後仍沿用 EPIC 級 advised 集合。

### 失效情境與影響

[INFERENCE] Decisions 持續故障時，先建立 ADV 擋住 SHIP-A；ADV 尚未完成，後續新增 SHIP-B。新計畫產生相同 fallback findings，去重後不再建立或補接依賴，SHIP-B 沒有取得仍應等待的風險關卡。

### 建議方案

1. 分離「避免重複建 ADV」與「同步所有 SHIP 的必要依賴」。
2. Advisor 結論與 idempotency key 綁定穩定的計畫版本。
3. 同版本已有有效 pending ADV 時，仍補接新 SHIP。
4. 實質計畫變更後，不沿用不涵蓋該版本的結論。
5. 必要 link 失敗不得標成已處理。

### 驗收案例

- [ ] Decisions 持續故障，ADV 等待中新增 SHIP-B，下一 tick 必須補上有效 ADV parent。
- [ ] 同版本重跑不重複建 ADV。
- [ ] 舊版本的 ok 不得放行新增的高風險操作。

## GAP-05｜部署只有互斥，缺少分支與版本准入

**優先序：P1；類型：部署控制缺口。**

### 證據

- `devflow/templates/repo/.github/workflows/deploy.yml:3–5`：push 有分支限制，workflow_dispatch 沒有 ref guard。
- `devflow/templates/repo/.github/workflows/deploy.yml:7–23`：concurrency 互斥後，直接 checkout、寫入 secret 並部署。
- `devflow/templates/repo/.devflow/deploy.sh:15–17,93–99`：依 run SHA 部署並更新 `:live`，沒有過期版本檢查。
- `devflow/skills/devflow-devops/SKILL.md:88`：故障修復後可 rerun。

### 失效情境與影響

[INFERENCE]

- 對未合併的 feature branch dispatch 原版 deploy workflow，可繞過正常 PR 出貨順序。
- A 部署失敗，後續 B 成功；再重跑 A，可能把正式服務與 `:live` 降回 A。

`cancel-in-progress: false` 只避免同時執行，不提供版本單調性。

### 建議方案

1. 在取得部署資源與 secret 之前限制受信 ref。
2. 在部署鎖內核對目標版本與目前 release，拒絕過期的一般部署。
3. 刻意 rollback 使用獨立操作與驗證規則，不與普通 rerun 混用。
4. 分支 guard 防誤用；若要防分支自行修改 workflow，仍需限制 runner 的受信 workflow 與權限，見 GAP-09。

### 驗收案例

- [ ] 未合併分支不能取得部署資格。
- [ ] B 成功後 rerun A，在 build/up 前拒絕，B 的 image ID 不變。
- [ ] 正常 Bot-approved 合併仍可自動部署。
- [ ] 明確 rollback 可恢復允許的歷史 release，但不可偽裝成一般部署。

## GAP-06｜回滾不是完整 release，驗收也可能誤用歷史成功

**優先序：P1；類型：部署恢復與現況證明缺口。**

### 證據

- `devflow/templates/repo/.devflow/deploy.sh:19,67–74`：回滾使用當次 compose，只把 image 切回 `:live`。
- `devflow/templates/repo/.github/workflows/deploy.yml:16–21`：當次 `.env` 已被寫入。
- `devflow/bin/devflow:1408–1413`：驗收選取最新成功的 push deploy，沒有以後續失敗或執行中部署否決該歷史結果。

### 失效情境與影響

[INFERENCE] A 正常；B 把 compose command 改成不存在的程式。B 啟動失敗，回滾 A image 仍使用 B 的壞 command，A 也無法啟動。驗收卻可能選用較早 A 的成功 artifacts，忽略後來部署與回滾均失敗的現況。

### 建議方案

1. 成功時保存 release manifest：immutable image ID/digest、compose、設定版本、受保護的 secret-version reference。
2. 失敗時用上一份完整 manifest 恢復，回滾後執行 health 與 smoke。
3. 建立持久化 deployment ledger，記錄 deploying、healthy、failed、rollback_failed，連結 SHA、run ID、attempt 與實際 image 身分。
4. 驗收核對目前 release 與即時健康，不只查歷史綠燈。
5. 採證不能成為 rollback 的必要前置成功條件。
6. 資料 migration 另訂向後相容、備份與還原契約；換 image 不等於還原資料。

不必強迫每個產品新增 `/version`；可由部署端保存並讀回容器 image 身分。

### 驗收案例

- [ ] compose command 變更失敗，仍能恢復 A 的完整服務設定。
- [ ] 具名 volume 的資料在允許的回滾情境下保留。
- [ ] 後續 rollback_failed 時，舊成功 run 不得讓 EPIC PASS。
- [ ] 採證程序不可用時，仍嘗試回滾並明確回報採證缺口。

## GAP-07｜完整整合驗收在部署之後，且部署重新 build

**優先序：P1；類型：交付順序與建置產物一致性缺口。**

### 證據

- `devflow/skills/devflow-devops/SKILL.md:63–80`：merge → deploy → 完整整合驗收。
- `devflow/bin/devflow:368`：未要求嚴格最新 base 的 status checks。
- `devflow/templates/repo/.devflow/deploy.sh:82`：VM 上再次執行 compose build。

### 失效情境與影響

[INFERENCE] A、B 各自 CI 綠，不保證合併後的 A+B 正確。若問題只出現在非 smoke 情境，可能先部署才被完整整合驗收發現。

相同 source SHA 在 CI 與 VM 重新建置，也不保證相同 image，尤其基底映像或依賴可變時。

### 建議方案

1. 在候選合併版本上先跑完整整合驗收。
2. 發布並部署通過驗證的 immutable image digest，不在正式主機重新 build。
3. 可先採每 repo 串行 release lane；不必一開始就引入複雜 merge queue。
4. 部署後仍保留正式唯讀 smoke。

### 驗收案例

- [ ] 兩個單獨通過、組合失敗的 PR，不得讓錯誤組合上線。
- [ ] 被測與實際部署的 image digest 相同。
- [ ] 未通過完整整合驗收的候選版本沒有部署資格。

## GAP-08｜EPIC 完成條件仍主要靠 skill，而非程式強制

**優先序：P1；類型：完成狀態強制契約缺口。**

### 證據

- `devflow/skills/devflow/SKILL.md:64`：completion_contract 只用於 DEV/OPS，其他卡不設。
- `devflow/skills/devflow-orchestrate/SKILL.md:86–87`：orchestrator 可在記錄理由後覆判過嚴的模型結論，再呼叫 complete。

### 失效情境與影響

在目前 repo 的接入層，看不到 EPIC 完成必須附帶有效驗收結果的程式級關卡。

[INFERENCE] Agent 若漏跑驗收、誤讀結果，或將模型覆判擴大使用，可能仍呼叫完成工具。多寫一段「不可跳過」不能補成強制保證。

### 建議方案

1. 驗收產生完成憑證，綁定：
   - EPIC 與 spec hash。
   - PR heads 與 release 身分。
   - evidence 身分與摘要。
   - policy version 與判斷結果。
2. 在完成工具入口驗證憑證，而非只新增一個可選 CLI。
3. 模型覆判交獨立 reviewer/advisor，保留結構化理由；orchestrator 不自我核准。
4. 確定性檢查失敗不可覆判。
5. 若 Hermes 現有擴充點不能拒絕無憑證 complete，需明確決定是否增加最小平台能力；不能宣稱新增 wrapper 就封住舊入口。

### 驗收案例

- [ ] 無憑證或過期憑證時，直接 complete 被拒絕。
- [ ] AC、PR head 或 release 改變後，舊憑證失效。
- [ ] 模型覆判有獨立裁決紀錄，且不能推翻確定性失敗。

## GAP-09｜檢查權、測試執行與憑證尚未形成真正隔離

**優先序：P1，尤其在擴大 repo 或輸入信任範圍前；類型：已知信任限制與具體入口。**

### 證據

- `devflow/skills/devflow-review/SKILL.md:22`：reviewer 在角色環境執行 PR 的 test.sh。
- `devflow/install.py:251–262`：角色環境配置其 GitHub 身分。
- `devflow/bin/devflow:1551`：gate 檔案清單未涵蓋 acceptance.py、verify.sh 等全部驗收程式。
- `devflow/README.md:88,95–99`：已明確說明 PR 可修改自己的驗收、builder 可寫 judgment、runner 共用使用者與 Docker daemon。

### 失效情境與影響

[INFERENCE] 若被審程式能接觸 reviewer 憑證，獨立帳號核准將失去預期隔離。不同 GitHub 帳號不等於不同執行信任域。

證據身分核對能防錯 run、錯 SHA，但不能阻止可修改檢查器的程式偽造自己的結果。

### 建議方案

1. PR 測試在無憑證、暫時性的執行環境運行。
2. review 發布與 judgment 由外部受信程序或最小權限 GitHub App 執行。
3. 驗收核心固定在受保護來源，不由被驗 PR 自行改寫。
4. runner 按 repo 或信任域隔離。
5. 短期補齊 gate 變更清單，但不把路徑清單當成安全邊界。

### 驗收案例

- [ ] PR 測試無法讀取 reviewer/builder token。
- [ ] 一個 repo 的測試或部署無法操作另一個信任域。
- [ ] PR 無法自行發布受信的驗收／judgment 結果。
- [ ] 修改驗收核心必須經過獨立、受保護的升版流程。

## GAP-10｜外部狀態與 Decisions 恢復尚未形成自動重入閉環

**優先序：P2；類型：外部事件刷新缺口與既定人工恢復邊界。**

### 證據

- `devflow/bin/devflow:1041–1044`：hook 以 board mtime 判斷是否啟動 tick。
- `devflow/skills/devflow-orchestrate/SKILL.md:85`：D3 無法判斷時，採 UNJUDGED → needs_input → resume。

### 失效情境與影響

[INFERENCE] GitHub head/body 或 pending judgment 改變，board 沒變，就未必觸發刷新。Decisions API 恢復本身，也不會自動喚醒 D3 驗收。

D3 的人工 resume 是目前明訂政策，不應誤稱程式 bug；但它確實限制無人運轉率。

### 建議方案

1. 沿用 dispatcher tick，加入 pending 外部狀態與 next-check 時間，不必新增 cron。
2. board mtime 只最佳化本地計畫檢查，不作為所有外部狀態刷新的唯一條件。
3. D3 採持久化、有退避的重新判斷，避免重新執行已完成副作用。
4. 同一證據與 policy 使用同一份判斷紀錄；區分傳輸錯誤重試與對有效低分反覆抽樣。
5. 長期無法判斷時交獨立 adjudicator；仍無解才告知人。

### 驗收案例

- [ ] board 不變，但 PR head/body 更新，仍在設定期限內刷新。
- [ ] 只新增 pending judgment，也能自動建立必要 ADV。
- [ ] Decisions 暫時故障後恢復，不需使用者手動 resume。
- [ ] 不因反覆抽樣將相同證據的有效 FAIL 洗成 PASS。

## 修補順序

### 第一階段：封住錯誤放行與權限破口

- GAP-01：root bridge 安全寫入。
- GAP-02：指定 reviewer 的 exact-head approval。
- GAP-05：部署分支與版本准入。

### 第二階段：讓恢復可靠

- GAP-03：操作日誌、可重入恢復與原子 checkpoint。
- GAP-04：計畫版本與 Advisor gate 同步。
- GAP-06：完整 release 回滾與目前部署狀態驗收。

### 第三階段：建立端到端強制契約

- GAP-07：先驗收候選版本，再部署相同產物。
- GAP-08：EPIC 完成憑證與強制完成入口。
- GAP-09：可信檢查、憑證與 runner 隔離。

GAP-09 的測試環境憑證隔離應視風險提前；此順序不是允許在第一、二階段接入不可信程式。

### 第四階段：提高無人運轉率

- GAP-10：外部狀態刷新與 Decisions 自動重入。

## 建議深化的共用模組

| 模組 | 對外保證 | 主要對應缺口 |
|---|---|---|
| Review gate | 只有指定 reviewer 審過的目前 head 可合併 | GAP-02、GAP-09 |
| Release transaction | 部署的是已驗證產物；可識別目前版本並恢復前一份完整 release | GAP-05、GAP-06、GAP-07 |
| Workflow journal | 每個副作用有進度，部分失敗可重入，風險依賴不漏接 | GAP-03、GAP-04、GAP-10 |
| Acceptance gate | 只有規格、版本、證據與判斷都有效時才能完成 EPIC | GAP-08、GAP-09 |

這些是責任與介面的收斂，不代表必須新增四個服務或四個 Agent。

**讓 Agent 決定做法，讓程式強制交付條件。**
