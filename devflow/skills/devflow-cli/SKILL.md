---
name: devflow-cli
description: "On-demand reference for all /opt/data/devflow/bin/devflow CLI commands and flags."
version: 3.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, cli, reference]
---

# devflow-cli｜指令完整參考（按需查詢）

執行檔：`/opt/data/devflow/bin/devflow`。背景 worker 只在需要查完整參數時讀本 skill。

| 指令 | 用途 |
|---|---|
| `devflow repo list` | 已接入 repo／board（含 `contract=yes/no`） |
| `devflow repo add OWNER/REPO [--create] [--public] [--no-runner]` | 建 board、clone、設權限、配埠、綁專案、申請 runner |
| `devflow repo bind SLUG` | 冪等綁定專案與看板 |
| `devflow repo doctor [SLUG]` | 唯讀檢查綁定、部署就緒、公版交付，並以 `agents_md_warnings` 列出 repo `AGENTS.md` 與 devflow 衝突的流程條文 |
| `devflow repo sync SLUG` / `repo path SLUG` | 快轉主 checkout／印出路徑 |
| `devflow repo access SLUG` | 服務唯一存取方式（SSH tunnel + 127.0.0.1 埠） |
| `devflow repo protect SLUG` | 設 default branch 的 `devflow-ci`／`devflow-judgment` required checks |
| `devflow contract SLUG [DIR] [--ui] [--sync-standard]` | 補齊契約檔；`--sync-standard` 還原公版交付檔 |
| `devflow check card --title T [--body-file F] [--board SLUG] [--assignee A] [--workspace W] [--parent P]` | 建卡／改卡前確定性檢查卡型、欄位、範圍宣告、`[ARCH]` 檔案涵蓋與技術洩漏 |
| `devflow check handoff SLUG TASK [--step STEP] [--metadata JSON \| --metadata-file F]` | 依 `handoff_schemas` 驗證卡的交接 `metadata` 欄位 |
| `devflow checkpoint SLUG TASK [--dir DIR] [--base REF] [--test T] [--summary S]` | `Stop-After: implement` 時驗證已提交本機修改並記錄本機 checkpoint（不 push、不開 PR） |
| `devflow epic advance SLUG EPIC [--stop-after STEP \| --to-end]` | 推進分段 EPIC 的 `Start-At:`／`Stop-After:` 往下一步或結尾 |
| `devflow scope add SLUG TASK --add GLOB... [--note T]` | 驗證並原子合併核准的 `Allowed-Scope` glob（拒絕萬用與禁止範圍） |
| `devflow scope check SLUG TASK --paths PATH... [--dir D]` | 唯讀查詢候選路徑是否屬 `Allowed-Scope`、`test_paths`/`artifact_paths` 豁免或禁止範圍 |
| `devflow check ac [--said TEXT] < AC行` | D0：建 EPIC 前檢查 AC 草稿 |
| `devflow check plan SLUG EPIC [--advise]` | 計畫確定性規則（含 Mode）與範圍風險 |
| `devflow preflight SLUG [TASK] [--dir DIR] [--pr-title T]` | 送審前確定性自檢（範圍、檔數、保護檔、公版、測試） |
| `devflow pr submit SLUG TASK [--dir DIR] --pr-title T [--pr-body B] [--dry-run]` | 單一提交入口：先跑 preflight，通過才 push 並開／讀回 PR |
| `devflow check pr SLUG PR` | PR 判斷（範圍、保護檔、公版、熔斷、Decisions） |
| `devflow check acceptance SLUG EPIC --pr N... [--review] [--evidence F] [--json]` | 核對 GitHub 證據身分、情境紅綠與逐條 AC |
| `devflow ship merge SLUG PR` | 核對 exact-head approve、judgment、人類核准（exit 6）、計畫風險（exit 7）後合併 |
| `devflow operate SLUG OP [--arg K=V]` | 在 VM runner 執行已審查的 UI 操作腳本 |
| `devflow runner request／status OWNER/REPO` | 申請／查詢 self-hosted runner |
| `devflow pending [PROFILE]` | 等使用者回答的卡與送達狀態 |
| `devflow answer SLUG TASK --event N --where W [--add-scope GLOB] TEXT` | 交回使用者回答（一題一次；可原子附加 `Allowed-Scope`） |
| `devflow resume SLUG TASK [--note T]` | 聊天端重跑 blocked／held／triage 卡 |
| `devflow approve SLUG PR --request REQ_ID` | 聊天端記錄受保護檔 exact-head 核准 |
| `devflow approve-plan SLUG EPIC --plan-hash H` | 聊天端記錄計畫風險核准 |
| `devflow inbox [show\|list\|set\|unset]` | 管理無對話來源卡片的收件聊天 |
| `devflow stats [SLUG] [--json]` | 唯讀遙測（EPIC、一次通過率、`[ADJ]` 來源分桶、補丁卡、閘門） |
| `devflow tick SLUG` | 看板自動化（由 dispatcher hook 觸發） |
