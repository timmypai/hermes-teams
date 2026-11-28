"""Declarative card handoff stage lint & anti-hardcode tests for devflow."""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, load_devflow

if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import policy as policy_lib  # noqa: E402
from lib import scope as scope_lib  # noqa: E402


EPIC, DEV, OPS, SHIP = "t_ep01", "t_dev01", "t_ops01", "t_sh01"


class DeclarativeHandoffLintScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        self.policy = policy_lib.load_policy(KIT, self.roles)
        self.rules = policy_lib.card_handoff_rules(self.policy)
        self.done_heading = self.rules["done_heading"]
        self.done_text = self.rules["done_text"]

    def _dev_card(self, acceptance: str, *, goal: str = "只記錄送音首錯與場內順序。") -> str:
        return (
            f"## 目標\n{goal}\n"
            f"{self.done_heading}\n{self.done_text}\n"
            f"## 驗收\n{acceptance}\n"
            f"EPIC: {EPIC}\n"
        )

    def test_catches_real_t_0b3c4df6_repro_in_acceptance(self) -> None:
        body = self._dev_card(
            "覆蓋：AC-1\n"
            "同刺激驗舊版 revoked_other、新版去識別首錯與順序；原有送音、靜音、清理、安全行為一致。"
            "交逐欄白名單。正式審查限非作者 paulpai0412 的 exact head。\n"
            "Allowed-Scope: teams_transcription/b_browser_tx.py\n"
            "Non-Goals: 不改部署；不合併、不做獨立審查"
        )
        probs = scope_lib.post_handoff_problems("DEV", body, self.rules)
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("驗收", probs[0])
        self.assertIn("正式審查", probs[0])

    def test_catches_downstream_review_merge_deploy_and_live_acceptance_in_goal_or_acceptance(self) -> None:
        cases = [
            ("review approval", "覆蓋：AC-1\n須經獨立審查通過並取得 APPROVED 才算完成。\nAllowed-Scope: app/**"),
            ("merge and deploy", "覆蓋：AC-1\nPR 須合併至 main 並部署上線。\nAllowed-Scope: app/**"),
            ("ship merge cli", "覆蓋：AC-1\n執行 devflow ship merge 並完成部署上線。\nAllowed-Scope: app/**"),
            ("live acceptance", "覆蓋：AC-1\n等待使用者真人驗收通過才算完成。\nAllowed-Scope: app/**"),
        ]
        for label, acc in cases:
            with self.subTest(label=label):
                probs = scope_lib.post_handoff_problems("DEV", self._dev_card(acc), self.rules)
                self.assertTrue(probs, f"expected finding for {label}")

        goal_bad = self._dev_card(
            "覆蓋：AC-1\nAllowed-Scope: app/**",
            goal="修正問題並合併至 main 部署上線後完成。",
        )
        self.assertTrue(scope_lib.post_handoff_problems("OPS", goal_bad, self.rules))

    def test_allows_clean_dev_cards_excluded_fields_negations_feature_acs_and_downstream_cards(self) -> None:
        clean_pr24_like = self._dev_card(
            "覆蓋：AC-1\n"
            "按欄位契約離線重放兩類失敗與正常對照，證明只新增白名單診斷及送音、靜音、撤銷不變。"
            "PR 定稿即交接欄位與取值條件。\n"
            "Allowed-Scope: teams_transcription/b_browser_tx.py\n"
            "Non-Goals: 不合併、不部署、不做獨立審查；不入會或發聲\n"
            "Out-of-Scope: .github/workflows/deploy.yml"
        )
        self.assertEqual(scope_lib.post_handoff_problems("DEV", clean_pr24_like, self.rules), [])

        negated_in_acc = self._dev_card(
            "覆蓋：AC-1\n"
            "本卡不負責合併與部署，也不需正式審查；待離線回歸通過即完成作者交付。\n"
            "Allowed-Scope: app/**"
        )
        self.assertEqual(scope_lib.post_handoff_problems("DEV", negated_in_acc, self.rules), [])

        feature_acs = self._dev_card(
            "覆蓋：AC-1\n"
            "- AC-1 [cli] 部署成功且健康檢查通過\n"
            "修復 review 狀態顯示與通知文案\n"
            "Allowed-Scope: app/**"
        )
        self.assertEqual(scope_lib.post_handoff_problems("DEV", feature_acs, self.rules), [])

        ship_body = (
            "## 目標\n確認 PR 的 paulpai0412 APPROVED 後執行 devflow ship merge 並部署上線。\n"
            "## 驗收\n覆蓋：AC-2\n合併後確認服務健康與真人驗收交接。\nEPIC: t_ep01\n"
        )
        for kind in ("SHIP", "EPIC", "RES", "ARCH"):
            with self.subTest(kind=kind):
                self.assertEqual(scope_lib.post_handoff_problems(kind, ship_body, self.rules), [])

    def test_declarative_override_drives_post_handoff_rules_without_code_change(self) -> None:
        custom_policy = {
            **self.policy,
            "card_handoff_rules": {
                **self.rules,
                "post_handoff_clauses": [
                    {
                        "id": "custom_token_gate",
                        "stage": "custom_stage",
                        "label": "自訂下游階段條件",
                        "patterns": [r"\bCUSTOM_DOWNSTREAM_SIGN_OFF\b"],
                    }
                ],
            },
        }
        custom_rules = policy_lib.card_handoff_rules(custom_policy)
        body = self._dev_card("覆蓋：AC-1\n須先通過 CUSTOM_DOWNSTREAM_SIGN_OFF。\nAllowed-Scope: app/**")
        probs = scope_lib.post_handoff_problems("DEV", body, custom_rules)
        self.assertEqual(len(probs), 1)
        self.assertIn("自訂下游階段條件", probs[0])
        self.assertIn("custom_stage", probs[0])

        # Original pattern is no longer active when custom rule table overrides post_handoff_clauses
        repro = self._dev_card("覆蓋：AC-1\n正式審查限非作者 exact head。\nAllowed-Scope: app/**")
        self.assertEqual(scope_lib.post_handoff_problems("DEV", repro, custom_rules), [])


class DevflowEntrypointsHandoffLintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-hl-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state").mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.root / "state" / "repos.json").write_text(
            json.dumps({"b": {"repo": "o/r", "path": str(self.repo), "contract": False}}),
            encoding="utf-8",
        )
        self.db = self.root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        self.db.parent.mkdir(parents=True)
        self._old_env = os.environ.get("DEVFLOW_HERMES_DATA")
        os.environ["DEVFLOW_HERMES_DATA"] = str(self.root / "data")
        self.addCleanup(self._restore_env)
        self.df = load_devflow(self.root)
        self.done = f"{self.df.DONE_HEADING}\n{self.df.DONE_TEXT}\n"

    def _restore_env(self) -> None:
        if self._old_env is None:
            os.environ.pop("DEVFLOW_HERMES_DATA", None)
        else:
            os.environ["DEVFLOW_HERMES_DATA"] = self._old_env

    def test_check_card_plan_rules_and_edit_guard_all_reject_t_0b3c4df6_repro(self) -> None:
        bad_body = (
            f"## 目標\n只記錄首錯。\n{self.done}"
            f"## 驗收\n覆蓋：AC-1\n離線驗證通過。正式審查限非作者 paulpai0412 的 exact head。\n"
            f"Allowed-Scope: teams_transcription/b_browser_tx.py\n"
            f"Non-Goals: 不合併、不部署\nEPIC: {EPIC}\n"
        )
        clean_body = (
            f"## 目標\n只記錄首錯。\n{self.done}"
            f"## 驗收\n覆蓋：AC-1\n離線驗證通過並交接白名單。\n"
            f"Allowed-Scope: teams_transcription/b_browser_tx.py\n"
            f"Non-Goals: 不合併、不部署、不做獨立審查\nEPIC: {EPIC}\n"
        )

        # 1. cmd_check_card: clean passes, bad fails
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_check_card(
                SimpleNamespace(
                    title="[DEV] 乾淨卡",
                    body=clean_body,
                    body_file=None,
                    assignee="developer",
                    workspace="worktree",
                    board="b",
                    parent=[],
                )
            )
        self.assertTrue(json.loads(buf.getvalue())["ok"])

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
            self.df.cmd_check_card(
                SimpleNamespace(
                    title="[DEV] 壞卡",
                    body=bad_body,
                    body_file=None,
                    assignee="developer",
                    workspace="worktree",
                    board="b",
                    parent=[],
                )
            )
        self.assertEqual(cm.exception.code, 1)
        bad_out = json.loads(buf.getvalue())
        self.assertFalse(bad_out["ok"])
        self.assertTrue(any("正式審查" in item for item in bad_out["findings"]), bad_out)

        # 2. plan_rules & _validate_edit_op
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, goal_mode INTEGER NOT NULL DEFAULT 0)")
            c.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?, ?)",
                [
                    (EPIC, "[EPIC] e", "## 目標\ng\n## 驗收\n- AC-1 [cli] x\n  - S1 s", 0),
                    (DEV, "[DEV] d", bad_body, 1),
                    (SHIP, "[SHIP] s", f"EPIC: {EPIC}", 0),
                ],
            )
        cards = {
            EPIC: {
                "id": EPIC, "title": "[EPIC] e", "body": "## 目標\ng\n## 驗收\n- AC-1 [cli] x\n  - S1 s",
                "status": "todo", "assignee": "orchestrator", "workspace_kind": "dir", "workspace_path": None,
                "completion_contract": None, "parents": [SHIP], "children": [], "created_by": "default", "goal_mode": False,
            },
            DEV: {
                "id": DEV, "title": "[DEV] d", "body": bad_body,
                "status": "todo", "assignee": "developer", "workspace_kind": "worktree", "workspace_path": str(self.repo),
                "completion_contract": None, "parents": [], "children": [SHIP], "created_by": "orchestrator", "goal_mode": True,
            },
            SHIP: {
                "id": SHIP, "title": "[SHIP] s", "body": f"EPIC: {EPIC}",
                "status": "todo", "assignee": "devops", "workspace_kind": "dir", "workspace_path": str(self.repo),
                "completion_contract": None, "parents": [DEV], "children": [EPIC], "created_by": "orchestrator", "goal_mode": False,
            },
        }
        plan_findings = self.df.plan_rules("b", EPIC, cards)
        self.assertTrue(any(f.startswith(f"{DEV}: [DEV]") and "正式審查" in f for f in plan_findings), plan_findings)

        edit_errs = self.df._validate_edit_op("b", {"op": "edit", "task": DEV, "body": bad_body})
        self.assertTrue(any("正式審查" in e for e in edit_errs), edit_errs)
        self.assertEqual(self.df._validate_edit_op("b", {"op": "edit", "task": DEV, "body": clean_body}), [])

    def test_custom_roles_policy_overrides_done_text_and_lint_rules(self) -> None:
        custom_root = Path(tempfile.mkdtemp(prefix="devflow-hl-custom-"))
        self.addCleanup(shutil.rmtree, custom_root, ignore_errors=True)
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        roles["policy"]["card_handoff_rules"] = {
            "cards": ["DEV"],
            "done_heading": "## 完成標準",
            "done_text": "完成條件：PR 與 CI 皆已綠燈。",
            "excluded_sections": ["完成標準"],
            "excluded_fields": ["Non-Goals"],
            "negation_prefixes": [r"不需"],
            "post_handoff_clauses": [
                {
                    "id": "custom_rule",
                    "stage": "review",
                    "label": "下游核簽",
                    "patterns": [r"需核簽放行"],
                }
            ],
        }
        (custom_root / "roles.json").write_text(json.dumps(roles, ensure_ascii=False), encoding="utf-8")
        custom_df = load_devflow(custom_root)
        self.assertEqual(custom_df.DONE_HEADING, "## 完成標準")
        self.assertEqual(custom_df.DONE_TEXT, "完成條件：PR 與 CI 皆已綠燈。")
        good = "## 目標\nx\n## 完成標準\n完成條件：PR 與 CI 皆已綠燈。\n## 驗收\n覆蓋：AC-1\n"
        self.assertIsNone(custom_df.done_problem("[DEV] d", good))
        self.assertEqual(custom_df.post_handoff_card_problems("DEV", good), [])
        bad = good + "須等待需核簽放行。\n"
        self.assertTrue(custom_df.post_handoff_card_problems("DEV", bad))


if __name__ == "__main__":
    unittest.main()
