"""Comprehensive unit tests for devflow P0–P10 hardening improvements:

- P0-1: PR goal-diff alignment (`out_of_scope_diff` + deterministic Allowed-Scope / Out-of-Scope / max_diff_files)
- P0-2: Developer preflight CLI (`devflow preflight`)
- P0-3: Review round loop breaker + EPIC card cap + bounded worker budgets
- P0-4: Declarative per-repo `DEVFLOW_MERGE_METHOD` (squash / merge / rebase / fail-closed)
- P1-5: Zero hardcoded project anecdotes, card IDs, or public host IP in live skills / README / roles.json
- P1-6: Clean, focused `roles.json` skill assignments & review mode separation
- P1-7: Tiered reviewer test command (`DEVFLOW_REVIEW_TEST_CMD`)
- P1-8: Upfront `devflow check card` & deterministic body leak detection (SHA, run ID, internal flags)
- P2-9: Modular `devflow/lib/` package + encapsulated `kanban_adapter`
- P2-10: Dedicated `judgment` GitHub credential role & protected harness hash lock (`needs_human_approval`)
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, REPO, Fake, load_devflow


class PolicyAndNoHardcode(unittest.TestCase):
    def test_roles_json_has_no_hardcoded_ip_and_bounded_worker_budgets(self) -> None:
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        # No hardcoded public IPv4 in host.ssh
        ssh = str((roles.get("host") or {}).get("ssh") or "")
        self.assertIsNone(re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", ssh), f"hardcoded IP in roles.json: {ssh!r}")
        # Worker budgets must be bounded (not null / 400 / 3600)
        wc = roles["worker_config"]
        self.assertLessEqual(int(wc["agent.max_turns"]), 200)
        self.assertNotEqual(wc["agent.run_budget_seconds"], "null")
        self.assertGreater(int(wc["agent.run_budget_seconds"]), 0)
        # Policy defaults exist and are positive
        pol = roles["policy"]
        for k in ("port_base", "card_chars", "adj_per_card", "ship_patch_limit",
                  "max_review_rounds", "max_epic_cards", "max_diff_files"):
            self.assertGreater(int(pol[k]), 0, k)
        # Developer does not load language-specific or devops-only skills
        dev = roles["roles"]["developer"]
        self.assertNotIn("typescript-best-practices", dev["pstack"])
        self.assertNotIn("devflow-devops", dev["team"])
        # Every referenced team skill exists under devflow/skills/
        for role_name, spec in roles["roles"].items():
            for sk in spec["team"]:
                self.assertTrue((KIT / "skills" / sk / "SKILL.md").is_file(), f"{role_name} missing skill {sk}")

    def test_live_skills_and_readme_contain_no_project_anecdotes_or_historical_card_ids(self) -> None:
        targets = [KIT / "README.md", *sorted((KIT / "skills").glob("*/SKILL.md"))]
        card_id_re = re.compile(r"\bt_[0-9a-f]{8}\b")
        for p in targets:
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("teams-transcription", text, f"hardcoded project name in {p}")
            self.assertIsNone(card_id_re.search(text), f"hardcoded historical card id in {p}")

    def test_runtime_config_and_env_override_policy_and_host_ssh(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="devflow-pol-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", root / "roles.json")
        (root / "config.json").write_text(json.dumps({
            "host": {"ssh": "ops@example.internal"},
            "policy": {"max_review_rounds": 3, "port_base": 19000},
        }), encoding="utf-8")
        (root / "state").mkdir()
        (root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(root / "repo"), "port": 19001, "default_branch": "main"}
        }))
        df = load_devflow(root)
        self.assertEqual(df.MAX_REVIEW_ROUNDS, 3)
        self.assertEqual(df.PORT_BASE, 19000)
        self.assertIn("ops@example.internal", df.repo_access("b")["tunnel"])

        os.environ["DEVFLOW_HOST_SSH"] = "envuser@envhost"
        self.addCleanup(os.environ.pop, "DEVFLOW_HOST_SSH", None)
        self.assertIn("envuser@envhost", df.repo_access("b")["tunnel"])


class ScopeAndPrGoalAlignment(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-scope-"))
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

    def _seed_board_db(self, *, card_body: str, epic_body: str, cr_events: int = 0) -> None:
        db = self.root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
                    completion_contract TEXT, goal_mode INTEGER DEFAULT 1);
                CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                    kind TEXT, payload TEXT, created_at INTEGER);
            """)
            c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, 1)",
                      ("t_ep01", "[EPIC] 時區與摘要", epic_body, "todo", None))
            c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, 1)",
                      ("t_dev1", "[DEV] 改時區", card_body, "review", f"https://github.com/{REPO}/pull/7"))
            for i in range(cr_events):
                c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'changes_requested', '{}', ?)",
                          ("t_dev1", 1000 + i))

    def test_pr_judgment_catches_out_of_scope_file_and_asks_scope_diff_predicate(self) -> None:
        epic_body = "## 目標\n改成台北時區\n\n## 驗收\n- AC-1 [ui] 顯示台北時間\n\n## 使用者原話\n只改時區，不要動認證模組\n"
        card_body = (
            "## 目標\n修改時區顯示\n"
            f"## 本卡完成\n{self.df.DONE_TEXT}\n"
            "## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/tz/**, tests/test_tz.py\nOut-of-Scope: app/auth/**\nEPIC: t_ep01\n"
        )
        self._seed_board_db(card_body=card_body, epic_body=epic_body)
        self.f.pr["body"] = "Updates timezone.\n\nKanban: t_dev1"
        self.f.files = [
            {"filename": "app/tz/format.py", "status": "modified", "patch": "+Asia/Taipei"},
            {"filename": "app/auth/login.py", "status": "modified", "patch": "+extra_gate()"},
        ]
        asked: list[str] = []
        self.df.decide = lambda content, qs: asked.extend(q["name"] for q in qs) or {
            q["name"]: {"probability": 0.9 if q["name"] == "out_of_scope_diff" else 0.05} for q in qs
        }
        rec = self.df.judgment("b", REPO, 7)
        self.assertIn("out_of_scope_diff", asked)
        self.assertTrue(any("Out-of-Scope" in f for f in rec["scope_findings"]))
        self.assertTrue(any("Allowed-Scope" in f for f in rec["scope_findings"]))
        self.assertTrue(any("Decisions out_of_scope_diff" in f for f in rec["findings"]))

    def test_ship_merge_reads_declarative_merge_method_and_fails_closed_on_invalid(self) -> None:
        (self.repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_MERGE_METHOD=merge\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        out = json.loads(buf.getvalue())
        self.assertEqual(out["merge_method"], "merge")
        self.assertIn("--merge", self.f.merges[0])
        self.assertNotIn("--squash", self.f.merges[0])

        # Invalid merge method must fail closed without calling gh pr merge
        self.f.pr.update(state="OPEN", mergeCommit=None)
        self.f.merges.clear()
        (self.repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_MERGE_METHOD=fast-forward\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self.df.cmd_ship_merge(SimpleNamespace(board="b", pr=7))
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(self.f.merges, [])

    def test_review_round_breaker_trips_at_limit_and_resets_when_approved(self) -> None:
        r = self.f.review
        self.f.reviews = [
            r("CHANGES_REQUESTED", at="2026-11-20T01:00:00Z"),
            r("CHANGES_REQUESTED", at="2026-11-20T02:00:00Z"),
        ]
        st = self.df.review_loop_status("b", REPO, 7)
        self.assertTrue(st["tripped"])
        self.assertEqual(st["rounds"], 2)
        self.assertIn("上限 2 輪", st["reason"])

        # After approval, breaker does not trip
        self.f.reviews.append(r("APPROVED", at="2026-11-20T03:00:00Z"))
        st2 = self.df.review_loop_status("b", REPO, 7)
        self.assertFalse(st2["tripped"])

    def test_plan_rules_enforces_epic_card_cap_and_kanban_review_breaker(self) -> None:
        epic_body = "## 目標\n測試上限\n## 驗收\n- AC-1 [manual] 人工驗證\n"
        card_body = f"## 目標\n子卡\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nEPIC: t_ep01"
        self._seed_board_db(card_body=card_body, epic_body=epic_body, cr_events=2)
        cards = {
            "t_ep01": {"id": "t_ep01", "title": "[EPIC] e", "body": epic_body, "status": "todo",
                       "assignee": "orchestrator", "workspace_kind": "dir", "parents": ["t_dev1"], "children": [], "goal_mode": False},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] d1", "body": card_body, "status": "ready",
                       "assignee": "developer", "workspace_kind": "worktree", "parents": [], "children": ["t_ep01"], "goal_mode": True},
        }
        # Add 12 more work cards to exceed MAX_EPIC_CARDS (12)
        for i in range(2, 15):
            tid = f"t_dev{i}"
            cards[tid] = {"id": tid, "title": f"[DEV] d{i}", "body": card_body, "status": "done",
                          "assignee": "developer", "workspace_kind": "worktree", "parents": [], "children": ["t_ep01"], "goal_mode": True}
        findings = self.df.plan_rules("b", "t_ep01", cards)
        self.assertTrue(any("工作卡（上限 12）" in f for f in findings), findings)
        self.assertTrue(any("review 退件已達 2 輪" in f for f in findings), findings)

    def test_trust_boundary_isolated_judgment_role_and_protected_paths(self) -> None:
        (self.repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_PROTECTED_PATHS=security/**\n", encoding="utf-8")
        self.f.files = [{"filename": "security/policy.yml", "status": "modified", "patch": "+allow: all"}]
        rec = self.df.judgment("b", REPO, 7)
        self.assertIn("security/policy.yml", rec["gate_files"])
        self.assertTrue(rec["needs_human_approval"])

        # Dedicated judgment GH dir via env
        os.environ["DEVFLOW_JUDGMENT_GH_DIR"] = "/opt/data/profiles/judgment/gh"
        self.addCleanup(os.environ.pop, "DEVFLOW_JUDGMENT_GH_DIR", None)
        gh_calls: list[str | None] = []
        orig_run = self.df.run

        def spy_run(cmd, gh=None, **kw):
            gh_calls.append(gh)
            return orig_run(cmd, gh=gh, **kw)

        self.df.run = spy_run
        self.df.publish(rec, "ok")
        self.assertEqual(gh_calls[-1], "judgment")


class BodyHygieneAndCheckCard(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-card-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        self.df = load_devflow(self.root)

    def test_body_problem_catches_leaked_sha_run_id_and_internal_flags(self) -> None:
        sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        self.assertIn("commit SHA", self.df.body_problem("DEV", "[DEV] d", f"## 目標\n修 {sha}") or "")
        self.assertIn("CI run URL/ID", self.df.body_problem("SHIP", "[SHIP] s", "看 /actions/runs/9876543") or "")
        self.assertIn("內部容器/核心旗標", self.df.body_problem("OPS", "[OPS] o", "不要用 --no-sandbox 或 seccomp") or "")
        clean = f"## 目標\n讓使用者在首頁看到台北時間\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1"
        self.assertIsNone(self.df.body_problem("DEV", "[DEV] 時區顯示", clean))

    def test_check_card_cli_passes_clean_and_rejects_bad_card(self) -> None:
        clean_body = f"## 目標\n顯示台北時間\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**"
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_check_card(SimpleNamespace(
                title="[DEV] 顯示台北時間", body=clean_body, body_file=None, assignee="developer", workspace="worktree"
            ))
        res = json.loads(buf.getvalue())
        self.assertTrue(res["ok"])
        self.assertEqual(res["contract"]["allowed_scope"], ["app/**"])

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
            self.df.cmd_check_card(SimpleNamespace(
                title="[DEV] 壞卡", body="## 目標\n使用 --no-sandbox", body_file=None, assignee="devops", workspace="dir"
            ))
        self.assertEqual(cm.exception.code, 1)
        bad = json.loads(buf.getvalue())
        self.assertFalse(bad["ok"])
        self.assertGreaterEqual(len(bad["findings"]), 3)


class PreflightCli(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-pf-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / ".devflow").mkdir()
        (self.repo / ".devflow" / "config.env").write_text(
            "DEVFLOW_MERGE_METHOD=merge\nDEVFLOW_REVIEW_TEST_CMD=python3 -c \"print('fast-ok')\"\n"
            "DEVFLOW_PROTECTED_PATHS=ops/**\n",
            encoding="utf-8",
        )
        (self.repo / "app").mkdir()
        (self.repo / "app" / "tz.py").write_text("TZ = 'UTC'\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)
        self.base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        (self.root / "state").mkdir()
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.repo), "port": 18102, "default_branch": "main"}
        }))
        self.df = load_devflow(self.root)

    def test_preflight_passes_in_scope_and_runs_review_test_cmd(self) -> None:
        (self.repo / "app" / "tz.py").write_text("TZ = 'Asia/Taipei'\n", encoding="utf-8")
        subprocess.run(["git", "add", "app/tz.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat: taipei tz"], cwd=self.repo, check=True)
        rep = self.df.preflight_lib.run_preflight(
            self.repo,
            policy=self.df.POLICY,
            base_ref=self.base_sha,
            card_body="Allowed-Scope: app/tz.py\nOut-of-Scope: ops/**",
            epic_id="t_ep01",
            valid_scenarios={"t_ep01/AC-1.S1"},
            pr_title="feat: taipei tz",
            pr_body="Covers: t_ep01/AC-1.S1\n",
            run_tests=True,
        )
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["checks"]["review_test"], "ok")
        self.assertFalse(rep["needs_human_approval"])

    def test_preflight_catches_out_of_scope_protected_hash_missing_lesson_and_bad_covers(self) -> None:
        (self.repo / "ops").mkdir()
        (self.repo / "ops" / "deploy.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        subprocess.run(["git", "add", "ops/deploy.sh"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "fix: touch ops"], cwd=self.repo, check=True)
        rep = self.df.preflight_lib.run_preflight(
            self.repo,
            policy=self.df.POLICY,
            base_ref=self.base_sha,
            card_body="Allowed-Scope: app/**\nOut-of-Scope: ops/**",
            epic_id="t_ep01",
            valid_scenarios={"t_ep01/AC-1.S1"},
            pr_title="fix: touch ops",
            pr_body="Covers: t_other/AC-9.S1\n",
            run_tests=False,
        )
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["needs_human_approval"])
        self.assertEqual(rep["protected_changes"][0]["filename"], "ops/deploy.sh")
        self.assertTrue(any("Out-of-Scope" in f for f in rep["findings"]))
        self.assertTrue(any("Lesson:" in f for f in rep["findings"]))
        self.assertTrue(any("與本卡 EPIC" in f for f in rep["findings"]))

    def test_preflight_real_git_rename_of_protected_file_cannot_bypass_scope_or_protection(self) -> None:
        (self.repo / ".devflow" / "check.py").write_text("print('gate')\n", encoding="utf-8")
        subprocess.run(["git", "add", ".devflow/check.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add check.py"], cwd=self.repo, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        subprocess.run(["git", "mv", ".devflow/check.py", "app/check.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "refactor: move check.py"], cwd=self.repo, check=True)

        rep = self.df.preflight_lib.run_preflight(
            self.repo,
            policy=self.df.POLICY,
            base_ref=base,
            card_body="Allowed-Scope: app/**\nOut-of-Scope: .devflow/**",
            pr_title="refactor: move check.py",
            pr_body="",
            run_tests=False,
        )
        self.assertFalse(rep["ok"], rep)
        self.assertTrue(rep["needs_human_approval"])
        self.assertEqual(len(rep["protected_changes"]), 1)
        pc = rep["protected_changes"][0]
        self.assertEqual(pc["previous_filename"], ".devflow/check.py")
        self.assertEqual(pc["filename"], "app/check.py")
        self.assertTrue(pc["base_sha256"])
        self.assertTrue(pc["head_sha256"])
        self.assertTrue(pc["hash_changed"])
        self.assertTrue(any("Out-of-Scope" in f and ".devflow/check.py" in f for f in rep["findings"]), rep["findings"])
        self.assertTrue(any("Allowed-Scope" in f and ".devflow/check.py" in f for f in rep["findings"]), rep["findings"])


class ReviewFindingsRegression(unittest.TestCase):
    """Regression tests reproducing all 3 blocking findings from PR #33 review."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-revfix-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state").mkdir()
        self.repo_dir = self.root / "repo"
        (self.repo_dir / ".devflow").mkdir(parents=True)
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.repo_dir), "port": 18105, "default_branch": "main"}
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
        self.db = self.root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
                    completion_contract TEXT, branch_name TEXT, goal_mode INTEGER DEFAULT 1);
                CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                    status TEXT, metadata TEXT);
                CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                    kind TEXT, payload TEXT, created_at INTEGER);
            """)

    def test_judge_catches_github_renamed_protected_file_and_delivery_file(self) -> None:
        goal_ctx = self.df.scope_lib.build_goal_context(
            epic_id="t_ep01", epic_title="[EPIC] e", epic_body="## 目標\n只改 app\n",
            card_id="t_dev1", card_title="[DEV] d", card_body="Allowed-Scope: app/**\nOut-of-Scope: .devflow/**",
        )
        renamed = [{
            "filename": "app/check.py",
            "previous_filename": ".devflow/check.py",
            "status": "renamed",
            "patch": "",
        }]
        res = self.df.judge(self.f.pr, renamed, goal_context=goal_ctx)
        self.assertIn(".devflow/check.py", res["gate_files"])
        self.assertTrue(res["needs_human_approval"])
        self.assertEqual(res["protected_changes"][0]["previous_filename"], ".devflow/check.py")
        self.assertTrue(any("Out-of-Scope" in f for f in res["scope_findings"]))
        self.assertTrue(any("Allowed-Scope" in f for f in res["scope_findings"]))

        # Renaming a delivery file out of .devflow/ must still trigger `replaces_standard`
        asked: list[str] = []
        self.df.decide = lambda content, qs: asked.extend(q["name"] for q in qs) or {
            q["name"]: {"probability": 0.05} for q in qs
        }
        renamed_deploy = [{
            "filename": "scripts/deploy.sh",
            "previous_filename": ".devflow/deploy.sh",
            "status": "renamed",
            "patch": "",
        }]
        self.df.judge(self.f.pr, renamed_deploy)
        self.assertIn("replaces_standard", asked)

    def test_pr_goal_context_resolves_normal_contract_yes_and_contract_no_handoffs(self) -> None:
        epic_body = "## 目標\n功能 A\n## 驗收\n- AC-1 [cli] 輸出正常\n## 使用者原話\n只要功能 A\n"
        dev_body = "## 目標\n實作 A\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nOut-of-Scope: secret/**\nEPIC: t_ep01\n"
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_ep01", "[EPIC] A", epic_body, "todo", None, None))
            # Normal contract=yes: completion_contract is OWNER/REPO, NOT PR URL; PR URL is in task_runs.metadata
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_dev_yes", "[DEV] yes", dev_body, "review", REPO, "wt/t_dev_yes"))
            c.execute("INSERT INTO task_runs (task_id, status, metadata) VALUES (?,?,?)",
                      ("t_dev_yes", "done", json.dumps({"published_pr": f"https://github.com/{REPO}/pull/7"})))
            # Normal contract=no: completion_contract is NULL; PR is recorded via pr_url / pr_number in task_runs
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_dev_no", "[DEV] no", dev_body, "review", None, "wt/t_dev_no"))
            c.execute("INSERT INTO task_runs (task_id, status, metadata) VALUES (?,?,?)",
                      ("t_dev_no", "done", json.dumps({"pr_url": f"https://github.com/{REPO}/pull/8", "pr_number": 8})))
            # Archived decoy card must never be chosen via spoofed Kanban: line
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_arch", "[DEV] old", "## 目標\nold", "archived", None, None))

        # PR #7 has Covers: only (no Kanban: line) -> resolves t_dev_yes via task_runs.metadata.published_pr
        pr7 = {**self.f.pr, "number": 7, "body": "Implements A.\n\nCovers: t_ep01/AC-1.S1\n"}
        ctx7 = self.df.pr_goal_context("b", REPO, 7, pr7)
        self.assertIsNotNone(ctx7)
        self.assertEqual(ctx7["card_id"], "t_dev_yes")
        self.assertEqual(ctx7["epic_id"], "t_ep01")
        self.assertEqual(ctx7["out_of_scope"], ["secret/**"])

        # PR #8 (contract=no) has Covers: only -> resolves t_dev_no via task_runs.metadata.pr_url
        pr8 = {**self.f.pr, "number": 8, "url": f"https://github.com/{REPO}/pull/8", "body": "Covers: t_ep01/AC-1.S1\n"}
        ctx8 = self.df.pr_goal_context("b", REPO, 8, pr8)
        self.assertIsNotNone(ctx8)
        self.assertEqual(ctx8["card_id"], "t_dev_no")
        self.assertEqual(ctx8["allowed_scope"], ["app/**"])

        # Spoofed Kanban: t_dev_yes on unrelated PR #99 must be rejected because t_dev_yes is bound to PR #7
        pr99 = {**self.f.pr, "number": 99, "url": f"https://github.com/{REPO}/pull/99", "body": "Kanban: t_dev_yes\n"}
        self.assertIsNone(self.df.pr_goal_context("b", REPO, 99, pr99))
        # Archived Kanban: t_arch must also be rejected
        pr98 = {**self.f.pr, "number": 98, "url": f"https://github.com/{REPO}/pull/98", "body": "Kanban: t_arch\n"}
        self.assertIsNone(self.df.pr_goal_context("b", REPO, 98, pr98))

    def test_judgment_cache_invalidates_on_card_scope_or_repo_policy_edit_with_unchanged_pr(self) -> None:
        epic_body = "## 目標\n改 app\n## 驗收\n- AC-1 [cli] ok\n"
        dev_body = "## 目標\n改 x\n## 驗收\n覆蓋：AC-1\nEPIC: t_ep01\n"
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_ep01", "[EPIC] e", epic_body, "todo", None, None))
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_dev1", "[DEV] d", dev_body, "review", REPO, "wt/t_dev1"))
            c.execute("INSERT INTO task_runs (task_id, status, metadata) VALUES (?,?,?)",
                      ("t_dev1", "done", json.dumps({"published_pr": f"https://github.com/{REPO}/pull/7"})))
        self.f.files = [{"filename": "app/x.py", "status": "modified", "patch": "+x = 1"}]

        # Initial judgment: clean
        rec1 = self.df.judgment("b", REPO, 7)
        self.assertEqual(rec1["scope_findings"], [])
        self.assertEqual(self.f.decide_calls, 1)
        # Simulate an [ADV] verdict attached to rec1's fingerprint
        self.df.update_judgment(f"{REPO}#7", fp=rec1["fingerprint"], advisor="t_adv1")
        self.f.cards["t_adv1"] = {"status": "done", "verdict": "ok", "body": f"fingerprint: {rec1['fingerprint']}"}

        # Edit governing card to add Out-of-Scope: app/** WITHOUT touching the PR at all
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE tasks SET body = ? WHERE id = 't_dev1'", (dev_body + "Out-of-Scope: app/**\n",))

        rec2 = self.df.judgment("b", REPO, 7)
        self.assertNotEqual(rec2["fingerprint"], rec1["fingerprint"])
        self.assertIsNone(rec2["advisor"])  # old [ADV] invalidated
        self.assertEqual(self.f.decide_calls, 2)
        self.assertTrue(any("Out-of-Scope" in f for f in rec2["scope_findings"]), rec2)

        # Now change repo protected paths in .devflow/config.env WITHOUT touching PR or card
        (self.repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_PROTECTED_PATHS=app/**\n", encoding="utf-8")
        rec3 = self.df.judgment("b", REPO, 7)
        self.assertNotEqual(rec3["fingerprint"], rec2["fingerprint"])
        self.assertEqual(self.f.decide_calls, 3)
        self.assertIn("app/x.py", rec3["gate_files"])
        self.assertTrue(rec3["needs_human_approval"])

        # Now tighten policy max_diff_files WITHOUT touching PR, card, or protected paths
        (self.repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_PROTECTED_PATHS=app/**\nDEVFLOW_MAX_DIFF_FILES=1\n", encoding="utf-8")
        self.f.files.append({"filename": "app/y.py", "status": "added", "patch": "+y = 2"})
        rec4 = self.df.judgment("b", REPO, 7)
        self.assertNotEqual(rec4["fingerprint"], rec3["fingerprint"])
        self.assertTrue(any("超過上限 1 檔" in f for f in rec4["scope_findings"]), rec4)


if __name__ == "__main__":
    unittest.main()
