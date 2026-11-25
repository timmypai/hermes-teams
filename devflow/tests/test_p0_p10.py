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

    def test_preflight_handles_binary_asset_and_protected_binary_exact_bytes_without_decode_error(self) -> None:
        (self.repo / "assets").mkdir()
        png1 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x01"
        png2 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x02"
        (self.repo / "assets" / "logo.png").write_bytes(png1)
        (self.repo / "ops").mkdir(exist_ok=True)
        bin1 = b"\x89BIN\r\n\x00v1"
        bin2 = b"\x89BIN\n\x00v1"  # differs ONLY by CRLF vs LF
        (self.repo / "ops" / "cert.der").write_bytes(bin1)
        subprocess.run(["git", "add", "assets/logo.png", "ops/cert.der"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add binary assets"], cwd=self.repo, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()

        # 1. Unprotected binary PNG modification must not crash with UnicodeDecodeError
        (self.repo / "assets" / "logo.png").write_bytes(png2)
        subprocess.run(["git", "add", "assets/logo.png"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "chore: update logo"], cwd=self.repo, check=True)
        rep1 = self.df.preflight_lib.run_preflight(
            self.repo, policy=self.df.POLICY, base_ref=base, run_tests=False,
        )
        self.assertTrue(rep1["ok"], rep1)
        self.assertEqual(rep1["protected_changes"], [])

        # 2. Protected binary modification hashes exact raw bytes (CRLF vs LF produces distinct hashes)
        (self.repo / "ops" / "cert.der").write_bytes(bin2)
        subprocess.run(["git", "add", "ops/cert.der"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "chore: update protected binary"], cwd=self.repo, check=True)
        rep2 = self.df.preflight_lib.run_preflight(
            self.repo, policy=self.df.POLICY, base_ref=base, run_tests=False,
        )
        self.assertTrue(rep2["ok"], rep2)
        self.assertTrue(rep2["needs_human_approval"])
        pc = rep2["protected_changes"][0]
        self.assertEqual(pc["filename"], "ops/cert.der")
        self.assertEqual(pc["base_sha256"], self.df.trust_lib.sha256_text(bin1))
        self.assertEqual(pc["head_sha256"], self.df.trust_lib.sha256_text(bin2))
        self.assertNotEqual(pc["base_sha256"], pc["head_sha256"])
        self.assertTrue(pc["hash_changed"])

    def test_preflight_parses_non_ascii_and_tab_newline_paths_without_git_c_quoting(self) -> None:
        (self.repo / "ops").mkdir(exist_ok=True)
        unicode_rel = "ops/憑證.der"
        ctrl_rel = "ops/tab\tline\n.der"
        raw_u = b"\xff\x00unicode-cert"
        raw_c = b"\xff\x01ctrl-cert"
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        (self.repo / unicode_rel).write_bytes(raw_u)
        (self.repo / ctrl_rel).write_bytes(raw_c)
        subprocess.run(["git", "add", "--", unicode_rel, ctrl_rel], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add non-ascii and control-char paths"], cwd=self.repo, check=True)

        rep = self.df.preflight_lib.run_preflight(
            self.repo,
            policy=self.df.POLICY,
            base_ref=base,
            card_body="Out-of-Scope: ops/**",
            run_tests=False,
        )
        self.assertFalse(rep["ok"], rep)
        self.assertEqual(rep["checks"]["scope"], "fail")
        self.assertTrue(rep["needs_human_approval"])
        self.assertIn(unicode_rel, rep["files"])
        self.assertIn(ctrl_rel, rep["files"])
        by_name = {pc["filename"]: pc for pc in rep["protected_changes"]}
        self.assertIn(unicode_rel, by_name)
        self.assertIn(ctrl_rel, by_name)
        self.assertEqual(by_name[unicode_rel]["head_sha256"], self.df.trust_lib.sha256_text(raw_u))
        self.assertEqual(by_name[ctrl_rel]["head_sha256"], self.df.trust_lib.sha256_text(raw_c))
        self.assertTrue(any("Out-of-Scope" in f and unicode_rel in f for f in rep["findings"]), rep["findings"])
        self.assertTrue(any("Out-of-Scope" in f and ctrl_rel in f for f in rep["findings"]), rep["findings"])


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

    def test_completed_dev_owner_is_not_shadowed_by_active_ship_or_adv_and_ambiguous_dev_fails_closed(self) -> None:
        epic_body = "## 目標\n安全改動\n## 驗收\n- AC-1 [cli] ok\n"
        dev_body = "## 目標\n只改 app\n## 驗收\n覆蓋：AC-1\nOut-of-Scope: secret/**\nEPIC: t_ep01\n"
        pr_url = f"https://github.com/{REPO}/pull/7"
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_ep01", "[EPIC] s", epic_body, "todo", None, None))
            # Completed DEV owner with Out-of-Scope restriction
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_dev1", "[DEV] d1", dev_body, "done", pr_url, "wt/t_dev1"))
            c.execute("INSERT INTO task_runs (task_id, status, metadata) VALUES (?,?,?)",
                      ("t_dev1", "done", json.dumps({"published_pr": pr_url})))
            # Active downstream SHIP and ADV cards referencing the exact same PR URL
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_ship1", "[SHIP] s1", "EPIC: t_ep01\n", "running", None, None))
            c.execute("INSERT INTO task_runs (task_id, status, metadata) VALUES (?,?,?)",
                      ("t_ship1", "running", json.dumps({"pr_url": pr_url})))
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_adv1", "[ADV] PR 判斷", f"PR: {pr_url}\n", "ready", None, None))
            c.execute("INSERT INTO task_runs (task_id, status, metadata) VALUES (?,?,?)",
                      ("t_adv1", "running", json.dumps({"pr_url": pr_url})))

        ctx = self.df.pr_goal_context("b", REPO, 7, self.f.pr)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["card_id"], "t_dev1")
        self.assertEqual(ctx["out_of_scope"], ["secret/**"])
        self.f.files = [{"filename": "secret/key.py", "status": "modified", "patch": "+k=1"}]
        rec = self.df.judgment("b", REPO, 7)
        self.assertTrue(any("Out-of-Scope" in f and "secret/key.py" in f for f in rec["scope_findings"]), rec)

        # Adding a second non-archived DEV card bound to the same PR must fail closed on ambiguous ownership
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,1)", ("t_dev2", "[DEV] d2", "EPIC: t_ep01\n", "review", REPO, "wt/t_dev2"))
            c.execute("INSERT INTO task_runs (task_id, status, metadata) VALUES (?,?,?)",
                      ("t_dev2", "done", json.dumps({"published_pr": pr_url})))
        ctx_amb = self.df.pr_goal_context("b", REPO, 7, self.f.pr)
        self.assertEqual(ctx_amb.get("ambiguous_owners"), ["t_dev1", "t_dev2"])
        rec_amb = self.df.judgment("b", REPO, 7)
        self.assertTrue(any("同時對應多張未封存開發卡" in f for f in rec_amb["scope_findings"]), rec_amb)


class StandardDeliveryAndStability(unittest.TestCase):
    """Tests for standard delivery enforcement, pending isolation, idempotent unsubscribe, and meta-loop defaults."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-std-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        shutil.copytree(KIT / "templates", self.root / "templates")
        (self.root / "state").mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "b": {"slug": "b", "repo": REPO, "path": str(self.repo), "port": 18101, "default_branch": "main"}
        }))
        os.environ["DEVFLOW_HERMES_DATA"] = str(self.root / "data")
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA", None)
        self.df = load_devflow(self.root)

    def test_sync_standard_enforces_canonical_delivery_and_preflight_blocks_custom_drift(self) -> None:
        (self.repo / ".devflow").mkdir()
        (self.repo / ".devflow" / "config.env").write_text(
            "DEVFLOW_APP=b\nDEVFLOW_PORT=18101\nDEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n",
            encoding="utf-8",
        )
        res = self.df.standard_lib.sync_standard_files(
            self.repo, self.df.TEMPLATE, repo_cfg=self.df.policy_lib.load_repo_config(self.repo)
        )
        self.assertIn(".devflow/deploy.sh", res["updated"])
        self.assertIn(".github/workflows/deploy.yml", res["updated"])
        # Manual-only mode must strip push trigger while keeping canonical workflow
        wf_text = (self.repo / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertNotIn("branches:", wf_text)
        self.assertIn("workflow_dispatch:", wf_text)
        # Clean repo passes standard delivery check
        chk = self.df.standard_lib.check_standard_delivery_dir(
            self.repo, self.df.TEMPLATE, repo_cfg=self.df.policy_lib.load_repo_config(self.repo)
        )
        self.assertTrue(chk["ok"], chk)

        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "standard contract"], cwd=self.repo, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()

        # Replacing .devflow/deploy.sh with a custom opaque wrapper must fail preflight deterministically
        (self.repo / ".devflow" / "deploy.sh").write_text("#!/usr/bin/env bash\nexec python3 custom.py\n", encoding="utf-8")
        subprocess.run(["git", "add", ".devflow/deploy.sh"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat: custom deployer"], cwd=self.repo, check=True)
        rep = self.df.preflight_lib.run_preflight(
            self.repo, policy=self.df.POLICY, base_ref=base, run_tests=False, template_dir=self.df.TEMPLATE
        )
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["checks"]["standard_delivery"], "fail")
        self.assertTrue(any("偏離 devflow 公版交付檔" in f and ".devflow/deploy.sh" in f for f in rep["findings"]), rep)

    def test_pending_skips_unregistered_or_corrupt_board_dbs_without_crashing(self) -> None:
        boards = self.root / "data" / "kanban" / "boards"
        # Orphan unregistered board with minimal invalid schema (like stray `boards/b` in production)
        orphan = boards / "orphan" / "kanban.db"
        orphan.parent.mkdir(parents=True)
        with closing(sqlite3.connect(orphan)) as c, c:
            c.execute("CREATE TABLE tasks (id TEXT, status TEXT, goal_mode INTEGER)")
        # Registered board `b` with corrupted/missing columns must ALSO be skipped gracefully
        bad_reg = boards / "b" / "kanban.db"
        bad_reg.parent.mkdir(parents=True)
        with closing(sqlite3.connect(bad_reg)) as c, c:
            c.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
        self.assertEqual(self.df.pending(), [])

    def test_default_policy_disables_async_meta_loops_and_allows_ship_only_epic(self) -> None:
        self.assertFalse(self.df.AUTO_PLAN_ADVISOR)
        self.assertFalse(self.df.AUTO_FLOW_ADJ)
        # Ship-only EPIC without synthetic Given/When/Then scenarios passes plan_rules when ACs are defined & covered
        cards = {
            "t_ep01": {"id": "t_ep01", "title": "[EPIC] 出貨部署", "status": "todo", "assignee": "orchestrator",
                       "workspace_kind": "dir", "parents": ["t_sh01"], "children": [], "goal_mode": False,
                       "body": "## 目標\n合併並部署 PR #16\n## 驗收\n- AC-1 [cli] 合併進 main\n- AC-2 [api] 部署健康\n"},
            "t_sh01": {"id": "t_sh01", "title": "[SHIP] 出貨 PR #16", "status": "ready", "assignee": "devops",
                       "workspace_kind": "dir", "parents": [], "children": ["t_ep01"], "goal_mode": False,
                       "body": "## 目標\n合併並部署\n## 驗收\n覆蓋：AC-1, AC-2\nEPIC: t_ep01\n"},
        }
        findings = self.df.plan_rules("b", "t_ep01", cards)
        self.assertEqual(findings, [], findings)
        # Ship-only EPIC still enforces meaningful acceptance coverage (uncovered AC fails plan_rules)
        uncovered = {
            **cards,
            "t_sh01": {**cards["t_sh01"], "body": "## 目標\n合併並部署\n## 驗收\n覆蓋：AC-1\nEPIC: t_ep01\n"},
        }
        self.assertTrue(any("AC-2" in f for f in self.df.plan_rules("b", "t_ep01", uncovered)))

    def test_judgment_standard_delivery_fails_closed_on_deletion_and_transient_fetch_error_without_caching(self) -> None:
        f = Fake(self.df)
        for name in ("pr_meta", "decide", "run", "kanban", "resume", "pr_reviews"):
            setattr(self.df, name, getattr(f, name))
        self.df.pr_files = lambda repo, n: f.files
        self.df.gh_login = lambda role: {"reviewer": "rev", "builder": "bob"}[role]
        self.df.deploy_readiness = lambda slug: []

        # 1. Deleting the only touched canonical file must fail closed (never silently pass)
        f.files = [{"filename": ".devflow/deploy.sh", "status": "removed", "patch": ""}]
        self.df.branch_file = lambda repo, ref, path: (_ for _ in ()).throw(AssertionError("should not fetch removed file"))
        pr_del = {**f.pr, "number": 11, "headRefOid": "d" * 40, "url": f"https://github.com/{REPO}/pull/11", "fingerprint": "fp-del"}
        rec_del = self.df.judgment("b", REPO, 11, pr_del)
        self.assertFalse(rec_del["standard_delivery"]["ok"], rec_del)
        self.assertIn(".devflow/deploy.sh", rec_del["standard_delivery"]["missing"])
        self.assertTrue(any("缺少 devflow 公版交付檔" in x for x in rec_del["findings"]), rec_del)

        # 2. Transient fetch exception on a modified canonical file must fail closed AND not cache
        f.files = [{"filename": ".devflow/deploy.sh", "status": "modified", "patch": "+echo hi"}]
        pr_err = {**f.pr, "number": 12, "headRefOid": "e" * 40, "url": f"https://github.com/{REPO}/pull/12", "fingerprint": "fp-err"}
        self.df.branch_file = lambda repo, ref, path: (_ for _ in ()).throw(RuntimeError("API 503"))
        rec_err = self.df.judgment("b", REPO, 12, pr_err)
        self.assertFalse(rec_err["standard_delivery"]["ok"], rec_err)
        self.assertTrue(rec_err["standard_delivery"].get("fetch_failed"), rec_err)
        self.assertFalse(rec_err.get("cached", True))
        self.assertTrue(any("無法讀取 PR head" in x for x in rec_err["findings"]), rec_err)
        self.assertNotIn(f"{REPO}#12", self.df.load_judgments())

        # 3. Returning None for a modified (non-removed) canonical file also fails closed without caching
        self.df.branch_file = lambda repo, ref, path: None
        rec_none = self.df.judgment("b", REPO, 12, pr_err)
        self.assertFalse(rec_none["standard_delivery"]["ok"], rec_none)
        self.assertTrue(rec_none["standard_delivery"].get("fetch_failed"), rec_none)
        self.assertNotIn(f"{REPO}#12", self.df.load_judgments())

        # 4. Retry succeeds once GitHub returns canonical template content and caches clean result
        canonical = self.df.standard_lib.render_standard_file(self.df.TEMPLATE, ".devflow/deploy.sh")
        self.df.branch_file = lambda repo, ref, path: canonical
        rec_ok = self.df.judgment("b", REPO, 12, pr_err)
        self.assertTrue(rec_ok["standard_delivery"]["ok"], rec_ok)
        self.assertEqual(rec_ok["standard_delivery"]["findings"], [])
        self.assertFalse(any("公版交付檔" in x for x in rec_ok["findings"]), rec_ok)
        self.assertIn(f"{REPO}#12", self.df.load_judgments())

        # 5. PR-level risk escalation remains active even when AUTO_PLAN_ADVISOR is False
        self.assertFalse(self.df.AUTO_PLAN_ADVISOR)
        self.df.decide = lambda content, qs: {
            q["name"]: {"probability": 0.9 if q["name"] == "unlabeled_fix" else 0.05} for q in qs
        }
        f.files = [{"filename": "app/migrate.py", "status": "modified", "patch": "+DROP TABLE x"}]
        pr_risk = {**f.pr, "number": 13, "headRefOid": "f" * 40, "url": f"https://github.com/{REPO}/pull/13", "fingerprint": "fp-risk"}
        rec_risk = self.df.judgment("b", REPO, 13, pr_risk)
        self.assertTrue(rec_risk["findings"], rec_risk)
        self.assertGreaterEqual(rec_risk["risks"].get("unlabeled_fix", 0), 0.9)

    def test_unsubscribe_idempotent_treats_no_such_subscription_as_success(self) -> None:
        self.df.kanban = lambda *a: (_ for _ in ()).throw(SystemExit("no such subscription"))
        # Must not raise
        self.df.unsubscribe_idempotent("b", "t_1", "local", "chat1", "thr1")

    def test_deploy_sh_pre_cutover_seed_filters_by_app_service_not_sidecar_or_db(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="devflow-deploy-seed-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        (work / ".devflow").mkdir()
        (work / "deploy").mkdir()
        shutil.copy2(KIT / "templates" / "repo" / ".devflow" / "deploy.sh", work / ".devflow" / "deploy.sh")
        (work / ".devflow" / "config.env").write_text(
            "DEVFLOW_APP=probe\nDEVFLOW_SERVICE=web\nDEVFLOW_PORT=18991\nDEVFLOW_APP_PORT=8080\nDEVFLOW_HEALTH_PATH=/health\n",
            encoding="utf-8",
        )
        (work / "deploy" / "compose.yml").write_text("services:\n  web:\n    image: ${DEVFLOW_IMAGE}\n", encoding="utf-8")
        for name, body in (
            ("deploy-build.sh", "#!/usr/bin/env bash\nexit 0\n"),
            ("capture.sh", "#!/usr/bin/env bash\nexit 0\n"),
            ("verify.sh", "#!/usr/bin/env bash\nexit 1\n"),
        ):
            p = work / ".devflow" / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)
        bin_dir = work / "bin"
        bin_dir.mkdir()
        calls_file = work / "calls.jsonl"
        (bin_dir / "curl").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (bin_dir / "curl").chmod(0o755)
        (bin_dir / "docker").write_text(
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
calls = Path(os.environ["MOCK_CALLS"])
with calls.open("a") as fh:
    fh.write(json.dumps({"args": args, "sha": os.environ.get("DEVFLOW_SOURCE_SHA", "")}) + "\\n")
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if Path(os.environ["MOCK_TAGGED"]).exists() else 1)
if args[:1] == ["ps"]:
    filters = [args[i + 1] for i, a in enumerate(args) if a == "--filter" and i + 1 < len(args)]
    if "label=com.docker.compose.service=web" in filters:
        print("cid-web-111111111111")
    else:
        print("cid-db-999999999999")
        print("cid-web-111111111111")
    sys.exit(0)
if args[:1] == ["inspect"]:
    cid = args[-1]
    fmt = args[args.index("--format") + 1] if "--format" in args else ""
    if cid == "cid-web-111111111111":
        print("sha256:web-old-img" if fmt == "{{.Image}}" else "old-web-sha-1111111111111111111111111111")
    else:
        print("sha256:db-img" if fmt == "{{.Image}}" else "old-db-sha-99999999999999999999999999999")
    sys.exit(0)
if args[:1] == ["tag"]:
    Path(os.environ["MOCK_TAGGED"]).write_text(args[1])
    sys.exit(0)
sys.exit(0)
""",
            encoding="utf-8",
        )
        (bin_dir / "docker").chmod(0o755)
        home = work / "home"
        home.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_SHA": "new-sha-22222222222222222222222222222222",
            "MOCK_CALLS": str(calls_file),
            "MOCK_TAGGED": str(work / "tagged"),
        }
        proc = subprocess.run(["bash", ".devflow/deploy.sh"], cwd=work, env=env, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        records = [json.loads(line) for line in calls_file.read_text().splitlines()]
        tag_calls = [r["args"] for r in records if r["args"][:1] == ["tag"]]
        self.assertEqual(tag_calls, [["tag", "sha256:web-old-img", "devflow/probe:live"]])
        rollback_ups = [r for r in records if r["args"][:1] == ["compose"] and "--no-build" in r["args"]]
        self.assertTrue(rollback_ups, records)
        self.assertEqual(rollback_ups[0]["sha"], "old-web-sha-1111111111111111111111111111")

    def test_judgment_uses_exact_head_config_for_manual_only_switches_and_invalidates_stale_and_template_caches(self) -> None:
        f = Fake(self.df)
        for name in ("pr_meta", "decide", "run", "kanban", "resume", "pr_reviews"):
            setattr(self.df, name, getattr(f, name))
        self.df.pr_files = lambda repo, n: f.files
        self.df.gh_login = lambda role: {"reviewer": "rev", "builder": "bob"}[role]
        self.df.deploy_readiness = lambda slug: []

        # 1. Main has manual_only=false; PR turns DEVFLOW_DEPLOY_MANUAL_ONLY=true and switches deploy.yml to workflow_dispatch
        (self.repo / ".devflow").mkdir(exist_ok=True)
        (self.repo / ".devflow" / "config.env").write_text("DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\n", encoding="utf-8")
        wf_manual = self.df.standard_lib.render_standard_file(
            self.df.TEMPLATE, ".github/workflows/deploy.yml", repo_cfg={"DEVFLOW_DEPLOY_MANUAL_ONLY": "true"}
        )
        f.files = [
            {"filename": ".devflow/config.env", "status": "modified", "patch": "+DEVFLOW_DEPLOY_MANUAL_ONLY=true"},
            {"filename": ".github/workflows/deploy.yml", "status": "modified", "patch": "-push:"},
        ]
        head_files = {
            ".devflow/config.env": "DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n",
            ".github/workflows/deploy.yml": wf_manual,
        }
        self.df.branch_file = lambda repo, ref, path: head_files.get(path)
        pr_on = {**f.pr, "number": 21, "headRefOid": "1" * 40, "url": f"https://github.com/{REPO}/pull/21", "fingerprint": "fp-on"}
        rec_on = self.df.judgment("b", REPO, 21, pr_on)
        self.assertTrue(rec_on["standard_delivery"]["ok"], rec_on)

        # 2. Main has manual_only=true; PR turns DEVFLOW_DEPLOY_MANUAL_ONLY=false and restores canonical push+workflow_dispatch
        (self.repo / ".devflow" / "config.env").write_text(
            "DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n", encoding="utf-8"
        )
        wf_auto = self.df.standard_lib.render_standard_file(
            self.df.TEMPLATE, ".github/workflows/deploy.yml", repo_cfg={"DEVFLOW_DEPLOY_MANUAL_ONLY": "false"}
        )
        head_files_off = {
            ".devflow/config.env": "DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=false\n",
            ".github/workflows/deploy.yml": wf_auto,
        }
        self.df.branch_file = lambda repo, ref, path: head_files_off.get(path)
        pr_off = {**f.pr, "number": 22, "headRefOid": "2" * 40, "url": f"https://github.com/{REPO}/pull/22", "fingerprint": "fp-off"}
        rec_off = self.df.judgment("b", REPO, 22, pr_off)
        self.assertTrue(rec_off["standard_delivery"]["ok"], rec_off)

        # 3. PR cannot self-exempt via DEVFLOW_ALLOW_CUSTOM_DELIVERY=true in head config.env
        f.files = [
            {"filename": ".devflow/config.env", "status": "modified", "patch": "+DEVFLOW_ALLOW_CUSTOM_DELIVERY=true"},
            {"filename": ".devflow/deploy.sh", "status": "modified", "patch": "+exec custom.py"},
        ]
        self.df.branch_file = lambda repo, ref, path: {
            ".devflow/config.env": "DEVFLOW_APP=b\nDEVFLOW_ALLOW_CUSTOM_DELIVERY=true\n",
            ".devflow/deploy.sh": "#!/usr/bin/env bash\nexec custom.py\n",
        }.get(path)
        pr_bypass = {**f.pr, "number": 23, "headRefOid": "3" * 40, "url": f"https://github.com/{REPO}/pull/23", "fingerprint": "fp-byp"}
        rec_bypass = self.df.judgment("b", REPO, 23, pr_bypass)
        self.assertFalse(rec_bypass["standard_delivery"]["ok"], rec_bypass)
        self.assertIn(".devflow/deploy.sh", rec_bypass["standard_delivery"]["drifted"])

        # 4. Pre-upgrade cached clean judgment (keyed by legacy base_fp without standard_templates) MUST be invalidated
        (self.repo / ".devflow" / "config.env").write_text("DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\n", encoding="utf-8")
        pr_mig = {**f.pr, "number": 24, "headRefOid": "4" * 40, "url": f"https://github.com/{REPO}/pull/24", "fingerprint": "legacyfp24"}
        self.df.update_judgment(f"{REPO}#24", board="b", repo=REPO, pr=24, url=pr_mig["url"],
                                head_sha=pr_mig["headRefOid"], fingerprint="legacyfp24", findings=[])
        f.files = [{"filename": ".devflow/deploy.sh", "status": "modified", "patch": "+exec custom.py"}]
        self.df.branch_file = lambda repo, ref, path: "#!/usr/bin/env bash\nexec custom.py\n"
        rec_mig = self.df.judgment("b", REPO, 24, pr_mig)
        self.assertNotEqual(rec_mig["fingerprint"], "legacyfp24")
        self.assertFalse(rec_mig["standard_delivery"]["ok"], rec_mig)
        self.assertIn(".devflow/deploy.sh", rec_mig["standard_delivery"]["drifted"])

        # 5. Updating a canonical template invalidates cached clean judgment and flags drift on next check
        canonical_before = self.df.standard_lib.render_standard_file(self.df.TEMPLATE, ".devflow/deploy.sh")
        self.df.branch_file = lambda repo, ref, path: canonical_before
        pr_tpl = {**f.pr, "number": 25, "headRefOid": "5" * 40, "url": f"https://github.com/{REPO}/pull/25", "fingerprint": "fp-tpl25"}
        rec_before = self.df.judgment("b", REPO, 25, pr_tpl)
        self.assertTrue(rec_before["standard_delivery"]["ok"], rec_before)
        # Upgrade kit template under self.df.TEMPLATE
        tpl_deploy = self.df.TEMPLATE / ".devflow" / "deploy.sh"
        tpl_deploy.write_text(tpl_deploy.read_text(encoding="utf-8") + "# template-v2\n", encoding="utf-8")
        rec_after = self.df.judgment("b", REPO, 25, pr_tpl)
        self.assertNotEqual(rec_after["fingerprint"], rec_before["fingerprint"])
        self.assertFalse(rec_after["standard_delivery"]["ok"], rec_after)
        self.assertIn(".devflow/deploy.sh", rec_after["standard_delivery"]["drifted"])

    def test_config_only_deploy_switches_validate_dependent_files_in_judgment_and_preflight(self) -> None:
        f = Fake(self.df)
        for name in ("pr_meta", "decide", "run", "kanban", "resume", "pr_reviews"):
            setattr(self.df, name, getattr(f, name))
        self.df.pr_files = lambda repo, n: f.files
        self.df.gh_login = lambda role: {"reviewer": "rev", "builder": "bob"}[role]
        self.df.deploy_readiness = lambda slug: []

        wf_auto = self.df.standard_lib.render_standard_file(
            self.df.TEMPLATE, ".github/workflows/deploy.yml", repo_cfg={"DEVFLOW_DEPLOY_MANUAL_ONLY": "false"}
        )
        wf_manual = self.df.standard_lib.render_standard_file(
            self.df.TEMPLATE, ".github/workflows/deploy.yml", repo_cfg={"DEVFLOW_DEPLOY_MANUAL_ONLY": "true"}
        )
        (self.repo / ".devflow").mkdir(exist_ok=True)
        (self.repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)

        # 1a. judgment: config-only switch manual_only false -> true with untouched push deploy.yml MUST fail
        (self.repo / ".devflow" / "config.env").write_text("DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\n", encoding="utf-8")
        f.files = [{"filename": ".devflow/config.env", "status": "modified", "patch": "+DEVFLOW_DEPLOY_MANUAL_ONLY=true"}]
        self.df.branch_file = lambda repo, ref, path: (
            "DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n"
            if path == ".devflow/config.env"
            else wf_auto
        )
        pr1 = {**f.pr, "number": 31, "headRefOid": "6" * 40, "url": f"https://github.com/{REPO}/pull/31", "fingerprint": "fp-c1"}
        r1 = self.df.judgment("b", REPO, 31, pr1)
        self.assertFalse(r1["standard_delivery"]["ok"], r1)
        self.assertIn(".github/workflows/deploy.yml", r1["standard_delivery"]["drifted"])

        # 1b. judgment: config-only switch manual_only true -> false with untouched manual-only deploy.yml MUST fail
        (self.repo / ".devflow" / "config.env").write_text(
            "DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n", encoding="utf-8"
        )
        self.df.branch_file = lambda repo, ref, path: (
            "DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=false\n"
            if path == ".devflow/config.env"
            else wf_manual
        )
        pr2 = {**f.pr, "number": 32, "headRefOid": "7" * 40, "url": f"https://github.com/{REPO}/pull/32", "fingerprint": "fp-c2"}
        r2 = self.df.judgment("b", REPO, 32, pr2)
        self.assertFalse(r2["standard_delivery"]["ok"], r2)
        self.assertIn(".github/workflows/deploy.yml", r2["standard_delivery"]["drifted"])

        # 1c. judgment: config-only enabling DEVFLOW_DEPLOY=true when deploy.sh/deploy.yml are missing on head MUST fail
        (self.repo / ".devflow" / "config.env").write_text("DEVFLOW_APP=b\nDEVFLOW_DEPLOY=false\n", encoding="utf-8")
        self.df.branch_file = lambda repo, ref, path: (
            "DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\n" if path == ".devflow/config.env" else None
        )
        pr3 = {**f.pr, "number": 33, "headRefOid": "8" * 40, "url": f"https://github.com/{REPO}/pull/33", "fingerprint": "fp-c3"}
        r3 = self.df.judgment("b", REPO, 33, pr3)
        self.assertFalse(r3["standard_delivery"]["ok"], r3)
        self.assertEqual(set(r3["standard_delivery"]["missing"]), {".devflow/deploy.sh", ".github/workflows/deploy.yml"})

        # 2. preflight: config-only switch manual_only=true with untouched push deploy.yml MUST fail
        (self.repo / ".devflow" / "config.env").write_text("DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\n", encoding="utf-8")
        (self.repo / ".github" / "workflows" / "deploy.yml").write_text(wf_auto, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base auto deploy"], cwd=self.repo, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        (self.repo / ".devflow" / "config.env").write_text(
            "DEVFLOW_APP=b\nDEVFLOW_DEPLOY=true\nDEVFLOW_DEPLOY_MANUAL_ONLY=true\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", ".devflow/config.env"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "chore: manual only without workflow update"], cwd=self.repo, check=True)
        pf = self.df.preflight_lib.run_preflight(
            self.repo, policy=self.df.POLICY, base_ref=base, run_tests=False, template_dir=self.df.TEMPLATE
        )
        self.assertFalse(pf["ok"], pf)
        self.assertEqual(pf["checks"]["standard_delivery"], "fail")
        self.assertIn(".github/workflows/deploy.yml", pf["standard_delivery"]["drifted"])

    def test_deploy_sh_rollback_from_pre_upgrade_snapshot_never_inherits_failed_candidate_sha(self) -> None:
        for mode in ("pre_upgrade_with_label", "pre_upgrade_no_label", "new_format_with_source_sha"):
            with self.subTest(mode=mode):
                work = Path(tempfile.mkdtemp(prefix=f"devflow-rb-{mode}-"))
                self.addCleanup(shutil.rmtree, work, ignore_errors=True)
                (work / ".devflow").mkdir()
                (work / "deploy").mkdir()
                shutil.copy2(KIT / "templates" / "repo" / ".devflow" / "deploy.sh", work / ".devflow" / "deploy.sh")
                (work / ".devflow" / "config.env").write_text(
                    "DEVFLOW_APP=rbprobe\nDEVFLOW_SERVICE=app\nDEVFLOW_PORT=18992\nDEVFLOW_APP_PORT=8080\nDEVFLOW_HEALTH_PATH=/health\n",
                    encoding="utf-8",
                )
                (work / "deploy" / "compose.yml").write_text("services:\n  app:\n    image: ${DEVFLOW_IMAGE}\n", encoding="utf-8")
                post_log = work / "post.log"
                for name, body in (
                    ("deploy-build.sh", "#!/usr/bin/env bash\nexit 0\n"),
                    ("capture.sh", "#!/usr/bin/env bash\nexit 0\n"),
                    ("verify.sh", "#!/usr/bin/env bash\nexit 1\n"),
                    ("deploy-post.sh", f'#!/usr/bin/env bash\nprintf "%s|%s\\n" "$DEVFLOW_DEPLOY_PHASE" "$DEVFLOW_SOURCE_SHA" >> "{post_log}"\n'),
                ):
                    p = work / ".devflow" / name
                    p.write_text(body, encoding="utf-8")
                    p.chmod(0o755)
                home = work / "home"
                live_dir = home / ".devflow-live" / "rbprobe.d" / "old-snap"
                live_dir.mkdir(parents=True)
                shutil.copy2(work / "deploy" / "compose.yml", live_dir / "compose.yml")
                shutil.copy2(work / ".devflow" / "config.env", live_dir / "config.env")
                (live_dir / ".env").write_text("", encoding="utf-8")
                (live_dir / "image").write_text("sha256:" + ("1" * 64) + "\n", encoding="utf-8")
                if mode == "new_format_with_source_sha":
                    (live_dir / "source_sha").write_text("saved-old-sha-" + ("0" * 26) + "\n", encoding="utf-8")
                (home / ".devflow-live" / "rbprobe").symlink_to(live_dir)

                bin_dir = work / "bin"
                bin_dir.mkdir()
                calls_file = work / "calls.jsonl"
                (bin_dir / "curl").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                (bin_dir / "curl").chmod(0o755)
                label_sha = "label-old-sha-" + ("9" * 26) if mode == "pre_upgrade_with_label" else ""
                (bin_dir / "docker").write_text(
                    f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path({str(calls_file)!r}).open("a") as fh:
    fh.write(json.dumps({{"args": args, "sha": os.environ.get("DEVFLOW_SOURCE_SHA", "")}}) + "\\n")
if args[:2] == ["image", "inspect"]:
    if "--format" in args:
        fmt = args[args.index("--format") + 1]
        if "org.opencontainers.image.revision" in fmt:
            print({label_sha!r} if {bool(label_sha)!r} else "<no value>")
        else:
            print("<no value>")
    sys.exit(0)
sys.exit(0)
""",
                    encoding="utf-8",
                )
                (bin_dir / "docker").chmod(0o755)
                failed_sha = "failed-cand-sha-" + ("f" * 24)
                env = {
                    **os.environ,
                    "HOME": str(home),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "GITHUB_SHA": failed_sha,
                }
                proc = subprocess.run(["bash", ".devflow/deploy.sh"], cwd=work, env=env, text=True, capture_output=True)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                records = [json.loads(line) for line in calls_file.read_text().splitlines()]
                rb_ups = [r for r in records if r["args"][:1] == ["compose"] and "--no-build" in r["args"]]
                self.assertTrue(rb_ups, records)
                expected_sha = (
                    "saved-old-sha-" + ("0" * 26)
                    if mode == "new_format_with_source_sha"
                    else (label_sha if mode == "pre_upgrade_with_label" else "")
                )
                self.assertEqual(rb_ups[0]["sha"], expected_sha)
                self.assertNotEqual(rb_ups[0]["sha"], failed_sha)
                self.assertEqual(post_log.read_text().strip(), f"rollback|{expected_sha}")

    def test_devflow_deploy_false_case_variants_match_between_standard_validation_and_deploy_sh(self) -> None:
        for cfg_line in ("false", "False", "FALSE", '" false "', "' FALSE '"):
            with self.subTest(cfg_line=cfg_line):
                parsed = self.df.policy_lib.env_values(f"DEVFLOW_DEPLOY={cfg_line}\n")
                self.assertEqual(
                    self.df.standard_lib.required_standard_files(parsed),
                    self.df.standard_lib.STANDARD_CORE_FILES,
                )
                work = Path(tempfile.mkdtemp(prefix="devflow-deploy-off-"))
                self.addCleanup(shutil.rmtree, work, ignore_errors=True)
                (work / ".devflow").mkdir()
                shutil.copy2(KIT / "templates" / "repo" / ".devflow" / "deploy.sh", work / ".devflow" / "deploy.sh")
                (work / ".devflow" / "config.env").write_text(
                    f"DEVFLOW_APP=offprobe\nDEVFLOW_DEPLOY={cfg_line}\n", encoding="utf-8"
                )
                bin_dir = work / "bin"
                bin_dir.mkdir()
                (bin_dir / "docker").write_text("#!/usr/bin/env bash\necho DOCKER_CALLED >&2; exit 99\n", encoding="utf-8")
                (bin_dir / "docker").chmod(0o755)
                proc = subprocess.run(
                    ["bash", ".devflow/deploy.sh"],
                    cwd=work,
                    env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("skip deploy", proc.stdout)
                self.assertNotIn("DOCKER_CALLED", proc.stderr)


if __name__ == "__main__":
    unittest.main()
