---
name: devflow-chat
description: "On-demand SSOT for answering blocked Kanban questions in Bot Chat / App Chat, including devflow pending, answer, approve, and approve-plan."
version: 3.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, chat, pending, approve, ssot]
---

# devflow-chat｜在 Bot Chat／App 對話回答卡片提問（唯一真相來源）

只在**非 worker 的對話介面**被卡片阻塞通知喚醒、或使用者回覆卡片問題時載入；背景 worker 不載入。

## 1. 查待答題目
執行 `terminal: /opt/data/devflow/bin/devflow pending <你的 profile 名>`（Hermes 主對話可不帶）：
- `handled_by` 有值（`[ADJ]` 卡號）：屬流程問題已交 orchestrator，不問使用者，只回「<task> 卡在流程問題，已交 <handled_by> 處理，不用你回答」。
- `ask_in` 不是目前對話的角色：完整問題已送到 `<ask_in>` 的 Bot Chat，這裡只回三行指向那邊，並說明「在這裡直接回也可以」。
- `comments_since` 是提問後別人的留言，**不是答案**；列出來問「要當成這題的回答嗎？」，使用者明確確認才交。

## 2. 提問格式（使用者不用再查就能回答）
```
需要你決定（<board> <task>）：<卡片標題>
在做什麼：<依 card 的目標一句話>
卡在哪：<依 question 背景 1–3 句白話，不寫 SHA／run ID>
問題：<question 第一行的問題>
1. <選項 A>：<後果與代價>（建議，理由：…）
2. <選項 B>：<後果與代價>
直接回覆編號或你的想法。
```
先用 `clarify` 問（建議選項放第一個）；被拒（沒人在看）就把整段留成訊息並結束本輪。

## 3. 收回覆並交回
對應到卡片與 `block_event`（取自 `devflow pending`）後，依題型處理，再執行：
`terminal: /opt/data/devflow/bin/devflow answer <board> <task> --event <block_event> --where "<介面>" "<使用者回答>"`
（不要用 `devflow resume` 交答案；`answer` 保證一題只收一次。）

- **受保護檔合併核准題**
  - **只有使用者明確確認核准合併（選 A／同意）時**，從原提問的 `核准請求碼：`（或 `pending_approval.request_id`）取 12 碼，執行 `terminal: devflow approve <board> <pr> --request <12碼核准請求碼>` 記錄核准，再 `devflow answer`。
  - **嚴禁改查 `devflow check pr` 或 `gh pr view` 的最新 head 代填**（PR 已換新 head 時 `approve` 會拒絕，須重新向使用者確認）。
  - 使用者拒絕或選 B 退回時，**絕不執行 `devflow approve`**，直接以 `devflow answer` 交回拒絕回覆。
- **計畫風險定錨題**
  - **只有使用者明確選擇核准目前計畫（選 B）時**，從 `question` 的 `計畫雜湊：` 取 12 碼，執行 `terminal: devflow approve-plan <board> <epic> --plan-hash <12碼雜湊>`，再 `devflow answer`。
  - 使用者選 A 調整縮減或拒絕時，**絕不執行 `devflow approve-plan`**，直接 `devflow answer` 交回調整指示。
- **範圍擴充核准題**（卡因修改需超出 `Allowed-Scope` 而阻擋）
  - **只有使用者明確同意放行該範圍時**，加上 `--add-scope <glob>`（可重複）一併原子寫入卡片 `Allowed-Scope:` 後才解鎖：`devflow answer <board> <task> --event <block_event> --where "<介面>" --add-scope "<path>" "<使用者回答>"`；若單純補核准範圍也可執行 `devflow scope add <board> <task> --add "<path>"`。遇 `Out-of-Scope`／`## 不能碰` 衝突時指令會拒絕寫入，不可手動繞過。
- **分段 Pipeline 推進**
  - 使用者確認要從 `Stop-After` 繼續往下跑時，執行 `devflow epic advance <board> <epic> [--stop-after <step> | --to-end]` 推進後再處理後續卡。
- **要使用者自己動手的 `capability` 題**（設 secret、OAuth、主機指令）
  - 只有使用者說「完成」或「暫緩／不做」才 `devflow answer`；只回「同意」「好」時不要交（卡片重跑後仍缺會被轉進 triage），回「做完回覆『完成』，我再交回」並附步驟。

## 4. 未套用與送達查詢
- `answer` 回 `not applied: ...`：不要套到別張卡，照輸出的 status 與 `events since` 照實說明，並列出還在等使用者的題目；只有卡片留言看得到另一則「使用者回覆（<地方>，問題事件 N）」才說已在那裡回答過。
- 查「有沒有送到」只看 `devflow pending` 的 `chats[]`：`woken=false` 說「那邊還沒看到（通常晚約 1 分鐘）」，超過 10 分鐘仍 false 才請使用者在這裡直接回答。
