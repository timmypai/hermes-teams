"""devflow repo checker (.devflow/check.py): run it the way CI does, in a throwaway git repo."""
from __future__ import annotations

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


class CheckRepo(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
