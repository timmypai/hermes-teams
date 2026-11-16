"""devflow PR judgment and the merge entry (`ship merge`): fail closed, bound to head + body.

GitHub, the kanban CLI and the Decisions API are replaced by in-memory fakes; the module under
test is bin/devflow with DEVFLOW_ROOT pointing at a temporary state directory.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

KIT = Path(__file__).resolve().parents[1]
REPO = "o/r"


def load_devflow(root: Path):
    os.environ["DEVFLOW_ROOT"] = str(root)
    loader = importlib.machinery.SourceFileLoader("devflow_cli", str(KIT / "bin" / "devflow"))
    spec = importlib.util.spec_from_loader("devflow_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Fake:
    """GitHub PR + kanban board + Decisions, recording every write."""

    def __init__(self, df):
        self.df = df
        self.pr = {"number": 7, "url": f"https://github.com/{REPO}/pull/7", "title": "feat: x", "body": "Adds x.",
                   "headRefOid": "a" * 40, "state": "OPEN", "mergeCommit": None}
        self.files = [{"filename": "app.py", "status": "modified", "patch": "-a\n+b"}]
        self.answers: dict[str, float] = {}
        self.decide_calls = 0
        self.decide_error: str | None = None
        self.edits_after_meta: list[dict] = []   # applied after the next pr_meta read
        self.statuses: list[str] = []
        self.merges: list[list[str]] = []
        self.merge_rc = 0
        self.cards: dict[str, dict] = {}
        self.links: list[tuple[str, str]] = []
        self.resumed: list[str] = []

    def pr_meta(self, repo, number):
        import hashlib
        pr = dict(self.pr)
        pr["fingerprint"] = hashlib.sha1(json.dumps([pr["headRefOid"], pr["title"], pr["body"]]).encode()).hexdigest()[:12]
        if self.edits_after_meta:
            self.pr.update(self.edits_after_meta.pop(0))
        return pr

    def decide(self, content, questions):
        self.decide_calls += 1
        if self.decide_error:
            raise self.df.DecisionsError(self.decide_error)
        return {q["name"]: {"name": q["name"], "probability": self.answers.get(q["name"], 0.05)} for q in questions}

    def run(self, cmd, gh=None, check=True, cwd=None, input=None):
        out, rc = "", 0
        if cmd[:3] == ["gh", "api", "-X"] and "/statuses/" in cmd[4]:
            self.statuses.append(next(x for x in cmd if x.startswith("state=")).split("=", 1)[1])
        elif cmd[:3] == ["gh", "pr", "merge"]:
            self.merges.append(cmd)
            rc = self.merge_rc
            if rc == 0:
                self.pr.update(state="MERGED", mergeCommit={"oid": "m" * 40})
        elif cmd[0] == self.df.HERMES and "create" in cmd:
            tid = f"t_{len(self.cards):08x}"
            self.cards[tid] = {"status": "todo", "title": cmd[cmd.index("create") + 1], "verdict": None}
            out = f"Created {tid}\n"
        elif cmd[0] == self.df.HERMES and "link" in cmd:
            self.links.append((cmd[-2], cmd[-1]))
        return SimpleNamespace(returncode=rc, stdout=out, stderr="refused" if rc else "")

    def kanban(self, board, *args):
        if args[0] == "show":
            c = self.cards[args[1]]
            runs = [{"status": "done", "metadata": {"verdict": c["verdict"]} if c["verdict"] else {}}] if c["status"] == "done" else []
            return json.dumps({"task": {"status": c["status"], "body": c.get("body", "")}, "runs": runs,
                               "children": c.get("children", [])})
        raise AssertionError(args)

    def resume(self, board, task, note=None):
        self.resumed.append(task)


class JudgmentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-judgment-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "state").mkdir()
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state" / "repos.json").write_text(json.dumps(
            {"b": {"slug": "b", "repo": REPO, "path": "/x", "port": 1, "default_branch": "main"}}))
        self.df = load_devflow(self.root)
        self.f = Fake(self.df)
        for name in ("pr_meta", "decide", "run", "kanban", "resume"):
            setattr(self.df, name, getattr(self.f, name))
        self.df.pr_files = lambda repo, n: self.f.files

    def ship(self, env_task: str | None = "t_ship") -> tuple[int, str]:
        if env_task:
            os.environ["HERMES_KANBAN_TASK"] = env_task
        else:
            os.environ.pop("HERMES_KANBAN_TASK", None)
        self.addCleanup(os.environ.pop, "HERMES_KANBAN_TASK", None)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
            return 0, buf.getvalue()
        except SystemExit as e:
            return int(e.code), buf.getvalue()

    def record(self) -> dict:
        return json.loads((self.root / "state" / "pr-judgments.json").read_text())[f"{REPO}#7"]


class Judge(JudgmentCase):
    def test_gate_file_change_is_a_finding_without_the_model(self) -> None:
        self.f.files.append({"filename": ".github/workflows/ci.yml", "status": "modified", "patch": "-x"})
        self.f.files.append({"filename": ".devflow/lessons/L-0001.toml", "status": "removed", "patch": None})
        rec = self.df.judgment("b", REPO, 7)
        self.assertEqual(rec["gate_files"], [".devflow/lessons/L-0001.toml", ".github/workflows/ci.yml"])
        self.assertEqual(len(rec["findings"]), 1)

    def test_flagged_question_is_a_finding(self) -> None:
        self.f.pr["body"] = "No-proof: hard to test here"
        self.f.answers = {"weak_exemption": 0.9}
        rec = self.df.judgment("b", REPO, 7)
        self.assertEqual([f.split("（")[0] for f in rec["findings"]], ["Decisions weak_exemption"])

    def test_questions_are_asked_only_when_they_apply(self) -> None:
        asked = []
        self.df.decide = lambda content, qs: asked.extend(q["name"] for q in qs) or {}
        self.f.pr.update(title="fix: x", body="Lesson: L-0001")
        self.df.judge(self.f.pr, self.f.files)
        self.assertEqual(asked, [])
        lesson = {"filename": ".devflow/lessons/L-0002.toml", "status": "added", "patch": "+x"}
        self.f.pr.update(title="chore: x", body="No-lesson: cannot recur because removed")
        self.df.judge(self.f.pr, [*self.f.files, lesson])
        self.assertEqual(asked, ["unlabeled_fix", "weak_exemption"])
        self.f.pr.update(title="fix: x", body="Lesson: L-0002")
        self.df.judge(self.f.pr, [*self.f.files, lesson])
        self.assertEqual(asked, ["unlabeled_fix", "weak_exemption", "lesson_mismatch"])

    def test_cached_per_head_and_body_and_a_new_body_drops_the_advisor(self) -> None:
        self.df.judgment("b", REPO, 7)
        self.df.judgment("b", REPO, 7)
        self.assertEqual(self.f.decide_calls, 1)
        self.df.update_judgment(f"{REPO}#7", advisor="t_old")
        self.f.pr["body"] = "Lesson: L-0001\nNo-proof: trust me"
        rec = self.df.judgment("b", REPO, 7)
        self.assertEqual(self.f.decide_calls, 2)
        self.assertIsNone(rec["advisor"])


class ShipMerge(JudgmentCase):
    def test_api_failure_refuses_without_status_or_merge(self) -> None:
        self.f.decide_error = "HTTP 500"
        code, _ = self.ship()
        self.assertEqual(code, 1)
        self.assertEqual((self.f.statuses, self.f.merges), ([], []))

    def test_clean_pr_posts_status_then_merges_the_judged_head(self) -> None:
        code, out = self.ship()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.f.statuses, ["success"])
        self.assertIn("a" * 40, self.f.merges[0][self.f.merges[0].index("--match-head-commit") + 1])

    def test_finding_without_advisor_waits_and_records_the_ship_card(self) -> None:
        self.f.answers = {"unlabeled_fix": 0.8}
        code, _ = self.ship()
        self.assertEqual(code, 3)
        self.assertEqual((self.f.statuses, self.f.merges), ([], []))
        self.assertEqual(self.record()["ship_card"], "t_ship")

    def test_advisor_verdict_decides(self) -> None:
        self.f.answers = {"unlabeled_fix": 0.8}
        self.df.judgment("b", REPO, 7)
        self.df.update_judgment(f"{REPO}#7", advisor="t_adv")
        for status, verdict, want in (("running", None, 3), ("done", "revise", 4), ("done", None, 4)):
            self.f.cards["t_adv"] = {"status": status, "verdict": verdict}
            self.assertEqual(self.ship()[0], want, (status, verdict))
            self.assertEqual(self.f.merges, [])
        self.f.cards["t_adv"] = {"status": "done", "verdict": "ok"}
        self.assertEqual(self.ship()[0], 0)
        self.assertEqual(len(self.f.merges), 1)

    def test_body_edited_after_judgment_withdraws_status_and_refuses(self) -> None:
        self.f.edits_after_meta = [{"body": "Lesson: L-0001\nNo-proof: edited after judging"}]
        code, _ = self.ship()
        self.assertEqual(code, 1)
        self.assertEqual(self.f.statuses, ["success", "failure"])
        self.assertEqual(self.f.merges, [])

    def test_refused_merge_withdraws_status(self) -> None:
        self.f.merge_rc = 1
        code, _ = self.ship()
        self.assertEqual(code, 1)
        self.assertEqual(self.f.statuses, ["success", "failure"])

    def test_already_merged_is_reported_without_judging(self) -> None:
        self.f.pr.update(state="MERGED", mergeCommit={"oid": "m" * 40})
        code, out = self.ship()
        self.assertEqual((code, self.f.decide_calls), (0, 0))
        self.assertIn("m" * 40, out)


class Tick(JudgmentCase):
    def tasks(self, review_status: str, ship_status: str) -> list[dict]:
        return [{"id": "t_dev", "title": "[DEV] x", "assignee": "reviewer", "status": review_status,
                 "completion_contract": f"https://github.com/{REPO}/pull/7"},
                {"id": "t_ship", "title": "[SHIP] x", "assignee": "devops", "status": ship_status,
                 "completion_contract": None}]

    def test_review_finding_opens_advisor_before_ship_can_run(self) -> None:
        self.f.answers = {"unlabeled_fix": 0.9}
        self.f.cards["t_dev"] = {"status": "running", "verdict": None, "children": ["t_ship"], "body": "EPIC: t_00000abc"}
        self.df.tick_prs("b", REPO, self.tasks("running", "todo"))
        adv = self.record()["advisor"]
        self.assertTrue(self.f.cards[adv]["title"].startswith("[ADV] PR 判斷"))
        self.assertEqual(self.f.links, [(adv, "t_ship")])
        self.df.tick_prs("b", REPO, self.tasks("running", "todo"))
        self.assertEqual(len(self.f.links), 1)

    def test_parked_ship_gets_an_advisor_and_resumes_after_its_verdict(self) -> None:
        self.f.answers = {"unlabeled_fix": 0.8}
        self.assertEqual(self.ship()[0], 3)  # review raced past the tick
        self.f.cards["t_ship"] = {"status": "blocked", "verdict": None, "body": "EPIC: t_00000abc"}
        tasks = [{"id": "t_ship", "title": "[SHIP] x", "assignee": "devops", "status": "blocked", "completion_contract": None}]
        self.df.tick_prs("b", REPO, tasks)
        adv = self.record()["advisor"]
        self.assertEqual((self.f.links, self.f.resumed), ([(adv, "t_ship")], []))
        self.f.cards[adv].update(status="done", verdict="ok")
        self.df.tick_prs("b", REPO, tasks)
        self.assertEqual(self.f.resumed, ["t_ship"])
        self.assertEqual(self.ship()[0], 0)


if __name__ == "__main__":
    unittest.main()
