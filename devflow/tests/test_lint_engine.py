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

    def test_audit_fixes_prune_worktrees_root_glob_xfile_bypass_js_comments_syntax_and_diff_budget(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-pr-f-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # 1. Root-only AGENTS.md vs nested sub/AGENTS.md & .worktrees/x/AGENTS.md
        (d / ".devflow").mkdir()
        (d / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY_MANUAL_ONLY=false\n", encoding="utf-8")
        (d / "AGENTS.md").write_text(
            "# Root clean policy\n# devflow: allow deploy_policy_mismatch\nmerge 不能觸發部署\n"
            "正文結尾：merge 不能觸發部署（未豁免）。\n",
            encoding="utf-8",
        )
        (d / "sub").mkdir()
        (d / "sub" / "AGENTS.md").write_text("merge 不能觸發部署\n", encoding="utf-8")
        (d / ".worktrees" / "wt1").mkdir(parents=True)
        (d / ".worktrees" / "wt1" / "AGENTS.md").write_text("merge 不能觸發部署\n", encoding="utf-8")

        wf_rules = lint_engine.load_rules(KIT / "rules" / "workflow")
        findings = lint_engine.scan_repo(wf_rules, d)
        # Earlier exempt match on line 3 MUST NOT hide line 4; sub/ and .worktrees/ MUST NOT match root "AGENTS.md"
        self.assertEqual([f["file"] for f in findings], ["AGENTS.md"], findings)
        self.assertEqual(findings[0]["line"], 4, findings)

        # Previous-line exemption suppresses the violation cleanly
        (d / "AGENTS.md").write_text(
            "# devflow: allow deploy_policy_mismatch\nmerge 不能觸發部署\n",
            encoding="utf-8",
        )
        self.assertEqual(lint_engine.scan_repo(wf_rules, d), [])

        # 2. JS comments stripped, dynamic import() caught, Python SyntaxError fails closed, diff_budget alias
        rules_dir = d / "rules"
        rules_dir.mkdir()
        (rules_dir / "imp.toml").write_text(
            'id = "imp"\nkind = "import_boundary"\nmessage = "no infra"\n'
            'include = ["src/**/*.py", "web/**/*.ts", "web/**/*.tsx"]\nforbidden_modules = ["infra", "infra/"]\n'
            '[[fixture]]\nexpect = "fail"\npath = "src/a.py"\ntext = "import infra\\n"\n'
            '[[fixture]]\nexpect = "pass"\npath = "src/a.py"\ntext = "import app\\n"\n',
            encoding="utf-8",
        )
        (rules_dir / "budget.toml").write_text(
            'id = "budget"\nkind = "diff_budget"\nmessage = "too big"\nmax_files = 1\n'
            '[[fixture]]\nexpect = "fail"\nchanged_files = ["a.py", "b.py"]\n'
            '[[fixture]]\nexpect = "pass"\nchanged_files = ["a.py"]\n',
            encoding="utf-8",
        )
        self.assertEqual(lint_engine.self_test_dir(rules_dir), [])
        imp_rule = lint_engine.load_rule(rules_dir / "imp.toml")
        hits = lint_engine.scan_files(
            [imp_rule],
            {
                "web/clean.ts": (
                    '// import { a } from "infra/x";\n'
                    '/* export * from "infra/y"; */\n'
                    'const documentation = "import(\'infra/private\')";\n'
                    "const singleq = 'require(\"infra/private\")';\n"
                    'const escaped = "a \\"import(\'infra/private\')\\" b";\n'
                    "const tpl = `hello ${documentation}\nimport('infra/private')\nrequire('infra/x')`;\n"
                    'const member = loader.import("infra/not_module");\n'
                    "const re = /import\\('infra\\/fake'\\)/;\n"
                    "if (member) /import\\('infra\\/cond_fake'\\)/;\n"
                    "if (member) {} /import\\('infra\\/block_fake'\\)/g;\n"
                    "function decl() {} /import\\('infra\\/decl_fake'\\)/g;\n"
                    "class Decl {} /import\\('infra\\/cls_fake'\\)/g;\n"
                    "{ const blk = 1; } /import\\('infra\\/stmt_fake'\\)/g;\n"
                    "switch (member) { case 1: { break; } /import\\('infra\\/sw_fake'\\)/g; }\n"
                    "const negRe = !/import\\('infra\\/neg_fake'\\)/.test(u);\n"
                    "const dblNeg = !!/import\\('infra\\/dblneg_fake'\\)/.test(u);\n"
                    "for (const m of /import\\('infra\\/for_of_fake'\\)/g.exec(u) || []) {}\n"
                    'const u = "https://x";\n'
                ),
                "web/dyn.ts": 'const mod = await import("infra/secret");\n',
                "web/tpl_real.ts": "const x = `${await import('infra/tpl_secret')}`;\n",
                "web/req.ts": "const r = require('infra/sync_client');\n",
                "web/div.ts": 'const ratio = 1 / 2; import x from "infra/after_div";\n',
                "web/div_of.ts": 'const of = 10; const r = of / 2; import x from "infra/of_div";\n',
                "web/div_ts_bang.ts": 'const ratio = value! / 2; import secret from "infra/private";\n',
                "web/div_ts_bang_tsx.tsx": 'const ratio = fn(value)!! / 2; const ui = <Box v={arr[0]! / 2}>don\'t</Box>; import secret from "infra/tsx_bang";\n',
                "web/div_obj.ts": 'const ratio = {} / 2; import x from "infra/after_obj";\n',
                "web/div_obj_dyn.ts": 'const o = { k: 1, m() {} } / 2; const m = await import("infra/after_obj_dyn");\n',
                "web/div_fn_cls.ts": 'const f = function() {} / 2; const c = class {} / 2; import y from "infra/after_fn_cls";\n',
                "web/div_block_obj.ts": 'if (ok) { const r = {} / 2; }\nimport b from "infra/after_block_obj";\n',
                "web/div_ternary_obj.ts": 'const q = ok ? 1 : {} / 2; import t from "infra/after_ternary_obj";\n',
                "web/div_expr.ts": 'const r = (1 + 2) / 3; const a = arr[0] / 2; const p = i++ / 2; import y from "infra/after_expr";\n',
                "web/div_tpl.ts": "const t = `${x}` / 2; import z from \"infra/after_tpl\";\n",
                "web/div_if.ts": 'if (ok) /ok/g; import w from "infra/after_if_regex";\n',
                "web/div_prop.ts": 'const q = cfg.default / 2; import p from "infra/after_kw_prop";\n',
                "web/div_jsx.tsx": 'const el = <div>a</div><div>b</div>; import j from "infra/after_jsx";\n',
                "web/div_jsx_self.tsx": 'const el = <Box /> / 2; import j2 from "infra/after_jsx_self";\n',
                "web/jsx_apostrophe.tsx": 'const el = <div>don\'t</div>; import x from "infra/after_jsx_apo";\n',
                "web/jsx_quote.tsx": 'const el = <>Say "hi {await import("infra/inside_jsx")}</>; const r = await import("infra/after_jsx_q");\n',
                "web/ts_cast_apo.ts": 'const s = <string>\'ok\'; const q = <T>(x: T) => x; import y from "infra/after_ts_cast";\n',
                "src/broken.py": "def bad(:\n",
            },
        )
        by_file = {h["file"]: h for h in hits}
        self.assertNotIn("web/clean.ts", by_file)
        for bad_js in (
            "web/dyn.ts",
            "web/tpl_real.ts",
            "web/req.ts",
            "web/div.ts",
            "web/div_of.ts",
            "web/div_ts_bang.ts",
            "web/div_ts_bang_tsx.tsx",
            "web/div_obj.ts",
            "web/div_obj_dyn.ts",
            "web/div_fn_cls.ts",
            "web/div_block_obj.ts",
            "web/div_ternary_obj.ts",
            "web/div_expr.ts",
            "web/div_tpl.ts",
            "web/div_if.ts",
            "web/div_prop.ts",
            "web/div_jsx.tsx",
            "web/div_jsx_self.tsx",
            "web/jsx_apostrophe.tsx",
            "web/jsx_quote.tsx",
            "web/ts_cast_apo.ts",
        ):
            self.assertIn(bad_js, by_file, bad_js)
        self.assertEqual(by_file["web/jsx_quote.tsx"]["match"], "infra/inside_jsx")
        self.assertEqual(by_file["src/broken.py"]["match"], "SyntaxError")

    def test_preflight_enforces_repo_diff_policy_rules(self) -> None:
        import json
        import subprocess
        from lib import policy as policy_lib  # noqa: E402
        from lib import preflight as preflight_lib  # noqa: E402

        repo = Path(tempfile.mkdtemp(prefix="devflow-preflight-diff-"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=repo, check=True)
        (repo / ".devflow" / "rules").mkdir(parents=True)
        (repo / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY=false\n", encoding="utf-8")
        (repo / ".devflow" / "rules" / "small-diff.toml").write_text(
            'id = "small-diff"\nkind = "diff_policy"\nseverity = "error"\nmessage = "PR 過大"\n'
            "max_files = 2\nmax_added_lines = 4\nexclude = [\"tests/**\"]\n"
            "[[fixture]]\nexpect = \"fail\"\nchanged_files = [\"a.py\", \"b.py\", \"c.py\"]\nadded_lines = 2\n"
            "[[fixture]]\nexpect = \"pass\"\nchanged_files = [\"a.py\"]\nadded_lines = 2\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
        base_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("A = 1\nB = 2\nC = 3\nD = 4\nE = 5\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "over-lines"], cwd=repo, check=True)

        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        pol = policy_lib.load_policy(KIT, roles)
        card = "## 目標\nx\n\n## 本卡完成\nok\n\n## 驗收\nAllowed-Scope: src/**\n"
        rep = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_sha, card_body=card, run_tests=False)
        self.assertFalse(rep["ok"], rep)
        self.assertEqual(rep["checks"].get("diff_policy"), "fail")
        self.assertTrue(any("added_lines=5 > 4" in f or "PR 過大" in f for f in rep["findings"]), rep["findings"])

    def test_character_class_globs_preserve_support_and_anchor_root(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-charcls-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # Positive & negated classes, ranges, leading literal ], non-crossing '/'
        self.assertTrue(lint_engine.glob_matches("src/a/x.py", "src/[ab]/*.py"))
        self.assertTrue(lint_engine.glob_matches("src/b/x.py", "src/[ab]/*.py"))
        self.assertFalse(lint_engine.glob_matches("src/c/x.py", "src/[ab]/*.py"))
        self.assertTrue(lint_engine.glob_matches("src/b/x.py", "src/[a-c]/*.py"))
        self.assertFalse(lint_engine.glob_matches("src/d/x.py", "src/[a-c]/*.py"))
        self.assertTrue(lint_engine.glob_matches("src/c/x.py", "src/[!a]/*.py"))
        self.assertFalse(lint_engine.glob_matches("src/a/x.py", "src/[!a]/*.py"))
        self.assertFalse(lint_engine.glob_matches("src/c/sub/x.py", "src/[!a]/*.py"))
        self.assertTrue(lint_engine.glob_matches("src/c/x.py", "src/[^a]/*.py"))
        self.assertFalse(lint_engine.glob_matches("src/a/x.py", "src/[^a]/*.py"))
        self.assertTrue(lint_engine.glob_matches("src/]/x.py", "src/[]a]/*.py"))
        self.assertTrue(lint_engine.glob_matches("src/a/x.py", "src/[]a]/*.py"))
        self.assertTrue(lint_engine.glob_matches("src/[a.py", "src/[a.py"))
        self.assertFalse(lint_engine.glob_matches("sub/AGENTS.md", "AGENTS.md"))
        self.assertTrue(lint_engine.glob_matches("AGENTS.md", "AGENTS.md"))

        rules_dir = d / "rules"
        rules_dir.mkdir()
        (rules_dir / "cls-imp.toml").write_text(
            'id = "cls-imp"\nkind = "import_boundary"\nmessage = "no infra"\n'
            'include = ["src/[ab]/*.py"]\nexclude = ["src/[b]/*.py"]\nforbidden_modules = ["infra"]\n'
            '[[fixture]]\nexpect = "fail"\npath = "src/a/x.py"\ntext = "import infra\\n"\n'
            '[[fixture]]\nexpect = "pass"\npath = "src/c/x.py"\ntext = "import infra\\n"\n',
            encoding="utf-8",
        )
        self.assertEqual(lint_engine.self_test_dir(rules_dir), [])
        rule = lint_engine.load_rule(rules_dir / "cls-imp.toml")
        hits = lint_engine.scan_files(
            [rule],
            {
                "src/a/x.py": "import infra\n",
                "src/b/x.py": "import infra\n",
                "src/c/x.py": "import infra\n",
            },
        )
        self.assertEqual([h["file"] for h in hits], ["src/a/x.py"], hits)

        # Invalid character range fails closed at load time
        bad_rule = rules_dir / "bad-range.toml"
        bad_rule.write_text(
            'id = "bad-range"\nkind = "import_boundary"\nmessage = "bad"\n'
            'include = ["src/[z-a]/*.py"]\nforbidden_modules = ["infra"]\n',
            encoding="utf-8",
        )
        with self.assertRaises(lint_engine.LintConfigError):
            lint_engine.load_rule(bad_rule)


if __name__ == "__main__":
    unittest.main()
