"""Unified zero-hardcode declarative TOML lint engine (`lib/lint_engine.py` + `rules/workflow/*.toml`)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import lint_engine  # noqa: E402


class BuiltinWorkflowRules(unittest.TestCase):
    def test_shipped_workflow_rules_load_and_pass_self_tests(self) -> None:
        rules_dir = KIT / "rules" / "workflow"
        rules = lint_engine.load_rules(rules_dir)
        self.assertGreaterEqual(len(rules), 4)
        self.assertEqual(
            {"sole_publisher_conflict", "forbids_pr_creation", "draft_pr_only", "deploy_policy_mismatch"}
            - {r.id for r in rules},
            set(),
        )
        self.assertEqual(lint_engine.self_test_dir(rules_dir), [])

    def test_deploy_policy_mismatch_cross_file_detection(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-xfile-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / ".devflow").mkdir()
        (d / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY_MANUAL_ONLY=false\n", encoding="utf-8")
        (d / "AGENTS.md").write_text("# Policy\n\nmerge 不能觸發部署，需手動執行 deploy.yml。\n", encoding="utf-8")
        rules = lint_engine.load_rules(KIT / "rules" / "workflow")
        findings = lint_engine.scan_repo(rules, d)
        ids = {f["id"] for f in findings}
        self.assertIn("deploy_policy_mismatch", ids, findings)

        # When DEVFLOW_DEPLOY_MANUAL_ONLY=true, no mismatch
        (d / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY_MANUAL_ONLY=true\n", encoding="utf-8")
        self.assertEqual([f for f in lint_engine.scan_repo(rules, d) if f["id"] == "deploy_policy_mismatch"], [])


class ImportBoundaryAndDiffPolicy(unittest.TestCase):
    def test_import_boundary_catches_python_ast_and_js_imports_and_respects_exemption(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-imp-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        rules_dir = d / "rules"
        rules_dir.mkdir()
        (rules_dir / "no-infra-in-domain.toml").write_text(
            'id = "no-infra-in-domain"\n'
            'kind = "import_boundary"\n'
            'severity = "error"\n'
            'message = "domain module must not import infra"\n'
            'include = ["src/domain/**/*.py", "web/domain/**/*.ts"]\n'
            'forbidden_modules = ["src.infra", "infra/"]\n'
            "\n"
            "[[fixture]]\n"
            'expect = "fail"\n'
            'path = "src/domain/order.py"\n'
            'text = "from src.infra.db import connect\\n"\n'
            "\n"
            "[[fixture]]\n"
            'expect = "pass"\n'
            'path = "src/domain/order.py"\n'
            'text = "from src.domain.types import Order\\n"\n',
            encoding="utf-8",
        )
        rules = lint_engine.load_rules(rules_dir)
        self.assertEqual(lint_engine.self_test_rules(rules), [])

        (d / "src" / "domain").mkdir(parents=True)
        (d / "web" / "domain").mkdir(parents=True)
        (d / "src" / "domain" / "bad.py").write_text("import src.infra.http\n", encoding="utf-8")
        (d / "src" / "domain" / "fromsub.py").write_text("from src import infra\n", encoding="utf-8")
        (d / "src" / "domain" / "relbad.py").write_text("from .. import infra\n", encoding="utf-8")
        (d / "src" / "domain" / "ok.py").write_text("import src.infra.http  # devflow: allow no-infra-in-domain L-0001\n", encoding="utf-8")
        (d / "src" / "domain" / "ok2.py").write_text("from src import types\n", encoding="utf-8")
        (d / "web" / "domain" / "bad.ts").write_text('import { client } from "infra/api";\n', encoding="utf-8")

        findings = lint_engine.scan_repo(rules, d)
        paths = sorted(f["file"] for f in findings)
        self.assertEqual(paths, ["src/domain/bad.py", "src/domain/fromsub.py", "src/domain/relbad.py", "web/domain/bad.ts"], findings)

        # Relative parent + child alias (`from ...src import infra` from src/domain/mod.py)
        rel_sub_rule = lint_engine.load_rule(rules_dir / "no-infra-in-domain.toml")
        rel_hits = lint_engine.scan_files(
            [rel_sub_rule],
            {"src/domain/other.py": "from ...src import types\n", "src/domain/nested.py": "from ...src import infra\n"},
        )
        # Verify single-line multi-alias doesn't emit duplicates
        dedup_hits = lint_engine.scan_files(
            [rel_sub_rule],
            {"src/domain/multi.py": "from src.infra import a, b\n"},
        )
        self.assertEqual(len(dedup_hits), 1)
        self.assertEqual(len(rel_hits), 1)

    def test_diff_policy_enforces_max_files_and_added_lines(self) -> None:
        rule_text = (
            'id = "small-pr-budget"\n'
            'kind = "diff_policy"\n'
            'severity = "error"\n'
            'message = "PR diff exceeds budget"\n'
            "max_files = 2\n"
            "max_added_lines = 5\n"
            'exclude = ["tests/**"]\n'
            "\n"
            "[[fixture]]\n"
            'expect = "fail"\n'
            'changed_files = ["a.py", "b.py", "c.py"]\n'
            "added_lines = 3\n"
            "\n"
            "[[fixture]]\n"
            'expect = "pass"\n'
            'changed_files = ["a.py", "tests/test_a.py", "tests/test_b.py"]\n'
            "added_lines = 5\n"
        )
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-diff-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "small-pr-budget.toml").write_text(rule_text, encoding="utf-8")
        rules = lint_engine.load_rules(d)
        self.assertEqual(lint_engine.self_test_rules(rules), [])
        errs = lint_engine.evaluate_diff_policy(rules, changed_files=["a.py", "b.py"], added_lines=9)
        self.assertEqual(len(errs), 1)
        self.assertIn("added_lines", errs[0]["detail"])

    def test_rule_without_both_pass_and_fail_fixtures_fails_self_test(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-badfix-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "missing-pass.toml").write_text(
            'id = "missing-pass"\n'
            'kind = "regex"\n'
            'message = "bad"\n'
            'include = ["*.py"]\n'
            'patterns = ["foo"]\n'
            "[[fixture]]\n"
            'expect = "fail"\n'
            'text = "foo()"\n',
            encoding="utf-8",
        )
        rules = lint_engine.load_rules(d)
        errs = lint_engine.self_test_rules(rules)
        self.assertTrue(any("pass" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
