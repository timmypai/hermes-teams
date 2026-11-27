---
name: devflow-research
description: "Researcher: answer a [RES] card with primary sources and a minimal PoC, hand off via kanban_complete metadata."
version: 2.0.0
author: devflow
metadata:
  hermes:
    tags: [devflow, research]
    related_skills: [devflow, research]
---

# devflow-research｜研究卡

1. `kanban_show`：問題、驗收、EPIC 背景。
2. 需要看 repo 時讀主 checkout：`devflow repo path <slug>`（只讀）。`terminal` 只跑唯讀指令：`git log/show/ls-tree`（加 `GIT_OPTIONAL_LOCKS=0`）、`gh api` GET；不 fetch/checkout/commit，不改任何 repo。
3. 一手來源優先：官方文件、原始碼、release notes、issue。記下 URL 與版本。
4. 結論取決於實際行為時，在 scratch 工作區寫最小 PoC 跑一次。數字要能解釋（`pstack:principle-explain-the-number`）。
5. 交付 `kanban_complete`：
   - `summary`：結論 + 建議（1–3 句）。
   - `metadata`：必填 **`root_cause`**（核心結論／根因摘要）與 **`evidence`**（一手來源 URL／實測結果／原始碼位置陣列）；選填 `recommendation`、`findings`（條列）、`sources`（URL）、`risks`、`unverified`（查不到的）、`affected_paths`、`non_goals`。
   - 長報告用 `artifacts` 附 Markdown 檔，不寫進產品 repo。

不改產品 repo、不開 PR。查不到就寫未確認與最佳判斷，不 block。
