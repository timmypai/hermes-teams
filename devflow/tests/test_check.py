"""devflow repo checker (.devflow/check.py): run it the way CI does, in a throwaway git repo."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "repo" / ".devflow"

LESSON_RULE = """\
id = "L-0001"
title = "retry loop hid a race"
event = "https://github.com/o/r/pull/7"
root_cause = "worker slept instead of waiting for the job state"
category = "code-pattern"
[enforcement]
kind = "rule"
ref = "no-sleep-fix"
"""


class RepoCase(unittest.TestCase):
    """A throwaway git repo holding the template checker and rules."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="devflow-check-"))
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copytree(TEMPLATE / "rules", self.dir / ".devflow" / "rules")
        shutil.copy(TEMPLATE / "check.py", self.dir / ".devflow" / "check.py")
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)

    def write(self, rel: str, text: str) -> None:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
        subprocess.run(["git", "add", rel], cwd=self.dir, check=True)

    def check(self, *args: str) -> tuple[int, str]:
        p = subprocess.run([sys.executable, ".devflow/check.py", *args], cwd=self.dir,
                           capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr


class CheckRepo(RepoCase):

    def test_clean_repo_passes(self) -> None:
        self.write("app/ok.py", "def add(a, b):\n    return a + b\n")
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_new_violation_fails_with_location(self) -> None:
        self.write("app/worker.py", "import time\n\ndef run():\n    time.sleep(2)\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("app/worker.py:4 [no-sleep-fix]", out)

    def test_baseline_hides_legacy_but_not_a_new_copy(self) -> None:
        self.write("app/worker.py", "import time\ntime.sleep(2)\n")
        self.assertEqual(self.check("--write-baseline")[0], 0)
        self.assertEqual(self.check()[0], 0)
        self.write("app/worker.py", "import time\ntime.sleep(2)\ntime.sleep(2)\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("1 baselined, 1 new violation", out)

    def test_exemption_needs_an_existing_lesson(self) -> None:
        self.write("app/worker.py", "import time\ntime.sleep(1)  # devflow: allow no-sleep-fix L-0001\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("missing lesson L-0001", out)
        self.write(".devflow/lessons/L-0001.toml", LESSON_RULE)
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_rule_that_misses_its_own_bad_example_fails(self) -> None:
        rule = self.dir / ".devflow/rules/no-sleep-fix.toml"
        rule.write_text(rule.read_text().replace(r"'\btime\.sleep\(',", r"'\btime\.snooze\(',"))
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("rule no-sleep-fix fixture 1: did not fire", out)

    def test_rule_without_pass_example_fails(self) -> None:
        self.write(".devflow/rules/no-print.toml", """\
            id = "no-print"
            message = "use logging"
            include = ["**/*.py"]
            patterns = ['\\bprint\\(']
            [[fixture]]
            path = "a.py"
            text = "print(1)"
            expect = "fail"
            """)
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("rule no-print: needs at least one fixture", out)

    def test_lesson_pointing_at_missing_things_fails(self) -> None:
        self.write(".devflow/lessons/L-0002.toml", LESSON_RULE.replace("L-0001", "L-0002")
                   .replace('kind = "rule"', 'kind = "test"')
                   .replace('ref = "no-sleep-fix"', 'ref = "tests/test_jobs.py::test_waits_for_state"'))
        self.write(".devflow/lessons/L-0003.toml", LESSON_RULE.replace("L-0001", "L-0003")
                   .replace('ref = "no-sleep-fix"', 'ref = "no-such-rule"'))
        self.write(".devflow/lessons/L-0004.toml", LESSON_RULE.replace("L-0001", "L-0004")
                   .replace('kind = "rule"', 'kind = "skill-only"'))
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("L-0002.toml: enforcement.ref file not found: tests/test_jobs.py", out)
        self.assertIn("L-0003.toml: enforcement.ref names no rule", out)
        self.assertIn("L-0004.toml: skill-only needs enforcement.reason", out)

    def test_lesson_test_ref_must_name_an_existing_test(self) -> None:
        self.write("tests/test_jobs.py", "def test_other():\n    assert True\n")
        self.write(".devflow/lessons/L-0005.toml", LESSON_RULE.replace("L-0001", "L-0005")
                   .replace('kind = "rule"', 'kind = "test"')
                   .replace('ref = "no-sleep-fix"', 'ref = "tests/test_jobs.py::test_waits_for_state"'))
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("'test_waits_for_state' not found in tests/test_jobs.py", out)
        self.write("tests/test_jobs.py", "def test_waits_for_state():\n    assert True\n")
        self.assertEqual(self.check()[0], 0)

    def test_broken_rule_file_is_a_config_error(self) -> None:
        self.write(".devflow/rules/bad.toml", "id = \n")
        code, out = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("CONFIG", out)

RULE_NO_EVAL = """\
id = "no-eval"
message = "do not eval input"
include = ["**/*.py"]
patterns = ['\\beval\\(']
[[fixture]]
path = "a.py"
text = "eval(x)"
expect = "fail"
[[fixture]]
path = "a.py"
text = "ast.literal_eval(x)"
expect = "pass"
"""


def lesson(lid: str, kind: str, ref: str) -> str:
    return LESSON_RULE.replace("L-0001", lid).replace('kind = "rule"', f'kind = "{kind}"') \
        .replace('ref = "no-sleep-fix"', f'ref = "{ref}"')


# Stand-in for a stack's test runner honouring the .devflow/test.sh contract used by the lesson gate:
# DEVFLOW_TEST_ONLY limits the run to one file, DEVFLOW_JUNIT receives per-test results.
JUNIT_RUNNER = """\
import os, sys, unittest
from xml.etree.ElementTree import Element, SubElement, ElementTree

class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.cases = []
    def addSuccess(self, t): self.cases.append((t, None))
    def addFailure(self, t, e): super().addFailure(t, e); self.cases.append((t, "failure"))
    def addError(self, t, e): super().addError(t, e); self.cases.append((t, "error"))
    def addSkip(self, t, r): super().addSkip(t, r); self.cases.append((t, "skipped"))

sys.path.insert(0, os.getcwd())
only = os.environ.get("DEVFLOW_TEST_ONLY")
loader = unittest.TestLoader()
suite = loader.loadTestsFromName(only[:-3].replace("/", ".")) if only else loader.discover("tests")
result = Result()
suite.run(result)
if os.environ.get("DEVFLOW_JUNIT"):
    root = Element("testsuite")
    for t, kind in result.cases:
        case = SubElement(root, "testcase", name=getattr(t, "_testMethodName", str(t)))
        if kind:
            SubElement(case, kind)
    ElementTree(root).write(os.environ["DEVFLOW_JUNIT"])
sys.exit(0 if result.wasSuccessful() else 1)
"""

ADD_TEST = ("import unittest\nfrom app.calc import add\n\n"
            "class T(unittest.TestCase):\n    def test_adds(self):\n        self.assertEqual(add(2, 3), 5)\n")


class PrRepo(RepoCase):
    """A base commit, then check.py --pr BASE on a head commit, as the CI step does."""

    def start(self, test_sh: str) -> None:
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.dir, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.dir, check=True)
        self.write(".devflow/test.sh", test_sh)
        self.write("tests/__init__.py", "")
        self.write("app/__init__.py", "")
        self.write("app/calc.py", "def add(a, b):\n    return a - b\n\ndef parse(x):\n    return eval(x)\n")
        self.base = self.commit("base")

    def commit(self, msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=self.dir, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", msg], cwd=self.dir, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.dir, check=True,
                              capture_output=True, text=True).stdout.strip()

    def gate(self, title: str, body: str = "") -> tuple[int, str]:
        self.commit("head")
        p = subprocess.run([sys.executable, ".devflow/check.py", "--pr", self.base], cwd=self.dir,
                           capture_output=True, text=True, env={**os.environ, "PR_TITLE": title, "PR_BODY": body})
        return p.returncode, p.stdout + p.stderr

    def fix_add_with_test(self, test_src: str = ADD_TEST) -> None:
        self.write("tests/test_calc.py", test_src)
        self.write(".devflow/lessons/L-0003.toml", lesson("L-0003", "test", "tests/test_calc.py::test_adds"))
        self.write("app/calc.py", "def add(a, b):\n    return a + b\n\ndef parse(x):\n    return eval(x)\n")


class PrGate(PrRepo):
    def setUp(self) -> None:
        super().setUp()
        self.write(".devflow/junit_run.py", JUNIT_RUNNER)
        self.start("set -e\npython3 .devflow/junit_run.py\n")

    def test_fix_pr_needs_a_lesson_or_a_reason(self) -> None:
        self.write("app/calc.py", "def add(a, b):\n    return a + b\n")
        code, out = self.gate("fix: add returns the sum")
        self.assertEqual(code, 1, out)
        self.assertIn("fix/revert PR: add `Lesson: L-NNNN`", out)
        code, out = self.gate("fix: add returns the sum", "No-lesson: typo in a one-off helper, no pattern")
        self.assertEqual(code, 0, out)

    def test_feature_pr_needs_no_lesson(self) -> None:
        self.write("app/extra.py", "X = 1\n")
        self.assertEqual(self.gate("feat: add extra")[0], 0)

    def test_rule_lesson_must_fire_on_the_pre_fix_code(self) -> None:
        self.write(".devflow/rules/no-eval.toml", RULE_NO_EVAL)
        self.write(".devflow/lessons/L-0002.toml", lesson("L-0002", "rule", "no-eval"))
        self.write("app/calc.py", "import ast\n\ndef parse(x):\n    return ast.literal_eval(x)\n")
        code, out = self.gate("fix: parse without eval", "Lesson: L-0002")
        self.assertEqual(code, 0, out)
        self.assertIn("PROOF .devflow/lessons/L-0002.toml: rule no-eval fires on the pre-fix (BASE) version of app/calc.py", out)

    def test_rule_lesson_that_misses_the_pre_fix_code_fails(self) -> None:
        self.write(".devflow/rules/no-exec.toml", RULE_NO_EVAL.replace("no-eval", "no-exec")
                   .replace("\\beval", "\\bexec").replace('"eval(x)"', '"exec(x)"'))
        self.write(".devflow/lessons/L-0002.toml", lesson("L-0002", "rule", "no-exec"))
        self.write("app/calc.py", "import ast\n\ndef parse(x):\n    return ast.literal_eval(x)\n")
        code, out = self.gate("fix: parse without eval", "Lesson: L-0002")
        self.assertEqual(code, 1, out)
        self.assertIn("rule no-exec does not fire on the pre-fix version", out)
        self.assertEqual(self.gate("fix: parse without eval",
                                   "Lesson: L-0002\nNo-proof: rule guards a sibling module, see #12")[0], 0)

    def test_test_lesson_must_fail_on_the_pre_fix_code(self) -> None:
        self.fix_add_with_test()
        code, out = self.gate("fix: add returns the sum", "Lesson: L-0003")
        self.assertEqual(code, 0, out)
        self.assertIn("PROOF .devflow/lessons/L-0003.toml: test tests/test_calc.py::test_adds: "
                      "BASE failure x1; this PR passed x1", out)

    def test_test_lesson_that_passes_on_the_pre_fix_code_fails(self) -> None:
        self.fix_add_with_test(ADD_TEST.replace("self.assertEqual(add(2, 3), 5)", "self.assertTrue(True)"))
        code, out = self.gate("fix: add returns the sum", "Lesson: L-0003")
        self.assertEqual(code, 1, out)
        self.assertIn("on the pre-fix code (BASE): passed", out)

    def test_unrelated_base_failure_is_not_proof(self) -> None:
        self.write("tests/test_other.py", "import unittest\n\nclass U(unittest.TestCase):\n"
                   "    def test_flaky(self):\n        self.fail('unrelated')\n")
        self.write("app/calc.py", "def add(a, b):\n    return a + b\n\ndef parse(x):\n    return eval(x)\n")
        self.base = self.commit("base: add already right, another test broken")
        self.fix_add_with_test()
        code, out = self.gate("fix: add returns the sum", "Lesson: L-0003")
        self.assertEqual(code, 1, out)
        self.assertIn("on the pre-fix code (BASE): passed", out)

    def test_import_error_on_base_is_not_proof(self) -> None:
        self.fix_add_with_test(ADD_TEST.replace("import add", "import add, mul"))
        self.write("app/calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        code, out = self.gate("fix: add returns the sum", "Lesson: L-0003")
        self.assertEqual(code, 1, out)
        self.assertIn("not run (import or collection error?)", out)

    def test_test_must_pass_after_the_fix(self) -> None:
        self.fix_add_with_test()
        self.write("app/calc.py", "def add(a, b):\n    return a * b\n")
        code, out = self.gate("fix: add returns the sum", "Lesson: L-0003")
        self.assertEqual(code, 1, out)
        self.assertIn("on this PR: failure; it must pass after the fix", out)

    def test_runner_without_junit_cannot_prove(self) -> None:
        self.write(".devflow/test.sh", "set -e\npython3 -m unittest discover -s tests -v\n")
        self.fix_add_with_test()
        code, out = self.gate("fix: add returns the sum", "Lesson: L-0003")
        self.assertEqual(code, 1, out)
        self.assertIn("wrote no JUnit results", out)


    def test_recurring_lesson_must_be_strengthened(self) -> None:
        self.write(".devflow/lessons/L-0004.toml", lesson("L-0004", "skill-only", "x")
                   .replace('ref = "x"', 'ref = "x"\nreason = "needs judgement"'))
        self.base = self.commit("lesson exists")
        self.write("app/calc.py", "def add(a, b):\n    return a + b\n")
        code, out = self.gate("fix: add again", "Lesson: L-0004")
        self.assertEqual(code, 1, out)
        self.assertIn("L-0004 already existed and this PR leaves it unchanged", out)
        self.fix_add_with_test()
        self.write(".devflow/lessons/L-0004.toml", lesson("L-0004", "test", "tests/test_calc.py::test_adds"))
        code, out = self.gate("fix: add again", "Lesson: L-0004")
        self.assertEqual(code, 0, out)


class TemplatePytest(PrRepo):
    """The template .devflow/test.sh (pytest branch) with real pytest JUnit output. Needs network for pip."""

    def setUp(self) -> None:
        super().setUp()
        self.write("requirements.txt", "")
        self.write(".gitignore", ".venv-test/\n")
        self.start((TEMPLATE / "test.sh").read_text())

    def test_assertion_failure_on_base_proves(self) -> None:
        self.fix_add_with_test()
        code, out = self.gate("fix: add returns the sum", "Lesson: L-0003")
        self.assertEqual(code, 0, out)

    def test_import_error_on_base_does_not_prove(self) -> None:
        self.fix_add_with_test(ADD_TEST.replace("import add", "import add, mul"))
        self.write("app/calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        code, out = self.gate("fix: add returns the sum", "Lesson: L-0003")
        self.assertEqual(code, 1, out)
        self.assertIn("not run (import or collection error?)", out)


class ImportBoundaryAndDiffBudget(PrRepo):
    def test_import_boundary_blocks_python_ast_and_js_and_honors_lesson_exemption(self) -> None:
        self.write(
            ".devflow/rules/domain-boundary.toml",
            """\
            id = "domain-boundary"
            kind = "import_boundary"
            message = "domain must not import infra"
            include = ["domain/**/*.py", "web/domain/**/*.ts"]
            forbidden_modules = ["infra", "infra/"]

            [[fixture]]
            expect = "fail"
            path = "domain/svc.py"
            text = "from infra.db import query\\n"

            [[fixture]]
            expect = "pass"
            path = "domain/svc.py"
            text = "from domain.types import Item\\n"
            """,
        )
        self.write("domain/bad.py", "import infra.client\n")
        self.write("domain/fromparent.py", "import domain\n")
        self.write("domain/sub/alias.py", "from .. import infra\n")
        self.write("domain/sub/pkgalias.py", "from ..sub_pkg import other\n")
        self.write("pkg/domain/direct_from.py", "from pkg import infra\n")
        # Rewrite rule to test both pkg.infra and infra prefixes on package-relative & direct from-import
        self.write(
            ".devflow/rules/domain-boundary.toml",
            """\
            id = "domain-boundary"
            kind = "import_boundary"
            message = "domain must not import infra"
            include = ["domain/**/*.py", "pkg/domain/**/*.py", "web/domain/**/*.ts"]
            forbidden_modules = ["infra", "pkg.infra", "domain.infra", "infra/"]

            [[fixture]]
            expect = "fail"
            path = "domain/svc.py"
            text = "from infra.db import query\\n"

            [[fixture]]
            expect = "pass"
            path = "domain/svc.py"
            text = "from domain.types import Item\\n"
            """,
        )
        self.write("domain/from_alias.py", "from domain import infra, helper\n")
        self.write("web/domain/bad.ts", 'import { x } from "infra/http";\n')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("domain/bad.py:1 [domain-boundary]", out)
        self.assertIn("domain/sub/alias.py:1 [domain-boundary]", out)
        self.assertIn("domain/from_alias.py:1 [domain-boundary]", out)
        self.assertIn("pkg/domain/direct_from.py:1 [domain-boundary]", out)
        self.assertIn("web/domain/bad.ts:1 [domain-boundary]", out)
        self.assertNotIn("fromparent.py", out)
        self.assertNotIn("pkgalias.py", out)

        # Exemption with existing L-0001 suppresses violations cleanly
        self.write(
            ".devflow/lessons/L-0001.toml",
            LESSON_RULE.replace("no-sleep-fix", "domain-boundary"),
        )
        self.write("domain/bad.py", "import infra.client  # devflow: allow domain-boundary L-0001\n")
        self.write("domain/sub/alias.py", "from .. import infra  # devflow: allow domain-boundary L-0001\n")
        self.write("domain/from_alias.py", "from domain import infra, helper  # devflow: allow domain-boundary L-0001\n")
        self.write("pkg/domain/direct_from.py", "from pkg import infra  # devflow: allow domain-boundary L-0001\n")
        self.write("web/domain/bad.ts", '// devflow: allow domain-boundary L-0001\nimport { x } from "infra/http";\n')
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_diff_budget_self_tests_and_enforces_pr_limits(self) -> None:
        self.write(
            ".devflow/rules/pr-budget.toml",
            """\
            id = "pr-budget"
            kind = "diff_budget"
            message = "PR diff exceeds small-step budget"
            max_files = 2
            max_added_lines = 4
            exclude = ["tests/**"]

            [[fixture]]
            expect = "fail"
            changed_files = ["a.py", "b.py", "c.py"]
            added_lines = 2

            [[fixture]]
            expect = "pass"
            changed_files = ["a.py", "tests/test_a.py", "tests/test_b.py"]
            added_lines = 3
            """,
        )
        self.start("exit 0\n")
        # Pass check self-test on clean repo
        self.assertEqual(self.check()[0], 0)

        # Within budget (1 code file, 2 added lines, plus excluded test file)
        self.write("app/a.py", "X = 1\nY = 2\n")
        self.write("tests/test_a.py", "A = 1\nB = 2\nC = 3\nD = 4\nE = 5\n")
        code, out = self.gate("feat: within budget")
        self.assertEqual(code, 0, out)

        # Exceeds added_lines budget
        self.write("app/a.py", "X = 1\nY = 2\nZ = 3\nW = 4\nQ = 5\n")
        code, out = self.gate("feat: too many lines")
        self.assertEqual(code, 1, out)
        self.assertIn("added_lines=5 > max_added_lines=4", out)

        # Exceeds max_files budget
        self.write("app/a.py", "X = 1\n")
        self.write("app/b.py", "X = 1\n")
        self.write("app/c.py", "X = 1\n")
        code, out = self.gate("feat: too many files")
        self.assertEqual(code, 1, out)
        self.assertIn("changed_files=3 > max_files=2", out)

    def test_js_comments_dynamic_import_py_syntax_error_and_diff_policy_alias(self) -> None:
        self.write(
            ".devflow/rules/domain-boundary.toml",
            """\
            id = "domain-boundary"
            kind = "import_boundary"
            message = "domain must not import infra"
            include = ["domain/**/*.py", "web/**/*.ts", "web/**/*.tsx"]
            forbidden_modules = ["infra", "infra/"]

            [[fixture]]
            expect = "fail"
            path = "domain/a.py"
            text = "import infra\\n"

            [[fixture]]
            expect = "pass"
            path = "domain/a.py"
            text = "import domain.types\\n"
            """,
        )
        self.write(
            ".devflow/rules/pr-cap.toml",
            """\
            id = "pr-cap"
            kind = "diff_policy"
            message = "diff cap"
            max_files = 1

            [[fixture]]
            expect = "fail"
            changed_files = ["a.py", "b.py"]

            [[fixture]]
            expect = "pass"
            changed_files = ["a.py"]
            """,
        )
        self.write(
            "web/ok.ts",
            '// import x from "infra/a";\n'
            '/* require("infra/b") */\n'
            'const s = "/* keep */";\n'
            'const documentation = "import(\'infra/private\')";\n'
            "const singleq = 'require(\"infra/private\")';\n"
            "const tpl = `hello ${s}\nimport('infra/private')\nrequire('infra/x')`;\n"
            'const member = obj.import("infra/not_real");\n'
            "const re = /import\\('infra\\/fake'\\)/;\n"
            "if (s) /import\\('infra\\/if_fake'\\)/;\n"
            "if (s) {} /import\\('infra\\/block_fake'\\)/g;\n"
            "function fd() {} /import\\('infra\\/fd_fake'\\)/g;\n"
            "class Cd {} /import\\('infra\\/cd_fake'\\)/g;\n"
            "const negRe = !/import\\('infra\\/neg_fake'\\)/.test(s);\n"
            "for (const m of /import\\('infra\\/for_of_fake'\\)/g.exec(s) || []) {}\n"
            '/* import { a } from "infra/mc_ignored";\nexport * from "infra/mc_ignored2"; */\n',
        )
        self.write("web/dyn.ts", 'export async function load() { return import("infra/client"); }\n')
        self.write("web/tpl_real.ts", "export const x = `${await import('infra/tpl_client')}`;\n")
        self.write("web/mc_imp.ts", 'const n = 1 /*\n*/ import x from "infra/mc_imp";\n')
        self.write("web/mc_exp.ts", 'const n = 1 /*\n*/ export { x } from "infra/mc_exp";\n')
        self.write("web/esc_hex.ts", 'import x from "\\x69nfra/hex";\n')
        self.write("web/esc_u4.ts", 'const m = await import("\\u0069nfra/u4");\n')
        self.write("web/esc_ucp.ts", 'const r = require("\\u{69}nfra/ucp");\n')
        self.write("web/esc_tpl.ts", 'const t = await import(`\\x69nfra/tpl_hex`);\n')
        self.write("web/esc_oct.ts", 'import o from "\\151nfra/oct";\n')
        self.write("web/esc_cont.ts", 'export * from "in\\\nfra/cont";\n')
        self.write("web/div.ts", 'const ratio = 1 / 2; import x from "infra/x";\n')
        self.write("web/div_of.ts", 'const of = 10; const r = of / 2; import x from "infra/of_div";\n')
        self.write("web/div_ts_bang.ts", 'const ratio = value! / 2; import secret from "infra/private";\n')
        self.write("web/div_ts_bang_tsx.tsx", 'const ratio = fn(value)! / 2; const ui = <Box>don\'t</Box>; import secret from "infra/tsx_bang";\n')
        self.write("web/div_obj.ts", 'const ratio = {} / 2; import x from "infra/obj_x";\n')
        self.write("web/div_obj_dyn.ts", 'const o = { m() {} } / 2; const m = await import("infra/obj_dyn");\n')
        self.write("web/div_fn_cls.ts", 'const f = function() {} / 2; const c = class {} / 2; import y from "infra/fn_cls";\n')
        self.write("web/div_block_obj.ts", 'if (s) { const r = {} / 2; }\nimport b from "infra/block_obj";\n')
        self.write("web/div_expr.ts", 'const a = (1 + 2) / 3; const b = arr[0] / 2; const c = i++ / 2; import y from "infra/y";\n')
        self.write("web/div_tpl.ts", 'const t = `${s}` / 2; import z from "infra/z";\n')
        self.write("web/div_if.ts", 'if (s) /ok/g; import w from "infra/w";\n')
        self.write("web/div_prop.ts", 'const d = cfg.default / 2; import p from "infra/p";\n')
        self.write("web/div_jsx.tsx", 'const el = <div>a</div><div>b</div>; import j from "infra/jsx";\n')
        self.write("web/div_jsx_self.tsx", 'const el = <Box /> / 2; import j2 from "infra/jsx2";\n')
        self.write("web/jsx_apostrophe.tsx", 'const el = <div>don\'t</div>; import j3 from "infra/jsx_apo";\n')
        self.write("web/jsx_quote.tsx", 'const el = <p>Say "hi</p>; const m = await import("infra/jsx_q");\n')
        self.write("domain/broken.py", "def broken(:\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertNotIn("web/ok.ts", out)
        for bad_js in (
            "web/dyn.ts",
            "web/tpl_real.ts",
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
            "web/div_expr.ts",
            "web/div_tpl.ts",
            "web/div_if.ts",
            "web/div_prop.ts",
            "web/div_jsx.tsx",
            "web/div_jsx_self.tsx",
            "web/jsx_apostrophe.tsx",
            "web/jsx_quote.tsx",
        ):
            self.assertIn(f"{bad_js}:1 [domain-boundary]", out, bad_js)
        self.assertIn("web/mc_imp.ts:2 [domain-boundary]", out)
        self.assertIn("web/mc_exp.ts:2 [domain-boundary]", out)
        self.assertIn("web/div_block_obj.ts:2 [domain-boundary]", out)
        self.assertIn("domain/broken.py:1 [domain-boundary]", out)
        self.assertIn("SyntaxError", out)

        # Clean python + JS lets repo check pass, while PR gate still enforces diff_policy alias
        for bad_js in (
            "web/dyn.ts",
            "web/tpl_real.ts",
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
            "web/div_expr.ts",
            "web/div_tpl.ts",
            "web/div_if.ts",
            "web/div_prop.ts",
            "web/div_jsx.tsx",
            "web/div_jsx_self.tsx",
            "web/jsx_apostrophe.tsx",
            "web/jsx_quote.tsx",
        ):
            self.write(bad_js, "export const ok = 1;\n")
        self.write("domain/broken.py", "X = 1\n")
        self.start("exit 0\n")
        code_ok, out_ok = self.check()
        self.assertEqual(code_ok, 0, out_ok)
        self.write("app/a.py", "X = 1\n")
        self.write("app/b.py", "X = 2\n")
        code_pr, out_pr = self.gate("feat: check diff_policy alias")
        self.assertEqual(code_pr, 1, out_pr)
        self.assertIn("changed_files=2 > max_files=1", out_pr)

    def test_js_binary_operators_keywords_optional_chaining_and_jsx_comments(self) -> None:
        import textwrap

        self.write(
            ".devflow/rules/js-op-boundary.toml",
            textwrap.dedent(
                """\
                id = "js-op-boundary"
                kind = "import_boundary"
                message = "web must not import infra"
                include = ["web/**"]
                forbidden_modules = ["infra"]

                [[fixture]]
                expect = "fail"
                path = "web/bad.ts"
                text = 'import x from "infra/bad";'

                [[fixture]]
                expect = "pass"
                path = "web/ok.ts"
                text = 'import x from "shared/ok";'
                """
            ),
        )
        self.write(
            "web/clean_ops.ts",
            'const x = 1 < /import("infra")/.source.length;\n'
            'const x2 = 1</import("infra")/.source.length;\n'
            'const y = 1 / /import("infra")/.source.length;\n'
            'const y2 = 1 / 2 / /import("infra")/.source.length;\n'
            'const z = 1 > /import("infra")/.source.length;\n'
            'const sp = [.../import("infra")/.source];\n'
            'const nu = a ?? /import("infra")/.source; a ??= /import("infra")/;\n'
            'const nb = a ?? b\nlabel: /import("infra")/.test(x);\n'
            'const ins = a instanceof /import("infra")/; const nw = new /import("infra")/;\n'
            'const opt = obj?.import("infra/not_module");\n',
        )
        self.write("web/after_lt_re.ts", 'const x = 1 < /import("infra\\/ok")/.source.length; import a from "infra/after_lt_re";\n')
        self.write("web/after_div_re.ts", 'const y = 1 / /import("infra\\/ok")/.source.length; import b from "infra/after_div_re";\n')
        self.write("web/after_div_obj.ts", 'const o1 = 1 / {} / 2; import c from "infra/after_div_obj";\n')
        self.write("web/after_lt_obj.ts", 'const o2 = 1 < {} / 2; import d from "infra/after_lt_obj";\n')
        self.write("web/after_ts_gen.ts", 'const g = fn<T>(x) / 2; import e from "infra/after_ts_gen";\n')
        self.write("web/after_opt_kw.ts", 'const q = cfg?.default / 2; import f from "infra/after_opt_kw";\n')
        self.write("web/after_ter_dec.ts", 'const t = ok ? .5 : {} / 2; import g2 from "infra/after_ter_dec";\n')
        self.write("web/after_jsx_cmt.tsx", 'const el = <Box /* cmt */ >don\'t</Box>; const m = await import("infra/after_jsx_cmt"); const r = 1 / 2;\n')

        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertNotIn("web/clean_ops.ts", out)
        for bad_js in (
            "web/after_lt_re.ts",
            "web/after_div_re.ts",
            "web/after_div_obj.ts",
            "web/after_lt_obj.ts",
            "web/after_ts_gen.ts",
            "web/after_opt_kw.ts",
            "web/after_ter_dec.ts",
            "web/after_jsx_cmt.tsx",
        ):
            self.assertIn(f"{bad_js}:1 [js-op-boundary]", out, bad_js)

    def test_js_ecmascript_whitespace_cr_only_and_hashbang(self) -> None:
        import importlib.util
        import os
        import sys
        import tempfile
        import textwrap
        from pathlib import Path

        rule_toml = textwrap.dedent(
            """\
            id = "js-ws-boundary"
            kind = "import_boundary"
            message = "web must not import infra"
            include = ["web/**"]
            forbidden_modules = ["infra"]

            [[fixture]]
            expect = "fail"
            path = "web/bad.ts"
            text = 'import x from "infra/bad";'

            [[fixture]]
            expect = "pass"
            path = "web/ok.ts"
            text = 'import x from "shared/ok";'
            """
        )
        self.write(".devflow/rules/js-ws-boundary.toml", rule_toml)
        self.write(
            "web/clean_ws.js",
            "#!/usr/bin/env node\n"
            "\ufeffconst\u00a0a = 1;\n"
            "const re = /import\\('infra\\/re_fake'\\)/g;\n"
            '/*\u2028import x from "infra/cmt_ls";\n*/\n',
        )
        self.write("web/bom.ts", '\ufeffimport x from "infra/bom";\n')
        self.write("web/hashbang.js", '#!/usr/bin/env node\nimport x from "infra/hashbang";\n')
        self.write("web/bom_hashbang.js", '\ufeff#!/usr/bin/env node\nimport x from "infra/bom_hb";\n')
        self.write("web/nbsp.ts", '\u00a0import\u00a0x\u00a0from\u00a0"infra/nbsp";\n')
        self.write("web/ecmas_ws.ts", '\v\f\u2003import\u00a0{ a }\u2003from\ufeff"infra/ecmas_ws";\n')
        self.write("web/jsx_ws.tsx", 'const\u00a0el = <Box\u00a0v={1}\u2028>don\'t</Box\u00a0>;\nimport\u00a0j\u00a0from\u00a0"infra/jsx_ws";\n')

        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertNotIn("web/clean_ws.js", out)
        self.assertIn("web/bom.ts:1 [js-ws-boundary]", out)
        self.assertIn("web/hashbang.js:2 [js-ws-boundary]", out)
        self.assertIn("web/bom_hashbang.js:2 [js-ws-boundary]", out)
        self.assertIn("web/nbsp.ts:1 [js-ws-boundary]", out)
        self.assertIn("web/ecmas_ws.ts:1 [js-ws-boundary]", out)
        self.assertIn("web/jsx_ws.tsx:3 [js-ws-boundary]", out)

        check_path = Path(__file__).resolve().parents[1] / "templates" / "repo" / ".devflow" / "check.py"
        spec = importlib.util.spec_from_file_location("tpl_check_js_ws", check_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
            old_cwd = os.getcwd()
            with tempfile.TemporaryDirectory(prefix="devflow-check-raw-cr-") as td:
                rules_dir = Path(td) / ".devflow" / "rules"
                rules_dir.mkdir(parents=True, exist_ok=True)
                (rules_dir / "js-ws-boundary.toml").write_text(rule_toml, encoding="utf-8")
                try:
                    os.chdir(td)
                    rules = mod.load_rules()
                finally:
                    os.chdir(old_cwd)
            clean_hits = mod.scan_text(
                rules,
                "web/clean_ws.js",
                "#!/usr/bin/env node\r\ufeffconst\u00a0a = 1\r"
                "const re = /import\\('infra\\/re_cr'\\)/g\r"
                '/*\u2028import x from "infra/cmt_ls";\r*/\r',
                set(),
            )
            self.assertEqual(clean_hits, [])
            for rel, src, expected_line in (
                ("web/cr_asi.js", 'const x = 1\rimport y from "infra/cr";\r', 2),
                ("web/cr_export.js", 'const x = 1\rexport { y } from "infra/cr_exp";\r', 2),
                ("web/hb_cr.js", '#!/usr/bin/env node\rimport z from "infra/hb_cr";\r', 2),
                ("web/bom_hb_crlf.js", '\ufeff#!/usr/bin/env node\r\nimport w from "infra/bom_hb_crlf";\r\n', 2),
                ("web/jsx_cr.tsx", 'const\u00a0el = <Box\u00a0v={1}\u2028>don\'t</Box\u00a0>;\rimport\u00a0j\u00a0from\u00a0"infra/jsx_cr";\r', 3),
            ):
                hits = mod.scan_text(rules, rel, src, set())
                self.assertEqual([(v.path, v.line) for v in hits], [(rel, expected_line)], rel)
        finally:
            sys.modules.pop(spec.name, None)

    def test_character_class_globs_and_invalid_range_config_error(self) -> None:
        self.write(
            ".devflow/rules/charcls.toml",
            """\
            id = "charcls"
            kind = "import_boundary"
            message = "character class boundary"
            include = ["src/[ab]/*.py", "src/[!z]/ok.py"]
            exclude = ["src/[b]/*.py"]
            forbidden_modules = ["infra"]

            [[fixture]]
            expect = "fail"
            path = "src/a/x.py"
            text = "import infra\\n"

            [[fixture]]
            expect = "pass"
            path = "src/c/x.py"
            text = "import infra\\n"
            """,
        )
        self.write("src/a/x.py", "import infra\n")
        self.write("src/b/x.py", "import infra\n")
        self.write("src/c/x.py", "import infra\n")
        self.write("src/c/ok.py", "import infra\n")
        self.write("src/z/ok.py", "import infra\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("src/a/x.py:1 [charcls]", out)
        self.assertIn("src/c/ok.py:1 [charcls]", out)
        self.assertNotIn("src/b/x.py", out)
        self.assertNotIn("src/c/x.py", out)
        self.assertNotIn("src/z/ok.py", out)

        self.write(
            ".devflow/rules/charcls.toml",
            """\
            id = "charcls"
            kind = "import_boundary"
            message = "bad range"
            include = ["src/[z-a]/*.py"]
            forbidden_modules = ["infra"]
            """,
        )
        code_bad, out_bad = self.check()
        self.assertEqual(code_bad, 2, out_bad)
        self.assertIn("invalid glob pattern", out_bad)

    def test_require_resolve_boundary_and_member_exemption(self) -> None:
        self.write(
            ".devflow/rules/reqres.toml",
            """\
            id = "reqres"
            kind = "import_boundary"
            message = "forbidden require.resolve"
            include = ["web/**/*.js", "web/**/*.ts"]
            forbidden_modules = ["infra"]

            [[fixture]]
            expect = "fail"
            path = "web/bad.js"
            text = 'const m = require.resolve("infra/db");'

            [[fixture]]
            expect = "pass"
            path = "web/ok.ts"
            text = 'const m = helper.require.resolve("infra/db");'
            """,
        )
        self.write("web/bad.js", 'const db = require.resolve("infra/db");\n')
        self.write("web/ok.ts", 'const a = obj.require.resolve("infra/db");\nconst s = "require.resolve(\'infra/db\')";\n')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("web/bad.js:1 [reqres]", out)
        self.assertNotIn("web/ok.ts", out)


if __name__ == "__main__":
    unittest.main()
