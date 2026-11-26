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
        self.write("web/domain/bad.ts", 'import { x } from "infra/http";\n')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("domain/bad.py:1 [domain-boundary]", out)
        self.assertIn("web/domain/bad.ts:1 [domain-boundary]", out)

        # Exemption with existing L-0001 suppresses both violations cleanly
        self.write(
            ".devflow/lessons/L-0001.toml",
            LESSON_RULE.replace("no-sleep-fix", "domain-boundary"),
        )
        self.write("domain/bad.py", "import infra.client  # devflow: allow domain-boundary L-0001\n")
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


if __name__ == "__main__":
    unittest.main()
