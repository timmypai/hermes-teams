# devflow｜Hermes 七 Bot 共用開發流程

適用任何新 repo 或既有 repo。只用 Hermes 原生功能（Kanban、dispatcher、profile、skills、Codex app-server runtime、GPT-Live 語音），不改 Hermes 程式。

## 流程

```mermaid
flowchart TD
  U((使用者)) <-- "GPT-Live 語音/文字<br/>目標 + AC-1..n" --> H[hermes 預設 Bot]
  H -- "[EPIC] 卡" --> O[orchestrator]
  O -- "devflow check plan<br/>(dispatcher tick 自動)" --> C{有疑慮?}
  C -- 是 --> ADV["[ADV] hermes advisor<br/>Opus 5.5"]
  ADV -- revise --> ADJ["[ADJ] orchestrator 調整"]
  O -- 選用 --> R[researcher]
  O -- 選用 --> A[architect]
  R --> D
  A --> D[developer<br/>Codex app-server<br/>先寫情境（紅）+ 實作（綠）]
  D -- "PR + CI 綠<br/>(test + verify + verify-base 紅綠)" --> V[reviewer<br/>Opus 5.5]
  V -- request_changes --> D
  V -- 通過 --> S["devops [SHIP]<br/>merge → Actions deploy<br/>health + smoke → 整合驗收"]
  S --> O2["orchestrator 驗收<br/>devflow check acceptance"]
  O2 -- "全 PASS：完成通知" --> H
  O2 -- FAIL：補卡 --> D
```

- **一個 repo = 一個 Kanban board**；卡片依賴就是流程，dispatcher（default gateway 內）依依賴自動派工。
- 卡片 body（目標/驗收/提示）是規格；`kanban_complete` / `kanban_request_review` 的 summary + metadata 是交接。沒有額外交接文件。
- **驗收貫穿全程**：Hermes 把「怎樣算完成」寫成編號 `AC-n` 並標類型（`[ui]`/`[api]`/`[cli]`/`[lib]`/`[manual]`，語音確認一次）→ orchestrator 把每條展開成 Given/When/Then 情境 `<EPIC>/AC-n.Sm` → developer 先寫可執行情境再實作 → 證據鏈見下方「驗收證據鏈」，全 PASS 才完成 EPIC。
- **計畫檢查**：EPIC 等子卡期間，計畫一有變動（建卡、改卡、`[ADJ]` 調整），dispatcher tick 自動跑 `devflow check plan`：規則（卡型、assignee、workspace、contract、`[SHIP]`、parent 鏈、AC 覆蓋、AC 類型與 Given/When/Then 情境）+ Decisions API 風險分數；有新疑慮才建 `[ADV]` 給 Hermes（Opus 5.5）判斷，同樣的疑慮不重複送。只有「含不可回復操作」時 `[ADV]` 擋 `[SHIP]`，其他情況不擋。
- **看板自動化**：default profile 的 shell hook `on_kanban_dispatch_tick` 在 gateway 內的 dispatcher 每個 tick 觸發 `devflow tick`（board 沒變動就立即結束；有變動才背景執行，不拖慢派工）。它做兩件事：計畫檢查，以及執行 orchestrator 在自己卡上留的 `devflow-ops`（edit / archive / link / unlink / assign / resume）。
- 不設次數/時間卡控：沒有 run budget、沒有輪詢 cron；review 退件不限次數；失敗卡由 Hermes 自動轉 `[ADJ]` 給 orchestrator，只有需要使用者決定的事才問人。檢查工具出錯時只記錄，不擋流程。

## 七個 Bot

| Bot（profile） | 職責 | 卡片 | 主要技能 |
|---|---|---|---|
| hermes（default） | GPT-Live 語音/文字入口、把需求寫成 AC、建 EPIC、回報進度、Advisor（`[ADV]` 卡用 Opus 5.5） | `[ADV]` | devflow-intake；mattpocock grilling/grill-me/to-questionnaire/wait-what/teach/ask-matt；pstack in-your-own-words/bro/ketchup/how/why/sharpen… |
| orchestrator | 拆解（吞吐檢查）、AC 覆蓋、串依賴、等待、逐條驗收、處理失敗 | `[EPIC]` `[ADJ]` | devflow-orchestrate；to-tickets/to-spec/wayfinder；pstack figure-it-out/sequence-verifiable-units/outcome-oriented-execution/encode-lessons-in-structure… |
| researcher | 一手來源、方案比較、PoC | `[RES]` | devflow-research；research/prototype；pstack how/why/explain-the-number… |
| architect | 架構、模組邊界、資料模型、介面、ADR | `[ARCH]` | devflow-architect；codebase-design/domain-modeling/improve-codebase-architecture；pstack architect/statechart/boundary-discipline… |
| developer | Codex app-server 在 worktree 實作、測試、驗收情境、PR、CI | `[DEV]` | Codex skill `ub-mode`（寫程式規則）；devflow-develop；implement/tdd/diagnosing-bugs/pr；pstack prove-it-works/migrate-callers/create-verification-skill… |
| reviewer | 獨立審查（不同 GitHub 帳號、Opus 5.5，Copilot 不可用時退回 Codex）、跑測試、不提交的獨立探測、看證據、approve 或退件 | review 欄 | devflow-review；ub-mode（同一份規則審 Make it Right）；code-review/diagnosing-bugs；pstack interrogate/blast-radius/correct… |
| devops | repo 接入、runner、merge、部署、回滾 | `[OPS]` `[SHIP]` | devflow-devops；wizard/diagnosing-bugs/pr；pstack make-operations-idempotent/build-the-lever/encode-lessons-in-structure… |

pstack 24 條 principle 每條至少分配給一個 Bot（developer 的部分由 Codex 的 ub-mode 承擔）。分配只代表可用；流程中一定要用的，直接寫在 devflow skill 的步驟裡並註明 principle 名：orchestrator（attack-the-premise、sequence-verifiable-units、encode-lessons-in-structure）、researcher（explain-the-number）、architect（model-the-domain、boundary-discipline）、reviewer（minimize-reader-load）、devops（make-operations-idempotent、explain-the-number、encode-lessons-in-structure）。

完整分配見 `roles.json`。pstack 透過 plugin 載入，每個 profile 以 `skills.disabled` 只留分配到的；mattpocock 技能 vendor 在 `vendor/mattpocock-skills`，以 `roles/<role>/` 符號連結經 `skills.external_dirs` 提供。

**ub-mode**（[unclebob/ub-mode](https://github.com/unclebob/ub-mode) @ `3da62bf`，MIT，原文放在 `skills/ub-mode/`）：
- 主要開發在 Codex，所以 install 把它複製到 developer 的 `$CODEX_HOME/skills/ub-mode`（Codex 原生 skill，`skills/list` 可見），並只移走具名項目（`roles.json` `obsolete_skills.codex`）：舊契約的 `seven-pstack-*`、`team-operations`，以及規則已由 ub-mode 負責的 Codex 複本 `test-driven-development`（「一律先寫測試」與 ub-mode「寫不出誠實測試就不寫」衝突）、`pstack/skills/{tdd,principle-fix-root-causes,principle-test-behavior-not-implementation}`。補充而不衝突的 `systematic-debugging`、`principle-prove-it-works` 與其他既有 Codex 技能保留。`codex/AGENTS.md` 要求每次改程式都套用 ub-mode，並覆寫與 devflow 衝突的條目：只 push 卡片分支走 PR（原文是 push 到 main line）、worktree 由 Hermes 管、Keep a Log 寫進 handoff metadata `log`。
- reviewer 用同一份 skill 審 Make it Right。developer Hermes profile 的 pstack 分配移除了 ub-mode 已涵蓋的 principle（laziness、subtract、boundary、idempotent、type-system、fix-root-causes、test-behavior、tdd），避免兩套規則。
- ub-mode 的 Crapper / Dryer / Mutator 是第三方程式，只在獨立的 `quality.yml` workflow（GitHub-hosted、唯讀 token、pinned commit、`continue-on-error`）執行，不在 Hermes 容器執行。它是 repo 契約的預設檔，`devflow contract` 會補進既有 repo；不是 required check，也不在 `devflow-ci` 內，**不擋合併**。結果是 artifact `devflow-quality-<sha>` 與 run summary，供 developer 修正、reviewer 參考。

## CI/CD

| 階段 | 執行位置 | 內容 |
|---|---|---|
| PR CI | GitHub-hosted `ubuntu-latest` | `ci.yml`：`test`（`.devflow/test.sh` + `docker build`）、`check`（`.devflow/check.sh`：規則自測 + 教訓紀錄 + 規則掃描）、`verify`（`.devflow/up.sh` 起服務 → `.devflow/verify.sh` 跑全部情境 → `acceptance.py gate` → artifact `devflow-evidence-pr-<head sha>-<attempt>`，失敗時 `.devflow/capture.sh` → `devflow-diagnostics-pr-<sha>`）、`verify-base`（PR base 紅綠證明 → `devflow-evidence-base-<head sha>-<attempt>`）；彙總 job `devflow-ci`（needs `test`、`check`、`verify`、`verify-base`）是 default branch 的 required check。`workflow_dispatch` 在 default branch = 整合驗收（`devflow-evidence-integration-<sha>-<attempt>`）。`quality.yml`（獨立 workflow，不擋）：`.devflow/quality.sh`（ub-mode 指標）→ artifact `devflow-quality-<sha>` |
| Deploy | Oracle VM 主機 `devflow` 使用者（rootless Docker）上的 self-hosted runner `[self-hosted, devflow]` | `.devflow/deploy.sh`：compose build → up → health → `verify.sh`（`DEVFLOW_SMOKE=1`，只跑唯讀 `[smoke]` 情境；缺 `verify.sh` 視為失敗）；up / health / verify 失敗先採證再回滾，build 失敗不動執行中版本；artifact `devflow-evidence-deploy-<sha>-<attempt>`、`devflow-diagnostics-deploy-<sha>` |
| Repro（除錯） | GitHub-hosted `ubuntu-latest`，`workflow_dispatch` | `repro.yml`：任一分支起服務、只跑指定情境（`DEVFLOW_ONLY=<EPIC>/AC-n[.Sm]`）、全程 trace、採證 → artifact `devflow-repro-<run>`。developer 用它在 Hermes 外重現整合層問題，不碰正式主機、沒有部署秘密 |

**診斷與除錯**：`capture.sh` 只讀（容器清單、`State` 的退出碼/OOM/健康/重啟次數、限時 log、健康探測、主機磁碟/記憶體），不輸出 `Config.Env`；`redact.py` 以 `deploy/.env` 的值與常見憑證樣式遮罩，遮罩失敗就刪 log。遮罩是盡力而為，應用程式仍不應記錄秘密。診斷 artifact 與 AC 證據分開，保留 7 天。

**驗收證據鏈（真的驗證，不是空跑）**：
- 情境：EPIC 每條非 `[manual]` AC 至少一個 `S<m>`，各有 Given/When/Then；`devflow check plan` 檢查類型、情境與三段結構，`[manual]` 送 advisor。
- 執行：有服務的 `[ui]`/`[api]` 用 `.devflow/ui/ac.ts` 的 `scenario()`（Playwright，`retries: 0`），`[cli]`/`[lib]` 用 `verify.sh` 的 `scenario`（打建好的映像或套件）。兩者寫同一種事件，`acceptance.py` 彙整成 `acceptance.json`／`acceptance-matrix.md`；情境失敗不中斷，最後才以非 0 結束；不再有「沒有 Dockerfile 就略過」。
- 紅綠證明：`verify-base` 在 PR base 上用 head 的 `.devflow/` 只跑 `Covers:` 的情境。RED = 健康檢查與 Given 通過、第一個失敗在 When/Then；base 也通過（GREEN）只有 `Already-met:`、Given/環境失敗（INVALID）只有 `Base-unverifiable:` 才放行，兩種理由都由 `devflow-judgment` 與 reviewer 判斷。
- 身分：`devflow check acceptance` 不信卡片與報告自述，由 GitHub API 找 PR head 的 `ci.yml` run（`pull_request`、最新 attempt、成功）、最新成功部署 run（SHA = D）與 D 上的整合 run（`workflow_dispatch`），核對 workflow 路徑、事件、`head_sha`、attempt、artifact 歸屬與 `acceptance.json` 的身分欄位，並確認每個 PR 的合併 commit 是 D 的祖先。情境 id 帶 EPIC，步驟文字要和目前 EPIC 一致。
- 整合：部署後只跑唯讀 smoke；完整情境在 D 上的一次性 CI 環境重跑，沒有這份證據 EPIC 不能完成。
- 判斷：確定性檢查有任何不符，該 AC 直接 FAIL；全過才每條 AC 呼叫一次 Decisions（只給該 AC 的情境結果、步驟、base 判定、情境原始碼與截圖），問「證據是否顯示達成」與「斷言是否足以證明」。
- 限制：PR 可以改自己的 `ci.yml`、`verify.sh`、`acceptance.py`，所以身分核對擋得住用錯 run、舊版本、別的 EPIC 的報告，擋不住刻意偽造；防偽需要驗收程式改由 default branch 或 Hermes 端提供（見「信任邊界」）。

**規則與教訓（用檢查取代 prompt）**：同樣的錯誤不再寫進 skill 提醒，而是變成 `check` job 的檢查。
- 規則 `.devflow/rules/<id>.toml`：路徑範圍 + 正規表示式，**每條必須附 `expect = "fail"` 與 `"pass"` 範例**；`check` 先用範例自測，規則抓不到自己的錯誤範例就 CI 失敗。範本內建 `no-sleep-fix`（固定等待掩蓋時序問題）、`no-suppress`（壓 lint／型別警告）、`no-skip-test`（跳過或聚焦測試）；各 repo 自行加模組邊界等規則。
- 豁免：該行或上一行 `devflow: allow <rule-id> L-NNNN`，L-NNNN 必須是存在的教訓。既有 repo 接入時 `check.sh --write-baseline` 記錄舊違規（以規則＋路徑＋該行內容比對），之後只擋新增。
- 教訓 `.devflow/lessons/L-NNNN.toml`：事件、根因、分類、由什麼擋（`rule`／`test`／`journey`／`template`／`cli`／`skill-only`）；`check` 驗證 ref 指到的規則或檔案確實存在，`skill-only` 必須寫理由。
- PR 的 lesson gate（`check.py --pr`，`check` job 的一步）：標題 `fix:`／`revert:` 的 PR，說明必須有 `Lesson: L-NNNN` 或 `No-lesson: <理由>`；引用 base 已存在的教訓（同一根因再出現）時，必須在這個 PR 更新那筆教訓；這個 PR 新增的教訓要證明抓得到修正前的程式：`rule` 必須在改動檔案的 base 版本上報錯；`test`（`ref = "路徑::測試名"`）連同 PR 的 `.devflow/test.sh` 放到 base 上，只跑那個測試檔（`DEVFLOW_TEST_ONLY`），從 JUnit 結果（`DEVFLOW_JUNIT`）判定：那條測試在 base 必須是斷言失敗（`<failure>`；error、skip、因匯入或收集錯誤沒跑到都不算），在 PR 上必須通過。範本 `test.sh` 的 pytest 分支已支援；其他技術棧要自己加 JUnit 輸出，否則無法證明。`journey` 不做自動證明；`No-proof: <理由>` 跳過證明，由 reviewer 判斷。PR 說明修改會重跑 CI（`pull_request` 加了 `edited`）。
- PR 判斷（Hermes 主機上的 `devflow`，PR 改不到）：`check` 程式、規則與 `ci.yml` 都在 repo 內，CI 跑的是 PR 自己的版本，而且 gate 判斷不了語意。所以另外判斷：PR 改到 gate 檔案（`.devflow/check.py`、`check.sh`、`test.sh`、`rules/`、`.github/workflows/`、刪除教訓）一律列為疑慮；Decisions API 問三題，每題只在前提成立時才問：`unlabeled_fix`（標題不是 `fix:`/`revert:` 時問：diff 是不是修 bug）、`weak_exemption`（有 `No-lesson`/`No-proof` 時問：理由成不成立）、`lesson_mismatch`（fix PR 改了教訓時問：教訓有沒有對準這次的錯），機率 ≥ `flag` 算疑慮；沒回答的題目視為 API 失敗。判定綁在 PR head + 標題 + 說明上（`state/pr-judgments.json`）。結果寫成 commit status `devflow-judgment`：沒有疑慮或 advisor `ok` → success；有疑慮就開 `[ADV]`（Opus），等待中 → pending；advisor `revise` → failure。dispatcher tick 對 review 中的 PR 卡判斷並寫入，reviewer 的 `devflow check pr` 也會寫；PR 卡的 completion contract 要求它成功，所以疑慮沒解決前 PR 卡完成不了。`devflow ship merge` 合併前再判斷一次（說明在 review 後被改也擋得到），API 失敗、沒有 advisor `ok`、判斷後 PR 被改都拒絕，通過才用 `--match-head-commit` 合併。`devflow repo protect` 把 `devflow-judgment` 設為必過檢查並移除 admin bypass，所以直接 `gh pr merge` 也要等判斷通過。模型只負責把 PR 送去 advisor，不負責放行。
- 限制：寫入 `devflow-judgment` 用的是 builder token，agent 也拿得到，所以擋得住忘記檢查與搶在判斷前合併，擋不住刻意偽造。要做成權限關卡（只有人能核准放寬），核准者必須是 agent 拿不到 token 的帳號：目前 builder agent 用的是 repo owner 帳號，reviewer agent 用另一個帳號且平常就會 approve，所以兩者都不能當核准者，需先把 agent 帳號與人的帳號分開。只改 PR 說明不會改 head，舊的 success 會留著：review 中由下一次 tick 重判，review 完成後只有 `devflow ship merge` 會重判，所以直接 `gh pr merge` 可能用到舊的判定。已設 `contract=yes` 的 repo 要再跑一次 `devflow repo protect <slug>` 才會加上 `devflow-judgment`。教訓 ref 指到同一檔案裡不相干的 journey 時，模型判斷不出來。等 advisor 的方式：tick 只建 `[ADV]`；等待的 worker 自己 `kanban_link` 把它設為本卡 parent，再 `kanban_block(kind="dependency")`（Hermes 不允許從外部 link 執行中的卡；dependency block 不計入連續 block 次數，不會被送去 triage）。`[ADV]` 一直沒完成時，卡會留在 todo。
- devflow 本身的檢查：repo 根目錄 `.github/workflows/devflow-kit.yml`（範本腳本 shellcheck、範本 workflow actionlint、`devflow/tests/` 檢查器單元測試、`devflow/tests/deploy_scenarios.sh` 以一次性 Docker 跑範本 `deploy.sh` 的四情境：成功上線、build 失敗不動執行中版本、health 失敗與 verify 失敗都採證後回滾到 `:live`）。

**信任邊界**：workflow YAML 的條件（`if`、`environment`、`concurrency`）只防誤用，不是安全邊界。有任一 devflow repo push 權限的帳號，都能在分支上寫 workflow 跑在 `[self-hosted, devflow]` runner，等同主機 `devflow` 使用者與其 rootless Docker 的權限；所有 repo 共用這個使用者與 Docker daemon。要收斂需另做主機隔離（每個 repo 獨立使用者與 Docker，或組織層級 runner group 限定 workflow）。

- repo 契約：`templates/repo/`（`devflow contract <slug> . [--ui]` 補進 repo，列出與範本不同的檔供既有 repo 升級）。契約合併後 devops 跑 `devflow repo protect <slug>`，之後 PR 卡由 Hermes 原生 contract 強制 exact-head CI 全綠才能完成。
- Runner：`devflow repo add` 自動申請（`--no-runner` 跳過）；手動補 `devflow runner request OWNER/REPO`。請求放在 `/opt/data/devflow/runner/requests/`，主機 `devflow-runner-requests.path` 觸發 `runner-host.sh` 註冊並啟動 `devflow-runner@<owner>__<repo>`（user systemd），狀態看 `devflow runner status OWNER/REPO`。
- 服務只綁 `127.0.0.1:<port>`（`devflow repo add` 分配，從 18100 起）。
- 專案綁定：一個 Hermes 專案（根 profile）↔ 一個看板 ↔ 一個 repo。`devflow repo add` 呼叫 `repo bind`：找主路徑就是 clone 的專案沿用，沒有就新建，再寫看板 `board.json` 的 `project_id`（Kanban API、Mobile、語音讀這個）與專案的 `board_slug`。同 remote 的專案在別的路徑、專案已綁別的看板、多個候選、看板指向已封存專案時一律停止、不做變更，由人處理。`devflow repo doctor` 唯讀檢查。

## 身分與權限
- GitHub：builder = devops/gh 帳號（hermes、orchestrator、researcher、architect、developer、devops）；reviewer = reviewer/gh 帳號。`devflow repo add` 會讓兩個帳號都有 push 權限。
- developer 的 Codex：workspace-write 沙箱 + 網路；`.git` 在沙箱內唯讀，git/gh 以 Codex 原生升權執行，developer profile `approvals.mode: off` 自動核准（使用者 2026-11-15 選定）。
- Codex 主模型預設為 `gpt-6.1-sol`，推理強度 `high`；developer 的 Hermes profile 與 Codex app-server 設定須保持一致。更換模型時確認目前帳號與 runtime 支援。
- `bin/codex-developer` 依 PATH 使用 `codex`（可用 `DEVFLOW_CODEX_BIN` 明確覆寫），不固定到系統舊版。模型設定、驗證身分與 skills 使用同一個 `CODEX_HOME`（預設 `/opt/data/profiles/developer/codex`；可用 `DEVFLOW_CODEX_HOME` 覆寫），更新 binary 不會搬移或清除 skills。若要新增 developer 的 Codex skills，須安裝到這個 home 的 `skills/`，不是其他帳號的 `~/.codex/skills/`。
- Opus 5.5 走 `github-copilot` plugin（provider `copilot`，憑證在 Hermes 原生 credential pool）。reviewer profile 預設 Opus、`fallback_providers` 退回 `openai-codex`；`[ADV]` 卡由 `devflow check plan` 以卡片 model/provider 覆寫指定 Opus。
- Decisions API（`roles.json` 的 `decisions`）用 Hermes 既有 OpenAI key（GPT-Live 同一把）；`check plan` 的 `flag`、`check acceptance` 的 `pass` 為機率門檻。

## Hermes 原生限制與對應（不改 Hermes 程式）
| 原生行為 | 影響 | devflow 對應 |
|---|---|---|
| PR `completion_contract` 以卡片 assignee 的 GitHub 身分讀 required checks（classic branch protection 只有 admin 讀得到；ruleset 任何協作者都讀得到） | 沒有 required checks、或 reviewer（非 admin）完成時看不到 classic protection，卡永遠完成不了 | `devflow repo protect` 建 ruleset `devflow`（default branch 必須 `devflow-ci` 與 `devflow-judgment`，不允許 bypass），成功才標 `contract=yes`；`[DEV]`/`[OPS]` 依此設 contract，否則 local-only，由 reviewer / devops 用 `gh` 核對 |
| dispatcher 派出的 worker 寫入看板只能透過 `kanban_*` 工具；`hermes kanban` CLI 的寫入（edit、archive、unlink、assign、unblock）與 `kanban list`（會重算 ready）在 worker 內被 Hermes 擋下 | orchestrator 在 `[EPIC]`/`[ADJ]` 不能改寫、封存、解除依賴或重跑別張卡；worker 內的 hook 也被擋 | orchestrator 留 `devflow-ops` 留言；default profile 的 `on_kanban_dispatch_tick` hook（在 gateway dispatcher 內，不受 worker 限制）由 `devflow tick` 執行。devflow 讀看板只用 `kanban show` |
| 留言含 PR 網址的卡，dispatcher 24 小時內不重派（除非有交接事件）；unblock 不重設失敗計數 | 開過 PR 後 block / crash 的卡 unblock 了也不會跑 | `resume`（block → 取消指派 → 指派回原 Bot → unblock）：Hermes 聊天用 `devflow resume`，orchestrator 用 `devflow-ops` |
| Codex app-server 的模型可用性依帳號與 runtime 而異 | 不支援的模型會令 developer run 失敗 | developer 預設 `gpt-6.1-sol`、推理強度 `high`；變更後核對 runtime 可用性 |

## 新 repo / 既有 repo
跟 Hermes 說「幫 OWNER/REPO 做 X」或「開一個新專案 NAME 做 Y」。Hermes 會 `devflow repo add`（新專案加 `--create`）並建 EPIC；orchestrator 判斷需不需要接入 CI/CD（`[OPS]`）或骨架（`[ARCH]`→`[DEV]`）。

## 檔案
| 路徑（容器內 `/opt/data/devflow/`） | 內容 |
|---|---|
| `roles.json` | 角色、技能分配、設定 |
| `souls/` | 各 Bot SOUL（install 時寫入 `<profile>/SOUL.md` 的 devflow 區塊） |
| `skills/` | devflow 團隊技能 |
| `bin/devflow` | repo / board / contract / runner / check plan / check acceptance / check pr / ship merge / tick CLI |
| `bin/codex-developer` | developer 的 Codex 啟動器 |
| `codex/AGENTS.md` | developer Codex 全域指示 |
| `gitconfig` | 所有 Bot 共用的 git 設定（`GIT_CONFIG_GLOBAL`，經 `gh auth git-credential` 取憑證） |
| `templates/repo/`、`templates/ui/` | repo 契約（CI/CD、deploy、test、驗收情境與 `acceptance.py`）與 Playwright 情境骨架（`ac.ts`） |
| `install.py` | 套用到 7 個 profile（冪等；舊設定備份到 `/opt/data/devflow-backup/<時間>/`） |
| `state/repos.json` | 已接入 repo 與埠 |

主機端：`host/setup-host.sh`、`host/runner-host.sh`（本機 repo），由 `deploy-kit.sh --host` 安裝。只改 skills/CLI/templates 時用 `deploy-kit.sh --kit-only`；改 `roles.json`、SOUL 後用 `deploy-kit.sh` 並重啟 gateway（`s6-svc -r /run/hermes-service/gateway-default`）。
