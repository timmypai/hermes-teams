"""Regression tests for PR-B:
1. Required bounded Allowed-Scope + DEVFLOW_TEST_PATHS exemption (Out-of-Scope still wins).
2. Review round breaker intervention watermark reset (completed [ADJ] / human unblock) + preflight enforcement.
3. Unified DEVFLOW_DEPLOY parsing + DEVFLOW_DEPLOY_MANUAL_ONLY=true closed loop.
4. tick_key hashing devflow/lib/*.py, templates_digest, human-approvals, and policy flags.
5. cmd_preflight reading local worktree .devflow/scenarios.json and failing closed on malformed JSON.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, REPO, Fake, load_devflow


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_git_repo(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")


def _seed_board_db(db: Path, tasks: list[tuple], links: list[tuple] = (), events: list[tuple] = ()) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as c, c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT,
                workspace_kind TEXT, workspace_path TEXT, completion_contract TEXT,
                created_by TEXT, result TEXT, goal_mode INTEGER NOT NULL DEFAULT 0,
                block_kind TEXT, completed_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER
            );
        """)
        c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tasks)
        if links:
            c.executemany("INSERT INTO task_links VALUES (?,?)", links)
        if events:
            c.executemany("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)", events)


class AllowedScopeAndTestPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-prb-scope-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir()
        (self.repo_dir / ".devflow").mkdir()
        (self.root / "state").mkdir()
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.repo_dir), "port": 18100,
                  "default_branch": "main", "contract": True}
        }))
        self.df = load_devflow(self.root)

    def test_check_card_and_plan_rules_require_bounded_allowed_scope(self) -> None:
        base_body = f"## 目標\n新增顯示\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nEPIC: t_ep1"
        # Missing Allowed-Scope fails check card
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
            self.df.cmd_check_card(SimpleNamespace(
                title="[DEV] 缺範圍", body=base_body, body_file=None, assignee="developer", workspace="worktree", board="b"
            ))
        self.assertEqual(cm.exception.code, 1)
        self.assertTrue(any("Allowed-Scope" in f for f in json.loads(buf.getvalue())["findings"]))

        # Overly broad wildcards fail check card
        for bad in ("*", "**", "**/*", ".", "./**"):
            buf = io.StringIO()
            with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
                self.df.cmd_check_card(SimpleNamespace(
                    title="[DEV] 過寬", body=f"{base_body}\nAllowed-Scope: {bad}", body_file=None,
                    assignee="developer", workspace="worktree", board="b"
                ))
            self.assertEqual(cm.exception.code, 1, bad)
            self.assertTrue(any("過寬" in f for f in json.loads(buf.getvalue())["findings"]), bad)

        # Bounded Allowed-Scope passes check card
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_check_card(SimpleNamespace(
                title="[DEV] 明確範圍", body=f"{base_body}\nAllowed-Scope: app/**", body_file=None,
                assignee="developer", workspace="worktree", board="b"
            ))
        self.assertTrue(json.loads(buf.getvalue())["ok"])

        # [SHIP] does not require Allowed-Scope
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_check_card(SimpleNamespace(
                title="[SHIP] 合併部署", body="## 目標\n合併並部署\nEPIC: t_ep1", body_file=None,
                assignee="devops", workspace="dir", board="b"
            ))
        self.assertTrue(json.loads(buf.getvalue())["ok"])

        # plan_rules flags missing & overly broad Allowed-Scope on open [DEV], and honors repo opt-out
        epic_body = "## 目標\ng\n## 驗收\n- AC-1 [cli] ok\n  - S1 s\n    - Given g\n    - When w\n    - Then t\n"
        cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "body": epic_body, "status": "todo",
                      "assignee": "orchestrator", "workspace_kind": "dir", "parents": ["t_sh1"], "children": [], "goal_mode": False},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] d1", "body": base_body, "status": "ready",
                       "assignee": "developer", "workspace_kind": "worktree", "completion_contract": REPO,
                       "parents": [], "children": ["t_sh1"], "goal_mode": True},
            "t_sh1": {"id": "t_sh1", "title": "[SHIP] s1", "body": "EPIC: t_ep1", "status": "todo",
                      "assignee": "devops", "workspace_kind": "dir", "parents": ["t_dev1"], "children": ["t_ep1"], "goal_mode": False},
        }
        findings = self.df.plan_rules("b", "t_ep1", cards)
        self.assertTrue(any("Allowed-Scope" in f for f in findings), findings)

        (self.repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_REQUIRE_ALLOWED_SCOPE=false\n", encoding="utf-8")
        findings_optout = self.df.plan_rules("b", "t_ep1", cards)
        self.assertFalse(any("Allowed-Scope" in f for f in findings_optout), findings_optout)

    def test_devflow_test_paths_exemption_and_out_of_scope_precedence_and_fingerprint(self) -> None:
        # Without DEVFLOW_TEST_PATHS, modifying tests/test_a.py fails Allowed-Scope: app/**
        f_no = self.df.scope_lib.check_files_against_scope(
            ["app/a.py", "tests/test_a.py"], allowed_scope=["app/**"]
        )
        self.assertEqual(len(f_no), 1)
        self.assertIn("tests/test_a.py", f_no[0])

        # With DEVFLOW_TEST_PATHS, tests/test_a.py is exempted alongside app/**
        f_yes = self.df.scope_lib.check_files_against_scope(
            ["app/a.py", "tests/test_a.py"], allowed_scope=["app/**"], test_paths=["tests/**"]
        )
        self.assertEqual(f_yes, [])

        # Out-of-Scope ALWAYS overrides DEVFLOW_TEST_PATHS
        f_oos = self.df.scope_lib.check_files_against_scope(
            ["app/a.py", "tests/secret/test_x.py"],
            allowed_scope=["app/**"],
            out_of_scope=["tests/secret/**"],
            test_paths=["tests/**"],
        )
        self.assertEqual(len(f_oos), 1)
        self.assertIn("Out-of-Scope", f_oos[0])

        # judgment_fingerprint binds DEVFLOW_TEST_PATHS so changing it invalidates PR cache
        pr = {"fingerprint": "abc123def456"}
        fp1 = self.df.judgment_fingerprint(pr, repo_cfg={})
        fp2 = self.df.judgment_fingerprint(pr, repo_cfg={"DEVFLOW_TEST_PATHS": "tests/**"})
        self.assertNotEqual(fp1, fp2)


class ReviewRoundBreakerWatermark(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-prb-brk-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        self.repo_dir = self.root / "repo"
        _init_git_repo(self.repo_dir, {
            ".devflow/config.env": "DEVFLOW_DEPLOY=false\n",
            "app/a.py": "x = 1\n",
        })
        (self.root / "state").mkdir()
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.repo_dir), "port": 18100,
                  "default_branch": "main", "contract": True}
        }))
        self.df = load_devflow(self.root)
        self.df.BOARDS = self.root / "data" / "kanban" / "boards"
        self.db = self.df.BOARDS / "b" / "kanban.db"

    def test_watermark_resets_on_completed_adj_and_human_unblock_but_not_transient_and_blocks_preflight(self) -> None:
        dev_body = f"## 目標\ng\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1"
        _seed_board_db(
            self.db,
            tasks=[
                ("t_ep1", "[EPIC] e", "## 目標\ng\n## 驗收\n- AC-1 [cli] ok\n  - S1 s\n    - Given g\n    - When w\n    - Then t\n",
                 "todo", "orchestrator", "dir", str(self.repo_dir), None, "default", None, 0, None, None),
                ("t_dev1", "[DEV] d1", dev_body, "ready", "developer", "worktree", str(self.repo_dir),
                 REPO, "orchestrator", None, 1, None, None),
            ],
            links=[("t_dev1", "t_ep1")],
            events=[
                ("t_dev1", "changes_requested", "{}", 1000),
                ("t_dev1", "changes_requested", "{}", 1100),
                # Transient block + unblock MUST NOT reset review round breaker
                ("t_dev1", "blocked", json.dumps({"kind": "transient", "reason": "tmp"}), 1150),
                ("t_dev1", "unblocked", "{}", 1160),
            ],
        )
        self.assertEqual(self.df.kanban_adapter.card_changes_requested_count(self.df.BOARDS, "b", "t_dev1"), 2)

        # Modify app/a.py and commit so preflight checks review_loop
        (self.repo_dir / "app" / "a.py").write_text("x = 2\n", encoding="utf-8")
        _git(self.repo_dir, "add", "app/a.py")
        _git(self.repo_dir, "commit", "-q", "-m", "feat: update a")

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
            self.df.cmd_preflight(SimpleNamespace(
                slug="b", task="t_dev1", dir=str(self.repo_dir), base="HEAD~1",
                pr_title="feat: update a", pr_body="", pr_body_file=None, no_test=True, allow_dirty=False,
            ))
        self.assertEqual(cm.exception.code, 1)
        rep = json.loads(buf.getvalue())
        self.assertEqual(rep["checks"]["review_loop"], "fail")
        self.assertTrue(rep["review_loop"]["tripped"])

        # Completing an [ADJ] targeting t_dev1 (multi-target body) resets watermark -> count=0 & preflight passes!
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("t_adj1", "[ADJ] 處理失敗", "處理：t_other, t_dev1\n重新定錨根因", "done", "orchestrator",
                 "dir", str(self.repo_dir), None, "orchestrator", "done", 0, None, 1500),
            )
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_adj1', 'completed', '{}', 1500)")
        self.assertEqual(self.df.kanban_adapter.card_changes_requested_count(self.df.BOARDS, "b", "t_dev1"), 0)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            self.df.cmd_preflight(SimpleNamespace(
                slug="b", task="t_dev1", dir=str(self.repo_dir), base="HEAD~1",
                pr_title="feat: update a", pr_body="", pr_body_file=None, no_test=True, allow_dirty=False,
            ))
        rep2 = json.loads(buf2.getvalue())
        self.assertTrue(rep2["ok"], rep2)
        self.assertEqual(rep2["checks"]["review_loop"], "ok")

        # 2 new rejections AFTER the [ADJ] trip again, then human needs_input -> unblocked resets again!
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_dev1', 'changes_requested', '{}', 1600)")
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_dev1', 'changes_requested', '{}', 1700)")
        self.assertEqual(self.df.kanban_adapter.card_changes_requested_count(self.df.BOARDS, "b", "t_dev1"), 2)

        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_dev1', 'blocked', ?, 1800)",
                      (json.dumps({"kind": "needs_input", "reason": "人定錨"}),))
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_dev1', 'unblocked', '{}', 1900)")
        self.assertEqual(self.df.kanban_adapter.card_changes_requested_count(self.df.BOARDS, "b", "t_dev1"), 0)

    def test_github_review_loop_status_respects_watermark_timestamp(self) -> None:
        f = Fake(self.df)
        for name in ("pr_meta", "run", "pr_reviews"):
            setattr(self.df, name, getattr(f, name))
        self.df.gh_login = lambda role: {"reviewer": "rev", "builder": "bob"}[role]
        # Watermark at 2026-11-20T05:00:00Z = 1795150800
        wm_ts = 1795150800
        _seed_board_db(
            self.db,
            tasks=[
                ("t_dev1", "[DEV] d1", "Allowed-Scope: app/**", "ready", "developer", "worktree",
                 str(self.repo_dir), REPO, "orchestrator", None, 1, None, None),
                ("t_adj1", "[ADJ] 流程阻塞 t_dev1", "reset", "done", "orchestrator", "dir",
                 str(self.repo_dir), None, "orchestrator", "ok", 0, None, wm_ts),
            ],
        )
        r = f.review
        # 2 rejections BEFORE watermark + 1 AFTER watermark -> rounds=1, NOT tripped!
        f.reviews = [
            r("CHANGES_REQUESTED", at="2026-11-20T01:00:00Z"),
            r("CHANGES_REQUESTED", at="2026-11-20T02:00:00Z"),
            r("CHANGES_REQUESTED", at="2026-11-20T06:00:00Z"),
        ]
        st = self.df.review_loop_status("b", REPO, 7, "t_dev1")
        self.assertEqual(st["rounds"], 1)
        self.assertFalse(st["tripped"])
        self.assertEqual(st["reset_at"], "2026-11-20T05:00:00Z")

        # 2nd rejection AFTER watermark -> rounds=2, tripped!
        f.reviews.append(r("CHANGES_REQUESTED", at="2026-11-20T07:00:00Z"))
        st2 = self.df.review_loop_status("b", REPO, 7, "t_dev1")
        self.assertEqual(st2["rounds"], 2)
        self.assertTrue(st2["tripped"])
        self.assertEqual(st2["action"], "stop_ping_pong_escalate")


class UnifiedDeployAndManualOnly(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-prb-dep-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        self.repo_dir = self.root / "repo"
        (self.repo_dir / ".devflow").mkdir(parents=True)
        (self.root / "state").mkdir()
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.repo_dir), "port": 18100,
                  "default_branch": "main", "contract": True}
        }))
        self.df = load_devflow(self.root)

    def test_repo_deploys_unified_across_policy_standard_and_cli(self) -> None:
        for val, want in (("false", False), ("FALSE", False), ("0", False), ("no", False), ("off", False),
                          ("true", True), ("1", True), ("", True)):
            cfg = {"DEVFLOW_DEPLOY": val} if val != "" else {}
            self.assertEqual(self.df.policy_lib.repo_deploys(cfg), want, val)
            req = self.df.standard_lib.required_standard_files(cfg)
            self.assertEqual(".github/workflows/deploy.yml" in req, want, val)
            (self.repo_dir / ".devflow" / "config.env").write_text(f"DEVFLOW_DEPLOY={val}\n" if val else "", encoding="utf-8")
            self.assertEqual(self.df.repo_deploys("b"), want, val)

    def test_ship_merge_outputs_deploy_manual_only_flag(self) -> None:
        (self.repo_dir / ".devflow" / "config.env").write_text(
            "DEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n", encoding="utf-8"
        )
        f = Fake(self.df)
        for name in ("pr_meta", "run", "pr_reviews"):
            setattr(self.df, name, getattr(f, name))
        self.df.pr_files = lambda repo, n: f.files
        self.df.gh_login = lambda role: {"reviewer": "rev", "builder": "bob"}[role]
        self.df.decide = lambda content, qs: {q["name"]: {"probability": 0.05} for q in qs}
        self.df.deploy_readiness = lambda slug: []
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        out = json.loads(buf.getvalue())
        self.assertTrue(out["merged"])
        self.assertTrue(out["deploys"])
        self.assertTrue(out["deploy_manual_only"])

    def test_check_acceptance_reports_manual_only_deploy_guidance_when_no_run_exists(self) -> None:
        (self.repo_dir / ".devflow" / "config.env").write_text(
            "DEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n", encoding="utf-8"
        )
        epic_body = (
            "## 目標\ng\n## 驗收\n"
            "- AC-1 [cli] ok\n"
            "  - S1 s\n"
            "    - Given g\n"
            "    - When w\n"
            "    - Then t\n"
        )
        self.df.plan_cards = lambda board, epic: {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "body": epic_body, "status": "todo",
                      "assignee": "orchestrator", "workspace_kind": "dir", "parents": [], "children": []},
        }
        self.df.pr_meta = lambda repo, n: {
            "number": n, "state": "MERGED", "headRefOid": "a" * 40,
            "body": "Covers: t_ep1/AC-1.S1", "mergeCommit": {"oid": "b" * 40},
        }
        self.df.gh_json = lambda args, **kw: (
            {"default_branch": "main"} if args == ["api", f"repos/{REPO}"]
            else {"sha": "c" * 40} if args == ["api", f"repos/{REPO}/commits/main"]
            else None
        )
        self.df.workflow_runs = lambda *a, **kw: []
        self.df.branch_file = lambda repo, ref, path: (
            "DEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n" if path == ".devflow/config.env" else None
        )
        rep = self.df.check_acceptance("b", "t_ep1", [7], review=False, evidence=[], tmp=self.root / "tmp")
        reasons = rep["criteria"][0]["reasons"]
        self.assertTrue(any("DEVFLOW_DEPLOY_MANUAL_ONLY=true" in r and "gh workflow run deploy.yml" in r for r in reasons), reasons)


class TickKeyAndPreflightLocalScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-prb-tk-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        shutil.copytree(KIT / "templates", self.root / "templates")
        (self.root / "lib").mkdir()
        (self.root / "lib" / "sample.py").write_text("V = 1\n", encoding="utf-8")
        self.repo_dir = self.root / "repo"
        self.worktree_dir = self.root / "wt"
        _init_git_repo(self.repo_dir, {".devflow/config.env": "DEVFLOW_DEPLOY=false\n", "app/a.py": "x = 1\n"})
        _init_git_repo(self.worktree_dir, {".devflow/config.env": "DEVFLOW_DEPLOY=false\n", "app/a.py": "x = 1\n"})
        (self.root / "state").mkdir()
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.repo_dir), "port": 18100,
                  "default_branch": "main", "contract": True}
        }))
        self.df = load_devflow(self.root)
        self.df.BOARDS = self.root / "data" / "kanban" / "boards"
        (self.df.BOARDS / "b").mkdir(parents=True, exist_ok=True)

    def test_tick_key_changes_on_lib_template_and_policy_changes(self) -> None:
        k0 = self.df.tick_key("b")
        (self.root / "lib" / "sample.py").write_text("V = 2\n", encoding="utf-8")
        k1 = self.df.tick_key("b")
        self.assertNotEqual(k0, k1)

        (self.root / "templates" / "repo" / ".devflow" / "deploy.sh").write_text("#!/bin/sh\necho v2\n", encoding="utf-8")
        k2 = self.df.tick_key("b")
        self.assertNotEqual(k1, k2)

        self.df.REQUIRE_ALLOWED_SCOPE = False
        k3 = self.df.tick_key("b")
        self.assertNotEqual(k2, k3)

    def test_preflight_reads_worktree_scenarios_json_and_fails_closed_on_malformed_json(self) -> None:
        db = self.df.BOARDS / "b" / "kanban.db"
        epic_body = (
            "## 目標\ng\n## 驗收\n"
            "- AC-1 [cli] ok\n"
            "  - S1 existing\n"
            "    - Given g1\n"
            "    - When w1\n"
            "    - Then t1\n"
        )
        dev_body = f"## 目標\ng\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1"
        _seed_board_db(db, tasks=[
            ("t_ep1", "[EPIC] e", epic_body, "todo", "orchestrator", "dir", str(self.repo_dir), None, "default", None, 0, None, None),
            ("t_dev1", "[DEV] d1", dev_body, "ready", "developer", "worktree", str(self.repo_dir), REPO, "orchestrator", None, 1, None, None),
        ])
        # Add NEW scenario S2 ONLY in local worktree_dir/.devflow/scenarios.json (not in main repo_dir, and AC-1 already has S1!)
        (self.worktree_dir / ".devflow" / "scenarios.json").write_text(json.dumps({
            "t_ep1/AC-1.S2": {
                "description": "new local scenario", "given": "g2", "when": "w2", "then": "t2",
            }
        }), encoding="utf-8")
        (self.worktree_dir / "app" / "a.py").write_text("x = 2\n", encoding="utf-8")
        _git(self.worktree_dir, "add", ".")
        _git(self.worktree_dir, "commit", "-q", "-m", "feat: add S2")

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_preflight(SimpleNamespace(
                slug="b", task="t_dev1", dir=str(self.worktree_dir), base="HEAD~1",
                pr_title="feat: add S2", pr_body="Covers: t_ep1/AC-1.S2", pr_body_file=None,
                no_test=True, allow_dirty=False,
            ))
        rep = json.loads(buf.getvalue())
        self.assertTrue(rep["ok"], rep)

        # Malformed local scenarios.json must fail closed
        (self.worktree_dir / ".devflow" / "scenarios.json").write_text("{broken json", encoding="utf-8")
        buf_bad = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(buf_bad):
            self.df.cmd_preflight(SimpleNamespace(
                slug="b", task="t_dev1", dir=str(self.worktree_dir), base="HEAD~1",
                pr_title="feat: add S2", pr_body="Covers: t_ep1/AC-1.S2", pr_body_file=None,
                no_test=True, allow_dirty=True,
            ))
        self.assertEqual(cm.exception.code, 1)
        rep_bad = json.loads(buf_bad.getvalue())
        self.assertEqual(rep_bad["checks"].get("scenarios"), "fail")


if __name__ == "__main__":
    unittest.main()
