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
1. `kanban_show`：目標、驗收（「覆蓋：AC-n」）、parent 的 summary/metadata（架構決策、研究結論）、留言。若有 `request_changes` 退件理由，這次就是修正回合：照理由修（含補 journey），再走 4–6。
2. `git status && git log --oneline -3`；讀 `AGENTS.md` / README / `.devflow/`。
3. `git fetch origin` 後若 main 已前進且會影響你的改動，`git rebase origin/<default>`。

## 2. 實作
- 寫程式的規則是 Codex skill `ub-mode`（Make it Work → Make it Right → Practice；devflow 的覆寫寫在 Codex `AGENTS.md`）。流程細節可用 `implement`、`tdd`。
- bug 卡（`pstack` bug-fix playbook）：先用一個會失敗的測試或 journey 重現；用證據二分找原因，不猜；只交付證據支持的最小修正，被證據推翻的改動要撤掉。
- 架構卡有 `adr_markdown` 時放到 `docs/adr/NNNN-<slug>.md` 一起提交。
- 新專案或 `[OPS]` 卡：`devflow contract <slug> .`（有畫面加 `--ui`）補 devflow 契約檔，把 `DEVFLOW_*` 值填好；指令列出「differs from current template」的檔案，對照 `/opt/data/devflow/templates/repo/` 手動合併，保留專案自己的改動。

## 3. 驗收 journey（每條 AC 都要有）
- 卡片「覆蓋：AC-n」的每一條，在 `.devflow/verify.sh` 加一個 journey：`journey AC-n "<說明>" <指令>`，像使用者一樣打真的服務（curl 真的 HTTP、看回應內容），失敗要回非 0。
- 有畫面的 AC：`devflow contract <slug> . --ui` 建 `.devflow/ui/`（Playwright），一條 AC 一個 `test('AC-n …')`，用 role/label/文字定位、斷言畫面上看得到的結果，最後 `page.screenshot({ path: '/evidence/AC-n.png' })`。
- repo 還沒有任何可腳本化的驗證方式時：`skill_view pstack:create-verification-skill`，照它的方法盤點啟動/操作/證據，但產物寫成 `.devflow/verify.sh` 與 `.devflow/ui/`（不是 `.cursor/skills`）；功能改了就用 `maintain-verification-skill` 的方法更新 journey。
- `verify.sh` 會在 PR CI（`.devflow/up.sh` 起服務）和部署後（Oracle VM）各跑一次，證據上傳成 artifact，最後由 orchestrator 逐條判定。不要寫只在本機能過的 journey。

## 4. 驗證
- 跑 `.devflow/test.sh`（沒有就用 repo 慣用的測試/lint/build）與 `bash .devflow/check.sh`（規則與教訓，CI 的必過 `check` job）。Hermes 容器內沒有 Docker：映像建置、`verify.sh` 與部署驗證交給 CI。
- 失敗就修根因，不刪測試、不 skip、不加壓警告註解（這些都由 `check` 擋）。規則確實誤判時，才在該行或上一行加 `devflow: allow <rule-id> L-NNNN`，並在同一個 PR 新增那筆教訓。
- 修 bug、修退件或處理部署失敗時，若同類錯誤可能再發生，新增教訓 `.devflow/lessons/L-NNNN.toml`（`id`、`title`、`event`＝PR／run／卡片連結、`root_cause`、`category` 為 `code-pattern`／`architecture`／`verification`／`process`／`kit` 之一、`[enforcement] kind/ref`）。選最能擋住**這個錯誤的語意**的方式：
  - `rule`：錯誤在**單一行**就看得出來（禁用的呼叫、匯入、註解、設定值）。新增 `.devflow/rules/<id>.toml`，附 `expect = "fail"`（這次的錯誤那一行）與 `"pass"` 範例。規則逐行比對，不能跨行，不懂語意；範例先跑 `bash .devflow/check.sh` 確認自測通過。
  - `test`／`journey`（`ref = "路徑::測試名"`）：要看行為或跨行結構才知道錯（權限、資料流、錯誤處理、時序）。寫回歸測試或 AC journey，例如「沒有權限的使用者呼叫 X 會得到 403」。這類錯誤不要寫成正規表示式規則。
  - `skill-only`：上面都做不到才用，`reason` 寫為何無法自動檢查。
  `check` 會驗證 ref 指到的東西真的存在。

## 5. PR
```bash
git add -A && git commit -m "<type>: <摘要> (<task-id>)"
git push -u origin HEAD
gh pr view --json url,number || gh pr create --base <default> --title "<摘要>" --body "<做了什麼/怎麼驗證>

Kanban: <task-id>"
gh pr checks --watch          # 等 CI；紅燈 → 修 → push → 再等
```
- 等 CI 期間偶爾 `kanban_heartbeat`。repo 還沒有 CI workflow 時跳過等待，在 handoff 註明。
- `ci.yml` 一定保留彙總 job `devflow-ci`（default branch 的 required check，也是 Hermes PR contract 的判定依據）；新增的 job 要加進它的 `needs`。ub-mode 指標在獨立的 `quality.yml`，不要把它加進 `devflow-ci`。
- CI 綠後看 `quality`（ub-mode 的 Make it Right）：`gh run download <run> -n devflow-quality-<sha> -D /tmp/q-<task>`，讀 `crap.txt`（你改到的函式 CRAP ≥ 10 → 拆或補測試）、`dry.txt`（≥ 0.8 的重複 → 合併）、`mutate.txt`（uncovered 補測試、survivor 補斷言、死碼刪掉）。只處理這次改到的程式；舊程式的數字寫進 `followups`。數字不是門檻，quality workflow 失敗也不擋送審。repo 還沒有 `quality.yml` 就跳過，在 `followups` 寫「升級 devflow 契約（quality）」。
- `crap.txt` 全部 `Cov% 0.0` 或 `mutate.txt` 全是 UNCOVERED，通常是工具找不到這個 repo 的測試：加可執行的 `.devflow/coverage.sh`，用 repo 自己的測試指令產生 LCOV 到 `target/coverage/<語言>/lcov.info`（Go 是 `coverage.out`），`quality.sh` 會改讀它。
- CI 的 `verify` job 失敗：`gh run view <id> --log-failed` 看哪條 AC 的 journey 失敗；`gh run download <id>` 取證據（截圖可直接看）與 `devflow-diagnostics-pr-<sha>`（容器狀態、退出碼、log）。修到綠。
- CI 失敗修到綠；若是 runner/基礎設施問題（非程式碼），在 handoff 註明並照樣送 review，由 devops 處理。

## 5.5 除錯迴圈（整合層問題、部署後才出現的 bug）
Hermes 容器沒有 Docker，整合層的重現跑在 GitHub-hosted 的 `repro.yml`，不碰正式主機。照 `diagnosing-bugs`：先有會紅的重現，再假設、再修。
1. 先讀現有證據：卡片或 parent 給的 deploy run → `gh run download <id> -n devflow-diagnostics-deploy-<sha> -D /tmp/diag-<task>`（`candidate/logs/`、`state.jsonl` 的 `ExitCode`/`OOMKilled`/`restart_count`、`health.json`）。
2. 能在 worktree 用 `.devflow/test.sh` 這一層重現就在這裡寫失敗的測試（快）；需要整個服務才寫成 `verify.sh` 的 AC journey。
3. 整合層重現 / 驗證假設：push 卡片分支後
   ```bash
   gh workflow run repro.yml --ref wt/<task-id> -f only="AC-2" -f trace=true
   gh run list --workflow repro.yml --branch wt/<task-id> -L 1 --json databaseId,status,url
   gh run watch <id> --exit-status; gh run download <id> -D /tmp/repro-<task>-<id>
   ```
   artifact 有 `evidence/`（journey 輸出、截圖、trace.zip）與 `diagnostics/repro/`（容器狀態、log）。一次只改一個變數；暫時的除錯 log 加唯一前綴（如 `[DEBUG-a4f2]`），非秘密的除錯設定（如 `LOG_LEVEL=debug`）放 `.devflow/repro.env`。
4. 送 review 前：移除除錯 log 前綴與 `.devflow/repro.env`，把重現留成正式測試或 journey，handoff `log` 寫下被證實的原因。
- repo 沒有 `repro.yml`（或還不在 default branch）→ 只能用一般 CI 迭代，並在 `followups` 寫「升級 devflow 契約（repro）」。
- repro 沒有部署秘密與正式資料；只在正式環境才出現的問題，在 handoff 寫明重現不了的原因與已排除的假設，交 orchestrator。

## 6. 交付
```
kanban_request_review(
  reviewer="reviewer",
  summary="<做了什麼>；測試 <指令> 通過；CI <綠/無>",
  metadata={"published_pr": "<PR URL>", "pr_url": "<PR URL>", "pr_number": N, "branch": "wt/<id>", "head_sha": "<git rev-parse HEAD>",
            "tests": ["<指令> → pass"], "acceptance": {"AC-1": "journey AC-1（verify.sh）/ ui AC-1 → CI run <id> pass"},
            "ci_run": "<CI run URL>", "quality": "<CRAP 最高的改動函式/重複/存活 mutant 摘要，或「無 quality job」>",
            "log": ["<做了什麼> — <為什麼>", ...], "followups": [...]})
```
reviewer 會被自動派工。退件時這張卡回到你手上（同一 worktree、同一 PR），修正 → push → 等 CI → 再 `kanban_request_review(reviewer="reviewer", ...)`。

## 不做
不 merge、不部署、不 push 到 main、不改 `.github/workflows` 的部署目標（`[OPS]` 卡除外）。範圍外問題寫進 `followups`。
