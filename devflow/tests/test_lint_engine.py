"""Unified zero-hardcode declarative TOML lint engine (`lib/lint_engine.py` + `rules/workflow/*.toml`)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import lint_engine  # noqa: E402
from lib import policy as policy_lib  # noqa: E402
from lib import preflight as preflight_lib  # noqa: E402


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
            'include = ["src/**/*.py", "web/**/*.js", "web/**/*.ts", "web/**/*.tsx"]\nforbidden_modules = ["infra", "infra/"]\n'
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
                    '/* import { z } from "infra/commented";\nexport * from "infra/commented_exp"; */\n'
                    'const u = "https://x";\n'
                ),
                "web/clean_ws.js": (
                    "#!/usr/bin/env node\r"
                    "\ufeffconst\u00a0a = 1\r"
                    "const re = /import\\('infra\\/re_cr'\\)/g\r"
                    '/*\u2028import x from "infra/cmt_ls";\r*/\r'
                ),
                "web/clean_ops.ts": (
                    'const x = 1 < /import("infra")/.source.length;\n'
                    'const x2 = 1</import("infra")/.source.length;\n'
                    'const y = 1 / /import("infra")/.source.length;\n'
                    'const y2 = 1 / 2 / /import("infra")/.source.length;\n'
                    'const z = 1 > /import("infra")/.source.length;\n'
                    'const sp = [.../import("infra")/.source];\n'
                    'const nu = a ?? /import("infra")/.source; a ??= /import("infra")/;\n'
                    'const nb = a ?? b\nlabel: /import("infra")/.test(x);\n'
                    'const ins = a instanceof /import("infra")/; const nw = new /import("infra")/;\n'
                    'const opt = obj?.import("infra/not_module");\n'
                ),
                "web/after_lt_re.ts": 'const x = 1 < /import("infra\\/ok")/.source.length; import a from "infra/after_lt_re";\n',
                "web/after_div_re.ts": 'const y = 1 / /import("infra\\/ok")/.source.length; import b from "infra/after_div_re";\n',
                "web/after_div_obj.ts": 'const o1 = 1 / {} / 2; import c from "infra/after_div_obj";\n',
                "web/after_lt_obj.ts": 'const o2 = 1 < {} / 2; import d from "infra/after_lt_obj";\n',
                "web/after_ts_gen.ts": 'const g = fn<T>(x) / 2; import e from "infra/after_ts_gen";\n',
                "web/after_opt_kw.ts": 'const q = cfg?.default / 2; import f from "infra/after_opt_kw";\n',
                "web/after_ter_dec.ts": 'const t = ok ? .5 : {} / 2; import g2 from "infra/after_ter_dec";\n',
                "web/after_jsx_cmt.tsx": 'const el = <Box /* cmt */ >don\'t</Box>; const m = await import("infra/after_jsx_cmt"); const r = 1 / 2;\n',
                "web/bom.ts": '\ufeffimport x from "infra/bom";\n',
                "web/hashbang.js": '#!/usr/bin/env node\nimport x from "infra/hashbang";\n',
                "web/bom_hashbang.js": '\ufeff#!/usr/bin/env node\r\nimport x from "infra/bom_hb";\r\n',
                "web/nbsp.ts": '\u00a0import\u00a0x\u00a0from\u00a0"infra/nbsp";\n',
                "web/ecmas_ws.ts": '\v\f\u2003import\u2003{ a }\u00a0from\ufeff"infra/ecmas_ws";\n',
                "web/cr_asi.js": 'const x = 1\rimport y from "infra/cr_asi";\r',
                "web/cr_export.js": 'const x = 1\rexport { y } from "infra/cr_exp";\r',
                "web/crlf_asi.js": 'const x = 1\r\nimport y from "infra/crlf_asi";\r\n',
                "web/jsx_ws.tsx": 'const\u00a0el = <Box\u00a0v={1}\u2028>don\'t</Box\u00a0>;\rimport\u00a0j\u00a0from\u00a0"infra/jsx_ws";\r',
                "web/div_ls.ts": 'const r = 1 / 2\u2028const m = await import("infra/after_ls");\u2029',
                "web/dyn.ts": 'const mod = await import("infra/secret");\n',
                "web/tpl_real.ts": "const x = `${await import('infra/tpl_secret')}`;\n",
                "web/req.ts": "const r = require('infra/sync_client');\n",
                "web/mc_imp.ts": 'const n = 1 /*\n*/ import x from "infra/mc_imp";\n',
                "web/mc_exp.ts": 'const n = 1 /*\r\n*/ export { x } from "infra/mc_exp";\n',
                "web/esc_hex.ts": 'import x from "\\x69nfra/hex";\n',
                "web/esc_u4.ts": 'const m = await import("\\u0069nfra/u4");\n',
                "web/esc_ucp.ts": 'const r = require("\\u{69}nfra/ucp");\n',
                "web/esc_tpl.ts": 'const t = await import(`\\x69nfra/tpl_hex`);\n',
                "web/esc_oct.ts": 'import o from "\\151nfra/oct";\n',
                "web/esc_cont.ts": 'export * from "in\\\nfra/cont";\n',
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
        self.assertNotIn("web/clean_ws.js", by_file)
        self.assertNotIn("web/clean_ops.ts", by_file)
        for bad_js in (
            "web/after_lt_re.ts",
            "web/after_div_re.ts",
            "web/after_div_obj.ts",
            "web/after_lt_obj.ts",
            "web/after_ts_gen.ts",
            "web/after_opt_kw.ts",
            "web/after_ter_dec.ts",
            "web/after_jsx_cmt.tsx",
            "web/bom.ts",
            "web/hashbang.js",
            "web/bom_hashbang.js",
            "web/nbsp.ts",
            "web/ecmas_ws.ts",
            "web/cr_asi.js",
            "web/cr_export.js",
            "web/crlf_asi.js",
            "web/jsx_ws.tsx",
            "web/div_ls.ts",
            "web/dyn.ts",
            "web/tpl_real.ts",
            "web/req.ts",
            "web/mc_imp.ts",
            "web/mc_exp.ts",
            "web/esc_hex.ts",
            "web/esc_u4.ts",
            "web/esc_ucp.ts",
            "web/esc_tpl.ts",
            "web/esc_oct.ts",
            "web/esc_cont.ts",
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
        self.assertEqual(by_file["web/bom.ts"]["line"], 1)
        self.assertEqual(by_file["web/hashbang.js"]["line"], 2)
        self.assertEqual(by_file["web/bom_hashbang.js"]["line"], 2)
        self.assertEqual(by_file["web/nbsp.ts"]["line"], 1)
        self.assertEqual(by_file["web/ecmas_ws.ts"]["line"], 1)
        self.assertEqual(by_file["web/cr_asi.js"]["line"], 2)
        self.assertEqual(by_file["web/cr_export.js"]["line"], 2)
        self.assertEqual(by_file["web/crlf_asi.js"]["line"], 2)
        self.assertEqual(by_file["web/jsx_ws.tsx"]["line"], 3)
        self.assertEqual(by_file["web/div_ls.ts"]["line"], 2)
        self.assertEqual(by_file["src/broken.py"]["match"], "SyntaxError")

    def test_preflight_enforces_repo_diff_policy_rules(self) -> None:
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

    def test_require_resolve_and_preflight_static_toml_rules_and_lexer_parity(self) -> None:
        # 1. Byte-for-byte parity between lint_engine.py and template check.py shared lexer block
        b_mark = "# --- BEGIN SHARED IMPORT LEXER ---"
        e_mark = "# --- END SHARED IMPORT LEXER ---"
        le_txt = (KIT / "lib" / "lint_engine.py").read_text(encoding="utf-8")
        cp_txt = (KIT / "templates" / "repo" / ".devflow" / "check.py").read_text(encoding="utf-8")
        self.assertEqual(
            le_txt[le_txt.index(b_mark) : le_txt.index(e_mark) + len(e_mark)],
            cp_txt[cp_txt.index(b_mark) : cp_txt.index(e_mark) + len(e_mark)],
        )

        # 2. require.resolve(...) vs member access / string literal
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-reqres-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        rfile = d / "imp.toml"
        rfile.write_text(
            'id = "imp"\nkind = "import_boundary"\nmessage = "no infra"\n'
            'include = ["web/**/*.js", "web/**/*.ts"]\nforbidden_modules = ["infra"]\n'
            '[[fixture]]\nexpect = "fail"\npath = "web/bad.js"\ntext = \'const p = require.resolve("infra/db");\'\n'
            '[[fixture]]\nexpect = "pass"\npath = "web/ok.js"\ntext = \'const p = obj.require.resolve("infra/db");\'\n',
            encoding="utf-8",
        )
        self.assertEqual(lint_engine.self_test_dir(d), [])
        rule = lint_engine.load_rule(rfile)
        hits = lint_engine.scan_files(
            [rule],
            {
                "web/a.js": 'const x = require.resolve("infra/db");\nconst y = require?.resolve("infra/cache");\n',
                "web/safe.ts": 'const a = obj.require.resolve("infra/db");\nconst s = "require.resolve(\'infra/db\')";\n',
            },
        )
        self.assertEqual([(h["file"], h["line"]) for h in hits], [("web/a.js", 1), ("web/a.js", 2)], hits)

        # 3. preflight runs static TOML rules when .devflow/check.sh is missing & scopes regex/import_boundary to touched files
        repo = d / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
        for cfg_k, cfg_v in (("user.name", "Dev"), ("user.email", "dev@example.com")):
            subprocess.run(["git", "-C", str(repo), "config", cfg_k, cfg_v], check=True)
        (repo / ".devflow" / "rules").mkdir(parents=True)
        (repo / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY=false\n", encoding="utf-8")
        (repo / ".devflow" / "rules" / "no-infra.toml").write_text(
            'id = "no-infra"\nkind = "import_boundary"\nmessage = "web must not import infra"\n'
            'include = ["web/**/*.js"]\nforbidden_modules = ["infra"]\n'
            '[[fixture]]\nexpect = "fail"\npath = "web/b.js"\ntext = \'require.resolve("infra/a")\'\n'
            '[[fixture]]\nexpect = "pass"\npath = "web/ok.js"\ntext = \'require("ui/a")\'\n',
            encoding="utf-8",
        )
        (repo / "web").mkdir()
        (repo / "web" / "legacy.js").write_text('require.resolve("infra/legacy");\n', encoding="utf-8")
        (repo / "web" / "ok.js").write_text('const v = 1;\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
        base_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        # Touch only web/ok.js cleanly -> legacy.js untouched violation does NOT block unrelated PR
        (repo / "web" / "ok.js").write_text('const v = 2;\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "web/ok.js"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "clean change"], check=True)
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        pol = policy_lib.load_policy(KIT, roles)
        card = "## 目標\nx\n\n## 本卡完成\nok\n\n## 驗收\nAllowed-Scope: web/**\n"
        rep_clean = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_sha, card_body=card, run_tests=True)
        self.assertTrue(rep_clean["ok"], rep_clean)
        self.assertEqual(rep_clean["checks"].get("static_rules"), "ok")

        # Now add a forbidden require.resolve import in web/ok.js -> preflight fails closed
        (repo / "web" / "ok.js").write_text('const db = require.resolve("infra/db");\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "web/ok.js"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "bad import"], check=True)
        rep_bad = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_sha, card_body=card, run_tests=True)
        self.assertFalse(rep_bad["ok"], rep_bad)
        self.assertEqual(rep_bad["checks"].get("static_rules"), "fail")
        self.assertTrue(any("no-infra" in f and "web/ok.js" in f for f in rep_bad["findings"]), rep_bad["findings"])

        # 4. Spoofed `.devflow/check.sh` mentioning `check.py` in comments or dead code must NOT bypass static_rules
        for label, script_body in (
            ("comment", "#!/bin/sh\n# TODO: invoke check.py\nexit 0\n"),
            ("dead_branch", "#!/bin/sh\nif false; then python3 .devflow/check.py; fi\nexit 0\n"),
        ):
            chk = repo / ".devflow" / "check.sh"
            chk.write_text(script_body, encoding="utf-8")
            chk.chmod(0o755)
            subprocess.run(["git", "-C", str(repo), "add", ".devflow/check.sh"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"spoof {label}"], check=True)
            rep_spoof = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_sha, card_body=card, run_tests=True)
            self.assertFalse(rep_spoof["ok"], (label, rep_spoof))
            self.assertEqual(rep_spoof["checks"].get("static_rules"), "fail", (label, rep_spoof))
            self.assertTrue(any("no-infra" in f and "web/ok.js" in f for f in rep_spoof["findings"]), (label, rep_spoof))

        # 5. `.devflow/baseline.json` suppresses matching legacy line counts while catching new violations
        (repo / "web" / "ok.js").write_text('const v = 2;\n', encoding="utf-8")
        (repo / ".devflow" / "baseline.json").write_text(
            json.dumps({
                "version": 1,
                "entries": [
                    {"rule": "no-infra", "path": "web/ok.js", "text": 'const db = require.resolve("infra/db");'},
                ],
            }),
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", ".devflow/baseline.json", "web/ok.js"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "baseline legacy"], check=True)
        base_2 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        (repo / "web" / "ok.js").write_text('const db = require.resolve("infra/db");\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "web/ok.js"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "baselined line"], check=True)
        rep_base = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_2, card_body=card, run_tests=True)
        self.assertTrue(rep_base["ok"], rep_base)
        self.assertEqual(rep_base["checks"].get("static_rules"), "ok")

        (repo / "web" / "ok.js").write_text(
            'const db = require.resolve("infra/db");\nconst db2 = require.resolve("infra/cache");\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "web/ok.js"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "new violation"], check=True)
        rep_new = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_2, card_body=card, run_tests=True)
        self.assertFalse(rep_new["ok"], rep_new)
        self.assertEqual(rep_new["checks"].get("static_rules"), "fail")
        self.assertTrue(any("no-infra" in f and "web/ok.js:2" in f for f in rep_new["findings"]), rep_new["findings"])

        # 6. Canonical `.devflow/rules/baseline.json` (written by check.py --write-baseline) is also honored
        canon_baseline = repo / ".devflow" / "rules" / "baseline.json"
        canon_baseline.write_text((repo / ".devflow" / "baseline.json").read_text(encoding="utf-8"), encoding="utf-8")
        (repo / ".devflow" / "baseline.json").unlink()
        (repo / "web" / "ok.js").write_text('const v = 3;\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "use canonical baseline path"], check=True)
        base_3 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        (repo / "web" / "ok.js").write_text('const db = require.resolve("infra/db");\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "web/ok.js"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "baselined line under rules/"], check=True)
        rep_canon = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_3, card_body=card, run_tests=True)
        self.assertTrue(rep_canon["ok"], rep_canon)
        self.assertEqual(rep_canon["checks"].get("static_rules"), "ok")

        (repo / "web" / "ok.js").write_text(
            'const db = require.resolve("infra/db");\nconst db2 = require.resolve("infra/cache");\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "web/ok.js"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "new violation under rules/"], check=True)
        rep_canon_new = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_3, card_body=card, run_tests=True)
        self.assertFalse(rep_canon_new["ok"], rep_canon_new)
        self.assertEqual(rep_canon_new["checks"].get("static_rules"), "fail")
        self.assertTrue(any("no-infra" in f and "web/ok.js:2" in f for f in rep_canon_new["findings"]), rep_canon_new["findings"])

    def test_regex_overlapping_patterns_dedup_per_line_and_baseline(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-regex-dedup-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        rule_toml = (
            'id = "no-sleep-fix"\n'
            'message = "avoid fixed sleep"\n'
            'include = ["**/*.py", "**/*.go"]\n'
            'patterns = [\n'
            r"  '\btime\.sleep\('," + "\n"
            r"  '\btime\.Sleep\('," + "\n"
            ']\n'
            '[[fixture]]\nexpect = "fail"\npath = "app/run.py"\ntext = "time.sleep(1)"\n'
            '[[fixture]]\nexpect = "pass"\npath = "app/run.py"\ntext = "wait_ready()"\n'
        )
        rfile = d / "no-sleep-fix.toml"
        rfile.write_text(rule_toml, encoding="utf-8")
        self.assertEqual(lint_engine.self_test_dir(d), [])
        rule = lint_engine.load_rule(rfile)
        hits = lint_engine.scan_files([rule], {"app/run.py": "    time.sleep(.5)\n"})
        self.assertEqual([(h["file"], h["line"]) for h in hits], [("app/run.py", 1)], hits)

        repo = d / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
        for cfg_k, cfg_v in (("user.name", "Dev"), ("user.email", "dev@example.com")):
            subprocess.run(["git", "-C", str(repo), "config", cfg_k, cfg_v], check=True)
        (repo / ".devflow" / "rules").mkdir(parents=True)
        (repo / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY=false\n", encoding="utf-8")
        (repo / ".devflow" / "rules" / "no-sleep-fix.toml").write_text(rule_toml, encoding="utf-8")
        (repo / ".devflow" / "rules" / "baseline.json").write_text(
            json.dumps({
                "version": 1,
                "entries": [{"rule": "no-sleep-fix", "path": "app/run.py", "text": "time.sleep(.5)"}],
            }),
            encoding="utf-8",
        )
        (repo / "app").mkdir()
        (repo / "app" / "run.py").write_text("def wait():\n    time.sleep(.5)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
        base_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        (repo / "app" / "run.py").write_text("def wait():\n    time.sleep(.5)\n    return True\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "app/run.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "touch baselined file cleanly"], check=True)
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        pol = policy_lib.load_policy(KIT, roles)
        card = "## 目標\nx\n\n## 本卡完成\nok\n\n## 驗收\nAllowed-Scope: app/**\n"
        rep = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_sha, card_body=card, run_tests=True)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["checks"].get("static_rules"), "ok")

    def test_regex_multiple_lines_and_exempt_lines_sorted_and_preflight(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="devflow-lint-regex-multi-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        rule_toml = (
            'id = "no-sleep-fix"\n'
            'message = "avoid fixed sleep"\n'
            'include = ["**/*.py"]\n'
            'patterns = [\n'
            r"  '\btime\.sleep\('," + "\n"
            r"  '\basyncio\.sleep\('," + "\n"
            r"  '\btime\.Sleep\('," + "\n"
            ']\n'
            '[[fixture]]\nexpect = "fail"\npath = "app/run.py"\ntext = "time.sleep(1)"\n'
            '[[fixture]]\nexpect = "pass"\npath = "app/run.py"\ntext = "wait_ready()"\n'
        )
        rfile = d / "no-sleep-fix.toml"
        rfile.write_text(rule_toml, encoding="utf-8")
        rule = lint_engine.load_rule(rfile)
        hits = lint_engine.scan_files(
            [rule],
            {
                "app/run.py": (
                    "time.sleep(.5)\n"
                    "time.sleep(2)\n"
                    "time.sleep(1)  # devflow: allow no-sleep-fix L-0001\n"
                    "wait_ready()\n"
                    "await asyncio.sleep(3)\n"
                ),
            },
        )
        self.assertEqual([(h["file"], h["line"]) for h in hits], [("app/run.py", 1), ("app/run.py", 2), ("app/run.py", 5)], hits)

        repo = d / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
        for cfg_k, cfg_v in (("user.name", "Dev"), ("user.email", "dev@example.com")):
            subprocess.run(["git", "-C", str(repo), "config", cfg_k, cfg_v], check=True)
        (repo / ".devflow" / "rules").mkdir(parents=True)
        (repo / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY=false\n", encoding="utf-8")
        (repo / ".devflow" / "rules" / "no-sleep-fix.toml").write_text(rule_toml, encoding="utf-8")
        (repo / ".devflow" / "rules" / "baseline.json").write_text(
            json.dumps({
                "version": 1,
                "entries": [{"rule": "no-sleep-fix", "path": "app/run.py", "text": "time.sleep(.5)"}],
            }),
            encoding="utf-8",
        )
        (repo / "app").mkdir()
        (repo / "app" / "run.py").write_text("time.sleep(.5)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
        base_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        (repo / "app" / "run.py").write_text(
            "time.sleep(.5)\ntime.sleep(2)\ntime.sleep(1)  # devflow: allow no-sleep-fix L-0001\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "app/run.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add new unbaselined sleep on line 2"], check=True)
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        pol = policy_lib.load_policy(KIT, roles)
        card = "## 目標\nx\n\n## 本卡完成\nok\n\n## 驗收\nAllowed-Scope: app/**\n"
        rep_fail = preflight_lib.run_preflight(repo, policy=pol, base_ref=base_sha, card_body=card, run_tests=True)
        self.assertFalse(rep_fail["ok"], rep_fail)
        self.assertEqual(rep_fail["checks"].get("static_rules"), "fail")
        self.assertTrue(any("no-sleep-fix" in f and "app/run.py:2" in f for f in rep_fail["findings"]), rep_fail["findings"])


if __name__ == "__main__":
    unittest.main()
