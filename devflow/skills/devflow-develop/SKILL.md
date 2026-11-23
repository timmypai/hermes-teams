---
name: devflow-develop
description: "Developer (Codex app-server): implement a [DEV]/[OPS] card in its git worktree, open/update the PR, get CI green, and hand off with kanban_request_review."
version: 2.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, developer, codex, pr, ci]
    related_skills: [devflow, ub-mode, implement, tdd, diagnosing-bugs, pr]
---

# devflow-develop｜開發卡

你的 cwd 是這張卡的 worktree（分支 `wt/<task-id>`），基於建立時的 `origin` 預設分支。寫入只在 worktree 內；網路可用（git push / gh / 套件安裝）。Codex 沙箱內 `.git` 唯讀：`git commit/push/fetch/rebase` 與 `gh` 直接以升權執行（自動核准），其餘指令留在沙箱內。

## 1. 準備
1. `kanban_show`：目標、驗收（「覆蓋：AC-n」）、parent 的 summary/metadata（架構決策、研究結論）、留言；再 `kanban_show(<EPIC>)` 讀這些 AC 的情境（`S<m>` 與 Given/When/Then 原文）。若有 `request_changes` 退件理由，這次就是修正回合：照理由修（含補情境），再走 4–6。
2. `git status && git log --oneline -3`；讀 `AGENTS.md` / README / `.devflow/`。
3. `git fetch origin` 後若 main 已前進且會影響你的改動，`git rebase origin/<default>`。

## 2. 實作
- 寫程式的規則是 Codex skill `ub-mode`（Make it Work → Make it Right → Practice；devflow 的覆寫寫在 Codex `AGENTS.md`）。流程細節可用 `implement`、`tdd`。
- bug 卡（`pstack` bug-fix playbook）：先用一個會失敗的測試或情境重現；用證據二分找原因，不猜；只交付證據支持的最小修正，被證據推翻的改動要撤掉。
- 架構卡有 `adr_markdown` 時放到 `docs/adr/NNNN-<slug>.md` 一起提交。
- 新專案或 `[OPS]` 卡：`devflow contract <slug> .`（有畫面加 `--ui`）補 devflow 契約檔，把 `DEVFLOW_*` 值填好；指令列出「differs from current template」的檔案，對照 `/opt/data/devflow/templates/repo/` 手動合併，保留專案自己的改動。

## 3. 驗收情境（先寫，先紅）
卡片覆蓋的每條 AC，EPIC 裡的每個情境 `S<m>` 都要有一個可執行情境，**在實作前寫好並確認它因為功能還不存在而失敗**。完整 id 是 `<EPIC id>/AC-n.Sm`；Given/When/Then 的文字從 EPIC **逐字複製**（`devflow check acceptance` 會比對，不一致就 FAIL）。EPIC 的情境寫錯或做不到時，不要自己改寫文字：在 handoff `followups` 說明，交 orchestrator 改 EPIC。
- `[ui]`／`[api]`（有服務的 repo）：`devflow contract <slug> . --ui` 建 `.devflow/ui/`，在 `*.spec.ts` 用 `ac.ts` 的 `scenario()`：
  ```ts
  scenario('t_ab12/AC-1.S1', '新增後重新整理仍存在', {
    kind: 'ui',                         // 只讀、不 seed、不新增的情境才加 smoke: true；api 情境用 kind: 'api'（只有 request，不開瀏覽器）
    given: { title: '已登入、清單為空', act: async ({ page }) => { ... }, check: async ({ page }) => { /* 斷言前置狀態 */ } },
    when:  { title: '輸入「買牛奶」並按「新增」', act: async ({ page }) => { ... } },
    then:  { title: '清單只有「買牛奶」，重新整理後仍在', check: async ({ page }) => { /* 斷言結果 */ } },
  });
  ```
  用 role/label/文字定位；`then.check` 斷言畫面或回應上看得到的具體值（只檢查「有標題」「回 200」不算證明 AC）。`ui` 情境結束會自動截圖。
- `[cli]`／`[lib]`（或沒有服務的 repo）：在 `.devflow/verify.sh` 定義 `<前綴>_given`、`_when`、`_then` 三個函式，再 `scenario <id> "<說明>" <前綴> --given "<原文>" --when "<原文>" --then "<原文>" [--smoke]`。打建出來的成品（`docker run "$DEVFLOW_IMAGE" ...` 或安裝後的套件），不要直接呼叫原始碼。
- **Given 一定以斷言前置狀態結尾**（已登入、在正確頁面、seed 已生效）。CI 的 base 判定靠它分辨「功能不存在」（When/Then 失敗 = 紅燈）與「環境壞了」（Given 失敗 = 無效）。
- `[smoke]` 情境只能讀，不能寫資料：部署後會對正式服務跑。
- 情境不得 skip、不得只在本機能過；flaky 視同失敗（`retries: 0`）。
- repo 還沒有任何可腳本化的驗證方式時：`skill_view pstack:create-verification-skill`，照它的方法盤點啟動/操作/證據，但產物寫成 `.devflow/verify.sh` 的 `scenario` 與 `.devflow/ui/`（不是 `.cursor/skills`）。
- 卡片要你寫「操作腳本」（Bot 對正式服務的 UI 操作，例如登入管理頁後建立或選擇資料、讀出 id）：寫在 `.devflow/ui/ops/<op>.spec.ts`，一個檔一個 `test`，形狀照 `ops/look.spec.ts`：參數讀 `DEVFLOW_OP_ARGS`（JSON），秘密讀環境變數（來自 repo secret `DEVFLOW_OPS_ENV`），結果寫 `DEVFLOW_OP_RESULT`（JSON），關鍵畫面截圖到 `/evidence/<名稱>.png`。每一步用 `expect` 斷言畫面真的到了（找不到就失敗，不要默默略過），寫入前先確認不是重複建立（同名資料已存在就選它）。秘密不寫進結果、截圖或 console。操作腳本不是情境：驗收不會跑 `ops/`，它的正確性另外用一個情境證明（在 CI 的一次性服務上跑同一套步驟）。

## 4. 驗證
- 跑 `.devflow/test.sh`（沒有就用 repo 慣用的測試/lint/build）與 `bash .devflow/check.sh`（規則與教訓，CI 的必過 `check` job）。Hermes 容器內沒有 Docker：映像建置、`verify.sh`、base 紅綠證明與部署驗證交給 CI；要先看情境在 base 是否紅燈，push 後用 `repro.yml`（見 5.5）或直接看 PR CI 的 `verify-base`。
- 失敗就修根因，不刪測試、不 skip、不加壓警告註解（這些都由 `check` 擋）。規則確實誤判時，才在該行或上一行加 `devflow: allow <rule-id> L-NNNN`，並在同一個 PR 新增那筆教訓。
- 修 bug、修退件或處理部署失敗時，若同類錯誤可能再發生，新增教訓 `.devflow/lessons/L-NNNN.toml`（`id`、`title`、`event`＝PR／run／卡片連結、`root_cause`、`category` 為 `code-pattern`／`architecture`／`verification`／`process`／`kit` 之一、`[enforcement] kind/ref`）。選最能擋住**這個錯誤的語意**的方式：
  - `rule`：錯誤在**單一行**就看得出來（禁用的呼叫、匯入、註解、設定值）。新增 `.devflow/rules/<id>.toml`，附 `expect = "fail"`（這次的錯誤那一行）與 `"pass"` 範例。規則逐行比對，不能跨行，不懂語意；範例先跑 `bash .devflow/check.sh` 確認自測通過。
  - `test`／`journey`（`ref = "路徑::測試名"`，journey 的測試名用情境完整 id）：要看行為或跨行結構才知道錯（權限、資料流、錯誤處理、時序）。寫回歸測試或驗收情境，例如「沒有權限的使用者呼叫 X 會得到 403」。這類錯誤不要寫成正規表示式規則。
  - `skill-only`：上面都做不到才用，`reason` 寫為何無法自動檢查。
  `check` 會驗證 ref 指到的東西真的存在。PR 上 CI 還會證明**新增的**教訓抓得到這次的錯：`rule` 必須在 PR 改動檔案的修正前版本（base）上報錯；`test` 會連同你的 `.devflow/test.sh` 放到 base 上只跑那個測試檔，那條測試在 base 必須是**斷言失敗**、在 PR 上必須通過（依 JUnit 結果判定；匯入錯誤、例外、skip 都不算證明）。所以回歸測試要從修正前就存在的入口測行為（既有函式、HTTP 端點、CLI），才能在 base 上載入並跑到斷言；修正本身必須新增 API 而無法這樣測時，PR 說明寫 `No-proof: <原因>`。`.devflow/test.sh` 要支援 `DEVFLOW_TEST_ONLY`（只跑這個檔）與 `DEVFLOW_JUNIT`（寫 JUnit XML）；範本的 pytest 分支已支援，其他技術棧照註解補上。`journey` 不做自動證明。

## 5. PR
```bash
git add -A && git commit -m "<type>: <摘要> (<task-id>)"
git push -u origin HEAD
gh pr view --json url,number || gh pr create --base <default> --title "<type>: <摘要>" --body "<做了什麼/怎麼驗證>

Covers: <EPIC>/AC-n.S1, <EPIC>/AC-n.S2
<只有需要時：Already-met: <情境 id> <理由> 或 Base-unverifiable: <情境 id> <理由>>
<只有 fix／revert PR：Lesson: L-NNNN 或 No-lesson: 理由>
Kanban: <task-id>"
gh pr checks --watch          # 等 CI；紅燈 → 修 → push → 再等
```
- PR 標題用 `<type>: <摘要>`。修 bug、修退件、修部署失敗用 `fix:`（撤回用 `revert:`）；CI `check` job 的 lesson gate 要求這類 PR 的說明有一行 `Lesson: L-NNNN`（這個 PR 新增或更新的教訓），或 `No-lesson: <為什麼這個錯誤不會再犯>`（至少 10 字，reviewer 會判斷）。不是 fix 的 PR 不用寫。
- `Covers:` 列出這個 PR 負責的每個情境完整 id（可多行）。CI 的 `verify` 要求這些情境都在報告裡且通過；`verify-base` 在 PR 的 base 上（用你的測試程式）只跑這些情境，每個都必須是**紅燈**：健康檢查與 Given 通過，第一個失敗出現在 When 或 Then。
  - base 上就通過（GREEN）：通常是斷言太弱，先加強 Then；真的修改前就已成立（回歸保護）才寫 `Already-met: <id> <理由>`。
  - base 跑不到 When（INVALID：Given、seed、登入、導航失敗，或 base 沒有服務）：先確認不是情境寫錯；確實因為 base 缺前置條件才寫 `Base-unverifiable: <id> <理由>`。
  - 兩種豁免都會由 `devflow-judgment` 與 reviewer 判斷理由，不成立會退件。
- 引用的教訓在 base 就已存在 = 同一根因又出現，表示原本的擋法沒擋住：在同一個 PR 更新那筆教訓（例如 `skill-only` 改成 `rule`／`test`，`event` 補上這次的連結），否則 lesson gate 失敗。
- 教訓的錯誤是在更早的 PR 修掉的（這個 PR 的 base 已經沒有錯誤程式）時，說明加 `No-proof: <原因與修正 PR>`，跳過證明；reviewer 會判斷。改了 PR 說明會自動重跑 CI。
- 等 CI 期間偶爾 `kanban_heartbeat`。repo 還沒有 CI workflow 時跳過等待，在 handoff 註明。
- `ci.yml` 一定保留彙總 job `devflow-ci`（default branch 的 required check，也是 Hermes PR contract 的判定依據）；新增的 job 要加進它的 `needs`。ub-mode 指標在獨立的 `quality.yml`，不要把它加進 `devflow-ci`。
- CI 綠後看 `quality`（ub-mode 的 Make it Right）：`gh run download <run> -n devflow-quality-<sha> -D /tmp/q-<task>`，讀 `crap.txt`（你改到的函式 CRAP ≥ 10 → 拆或補測試）、`dry.txt`（≥ 0.8 的重複 → 合併）、`mutate.txt`（uncovered 補測試、survivor 補斷言、死碼刪掉）。只處理這次改到的程式；舊程式的數字寫進 `followups`。數字不是門檻，quality workflow 失敗也不擋送審。repo 還沒有 `quality.yml` 就跳過，在 `followups` 寫「升級 devflow 契約（quality）」。
- `crap.txt` 全部 `Cov% 0.0` 或 `mutate.txt` 全是 UNCOVERED，通常是工具找不到這個 repo 的測試：加可執行的 `.devflow/coverage.sh`，用 repo 自己的測試指令產生 LCOV 到 `target/coverage/<語言>/lcov.info`（Go 是 `coverage.out`），`quality.sh` 會改讀它。
- CI 的 `verify`／`verify-base` 失敗：`gh run view <id> --log-failed` 看哪個情境、哪一段；`gh run download <id>` 取證據（`acceptance-matrix.md`、`acceptance.json`、`acceptance-base.json`、截圖 `shots/`、步驟輸出 `logs/`）與 `devflow-diagnostics-pr-<sha>`（容器狀態、退出碼、log）。修到綠。
- CI 失敗修到綠；若是 runner/基礎設施問題（非程式碼），在 handoff 註明並照樣送 review，由 devops 處理。
- **送審前確定性自檢（一次就做對）**：`gh pr checks --watch` 綠燈後、呼叫 `kanban_request_review` 之前，若本卡有 `Covers:` 可執行情境，先跑一次唯讀自檢：
  ```bash
  /opt/data/devflow/bin/devflow check acceptance <slug> <EPIC> --pr <PR號> --review --json
  ```
  確認 `Covers:` 的每個情境 ID 存在於 EPIC、Given/When/Then 步驟文字與 EPIC 一字不差、head `verify` 通過且 `verify-base` 為紅燈（或豁免格式正確）。若自檢非 0（除 Decisions API 暫時無法使用的 `UNJUDGED` exit 3 外），當場在同一回合修好並重跑 CI 後再送審，不要把確定性不符丟給 reviewer 退件。

## 5.5 除錯迴圈（整合層問題、部署後才出現的 bug）
Hermes 容器沒有 Docker，整合層的重現跑在 GitHub-hosted 的 `repro.yml`，不碰正式主機。照 `diagnosing-bugs`：先有會紅的重現，再假設、再修。
1. 先讀現有證據：卡片或 parent 給的 deploy run → `gh run download <id> -n devflow-diagnostics-deploy-<sha> -D /tmp/diag-<task>`（`candidate/logs/`、`state.jsonl` 的 `ExitCode`/`OOMKilled`/`restart_count`、`health.json`）。
2. 能在 worktree 用 `.devflow/test.sh` 這一層重現就在這裡寫失敗的測試（快）；需要整個服務才寫成驗收情境。
3. 整合層重現 / 驗證假設：push 卡片分支後
   ```bash
   gh workflow run repro.yml --ref wt/<task-id> -f only="<EPIC>/AC-2.S1" -f trace=true
   gh run list --workflow repro.yml --branch wt/<task-id> -L 1 --json databaseId,status,url
   gh run watch <id> --exit-status; gh run download <id> -D /tmp/repro-<task>-<id>
   ```
   artifact 有 `evidence/`（情境步驟輸出、截圖、trace.zip、`acceptance.json`）與 `diagnostics/repro/`（容器狀態、log）。一次只改一個變數；暫時的除錯 log 加唯一前綴（如 `[DEBUG-a4f2]`），非秘密的除錯設定（如 `LOG_LEVEL=debug`）放 `.devflow/repro.env`。
4. 送 review 前：移除除錯 log 前綴與 `.devflow/repro.env`，把重現留成正式測試或情境，handoff `log` 寫下被證實的原因。
- repo 沒有 `repro.yml`（或還不在 default branch）→ 只能用一般 CI 迭代，並在 `followups` 寫「升級 devflow 契約（repro）」。
- repro 沒有部署秘密與正式資料；只在正式環境才出現的問題，在 handoff 寫明重現不了的原因與已排除的假設，交 orchestrator。

## 6. 交付
```
kanban_request_review(
  reviewer="reviewer",
  summary="<做了什麼>；測試 <指令> 通過；CI <綠/無>",
  metadata={"published_pr": "<PR URL>", "pr_url": "<PR URL>", "pr_number": N, "branch": "wt/<id>", "head_sha": "<git rev-parse HEAD>",
            "tests": ["<指令> → pass"], "acceptance": {"<EPIC>/AC-1.S1": "CI run <id>：head passed、base RED（When）"},
            "ci_run": "<CI run URL>", "quality": "<CRAP 最高的改動函式/重複/存活 mutant 摘要，或「無 quality job」>",
            "log": ["<做了什麼> — <為什麼>", ...], "followups": [...]})
```
reviewer 會被自動派工。退件時這張卡回到你手上（同一 worktree、同一 PR），修正 → push → 等 CI → 再 `kanban_request_review(reviewer="reviewer", ...)`。
作者只用 `kanban_request_review` 交出這張卡，不呼叫 `kanban_complete`：卡片 `## 本卡完成` 的「完成條件」是送審的條件，審查在這張卡自己的 review 欄，由 reviewer 核准後完成；`[SHIP]` 不審查。自己 complete 的卡不能再回 review 欄，PR 就沒人審、合併不了（teams-transcription t_48d094f6、t_1463bbcb）。

## 不做
不 merge、不部署、不 push 到 main、不改 `.github/workflows` 的部署目標（`[OPS]` 卡除外）。範圍外問題寫進 `followups`。
