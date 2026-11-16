"""devflow acceptance scenarios (.devflow/acceptance.py and the verify.sh shell helper), run as CI runs them."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "repo" / ".devflow"
PHASES = ("given", "when", "then")
IDENTITY_ENV = {"GITHUB_REPOSITORY": "o/app", "GITHUB_SHA": "f" * 40, "DEVFLOW_HEAD_SHA": "a" * 40,
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_WORKFLOW_REF": "o/app/.github/workflows/ci.yml@refs/pull/7/merge"}


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GITHUB_", "DEVFLOW_")) and k != "PR_BODY"}
    env.update(extra)
    return env


class AcceptanceCase(unittest.TestCase):
    """A throwaway repo root holding .devflow/acceptance.py and an evidence directory."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="devflow-acceptance-"))
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / ".devflow" / "ui").mkdir(parents=True)
        shutil.copy(TEMPLATE / "acceptance.py", self.dir / ".devflow" / "acceptance.py")
        (self.dir / ".devflow" / "verify.sh").write_text("# scenarios\n")
        (self.dir / ".devflow" / "ui" / "ac.spec.ts").write_text("// ui scenarios\n")
        self.ev = self.dir / "ev"
        self.ev.mkdir()
        self.env = clean_env(DEVFLOW_EVIDENCE=str(self.ev), **IDENTITY_ENV)

    def run_acc(self, *args: str, **env: str) -> tuple[int, str]:
        p = subprocess.run([sys.executable, ".devflow/acceptance.py", *args], cwd=self.dir,
                           env={**self.env, **env}, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def events(self, sid: str, statuses: tuple[str, ...], runner: str = "shell", smoke: bool = False,
               error: str = "boom", phases: tuple[str, ...] = PHASES) -> None:
        with (self.ev / "scenario-events.jsonl").open("a") as f:
            for phase, status in zip(phases, statuses):
                f.write(json.dumps({"id": sid, "runner": runner, "smoke": smoke, "phase": phase,
                                    "title": f"{phase} text", "status": status,
                                    "error": error if status == "failed" else ""}) + "\n")

    def playwright(self, *tests: tuple[str, str, str]) -> None:
        """Minimal Playwright JSON report: (title, test status, result status) per test."""
        specs = [{"title": title, "file": "ac.spec.ts", "tags": [],
                  "tests": [{"status": st, "results": [{"status": rs, "error": {"message": "\x1b[31mTimeout 30000ms\x1b[39m"}
                                                       if rs in ("failed", "timedOut") else {}}]}]}
                 for title, st, rs in tests]
        report = {"config": {}, "suites": [{"title": "ac.spec.ts", "file": "ac.spec.ts", "specs": specs, "suites": []}],
                  "errors": [], "stats": {}}
        (self.ev / "playwright-report.json").write_text(json.dumps(report))

    def build(self, *args: str, **env: str) -> dict:
        code, out = self.run_acc("build", "--kind", "pr", *args, **env)
        self.assertEqual(code, 0, out)
        return json.loads((self.ev / "acceptance.json").read_text())

    def scenario(self, report: dict, sid: str) -> dict:
        return next(s for s in report["scenarios"] if s["id"] == sid)


class Event(AcceptanceCase):

    def test_event_appends_one_json_line(self) -> None:
        code, out = self.run_acc("event", "--id", "t_ab12/AC-1.S1", "--runner", "shell", "--phase", "given",
                                 "--title", "清單為空", "--status", "failed", "--smoke", "--error", "x" * 3000)
        self.assertEqual(code, 0, out)
        [line] = (self.ev / "scenario-events.jsonl").read_text(encoding="utf-8").splitlines()
        ev = json.loads(line)
        self.assertEqual((ev["id"], ev["runner"], ev["phase"], ev["title"], ev["status"], ev["smoke"]),
                         ("t_ab12/AC-1.S1", "shell", "given", "清單為空", "failed", True))
        self.assertEqual(len(ev["error"]), 2000)

    def test_event_rejects_bad_id_and_status(self) -> None:
        code, out = self.run_acc("event", "--id", "AC-1", "--runner", "shell", "--phase", "given",
                                 "--title", "t", "--status", "passed")
        self.assertEqual(code, 2, out)
        code, out = self.run_acc("event", "--id", "t_a/AC-1.S1", "--runner", "shell", "--phase", "given",
                                 "--title", "t", "--status", "skipped")
        self.assertEqual(code, 2, out)
        self.assertFalse((self.ev / "scenario-events.jsonl").exists())


class Build(AcceptanceCase):

    def test_identity_comes_from_env_and_scenario_passes(self) -> None:
        self.events("t_ab12/AC-1.S1", ("passed", "passed", "passed"), smoke=True)
        (self.ev / "logs").mkdir()
        (self.ev / "logs" / "t_ab12__AC-1.S1.when.txt").write_text("ok\n")
        report = self.build("--health", "passed")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["kind"], "pr")
        self.assertEqual(report["health"], "passed")
        self.assertEqual(report["identity"], {
            "repo": "o/app", "head_sha": "a" * 40, "run_id": "123", "run_attempt": "2",
            "workflow_ref": "o/app/.github/workflows/ci.yml@refs/pull/7/merge", "image_id": ""})
        s = self.scenario(report, "t_ab12/AC-1.S1")
        self.assertEqual((s["status"], s["failed_phase"], s["runner"], s["smoke"]), ("passed", None, "shell", True))
        self.assertEqual([st["title"] for st in s["steps"]], ["given text", "when text", "then text"])
        self.assertEqual(s["evidence"], ["logs/t_ab12__AC-1.S1.when.txt"])
        self.assertIn("t_ab12/AC-1.S1 @smoke | passed", (self.ev / "acceptance-matrix.md").read_text())
        self.assertTrue((self.ev / "sources" / "verify.sh").is_file())
        self.assertTrue((self.ev / "sources" / "ui" / "ac.spec.ts").is_file())

    def test_head_sha_falls_back_to_github_sha(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "DEVFLOW_HEAD_SHA"}
        self.env = env
        report = self.build()
        self.assertEqual(report["identity"]["head_sha"], "f" * 40)
        self.assertEqual(report["health"], "none")

    def test_failed_phase_and_not_run_steps(self) -> None:
        self.events("t_ab12/AC-2.S1", ("passed", "failed", "not-run"), error="exit 1: wrong total")
        s = self.scenario(self.build(), "t_ab12/AC-2.S1")
        self.assertEqual((s["status"], s["failed_phase"]), ("failed", "when"))
        self.assertEqual([st["status"] for st in s["steps"]], ["passed", "failed", "not-run"])
        self.assertEqual(s["steps"][1]["error"], "exit 1: wrong total")

    def test_playwright_skipped_and_outside_failures(self) -> None:
        self.events("t_ab12/AC-1.S1", ("passed", "passed", "passed"), runner="playwright", smoke=True)
        self.events("t_ab12/AC-1.S2", ("passed",), runner="playwright")
        self.playwright(("t_ab12/AC-1.S1 新增後仍存在 @smoke", "expected", "passed"),
                        ("t_ab12/AC-1.S2 空白被拒", "unexpected", "timedOut"),
                        ("t_ab12/AC-1.S3 之後再做", "skipped", "skipped"),
                        ("t_ab12/AC-1.S4 fixture 壞了", "unexpected", "failed"))
        report = self.build("--playwright-exit", "1")
        by = {s["id"]: s for s in report["scenarios"]}
        self.assertEqual(by["t_ab12/AC-1.S1"]["status"], "passed")
        self.assertTrue(by["t_ab12/AC-1.S1"]["smoke"])
        self.assertEqual((by["t_ab12/AC-1.S2"]["status"], by["t_ab12/AC-1.S2"]["failed_phase"]), ("failed", "outside"))
        self.assertEqual(by["t_ab12/AC-1.S2"]["error"], "Timeout 30000ms")
        self.assertEqual([st["status"] for st in by["t_ab12/AC-1.S2"]["steps"]], ["passed", "not-run", "not-run"])
        self.assertEqual(by["t_ab12/AC-1.S3"]["status"], "skipped")
        self.assertEqual((by["t_ab12/AC-1.S4"]["status"], by["t_ab12/AC-1.S4"]["failed_phase"]), ("failed", "outside"))
        self.assertEqual(report["problems"], [])

    def test_playwright_test_without_scenario_id_or_phases(self) -> None:
        self.playwright(("AC-1 old journey", "expected", "passed"),
                        ("t_ab12/AC-3.S1 plain test", "expected", "passed"))
        report = self.build("--playwright-exit", "0")
        self.assertEqual(self.scenario(report, "t_ab12/AC-3.S1")["status"], "structure")
        self.assertTrue(any("without a scenario id" in p for p in report["problems"]), report["problems"])

    def test_playwright_exit_without_report_is_a_problem(self) -> None:
        report = self.build("--playwright-exit", "1")
        self.assertTrue(any("without writing playwright-report.json" in p for p in report["problems"]))
        code, out = self.run_acc("gate")
        self.assertEqual(code, 1, out)

    def test_structure_out_of_order_duplicate_and_marker(self) -> None:
        self.events("t_ab12/AC-4.S1", ("passed", "passed"), phases=("when", "given"))
        self.events("t_ab12/AC-4.S2", ("passed", "passed", "passed"))
        self.events("t_ab12/AC-4.S2", ("passed", "passed", "passed"))
        self.events("t_ab12/AC-4.S3", ("not-run", "not-run", "not-run"))
        report = self.build()
        for sid in ("t_ab12/AC-4.S1", "t_ab12/AC-4.S2", "t_ab12/AC-4.S3"):
            self.assertEqual(self.scenario(report, sid)["status"], "structure", sid)


class Gate(AcceptanceCase):

    def test_all_passed_and_covered(self) -> None:
        self.events("t_ab12/AC-1.S1", ("passed", "passed", "passed"))
        self.build("--health", "passed")
        code, out = self.run_acc("gate", PR_BODY="Fixes it\r\nCovers: t_ab12/AC-1.S1\r\n")
        self.assertEqual(code, 0, out)

    def test_missing_covered_id_fails(self) -> None:
        self.events("t_ab12/AC-1.S1", ("passed", "passed", "passed"))
        self.build("--health", "passed")
        code, out = self.run_acc("gate", PR_BODY="Covers: t_ab12/AC-1.S1, t_ab12/AC-1.S2\n")
        self.assertEqual(code, 1, out)
        self.assertIn("t_ab12/AC-1.S2: listed in `Covers:` but absent", out)
        code, out = self.run_acc("gate", PR_BODY="Covers: AC-1\n")
        self.assertEqual(code, 1, out)
        self.assertIn("not a scenario id", out)

    def test_skipped_scenario_fails(self) -> None:
        self.playwright(("t_ab12/AC-1.S1 later", "skipped", "skipped"))
        self.build("--playwright-exit", "0")
        code, out = self.run_acc("gate")
        self.assertEqual(code, 1, out)
        self.assertIn("t_ab12/AC-1.S1: skipped", out)

    def test_structure_fails(self) -> None:
        self.events("t_ab12/AC-1.S1", ("passed", "passed"), phases=("given", "then"))
        self.build()
        code, out = self.run_acc("gate")
        self.assertEqual(code, 1, out)
        self.assertIn("t_ab12/AC-1.S1: structure", out)

    def test_health_failed_fails(self) -> None:
        self.build("--health", "failed")
        code, out = self.run_acc("gate")
        self.assertEqual(code, 1, out)
        self.assertIn("health check failed", out)

    def test_failed_scenario_fails_and_missing_report_is_an_error(self) -> None:
        code, out = self.run_acc("gate")
        self.assertEqual(code, 2, out)
        self.events("t_ab12/AC-1.S1", ("passed", "passed", "failed"))
        self.build("--health", "passed")
        code, out = self.run_acc("gate")
        self.assertEqual(code, 1, out)
        self.assertIn("t_ab12/AC-1.S1: failed in then: boom", out)

    def test_covers_prints_ids(self) -> None:
        code, out = self.run_acc("covers", PR_BODY="Covers: t_ab12/AC-1.S1,t_ab12/AC-2.S1\nCovers: t_ab12/AC-1.S1 t_ab12/AC-3.S2\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "t_ab12/AC-1.S1 t_ab12/AC-2.S1 t_ab12/AC-3.S2")
        self.assertEqual(self.run_acc("covers")[1].strip(), "")


class ClassifyBase(AcceptanceCase):

    BASE = "b" * 40
    COVERS = "Covers: t_ab12/AC-1.S1, t_ab12/AC-1.S2, t_ab12/AC-1.S3, t_ab12/AC-1.S4, t_ab12/AC-1.S5\n"

    def base_report(self, health: str = "passed") -> None:
        self.events("t_ab12/AC-1.S1", ("passed", "passed", "failed"), error="exit 1: list is empty")  # RED
        self.events("t_ab12/AC-1.S2", ("passed", "passed", "passed"))  # GREEN
        self.events("t_ab12/AC-1.S3", ("failed", "not-run", "not-run"))  # Given broken
        self.events("t_ab12/AC-1.S4", ("passed",), runner="playwright")  # outside (timeout)
        self.playwright(("t_ab12/AC-1.S4 times out", "unexpected", "timedOut"))
        self.build("--health", health, "--playwright-exit", "1")

    def classify(self, body: str) -> tuple[int, str, dict]:
        code, out = self.run_acc("classify-base", "--base-sha", self.BASE, PR_BODY=body)
        return code, out, json.loads((self.ev / "acceptance-base.json").read_text())

    def test_verdicts_without_exemptions_fail(self) -> None:
        self.base_report()
        code, out, res = self.classify(self.COVERS)
        self.assertEqual(code, 1, out)
        verdicts = {r["id"]: (r["verdict"], r["failed_phase"], r["exempt"]) for r in res["results"]}
        self.assertEqual(verdicts, {
            "t_ab12/AC-1.S1": ("RED", "then", None),
            "t_ab12/AC-1.S2": ("GREEN", None, None),
            "t_ab12/AC-1.S3": ("INVALID", "given", None),
            "t_ab12/AC-1.S4": ("INVALID", "outside", None),
            "t_ab12/AC-1.S5": ("INVALID", None, None)})
        self.assertEqual(res["base_sha"], self.BASE)
        self.assertEqual(res["identity"]["head_sha"], "a" * 40)
        self.assertEqual(res["identity"]["run_attempt"], "2")
        self.assertEqual(res["results"][0]["error"], "exit 1: list is empty")
        self.assertIn("t_ab12/AC-1.S2: GREEN on base", out)
        self.assertIn("t_ab12/AC-1.S3: INVALID on base", out)
        self.assertNotIn("t_ab12/AC-1.S1: ", out.split("FAIL", 1)[1])

    def test_exemptions_accept_green_and_invalid(self) -> None:
        self.base_report()
        body = self.COVERS + (
            "Already-met: t_ab12/AC-1.S2 the endpoint already returned JSON before this PR\n"
            "Base-unverifiable: t_ab12/AC-1.S3 the seed table does not exist on base\n"
            "Base-unverifiable: t_ab12/AC-1.S4 page does not exist on base, navigation times out\n"
            "Base-unverifiable: t_ab12/AC-1.S5 scenario file is new and the route is missing\n")
        code, out, res = self.classify(body)
        self.assertEqual(code, 0, out)
        self.assertEqual([r["exempt"] for r in res["results"]],
                         [None, "Already-met", "Base-unverifiable", "Base-unverifiable", "Base-unverifiable"])

    def test_exemption_needs_a_real_reason_and_matching_verdict(self) -> None:
        self.base_report()
        body = ("Covers: t_ab12/AC-1.S2 t_ab12/AC-1.S3\n"
                "Already-met: t_ab12/AC-1.S2 dunno\n"
                "Already-met: t_ab12/AC-1.S3 the precondition was already true before\n")
        code, out, res = self.classify(body)
        self.assertEqual(code, 1, out)
        self.assertIn("needs a reason of at least 10 characters", out)
        self.assertEqual([r["exempt"] for r in res["results"]], [None, None])

    def test_health_failed_makes_every_covered_id_invalid(self) -> None:
        self.base_report(health="failed")
        code, out, res = self.classify("Covers: t_ab12/AC-1.S1 t_ab12/AC-1.S2\n")
        self.assertEqual(code, 1, out)
        self.assertEqual([r["verdict"] for r in res["results"]], ["INVALID", "INVALID"])
        self.assertIn("health check failed", res["results"][0]["error"])

    def test_bad_base_sha_is_usage_error(self) -> None:
        self.base_report()
        code, out = self.run_acc("classify-base", "--base-sha", "main", PR_BODY=self.COVERS)
        self.assertEqual(code, 2, out)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class ShellScenarios(unittest.TestCase):
    """The verify.sh `scenario` helper in a repo without a service (DEVFLOW_SERVICE empty, no Docker)."""

    SCENARIOS = r"""
ok_given() { echo 2 >"$W/count"; [[ $(cat "$W/count") == 2 ]]; }
ok_when() { echo "id,amount" >"$W/out.csv"; }
ok_then() { [[ $(head -1 "$W/out.csv") == id,amount ]]; }
scenario t_kit/AC-1.S1 "匯出 CSV" ok --given "有 2 筆訂單" --when "執行 export" --then "第一列是 id,amount" --smoke
red_given() { true; }
red_when() { false; echo "never"; }
red_then() { true; }
scenario t_kit/AC-1.S2 "When 中途失敗" red --given "g" --when "w" --then "t"
bad_given() { false; }
bad_when() { true; }
bad_then() { true; }
scenario t_kit/AC-2.S1 "Given 壞了" bad --given "g" --when "w" --then "t"
scenario t_kit/AC-3.S1 "少函式" nothere --given "g" --when "w" --then "t"
"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="devflow-verify-"))
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copytree(TEMPLATE, self.dir / ".devflow", ignore=shutil.ignore_patterns("__pycache__"))
        cfg = self.dir / ".devflow" / "config.env"
        cfg.write_text(cfg.read_text().replace("DEVFLOW_SERVICE=app", "DEVFLOW_SERVICE="))
        verify = self.dir / ".devflow" / "verify.sh"
        marker = "# devflow: end of scenarios"
        text = verify.read_text()
        self.assertIn(marker, text)
        verify.write_text(text.replace(marker, self.SCENARIOS + marker, 1))

    def verify(self, **env: str) -> tuple[int, str, dict]:
        p = subprocess.run(["bash", ".devflow/verify.sh"], cwd=self.dir, capture_output=True, text=True,
                           env=clean_env(**{"DEVFLOW_KIND": "pr", **env}))
        report = json.loads((self.dir / ".devflow" / "evidence" / "acceptance.json").read_text())
        return p.returncode, p.stdout + p.stderr, report

    def test_runs_every_scenario_then_gates(self) -> None:
        code, out, report = self.verify()
        self.assertEqual(code, 1, out)
        self.assertEqual(report["health"], "none")
        by = {s["id"]: s for s in report["scenarios"]}
        self.assertEqual((by["t_kit/AC-1.S1"]["status"], by["t_kit/AC-1.S1"]["smoke"]), ("passed", True))
        self.assertEqual([st["title"] for st in by["t_kit/AC-1.S1"]["steps"]], ["有 2 筆訂單", "執行 export", "第一列是 id,amount"])
        self.assertEqual((by["t_kit/AC-1.S2"]["status"], by["t_kit/AC-1.S2"]["failed_phase"]), ("failed", "when"))
        self.assertEqual([st["status"] for st in by["t_kit/AC-1.S2"]["steps"]], ["passed", "failed", "not-run"])
        self.assertEqual(by["t_kit/AC-2.S1"]["failed_phase"], "given")
        self.assertEqual(by["t_kit/AC-3.S1"]["status"], "structure")
        self.assertIn("nothere_given", by["t_kit/AC-3.S1"]["error"])
        self.assertIn("logs/t_kit__AC-1.S2.when.txt", by["t_kit/AC-1.S2"]["evidence"])

    def test_filters(self) -> None:
        code, out, report = self.verify(DEVFLOW_ONLY="t_kit/AC-1")
        self.assertEqual([s["id"] for s in report["scenarios"]], ["t_kit/AC-1.S1", "t_kit/AC-1.S2"])
        code, out, report = self.verify(DEVFLOW_SMOKE="1")
        self.assertEqual(code, 0, out)
        self.assertEqual([s["id"] for s in report["scenarios"]], ["t_kit/AC-1.S1"])
        code, out, report = self.verify(DEVFLOW_ONLY="t_kit/AC-1.S1", PR_BODY="Covers: t_kit/AC-1.S1 t_kit/AC-2.S1\n")
        self.assertEqual(code, 1, out)
        self.assertIn("t_kit/AC-2.S1: listed in `Covers:` but absent", out)

    def test_base_kind_only_reports(self) -> None:
        code, out, report = self.verify(DEVFLOW_KIND="base", DEVFLOW_ONLY="t_kit/AC-1.S2")
        self.assertEqual(code, 0, out)
        self.assertEqual(report["kind"], "base")
        self.assertEqual(self.scenario_ids(report), ["t_kit/AC-1.S2"])

    def scenario_ids(self, report: dict) -> list[str]:
        return [s["id"] for s in report["scenarios"]]


if __name__ == "__main__":
    unittest.main()
