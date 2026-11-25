"""PR-A tests: unified protected-file trust rules, enforced exact-head human approval,
plan_risk_mode (no wasted LLM calls / no advised poisoning when off), and doc/SOUL/skill consistency.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, REPO, Fake, load_devflow


class UnifiedTrustAndHumanApproval(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-pra-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state").mkdir()
        self.repo_dir = self.root / "repo"
        (self.repo_dir / ".devflow").mkdir(parents=True)
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.repo_dir), "port": 18101, "default_branch": "main"}
        }))
        os.environ["DEVFLOW_HERMES_DATA"] = str(self.root / "data")
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA", None)
        self.df = load_devflow(self.root)
        self.f = Fake(self.df)
        for name in ("pr_meta", "decide", "run", "kanban", "resume", "pr_reviews"):
            setattr(self.df, name, getattr(self.f, name))
        self.df.pr_files = lambda repo, n: self.f.files
        self.df.gh_login = lambda role: {"reviewer": "rev", "builder": "bob"}[role]
        self.df.deploy_readiness = lambda slug: []

    def test_unified_trust_covers_config_env_and_deploy_hooks_while_keeping_verify_sh_open(self) -> None:
        # Single source of truth between bin/devflow and lib/trust.py
        self.assertIs(self.df.GATE_FILES, self.df.trust_lib.TRUSTED_HARNESS_RE)
        self.assertIs(self.df.DELIVERY_FILES, self.df.trust_lib.DELIVERY_FILES_RE)

        for path in (
            ".devflow/config.env",
            ".devflow/deploy-build.sh",
            ".devflow/deploy-post.sh",
            ".devflow/operate.sh",
            ".devflow/test.sh",
        ):
            with self.subTest(path=path):
                res = self.df.judge(self.f.pr, [{"filename": path, "status": "modified", "patch": "+x"}])
                self.assertIn(path, res["gate_files"])
                self.assertTrue(res["needs_human_approval"])
                self.assertTrue(self.df.trust_lib.is_protected_file(path))

        # Editing deploy-build.sh / deploy-post.sh triggers replaces_standard, while config.env does not
        asked_hook: list[str] = []
        self.df.decide = lambda content, qs: asked_hook.extend(q["name"] for q in qs) or {
            q["name"]: {"probability": 0.05} for q in qs
        }
        self.df.judge({**self.f.pr, "title": "fix: x", "body": "Lesson: L-0001"},
                      [{"filename": ".devflow/deploy-build.sh", "status": "modified", "patch": "+x"}])
        self.assertIn("replaces_standard", asked_hook)

        asked_cfg: list[str] = []
        self.df.decide = lambda content, qs: asked_cfg.extend(q["name"] for q in qs) or {
            q["name"]: {"probability": 0.05} for q in qs
        }
        self.df.judge({**self.f.pr, "title": "fix: x", "body": "Lesson: L-0001"},
                      [{"filename": ".devflow/config.env", "status": "modified", "patch": "+DEVFLOW_PORT=18101"}])
        self.assertNotIn("replaces_standard", asked_cfg)

        # verify.sh stays non-gate (everyday scenario work)
        self.assertFalse(self.df.trust_lib.is_protected_file(".devflow/verify.sh"))

    def test_ship_merge_enforces_exact_head_human_approval_for_protected_files(self) -> None:
        self.f.files = [{"filename": ".devflow/config.env", "status": "modified", "patch": "+DEVFLOW_MAX_DIFF_FILES=999"}]
        # Advisor already signed off `ok`, reviewer approved exact head, yet human approval is still missing!
        rec = self.df.judgment("b", REPO, 7)
        self.df.update_judgment(f"{REPO}#7", fp=rec["fingerprint"], advisor="t_adv1")
        self.f.cards["t_adv1"] = {"status": "done", "verdict": "ok", "body": f"fingerprint: {rec['fingerprint']}"}

        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        self.assertEqual(cm.exception.code, 6)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["merged"])
        self.assertTrue(out["needs_human_approval"])
        self.assertTrue(out["question"].startswith("問題："))
        self.assertEqual(self.f.merges, [])

        # Worker cannot run `devflow approve`
        os.environ["HERMES_KANBAN_TASK"] = "t_ship"
        self.addCleanup(os.environ.pop, "HERMES_KANBAN_TASK", None)
        with self.assertRaises(SystemExit) as cm_w:
            self.df.cmd_approve(SimpleNamespace(board="b", pr=7, head="a" * 12, note="try", by="bot"))
        self.assertEqual(cm_w.exception.code, 1)
        os.environ.pop("HERMES_KANBAN_TASK", None)

        # Mismatched --head fails closed
        with self.assertRaises(SystemExit) as cm_bad:
            self.df.cmd_approve(SimpleNamespace(board="b", pr=7, head="b" * 12, note="wrong head", by="operator"))
        self.assertEqual(cm_bad.exception.code, 1)

        # Exact-head approval via `devflow approve` unblocks ship merge
        buf_app = io.StringIO()
        with redirect_stdout(buf_app):
            self.df.cmd_approve(SimpleNamespace(board="b", pr=7, head="a" * 12, note="ok", by="timmy"))
        self.assertTrue(json.loads(buf_app.getvalue())["approved"])

        buf_ok = io.StringIO()
        with redirect_stdout(buf_ok):
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        merged = json.loads(buf_ok.getvalue())
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["human_approval"], "state/human-approvals.json")
        self.assertEqual(len(self.f.merges), 1)

        # New head SHA invalidates old approval
        self.f.pr.update(state="OPEN", mergeCommit=None, headRefOid="c" * 40)
        self.f.reviews = [self.f.review("APPROVED", commit="c" * 40)]
        self.f.merges.clear()
        rec2 = self.df.judgment("b", REPO, 7)
        self.df.update_judgment(f"{REPO}#7", fp=rec2["fingerprint"], advisor="t_adv2")
        self.f.cards["t_adv2"] = {"status": "done", "verdict": "ok", "body": f"fingerprint: {rec2['fingerprint']}"}
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm_stale:
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        self.assertEqual(cm_stale.exception.code, 6)
        self.assertEqual(self.f.merges, [])

    def test_epic_user_decision_exact_head_directive_approves_protected_pr(self) -> None:
        db = self.root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        head = "a" * 40
        epic_body = (
            "## 目標\n調整設定\n## 驗收\n- AC-1 [cli] ok\n"
            f"## 使用者決定\n- 核准保護檔變更：PR #7 {head[:12]}\n"
        )
        with closing(sqlite3.connect(db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
                    completion_contract TEXT, goal_mode INTEGER DEFAULT 1);
            """)
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,1)", ("t_ep1", "[EPIC] e", epic_body, "todo", None))
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,1)",
                      ("t_dev1", "[OPS] cfg", "EPIC: t_ep1\n", "review", f"https://github.com/{REPO}/pull/7"))
        self.f.files = [{"filename": ".devflow/config.env", "status": "modified", "patch": "+DEVFLOW_PORT=18101"}]
        rec = self.df.judgment("b", REPO, 7)
        self.df.update_judgment(f"{REPO}#7", fp=rec["fingerprint"], advisor="t_adv1")
        self.f.cards["t_adv1"] = {"status": "done", "verdict": "ok", "body": f"fingerprint: {rec['fingerprint']}"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        out = json.loads(buf.getvalue())
        self.assertTrue(out["merged"])
        self.assertEqual(out["human_approval"], "EPIC ## 使用者決定")


class PlanRiskModeBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-prm-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state").mkdir()
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.root / "repo"), "port": 18101, "default_branch": "main"}
        }))
        os.environ["DEVFLOW_HERMES_DATA"] = str(self.root / "data")
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA", None)
        self.df = load_devflow(self.root)

    def test_tick_board_skips_llm_and_does_not_poison_advised_when_plan_risk_mode_is_off(self) -> None:
        decide_calls = 0
        self.df.decide = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("plan_risks must not call LLM when off"))
        self.df.deploy_readiness = lambda slug: []
        self.df.tick_prs = lambda *a: False
        self.df.release_held = lambda *a: None
        self.df.release_rate_limited_advisors = lambda *a: False
        self.df.route_questions = lambda *a: False
        comments: list[tuple[str, str]] = []
        epic_body = "## 目標\ng\n## 驗收\n- AC-1 [manual] 人工驗收\n"
        cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "body": epic_body, "status": "todo",
                      "assignee": "orchestrator", "workspace_kind": "dir", "parents": ["t_sh1"], "children": [], "goal_mode": False},
            "t_sh1": {"id": "t_sh1", "title": "[SHIP] s", "body": "覆蓋：AC-1\nEPIC: t_ep1", "status": "todo",
                      "assignee": "devops", "workspace_kind": "dir", "parents": [], "children": ["t_ep1"], "goal_mode": False},
        }
        self.df.plan_cards = lambda b, e: cards

        def fake_kanban(board, cmd, *args):
            if cmd == "list":
                return json.dumps([{"id": "t_ep1", "title": "[EPIC] e", "assignee": "orchestrator", "status": "todo"}])
            if cmd == "show":
                return json.dumps({"task": cards["t_ep1"], "parents": ["t_sh1"], "children": [], "comments": []})
            if cmd == "comment":
                comments.append((args[0], args[1]))
                return ""
            raise AssertionError((cmd, args))

        self.df.kanban = fake_kanban
        st: dict = {}
        self.df.tick_board("b", st)
        self.assertEqual(decide_calls, 0)
        self.assertEqual(st.get("advised", {}).get("t_ep1", []), [])
        self.assertTrue(any("plan_risk_mode=off" in body for _, body in comments), comments)

    def test_plan_risk_mode_needs_input_escalates_gating_risks_directly_without_adv(self) -> None:
        self.df.AUTO_PLAN_ADVISOR = False
        self.df.PLAN_RISK_MODE = "needs_input"
        self.df.deploy_readiness = lambda slug: []
        self.df.tick_prs = lambda *a: False
        self.df.release_held = lambda *a: None
        self.df.release_rate_limited_advisors = lambda *a: False
        self.df.route_questions = lambda *a: False
        self.df.decide = lambda content, qs: {
            q["name"]: {"probability": 0.9 if q["name"] == "scope_creep" else 0.05} for q in qs
        }
        escalated: list[tuple[str, str]] = []
        self.df.mark_plan_needs_input = lambda b, e, r: escalated.append((e, r)) or (99, False)
        epic_body = "## 目標\ng\n## 驗收\n- AC-1 [cli] ok\n  - S1 s\n    - Given g\n    - When w\n    - Then t\n"
        cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "body": epic_body, "status": "todo",
                      "assignee": "orchestrator", "workspace_kind": "dir", "parents": ["t_sh1"], "children": [], "goal_mode": False},
            "t_sh1": {"id": "t_sh1", "title": "[SHIP] s", "body": "覆蓋：AC-1\nEPIC: t_ep1", "status": "todo",
                      "assignee": "devops", "workspace_kind": "dir", "parents": [], "children": ["t_ep1"], "goal_mode": False},
        }
        self.df.plan_cards = lambda b, e: cards
        comments: list[str] = []

        def fake_kanban(board, cmd, *args):
            if cmd == "list":
                return json.dumps([{"id": "t_ep1", "title": "[EPIC] e", "assignee": "orchestrator", "status": "todo"}])
            if cmd == "show":
                return json.dumps({"task": cards["t_ep1"], "parents": ["t_sh1"], "children": [], "comments": []})
            if cmd == "comment":
                comments.append(args[1])
                return ""
            raise AssertionError((cmd, args))

        self.df.kanban = fake_kanban
        st: dict = {}
        self.df.tick_board("b", st)
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0][0], "t_ep1")
        self.assertIn("scope_creep", escalated[0][1])
        self.assertEqual(st.get("advised", {}).get("t_ep1", []), [])
        self.assertTrue(any("已轉 needs_input 通知使用者定錨" in c for c in comments), comments)


class DocAndContractConsistency(unittest.TestCase):
    """Ensure souls/, skills/, README.md, and bin/devflow never drift back to contradictory legacy rules."""

    def test_souls_and_skills_have_no_known_contradictions(self) -> None:
        default_soul = (KIT / "souls" / "default.md").read_text(encoding="utf-8")
        self.assertNotIn("devflow resume <slug> <task> --note", default_soul)
        self.assertIn("devflow answer", default_soul)
        self.assertNotIn("寫進卡片「提示」", default_soul)

        reviewer_soul = (KIT / "souls" / "reviewer.md").read_text(encoding="utf-8")
        self.assertNotIn("gh pr review --approve", reviewer_soul)
        self.assertIn("commit_id", reviewer_soul)

        orch_soul = (KIT / "souls" / "orchestrator.md").read_text(encoding="utf-8")
        self.assertNotIn("不設次數或時間上限", orch_soul)

        intake_skill = (KIT / "skills" / "devflow-intake" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("寫進卡片「提示」", intake_skill)

        cli_src = (KIT / "bin" / "devflow").read_text(encoding="utf-8")
        self.assertNotIn("metadata.board_ops", cli_src)

        # Every live doc / skill stays free of hardcoded project names or historical card IDs
        card_id_re = re.compile(r"\bt_[0-9a-f]{8}\b")
        for p in [KIT / "README.md", *sorted((KIT / "souls").glob("*.md")), *sorted((KIT / "skills").glob("*/SKILL.md"))]:
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("teams-transcription", text, str(p))
            self.assertIsNone(card_id_re.search(text), str(p))


if __name__ == "__main__":
    unittest.main()
