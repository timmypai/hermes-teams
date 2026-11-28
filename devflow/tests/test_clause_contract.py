"""Phase P1 deterministic Then-clause contract, AST assertion lint, and runner reachability tests."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

KIT = Path(__file__).resolve().parents[1]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import clause_contract  # noqa: E402
from lib import policy as policy_lib  # noqa: E402
from test_judgment import REPO, load_devflow  # noqa: E402

EPIC = "t_ep99"


class ClauseContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-clauses-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "state").mkdir()
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        self.repo_dir = self.root / "repo"
        (self.repo_dir / ".devflow").mkdir(parents=True)
        (self.repo_dir / "tests").mkdir(parents=True)
        (self.repo_dir / ".github" / "workflows").mkdir(parents=True)
        (self.root / "state" / "repos.json").write_text(json.dumps(
            {"b": {"slug": "b", "repo": REPO, "path": str(self.repo_dir), "port": 1, "contract": True, "default_branch": "main"}}
        ))
        self.df = load_devflow(self.root)
        self.df.decide = lambda content, qs: {q["name"]: {"name": q["name"], "probability": 0.0} for q in qs}

    def _seed_t98496ab2_repo(self, then_value, then_clauses=None) -> Path:
        (self.repo_dir / "tests" / "test_tx_failure_receipts.py").write_text(
            "import unittest\n\n"
            "class TXRuntimeFailureTests(unittest.TestCase):\n"
            "    def test_runtime_first_failure_and_order_reach_persistence(self):\n"
            "        diagnostic = {'site': 'guard', 'failure_sequence': 1, 'revoke_sequence': 2}\n"
            "        self.assertEqual(diagnostic['site'], 'guard')\n"
            "        self.assertLess(diagnostic['failure_sequence'], diagnostic['revoke_sequence'])\n\n"
            "    def test_event_and_diagnostic_loss_keep_unknowns_and_protection(self):\n"
            "        diagnostic = {'first_failed_condition': 'unknown', 'failure_sequence': None}\n"
            "        self.assertEqual(diagnostic['first_failed_condition'], 'unknown')\n"
            "        self.assertIsNone(diagnostic['failure_sequence'])\n\n"
            "    def test_illegal_diagnostics_do_not_change_legacy_policy_decision(self):\n"
            "        accepted, projected = False, {'reason': 'owned_identity', 'detail': 'revoked_other'}\n"
            "        self.assertFalse(accepted)\n"
            "        self.assertEqual(projected['reason'], 'owned_identity')\n"
            "        self.assertNotIn('tx_first_failure', projected)\n",
            encoding="utf-8",
        )
        (self.repo_dir / ".devflow" / "verify.sh").write_text(
            "#!/usr/bin/env bash\npython - < tests/test_tx_failure_receipts.py\n",
            encoding="utf-8",
        )
        (self.repo_dir / ".github" / "workflows" / "ci.yml").write_text(
            "jobs:\n  test:\n    steps:\n      - run: python -m unittest discover -s tests -p 'test_*.py'\n",
            encoding="utf-8",
        )
        entry = {
            "description": "回放語音 B 運行中失效",
            "smoke": False,
            "given": "同一合成場次，語音就緒且已授權輸出。",
            "when": "兩版重放相同失效，另重放正常與失效保護對照。",
            "then": then_value,
        }
        if then_clauses is not None:
            entry["then_clauses"] = then_clauses
        sc_path = self.repo_dir / ".devflow" / "scenarios.json"
        sc_path.write_text(json.dumps({f"{EPIC}/AC-1.S1": entry}, ensure_ascii=False, indent=2), encoding="utf-8")
        return sc_path

    def test_t98496ab2_replay_passes_when_all_claims_bound_and_blocks_uncovered_share_order(self) -> None:
        clauses_t1_t4 = [
            {
                "id": "T1",
                "text": "舊版回 revoked_other，診斷版新增首錯與場內順序",
                "kind": "changes",
                "tests": ["test_tx_failure_receipts.TXRuntimeFailureTests.test_runtime_first_failure_and_order_reach_persistence"],
            },
            {
                "id": "T2",
                "text": "無可靠信號則分類未知",
                "kind": "changes",
                "tests": ["test_tx_failure_receipts.TXRuntimeFailureTests.test_event_and_diagnostic_loss_keep_unknowns_and_protection"],
            },
            {
                "id": "T3",
                "text": "兩版送音量、撤銷、靜音、清理及拒絕結果相同",
                "kind": "preserves",
                "receipt": {"file": "tx-runtime-receipts.json", "key": "behavior"},
            },
            {
                "id": "T4",
                "text": "拒收非法欄位",
                "kind": "rejects",
                "mode": "preserves",
                "tests": ["test_tx_failure_receipts.TXRuntimeFailureTests.test_illegal_diagnostics_do_not_change_legacy_policy_decision"],
            },
        ]
        # 1) Covered T1..T4 -> 0 findings in plan_rules and `devflow check clauses`.
        self._seed_t98496ab2_repo(clauses_t1_t4)
        cards = {
            EPIC: {
                "title": "[EPIC] 首錯診斷契約",
                "body": "## 目標\n驗證首錯\n\n## 驗收\n- AC-1 [lib] 留下去識別首錯\n",
                "status": "todo",
                "assignee": "orchestrator",
                "workspace_kind": "dir",
                "parents": ["t_s1"],
            },
            "t_d1": {
                "title": "[DEV] 實作首錯診斷",
                "body": (
                    "## 目標\n補首錯診斷\n\n"
                    "## 本卡完成\n完成條件：PR 已開、exact-head CI 綠。審查在本卡自己的 review 欄進行，不是 [SHIP] 的事；合併與部署由 [SHIP] 負責。\n\n"
                    "## 驗收\n覆蓋：AC-1\nAllowed-Scope: tests/test_tx_failure_receipts.py\n"
                    f"EPIC: {EPIC}"
                ),
                "status": "todo",
                "assignee": "developer",
                "workspace_kind": "worktree",
                "completion_contract": REPO,
                "goal_mode": True,
                "parents": [],
                "children": ["t_s1"],
            },
            "t_s1": {
                "title": "[SHIP] 部署",
                "body": f"## 目標\n部署\n\n## 驗收\n覆蓋：AC-1\nEPIC: {EPIC}",
                "status": "todo",
                "assignee": "devops",
                "workspace_kind": "dir",
                "parents": ["t_d1"],
                "children": [],
            },
        }
        findings_ok = self.df.plan_rules("b", EPIC, cards)
        self.assertEqual(findings_ok, [], findings_ok)

        # 2) Inject the real unverified claim "重放分享順序" without a covering clause (both standalone
        #    and embedded in a long comma-joined segment) -> blocked at planning time!
        for bad_then in (
            "舊版回 revoked_other，診斷版新增首錯與場內順序，無可靠信號則分類未知；兩版重放相同失效與分享順序；兩版送音量、撤銷、靜音、清理及拒絕結果相同；拒收非法欄位。",
            "舊版回 revoked_other，診斷版新增首錯與場內順序，重放分享順序，無可靠信號則分類未知；兩版送音量、撤銷、靜音、清理及拒絕結果相同；拒收非法欄位。",
        ):
            self._seed_t98496ab2_repo(bad_then, then_clauses=clauses_t1_t4)
            findings_uncovered = self.df.plan_rules("b", EPIC, cards)
            self.assertTrue(
                any("分享順序" in f and "未被 then_clauses 覆蓋" in f for f in findings_uncovered),
                (bad_then, findings_uncovered),
            )

        # 3) Explicit clause T5 ("分享順序") with tests: [] -> blocked at planning time!
        clauses_t5_empty = [*clauses_t1_t4, {"id": "T5", "text": "重放分享順序", "kind": "changes", "tests": []}]
        self._seed_t98496ab2_repo(clauses_t5_empty)
        findings_empty = self.df.plan_rules("b", EPIC, cards)
        self.assertTrue(any("T5" in f and "必須綁定至少一個具名 test ID" in f for f in findings_empty), findings_empty)

    def test_schema_and_rejects_anti_loophole_fail_closed(self) -> None:
        # Bad schemas: missing id, duplicate id, invalid kind, receipt traversal, invalid mode, mode on non-rejects.
        _, _, errs_bad = clause_contract.parse_then_clauses({
            "then": [
                {"id": "T1", "text": "a", "kind": "bogus", "tests": ["t.f"], "mode": "new"},
                {"id": "T1", "text": "b", "kind": "preserves", "receipt": {"file": "../secret.json", "key": "k"}, "mode": "new"},
                {"id": "", "text": "", "kind": "changes", "tests": []},
                {"id": "T2", "text": "neg", "kind": "rejects", "mode": "weak", "tests": ["t.f"]},
            ]
        })
        joined = "\n".join(errs_bad)
        self.assertIn("重複：T1", joined)
        self.assertIn("無效", joined)
        self.assertIn("路徑穿越", joined)
        self.assertIn("不可設定 mode", joined)
        self.assertIn("mode='weak' 無效", joined)

        # `rejects` with mode="preserves" must still reject a test that lacks any negative/rejection assertion!
        (self.repo_dir / "tests" / "test_pos_only.py").write_text(
            "import unittest\n\n"
            "class PosOnlyTests(unittest.TestCase):\n"
            "    def test_only_positive(self):\n"
            "        res = {'status': 'ok'}\n"
            "        self.assertEqual(res['status'], 'ok')\n",
            encoding="utf-8",
        )
        (self.repo_dir / ".github" / "workflows" / "ci.yml").write_text("pytest\n")
        errs_neg = clause_contract.validate_scenario_clauses(
            f"{EPIC}/AC-1.S1",
            "拒收非法值",
            [{"id": "T1", "text": "拒收非法值", "kind": "rejects", "mode": "preserves",
              "tests": ["tests/test_pos_only.py::PosOnlyTests::test_only_positive"]}],
            repo_root=self.repo_dir,
        )
        self.assertTrue(any("缺少否定／異常拒收斷言" in e for e in errs_neg), errs_neg)

    def test_ast_lint_catches_empty_tautology_exit_code_only_missing_syntax_and_traversal(self) -> None:
        (self.root / "outside.py").write_text("def test_evil(): assert True\n")
        (self.repo_dir / "tests" / "test_bad_ast.py").write_text(
            "import subprocess, unittest\n\n"
            "class BadAstTests(unittest.TestCase):\n"
            "    def test_pass_only(self):\n"
            "        '''only doc'''\n"
            "        pass\n\n"
            "    def test_tautology(self):\n"
            "        assert True\n"
            "        self.assertEqual(1, 1)\n"
            "        self.assertTrue(True)\n\n"
            "    def test_exit_code_only(self):\n"
            "        res = subprocess.run(['true'])\n"
            "        self.assertEqual(res.returncode, 0, f'err={res.stderr}')\n"
            "        assert subprocess.run(['true']).returncode == 0\n\n"
            "    def test_hidden_inner_assert(self):\n"
            "        def inner():\n"
            "            assert False\n"
            "        pass\n\n"
            "    def test_hidden_lambda(self):\n"
            "        f = lambda: self.assertEqual(1, 2)\n"
            "        x = 1\n\n"
            "    def test_const_negative_tautology(self):\n"
            "        assert 1 != 2\n"
            "        assert not False\n"
            "        self.assertNotEqual(1, 2)\n"
            "        self.assertTrue(1 == 1)\n",
            encoding="utf-8",
        )
        (self.repo_dir / "tests" / "test_syntax_err.py").write_text("def broken(:\n", encoding="utf-8")
        (self.repo_dir / ".github" / "workflows" / "ci.yml").write_text("python -m unittest discover -s tests\n")

        cases = [
            ("../outside.py::test_evil", "路徑穿越"),
            ("tests/test_bad_ast.py::test_evil\x00bad", "無效"),
            ("tests/test_bad_ast.py::BadAstTests::test_pass_only", "空測試"),
            ("tests/test_bad_ast.py::BadAstTests::test_tautology", "恆真斷言"),
            ("tests/test_bad_ast.py::BadAstTests::test_exit_code_only", "只檢查 exit code"),
            ("tests/test_bad_ast.py::BadAstTests::test_hidden_inner_assert", "缺少實質斷言"),
            ("tests/test_bad_ast.py::BadAstTests::test_hidden_lambda", "缺少實質斷言"),
            ("tests/test_bad_ast.py::BadAstTests::test_const_negative_tautology", "恆真斷言"),
            ("tests/test_bad_ast.py::BadAstTests::test_missing_method", "找不到測試方法"),
            ("tests/test_syntax_err.py::test_any", "無法解析"),
        ]
        for tid, needle in cases:
            with self.subTest(tid=tid):
                errs = clause_contract.validate_scenario_clauses(
                    f"{EPIC}/AC-1.S1",
                    "check",
                    [{"id": "T1", "text": "check", "kind": "changes", "tests": [tid]}],
                    repo_root=self.repo_dir,
                )
                self.assertTrue(any(needle in e for e in errs), (tid, errs))

    def test_runner_reachability_and_coverage_resist_bypasses(self) -> None:
        (self.repo_dir / "orphan").mkdir()
        (self.repo_dir / "orphan" / "test_unwired.py").write_text(
            "def test_never_run():\n    x = 1 + 1\n    assert x == 2\n"
        )
        (self.repo_dir / "tests" / "sub").mkdir(parents=True)
        (self.repo_dir / "tests" / "sub" / "test_sub.py").write_text(
            "def test_in_sub():\n    x = 1 + 1\n    assert x == 2\n"
        )
        (self.repo_dir / "tests" / "test_wired.py").write_text(
            "def test_in_tests():\n    x = 1 + 1\n    assert x == 2\n"
        )
        # Comment mentioning pytest + `pip install pytest` + pattern `-p test_wired.py` must NOT mark orphan/sub reachable!
        (self.repo_dir / ".github" / "workflows" / "ci.yml").write_text(
            "# pytest 已支援\n"
            "run: pip install -q pytest && python3 -m unittest discover -s tests -p test_wired.py\n"
        )
        for bad_tid in ("orphan/test_unwired.py::test_never_run", "tests/sub/test_sub.py::test_in_sub"):
            errs_bad = clause_contract.validate_scenario_clauses(
                f"{EPIC}/AC-1.S1",
                "孤兒",
                [{"id": "T1", "text": "孤兒", "kind": "changes", "tests": [bad_tid]}],
                repo_root=self.repo_dir,
            )
            self.assertTrue(any("未被任何 CI workflow 或 .devflow runner 執行" in e for e in errs_bad), (bad_tid, errs_bad))

        # Narrow `pytest -q tests/test_wired.py` does NOT cover orphan or other tests; covers only `test_wired.py`.
        (self.repo_dir / ".github" / "workflows" / "ci.yml").write_text("run: pytest -q tests/test_wired.py\n")
        errs_sub = clause_contract.validate_scenario_clauses(
            f"{EPIC}/AC-1.S1",
            "狹義",
            [{"id": "T1", "text": "狹義", "kind": "changes", "tests": ["tests/sub/test_sub.py::test_in_sub"]}],
            repo_root=self.repo_dir,
        )
        self.assertTrue(any("未被任何 CI workflow 或 .devflow runner 執行" in e for e in errs_sub), errs_sub)

        errs_ok = clause_contract.validate_scenario_clauses(
            f"{EPIC}/AC-1.S1",
            "正常",
            [{"id": "T1", "text": "正常", "kind": "changes", "tests": ["tests/test_wired.py::test_in_tests"]}],
            repo_root=self.repo_dir,
        )
        self.assertEqual(errs_ok, [], errs_ok)

        # Coverage anti-recombination & short-tail check:
        bypass_cases = [
            (
                "舊版新增首錯",
                [{"id": "T1", "text": "舊版回 revoked_other，診斷版新增首錯與場內順序", "kind": "manual"},
                 {"id": "T2", "text": "無可靠信號則分類未知", "kind": "manual"}],
            ),
            (
                "invalid input succeeds",
                [{"id": "T1", "text": "valid input succeeds", "kind": "manual"},
                 {"id": "T2", "text": "invalid token fails", "kind": "manual"}],
            ),
            (
                "分享順序",
                [{"id": "T1", "text": "重放分享", "kind": "manual"},
                 {"id": "T2", "text": "順序相同", "kind": "manual"}],
            ),
            (
                "兩版在各種不同設定下都能正常執行且結果完全相同，新指標",
                [{"id": "T1", "text": "兩版在各種不同設定下都能正常執行且結果完全相同", "kind": "manual"}],
            ),
            (
                "不合法輸入通過",
                [{"id": "T1", "text": "合法輸入通過", "kind": "manual"},
                 {"id": "T2", "text": "不得遺漏", "kind": "manual"}],
            ),
        ]
        for claim, cls in bypass_cases:
            cov_errs = clause_contract.check_clause_coverage(f"{EPIC}/AC-1.S1", claim, cls)
            self.assertTrue(cov_errs, (claim, cov_errs))

    def test_cli_check_clauses_and_require_then_clauses_policy_flag(self) -> None:
        # Policy override via env & .devflow/config.env
        old = os.environ.get("DEVFLOW_REQUIRE_THEN_CLAUSES")
        try:
            os.environ["DEVFLOW_REQUIRE_THEN_CLAUSES"] = "true"
            cfg = policy_lib.load_policy(KIT, {})
            self.assertTrue(cfg["require_then_clauses"])
            self.assertFalse(policy_lib.repo_require_then_clauses({"DEVFLOW_REQUIRE_THEN_CLAUSES": "false"}, cfg))
            self.assertTrue(policy_lib.repo_require_then_clauses({"DEVFLOW_REQUIRE_THEN_CLAUSES": "true"}, {"require_then_clauses": False}))
        finally:
            if old is None:
                os.environ.pop("DEVFLOW_REQUIRE_THEN_CLAUSES", None)
            else:
                os.environ["DEVFLOW_REQUIRE_THEN_CLAUSES"] = old

        # Legacy scenario without clauses fails when `.devflow/config.env` sets DEVFLOW_REQUIRE_THEN_CLAUSES=true
        (self.repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_REQUIRE_THEN_CLAUSES=true\n")
        self._seed_t98496ab2_repo("純文字舊版 Then。")
        cards = {
            EPIC: {
                "title": "[EPIC] 需要契約",
                "body": "## 目標\n契約\n\n## 驗收\n- AC-1 [lib] 必有子句\n",
                "status": "todo",
                "assignee": "orchestrator",
                "workspace_kind": "dir",
                "parents": ["t_s1"],
            },
            "t_s1": {
                "title": "[SHIP] 部署",
                "body": f"## 目標\n部署\n\n## 驗收\n覆蓋：AC-1\nEPIC: {EPIC}",
                "status": "todo",
                "assignee": "devops",
                "workspace_kind": "dir",
                "parents": [],
            },
        }
        self.df.plan_cards = lambda board, epic: cards
        buf = io.StringIO()
        code = 0
        try:
            with redirect_stdout(buf):
                self.df.cmd_check_clauses(SimpleNamespace(slug="b", epic=EPIC, dir=None, require=False))
        except SystemExit as exc:
            code = int(exc.code)
        rep = json.loads(buf.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("必須在 .devflow/scenarios.json 宣告結構化 Then 子句" in f for f in rep["findings"]), rep)

        # Even inside a kanban worker (`HERMES_KANBAN_TASK`), read-only `devflow check clauses` is allowed!
        clauses_ok = [
            {"id": "T1", "text": "新增首錯", "kind": "changes",
             "tests": ["test_tx_failure_receipts.TXRuntimeFailureTests.test_runtime_first_failure_and_order_reach_persistence"]},
        ]
        self._seed_t98496ab2_repo(clauses_ok)
        old_kt = os.environ.get("HERMES_KANBAN_TASK")
        os.environ["HERMES_KANBAN_TASK"] = "t_worker1"
        try:
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                self.df.cmd_check_clauses(SimpleNamespace(slug="b", epic=EPIC, dir=str(self.repo_dir), require=True))
            rep2 = json.loads(buf2.getvalue())
            self.assertTrue(rep2["ok"], rep2)
            self.assertEqual(rep2["clauses_by_scenario"][f"{EPIC}/AC-1.S1"][0]["id"], "T1")
        finally:
            if old_kt is None:
                os.environ.pop("HERMES_KANBAN_TASK", None)
            else:
                os.environ["HERMES_KANBAN_TASK"] = old_kt


if __name__ == "__main__":
    unittest.main()
