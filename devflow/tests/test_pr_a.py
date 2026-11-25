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

    def test_plan_risk_mode_needs_input_escalates_and_durably_gates_ship_and_merge_until_resolved(self) -> None:
        self.df.AUTO_PLAN_ADVISOR = False
        self.df.PLAN_RISK_MODE = "needs_input"
        self.df.HERMES_PY = "/nonexistent-hermes-py"  # exercise real SQLite persistence path
        self.df.deploy_readiness = lambda slug: []
        self.df.tick_prs = lambda *a: False
        self.df.release_held = lambda *a: None
        self.df.release_rate_limited_advisors = lambda *a: False
        self.df.route_questions = lambda *a: False

        f = Fake(self.df)
        for name in ("pr_meta", "run", "pr_reviews"):
            setattr(self.df, name, getattr(f, name))
        self.df.pr_files = lambda repo, n: f.files
        self.df.gh_login = lambda role: {"reviewer": "rev", "builder": "bob"}[role]
        self.df.decide = lambda content, qs: {
            q["name"]: {"probability": 0.9 if q["name"] == "scope_creep" else 0.05} for q in qs
        }

        db = self.root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        epic_body = "## 目標\ng\n## 驗收\n- AC-1 [cli] ok\n  - S1 s\n    - Given g\n    - When w\n    - Then t\n"
        dev_body = f"## 目標\nd\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nEPIC: t_ep1\n"
        with closing(sqlite3.connect(db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT,
                    workspace_kind TEXT, workspace_path TEXT, completion_contract TEXT,
                    created_by TEXT, result TEXT, goal_mode INTEGER NOT NULL DEFAULT 0,
                    block_kind TEXT
                );
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER
                );
            """)
            c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("t_ep1", "[EPIC] e", epic_body, "todo", "orchestrator", "dir", str(self.root / "repo"),
                 None, "default", None, 0, None),
                ("t_dev1", "[DEV] d", dev_body, "done", "developer", "worktree", str(self.root / "repo"),
                 f"https://github.com/{REPO}/pull/7", "orchestrator", None, 1, None),
                ("t_sh1", "[SHIP] s", "覆蓋：AC-1\nEPIC: t_ep1\n", "todo", "devops", "dir", str(self.root / "repo"),
                 None, "orchestrator", None, 0, None),
            ])
            c.executemany("INSERT INTO task_links VALUES (?,?)", [("t_dev1", "t_sh1"), ("t_sh1", "t_ep1")])

        comments: list[str] = []
        resumed: list[tuple[str, str | None]] = []

        def fake_kanban(board, cmd, *args):
            if cmd == "list":
                with closing(sqlite3.connect(db)) as c:
                    c.row_factory = sqlite3.Row
                    return json.dumps([dict(r) for r in c.execute("SELECT id, title, assignee, status FROM tasks")])
            if cmd == "show":
                tid = args[0]
                with closing(sqlite3.connect(db)) as c:
                    c.row_factory = sqlite3.Row
                    t = dict(c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
                    parents = [r[0] for r in c.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (tid,))]
                    children = [r[0] for r in c.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (tid,))]
                return json.dumps({"task": t, "parents": parents, "children": children, "comments": []})
            if cmd == "comment":
                comments.append(args[1])
                return ""
            raise AssertionError((cmd, args))

        def fake_resume(board, tid, note=None):
            resumed.append((tid, note))
            with closing(sqlite3.connect(db)) as c, c:
                c.execute("UPDATE tasks SET status = 'ready', block_kind = NULL WHERE id = ?", (tid,))
            return f"resumed {tid}"

        self.df.kanban = fake_kanban
        self.df.resume = fake_resume

        # 1. Tick detects scope_creep -> blocks BOTH t_ep1 (needs_input) AND pending t_sh1 (dependency) in SQLite.
        st: dict = {}
        self.df.tick_board("b", st)
        with closing(sqlite3.connect(db)) as c:
            states = dict(c.execute("SELECT id, status || ':' || COALESCE(block_kind, '') FROM tasks").fetchall())
        self.assertEqual(states["t_ep1"], "blocked:needs_input")
        self.assertEqual(states["t_sh1"], "blocked:dependency")
        self.assertEqual(st.get("plan_risk_holds", {}).get("t_ep1"), ["t_sh1"])
        self.assertEqual(st.get("advised", {}).get("t_ep1", []), [])
        self.assertTrue(any("暫停待出貨 [SHIP]" in c for c in comments), comments)

        # 2. Even an already-running [SHIP] worker calling `ship merge` is blocked while t_ep1 awaits user decision!
        self.df.decide = lambda content, qs: {q["name"]: {"probability": 0.05} for q in qs}
        buf_blk = io.StringIO()
        with redirect_stdout(buf_blk), self.assertRaises(SystemExit) as cm:
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        self.assertEqual(cm.exception.code, 6)
        blk_out = json.loads(buf_blk.getvalue())
        self.assertFalse(blk_out["merged"])
        self.assertTrue(blk_out["plan_risk_blocked"])
        self.assertEqual(blk_out["epic"], "t_ep1")
        self.assertEqual(f.merges, [])

        # 3. User resolves the EPIC decision (t_ep1 leaves blocked/needs_input) -> tick releases t_sh1 & merge succeeds.
        with closing(sqlite3.connect(db)) as c, c:
            c.execute("UPDATE tasks SET status = 'ready', block_kind = NULL WHERE id = 't_ep1'")
        self.df.tick_board("b", st)
        self.assertEqual([t for t, _ in resumed], ["t_sh1"])
        self.assertNotIn("t_ep1", st.get("plan_risk_holds") or {})
        with closing(sqlite3.connect(db)) as c:
            states_after = dict(c.execute("SELECT id, status || ':' || COALESCE(block_kind, '') FROM tasks").fetchall())
        self.assertEqual(states_after["t_sh1"], "ready:")

        buf_ok = io.StringIO()
        with redirect_stdout(buf_ok):
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        self.assertTrue(json.loads(buf_ok.getvalue())["merged"])
        self.assertEqual(len(f.merges), 1)

        # 4. Materially changing the plan (adding a new work card) with renewed scope_creep MUST re-arm
        #    both the EPIC needs_input block AND the [SHIP] hold + merge block!
        self.df.decide = lambda content, qs: {
            q["name"]: {"probability": 0.92 if q["name"] == "scope_creep" else 0.05} for q in qs
        }
        with closing(sqlite3.connect(db)) as c, c:
            c.execute("UPDATE tasks SET status = 'todo', block_kind = NULL WHERE id = 't_ep1'")
            c.execute("UPDATE tasks SET status = 'ready', block_kind = NULL WHERE id = 't_sh1'")
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                "t_dev2", "[DEV] extra unrequested subsystem",
                f"## 目標\n額外功能\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nEPIC: t_ep1\n",
                "ready", "developer", "worktree", str(self.root / "repo"), None, "orchestrator", None, 1, None,
            ))
            c.execute("INSERT INTO task_links VALUES (?,?)", ("t_dev2", "t_sh1"))
        self.df.tick_board("b", st)
        with closing(sqlite3.connect(db)) as c:
            states_rearmed = dict(c.execute("SELECT id, status || ':' || COALESCE(block_kind, '') FROM tasks").fetchall())
        self.assertEqual(states_rearmed["t_ep1"], "blocked:needs_input")
        self.assertEqual(states_rearmed["t_sh1"], "blocked:dependency")
        self.assertEqual(st.get("plan_risk_holds", {}).get("t_ep1"), ["t_sh1"])
        f.pr.update(state="OPEN", mergeCommit=None)
        f.merges.clear()
        self.df.decide = lambda content, qs: {q["name"]: {"probability": 0.05} for q in qs}
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm_rearmed:
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        self.assertEqual(cm_rearmed.exception.code, 6)
        self.assertEqual(f.merges, [])

    def test_enabling_plan_risk_mode_invalidates_cached_plan_and_tick_key_and_gates_ship(self) -> None:
        self.df.AUTO_PLAN_ADVISOR = False
        self.df.PLAN_RISK_MODE = "off"
        self.df.HERMES_PY = "/nonexistent-hermes-py"
        self.df.deploy_readiness = lambda slug: []
        self.df.tick_prs = lambda *a: False
        self.df.release_held = lambda *a: None
        self.df.release_rate_limited_advisors = lambda *a: False
        self.df.route_questions = lambda *a: False

        db = self.root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        epic_body = "## 目標\ng\n## 驗收\n- AC-1 [cli] ok\n  - S1 s\n    - Given g\n    - When w\n    - Then t\n"
        with closing(sqlite3.connect(db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT,
                    workspace_kind TEXT, workspace_path TEXT, completion_contract TEXT,
                    created_by TEXT, result TEXT, goal_mode INTEGER NOT NULL DEFAULT 0,
                    block_kind TEXT
                );
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER
                );
            """)
            c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("t_ep1", "[EPIC] e", epic_body, "todo", "orchestrator", "dir", str(self.root / "repo"),
                 None, "default", None, 0, None),
                ("t_sh1", "[SHIP] s", "覆蓋：AC-1\nEPIC: t_ep1\n", "todo", "devops", "dir", str(self.root / "repo"),
                 None, "orchestrator", None, 0, None),
            ])
            c.execute("INSERT INTO task_links VALUES ('t_sh1', 't_ep1')")

        def fake_kanban(board, cmd, *args):
            if cmd == "list":
                with closing(sqlite3.connect(db)) as c:
                    c.row_factory = sqlite3.Row
                    return json.dumps([dict(r) for r in c.execute("SELECT id, title, assignee, status FROM tasks")])
            if cmd == "show":
                tid = args[0]
                with closing(sqlite3.connect(db)) as c:
                    c.row_factory = sqlite3.Row
                    t = dict(c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
                    parents = [r[0] for r in c.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (tid,))]
                    children = [r[0] for r in c.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (tid,))]
                return json.dumps({"task": t, "parents": parents, "children": children, "comments": []})
            if cmd == "comment":
                return ""
            raise AssertionError((cmd, args))

        self.df.kanban = fake_kanban
        calls: list[list[str]] = []
        self.df.decide = lambda content, qs: calls.append([q["name"] for q in qs]) or {
            q["name"]: {"probability": 0.9 if q["name"] == "scope_creep" else 0.05} for q in qs
        }

        # 1. Tick while plan_risk_mode="off": caches plan without calling LLM.
        st: dict = {}
        key_off = self.df.tick_key("b")
        self.df.tick_board("b", st)
        self.assertEqual(calls, [])
        self.assertIn("t_ep1", st.get("plans") or {})

        # 2. Switch policy off -> needs_input on the EXACT SAME unchanged plan.
        self.df.PLAN_RISK_MODE = "needs_input"
        key_on = self.df.tick_key("b")
        self.assertNotEqual(key_off, key_on)
        self.df.tick_board("b", st)
        self.assertEqual(len(calls), 1)
        with closing(sqlite3.connect(db)) as c:
            states = dict(c.execute("SELECT id, status || ':' || COALESCE(block_kind, '') FROM tasks").fetchall())
        self.assertEqual(states["t_ep1"], "blocked:needs_input")
        self.assertEqual(states["t_sh1"], "blocked:dependency")


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
