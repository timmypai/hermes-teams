"""PR-D regression tests: workflow modes, structural scope inheritance, deterministic PR fail-fast,
ADJ convergence, `devflow pr submit`, repo AGENTS.md conflict warnings and ADJ source telemetry."""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, REPO, JudgmentCase, load_devflow


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path, base: dict[str, str], change: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    for tree in (base, change):
        for rel, text in tree.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "c")


def _root(prefix: str, repo_dir: Path | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copy(KIT / "roles.json", root / "roles.json")
    (root / "state").mkdir()
    path = str(repo_dir) if repo_dir else str(root / "repo")
    (root / "state" / "repos.json").write_text(json.dumps({
        "b": {"slug": "b", "repo": REPO, "path": path, "port": 18100, "default_branch": "main", "contract": True}}))
    return root


class WorkflowModes(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_dir = Path(tempfile.mkdtemp(prefix="devflow-prd-modes-repo-"))
        self.addCleanup(shutil.rmtree, self.repo_dir, ignore_errors=True)
        (self.repo_dir / ".devflow").mkdir()
        self.root = _root("devflow-prd-modes-", self.repo_dir)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.df = load_devflow(self.root)

    def cards(self, mode: str | None, res_assignee: str = "devops", res_ws: str = "dir", with_dev: bool = False) -> dict:
        head = f"Mode: {mode}\n" if mode else ""
        cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo", "assignee": "orchestrator",
                      "workspace_kind": "dir", "parents": ["t_res1"], "children": [], "goal_mode": False,
                      "body": f"{head}## 目標\ng\n## 驗收\n- AC-1 [api] 查出第一個失敗環節\n"},
            "t_res1": {"id": "t_res1", "title": "[RES] 取證", "status": "ready", "assignee": res_assignee,
                       "workspace_kind": res_ws, "parents": [], "children": ["t_ep1"], "goal_mode": False,
                       "body": "## 目標\n唯讀取證\n## 驗收\n覆蓋：AC-1\nEPIC: t_ep1"},
        }
        if with_dev:
            body = (f"## 目標\ng\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1")
            cards["t_dev1"] = {"id": "t_dev1", "title": "[DEV] 改碼", "status": "ready", "assignee": "developer",
                               "workspace_kind": "worktree", "parents": [], "children": ["t_ep1"], "goal_mode": True,
                               "completion_contract": REPO, "body": body}
            cards["t_ep1"]["parents"].append("t_dev1")
        return cards

    def rules(self, cards: dict) -> list[str]:
        return self.df.plan_rules("b", "t_ep1", cards)

    def test_investigate_needs_no_scenarios_and_allows_devops_research_in_dir(self) -> None:
        found = self.rules(self.cards("investigate"))
        self.assertFalse(any("沒有任何情境" in f for f in found), found)
        self.assertFalse(any("assignee 應為" in f or "workspace 應為" in f for f in found), found)

    def test_deliver_default_keeps_the_full_rules(self) -> None:
        found = self.rules(self.cards(None))
        self.assertTrue(any("沒有任何情境" in f for f in found), found)
        self.assertTrue(any("assignee 應為 researcher" in f for f in found), found)
        self.assertTrue(any("workspace 應為 scratch" in f for f in found), found)
        self.assertTrue(any("assignee 應為 researcher" in f for f in self.rules(self.cards("deliver"))))

    def test_investigate_and_review_refuse_patch_cards(self) -> None:
        for mode in ("investigate", "review"):
            found = self.rules(self.cards(mode, with_dev=True))
            self.assertTrue(any(f"Mode: {mode}" in f and "[DEV]" in f for f in found), (mode, found))
        self.assertFalse(any("不開放改程式補丁" in f for f in self.rules(self.cards("deliver", "researcher", "scratch", True))))

    def test_review_mode_accepts_reviewer_research_and_skips_scenarios(self) -> None:
        found = self.rules(self.cards("review", res_assignee="reviewer", res_ws="dir"))
        self.assertFalse(any("沒有任何情境" in f or "assignee 應為" in f for f in found), found)

    def test_unknown_mode_fails_closed(self) -> None:
        found = self.rules(self.cards("bogus"))
        self.assertTrue(any("Mode 'bogus' 無效" in f for f in found), found)
        self.assertTrue(any("沒有任何情境" in f for f in found), found)  # falls back to the strict deliver rules

    def test_mode_helpers_read_declarative_policy(self) -> None:
        lib = self.df.policy_lib
        self.assertEqual(lib.valid_modes(self.df.POLICY), frozenset({"deliver", "investigate", "review"}))
        self.assertFalse(lib.mode_config(self.df.POLICY, "investigate")["allow_dev_cards"])
        self.assertTrue(lib.mode_config(self.df.POLICY, "deliver")["require_ship"])
        self.assertEqual(self.df.scope_lib.parse_epic_mode("Mode: Review\n## 目標\ng")[0], "review")
        self.assertEqual(self.df.scope_lib.parse_epic_mode("## 目標\ng"), ("deliver", None))


class StructuralScope(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _root("devflow-prd-scope-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.df = load_devflow(self.root)
        self.scope = self.df.scope_lib

    def test_non_goals_and_do_not_touch_paths_become_deterministic_scope(self) -> None:
        epic = "Mode: review\n## 目標\ng\n## 不能碰\n`deploy/compose.yml`\napp/auth/**\n只講白話不是路徑\n"
        ctx = self.scope.build_goal_context(
            epic_id="t_e", epic_title="e", epic_body=epic, card_id="t_d", card_title="[DEV] d",
            card_body="Allowed-Scope: app/**\nOut-of-Scope: secret/**\nNon-Goals: 不做 UI 重構")
        self.assertEqual(set(ctx["out_of_scope"]), {"secret/**", "deploy/compose.yml", "app/auth/**"})
        self.assertEqual(ctx["non_goals"], ["不做 UI 重構"])
        self.assertEqual(ctx["epic_mode"], "review")
        self.assertEqual(self.scope.parse_card_contract("Non-Goals: a\nNon-Goals: b")["non_goals"], ["a", "b"])
        self.assertEqual(self.scope.extract_do_not_touch_paths("見 https://example.com/x 與 `a/b.py`"), ["a/b.py"])

    def test_default_test_paths_exempt_tests_but_never_out_of_scope(self) -> None:
        lib = self.df.policy_lib
        defaults = lib.repo_test_paths({}, self.df.POLICY)
        self.assertIn("tests/**", defaults)
        self.assertEqual(lib.repo_test_paths({}), [])  # legacy call without policy keeps old behaviour
        self.assertEqual(lib.repo_test_paths({"DEVFLOW_TEST_PATHS": "none"}, self.df.POLICY), [])
        self.assertEqual(lib.repo_test_paths({"DEVFLOW_TEST_PATHS": "spec/**"}, self.df.POLICY), ["spec/**"])
        files = ["app/a.py", "tests/test_foo.py"]
        self.assertEqual(self.scope.check_files_against_scope(files, allowed_scope=["app/**"], test_paths=defaults), [])
        self.assertEqual(len(self.scope.check_files_against_scope(files, allowed_scope=["app/**"], test_paths=[])), 1)
        oos = self.scope.check_files_against_scope(
            ["app/a.py", "tests/secret/test_x.py"], allowed_scope=["app/**"], out_of_scope=["tests/secret/**"],
            test_paths=defaults)
        self.assertEqual(len(oos), 1)
        self.assertIn("Out-of-Scope", oos[0])

    def _preflight(self, config_env: str, epic_body: str | None, change: dict[str, str]):
        repo = Path(tempfile.mkdtemp(prefix="devflow-prd-pf-"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        base = {".devflow/config.env": config_env, "app/a.py": "x = 1\n", "app/auth/login.py": "ok = 1\n"}
        _init_repo(repo, base, change)
        return self.df.preflight_lib.run_preflight(
            repo, policy={**self.df.POLICY, "require_allowed_scope": True}, base_ref="HEAD~1",
            card_body="## 目標\ng\nAllowed-Scope: app/**\n", epic_body=epic_body, run_tests=False)

    def test_preflight_inherits_epic_do_not_touch_into_out_of_scope(self) -> None:
        rep = self._preflight("DEVFLOW_DEPLOY=false\n", "## 不能碰\napp/auth/**\n",
                              {"app/a.py": "x = 2\n", "app/auth/login.py": "ok = 2\n"})
        self.assertFalse(rep["ok"])
        self.assertTrue(any("Out-of-Scope" in f and "app/auth/login.py" in f for f in rep["findings"]), rep["findings"])
        clean = self._preflight("DEVFLOW_DEPLOY=false\n", None, {"app/a.py": "x = 2\n", "app/auth/login.py": "ok = 2\n"})
        self.assertTrue(clean["ok"], clean["findings"])

    def test_preflight_uses_default_test_paths_unless_repo_disables_them(self) -> None:
        change = {"app/a.py": "x = 2\n", "tests/test_a.py": "def test(): pass\n"}
        self.assertTrue(self._preflight("DEVFLOW_DEPLOY=false\n", None, change)["ok"])
        off = self._preflight("DEVFLOW_DEPLOY=false\nDEVFLOW_TEST_PATHS=none\n", None, change)
        self.assertFalse(off["ok"])
        self.assertTrue(any("tests/test_a.py" in f for f in off["findings"]), off["findings"])

    def test_do_not_touch_extracts_root_files_even_past_1000_chars(self) -> None:
        prose_pad = "禁止修改既有的發布與設定檔案。\n" * 80  # > 1000 chars before explicit paths
        epic = (
            f"## 目標\ng\n## 不能碰\n{prose_pad}"
            "`README.md`、`Dockerfile`、`package.json`、`-rf`、`123`、`.env.local` 與 app/secret/**\n"
        )
        secs = self.scope.parse_epic_sections(epic)
        self.assertGreater(len(secs["不能碰"]), 1000)
        paths = self.scope.extract_do_not_touch_paths(secs["不能碰"])
        self.assertEqual(paths, ["README.md", "Dockerfile", "package.json", ".env.local", "app/secret/**"])
        ctx = self.scope.build_goal_context(
            epic_id="t_e", epic_title="e", epic_body=epic, card_id="t_d", card_title="[DEV] d",
            card_body="Allowed-Scope: app/**",
        )
        for root_file in ("README.md", "Dockerfile", "package.json", ".env.local", "app/secret/**"):
            self.assertIn(root_file, ctx["out_of_scope"])
        for filename in ("README.md", "Dockerfile", "package.json"):
            rep = self._preflight("DEVFLOW_DEPLOY=false\n", epic, {"app/a.py": "x = 2\n", filename: "touch\n"})
            self.assertFalse(rep["ok"], (filename, rep))
            self.assertTrue(any("Out-of-Scope" in f and filename in f for f in rep["findings"]), (filename, rep))


class DeterministicPrFailFast(JudgmentCase):
    def setUp(self) -> None:
        super().setUp()
        self.df.pr_goal_context = lambda board, repo, number, pr: {
            "epic_id": "t_e", "card_id": "t_dev", "allowed_scope": ["app/**"], "out_of_scope": []}
        self.f.files = [{"filename": "docs/readme.md", "status": "modified", "patch": "+x"}]

    def test_scope_violation_fails_the_judgment_without_an_advisor(self) -> None:
        rec = self.df.judgment("b", REPO, 7)
        self.assertTrue(rec["deterministic_findings"])
        self.assertTrue(any("Allowed-Scope" in f for f in rec["deterministic_findings"]))
        gated, verdict = self.df.gate("b", REPO, 7)
        self.assertEqual(verdict, "deterministic_fail")
        self.assertEqual(self.f.statuses, ["failure"])
        self.assertEqual(self.record()["posted"], [gated["head_sha"], "failure"])

    def test_ship_merge_refuses_with_exit_4_and_never_merges(self) -> None:
        code, out = self.ship()
        self.assertEqual(code, 4, out)
        self.assertEqual(json.loads(out)["verdict"], "deterministic_fail")
        self.assertEqual((self.f.merges, self.f.statuses), ([], ["failure"]))

    def test_tick_never_opens_an_adv_card_for_a_deterministic_failure(self) -> None:
        self.f.cards["t_dev"] = {"verdict": None, "body": "EPIC: t_e", "status": "review",
                                 "runs": [{"status": "review", "metadata": {"published_pr": self.f.pr["url"]}}]}
        tasks = [{"id": "t_dev", "title": "[DEV] x", "assignee": "reviewer", "status": "review",
                  "completion_contract": REPO}]
        self.df.tick_prs("b", REPO, tasks)
        self.df.tick_prs("b", REPO, tasks)
        self.assertEqual(self.f.statuses, ["failure"])
        self.assertEqual([c for c in self.f.cards.values() if "title" in c], [])
        self.assertIsNone(self.record()["advisor"])

    def test_clean_diff_still_merges(self) -> None:
        self.f.files = [{"filename": "app/ok.py", "status": "modified", "patch": "+x"}]
        code, out = self.ship()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.f.statuses, ["success"])

    def test_judgment_catches_root_file_after_1000_chars_in_epic_do_not_touch(self) -> None:
        epic = "## 目標\ng\n## 不能碰\n" + ("禁止碰根目錄設定。\n" * 120) + "`Dockerfile`、`README.md`、`package.json`\n"
        ctx = self.df.scope_lib.build_goal_context(
            epic_id="t_e", epic_title="e", epic_body=epic, card_id="t_dev", card_title="[DEV] d",
            card_body="Allowed-Scope: app/**",
        )
        self.df.pr_goal_context = lambda board, repo, number, pr: ctx
        self.f.files = [{"filename": "Dockerfile", "status": "modified", "patch": "+FROM python:3.12"}]
        rec = self.df.judgment("b", REPO, 7)
        self.assertTrue(any("Out-of-Scope" in f and "Dockerfile" in f for f in rec["deterministic_findings"]), rec)
        _, verdict = self.df.gate("b", REPO, 7)
        self.assertEqual(verdict, "deterministic_fail")


class AdjConvergence(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _root("devflow-prd-adj-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.environ["DEVFLOW_HERMES_DATA"] = str(self.root / "data")
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA", None)
        self.df = load_devflow(self.root)
        self.db = self.root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT);
                CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT,
                    payload TEXT, created_at INTEGER);
            """)
            c.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [
                ("t_dev1", "[DEV] d", "", "ready"), ("t_ep1", "[EPIC] e", "## 目標\ng", "todo")])
        self.calls: list[tuple] = []
        self.df.kanban = lambda board, *a: self.calls.append(a) or ""

    def card(self, tid: str, title: str, status: str, **kw) -> dict:
        return {"id": tid, "title": title, "status": status, "assignee": "orchestrator", "workspace_kind": "dir",
                "parents": [], "children": [], "body": kw.get("body", ""), **kw}

    def test_resolved_plan_repair_adj_is_archived_but_other_cards_are_left_alone(self) -> None:
        cards = {
            "t_ep1": self.card("t_ep1", "[EPIC] e", "todo"),
            "t_stale": self.card("t_stale", "[ADJ] 計畫規則修正 t_ep1", "blocked"),
            "t_done": self.card("t_done", "[ADJ] 計畫規則修正 t_ep1", "done"),
            "t_other": self.card("t_other", "[ADJ] 計畫規則修正 t_ep9", "blocked"),
            "t_flow": self.card("t_flow", "[ADJ] 流程阻塞 t_dev1", "blocked"),
        }
        st = {"plan_adj": {"t_ep1": {"adj": "t_stale", "rkey": "x"}}}
        self.assertEqual(self.df.open_plan_adj("b", "t_ep1", cards, [], st), (None, False))
        self.assertEqual([c for c in self.calls if c[0] == "archive"], [("archive", "t_stale")])
        self.assertEqual(cards["t_stale"]["status"], "archived")
        self.assertEqual((cards["t_done"]["status"], cards["t_other"]["status"], cards["t_flow"]["status"]),
                         ("done", "blocked", "blocked"))
        self.assertNotIn("t_ep1", st["plan_adj"])

    def test_fingerprint_changes_when_a_review_rejection_lands(self) -> None:
        self.df.BOARDS = self.root / "data" / "kanban" / "boards"
        cards = {"t_ep1": self.card("t_ep1", "[EPIC] e", "todo", parents=["t_dev1"]),
                 "t_dev1": self.card("t_dev1", "[DEV] d", "review", assignee="developer", workspace_kind="worktree",
                                     children=["t_ep1"])}
        before = self.df.plan_fingerprint("t_ep1", cards, REPO, board="b")
        self.assertEqual(before, self.df.plan_fingerprint("t_ep1", cards, REPO, board="b"))
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES "
                      "('t_dev1', 'changes_requested', '{}', 100)")
        after = self.df.plan_fingerprint("t_ep1", cards, REPO, board="b")
        self.assertNotEqual(before, after)
        self.assertEqual(self.df.plan_fingerprint("t_ep1", cards, REPO), self.df.plan_fingerprint("t_ep1", cards, REPO))

    def _edit(self, task: str, **fields) -> None:
        self.df.apply_op("b", {"op": "edit", "task": task, **fields})

    def test_malformed_dev_edit_is_rejected_in_place_without_touching_the_board(self) -> None:
        good_head = f"## 目標\ng\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\n"
        with self.assertRaises(ValueError) as cm:
            self._edit("t_dev1", body=good_head + "EPIC: t_ep1")
        self.assertIn("Allowed-Scope", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            self._edit("t_dev1", body="## 目標\ng\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1")
        self.assertIn("本卡完成", str(cm.exception))
        with self.assertRaises(ValueError):
            self._edit("t_dev1", body=good_head + "Allowed-Scope: **\n")
        with self.assertRaises(ValueError):
            self._edit("t_dev1", title="沒有卡型前綴")
        self.assertEqual(self.calls, [])

    def test_valid_and_non_pr_card_edits_still_reach_the_board(self) -> None:
        self._edit("t_dev1", body=f"## 目標\ng\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\n"
                                  "Allowed-Scope: app/**\nEPIC: t_ep1")
        self._edit("t_ep1", body="## 目標\n改寫\n")
        self.assertEqual([c[0] for c in self.calls], ["edit", "edit"])

    def test_title_only_edit_converting_to_dev_or_ops_is_rejected_without_touching_board(self) -> None:
        for prefix in ("[DEV]", "[OPS]"):
            with self.assertRaises(ValueError) as cm:
                self._edit("t_ep1", title=f"{prefix} 偷轉任務卡")
            self.assertTrue("本卡完成" in str(cm.exception) or "Allowed-Scope" in str(cm.exception), cm.exception)
        with self.assertRaises(ValueError):
            self._edit("t_dev1", title="[DEV] 缺結構正文也應擋")
        self.assertEqual(self.calls, [])

    def test_title_only_edit_on_valid_dev_card_succeeds(self) -> None:
        valid_body = f"## 目標\ng\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1"
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE tasks SET body = ? WHERE id = 't_dev1'", (valid_body,))
        self._edit("t_dev1", title="[DEV] 改合法標題")
        self.assertEqual(self.calls, [("edit", "t_dev1", "--title", "[DEV] 改合法標題")])


class PrSubmit(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="devflow-prd-submit-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.root = _root("devflow-prd-submit-root-", self.repo)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.environ["DEVFLOW_HERMES_DATA"] = str(self.root / "data")
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA", None)
        self.df = load_devflow(self.root)
        db = self.root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT);
                CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT,
                    payload TEXT, created_at INTEGER);
            """)
            c.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [
                ("t_ep1", "[EPIC] e", "## 目標\ng\n## 驗收\n- AC-1 [api] e\n  - S1: ok", "todo"),
                ("t_dev1", "[DEV] d", f"## 目標\ng\n## 本卡完成\n{self.df.DONE_TEXT}\n## 驗收\n覆蓋：AC-1\n"
                                      "Allowed-Scope: app/**\nEPIC: t_ep1\n", "running"),
            ])
        self.calls: list[list[str]] = []
        self.pr: dict | None = None

        def _flag(cmd: list[str], name: str) -> str | None:
            return cmd[cmd.index(name) + 1] if name in cmd else None

        def run(cmd, **kw):
            self.calls.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "view"]:
                if self.pr is None:
                    return SimpleNamespace(returncode=1, stdout="", stderr="no pull requests found")
                return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(self.pr))
            if cmd[:3] == ["gh", "pr", "create"]:
                self.pr = {
                    "url": f"https://github.com/{REPO}/pull/5",
                    "number": 5,
                    "headRefOid": "a" * 40,
                    "state": "OPEN",
                    "title": _flag(cmd, "--title") or "",
                    "body": _flag(cmd, "--body") or "",
                }
            elif cmd[:3] == ["gh", "pr", "edit"] and self.pr is not None:
                if (t := _flag(cmd, "--title")) is not None:
                    self.pr["title"] = t
                if (b := _flag(cmd, "--body")) is not None:
                    self.pr["body"] = b
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        self.df.run = run

    def args(self, **kw) -> SimpleNamespace:
        base = dict(slug="b", task="t_dev1", dir=str(self.repo), base="HEAD~1", pr_title="feat: x", pr_body="做了 x",
                    pr_body_file=None, no_test=True, allow_dirty=False, dry_run=False)
        return SimpleNamespace(**{**base, **kw})

    def _mutating_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["git", "-C"] or c[:3] in (["gh", "pr", "create"], ["gh", "pr", "edit"])]

    def submit(self, **kw) -> tuple[int, dict]:
        buf = io.StringIO()
        code = 0
        try:
            with redirect_stdout(buf):
                self.df.cmd_pr_submit(self.args(**kw))
        except SystemExit as e:
            code = int(e.code)
        return code, json.loads(buf.getvalue())

    def make(self, change: dict[str, str]) -> None:
        sc = {"t_ep1/AC-1.S1": {"description": "d", "given": "g", "when": "w", "then": "t"}}
        _init_repo(
            self.repo,
            {".devflow/config.env": "DEVFLOW_DEPLOY=false\n", ".devflow/scenarios.json": json.dumps(sc), "app/a.py": "x = 1\n"},
            change,
        )

    def test_out_of_scope_change_stops_at_preflight_and_never_pushes(self) -> None:
        self.make({"docs/readme.md": "hi\n"})
        code, out = self.submit()
        self.assertEqual((code, out["ok"], out["stage"]), (1, False, "preflight"))
        self.assertTrue(any("Allowed-Scope" in f for f in out["preflight"]["findings"]))
        self.assertEqual(self._mutating_calls(), [])

    def test_dry_run_stops_after_a_green_preflight(self) -> None:
        self.make({"app/a.py": "x = 2\n"})
        code, out = self.submit(dry_run=True)
        self.assertEqual((code, out["ok"], out["dry_run"]), (0, True, True))
        self.assertEqual(self._mutating_calls(), [])

    def test_green_preflight_pushes_opens_the_pr_and_binds_the_card(self) -> None:
        self.make({"app/a.py": "x = 2\n"})
        code, out = self.submit()
        self.assertEqual(code, 0, out)
        self.assertEqual((out["stage"], out["created"], out["updated_metadata"], out["pr_number"], out["head_sha"]),
                         ("submitted", True, False, 5, "a" * 40))
        push_idx = next(i for i, c in enumerate(self.calls) if c[:2] == ["git", "-C"])
        self.assertIn("push", self.calls[push_idx])
        create = next(c for c in self.calls if c[:3] == ["gh", "pr", "create"])
        self.assertIn("Kanban: t_dev1", create[create.index("--body") + 1])
        self.calls.clear()
        code, out = self.submit()  # PR already exists with same metadata: push again, no create/edit
        self.assertEqual((code, out["created"], out["updated_metadata"]), (0, False, False))
        self.assertFalse(any(c[:3] in (["gh", "pr", "create"], ["gh", "pr", "edit"]) for c in self.calls))

    def test_repeated_submit_updates_pr_metadata_with_gh_pr_edit_and_verifies(self) -> None:
        self.make({"app/a.py": "x = 2\n"})
        code, _ = self.submit()
        self.assertEqual(code, 0)
        self.calls.clear()
        code, out = self.submit(pr_title="fix: 修 x", pr_body="修好 x\nLesson: L-0001\nCovers: t_ep1/AC-1.S1")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["created"], out["updated_metadata"]), (False, True))
        edits = [c for c in self.calls if c[:3] == ["gh", "pr", "edit"]]
        self.assertEqual(len(edits), 1)
        self.assertIn("fix: 修 x", edits[0])
        self.assertIn("Covers: t_ep1/AC-1.S1", self.pr["body"])
        self.assertIn("Lesson: L-0001", self.pr["body"])
        self.assertIn("Kanban: t_dev1", self.pr["body"])
        self.assertEqual(self.calls[-1][:3], ["gh", "pr", "view"])

    def test_repeated_submit_omitting_flags_validates_remote_pr_metadata(self) -> None:
        self.make({"app/a.py": "x = 2\n"})
        self.pr = {
            "url": f"https://github.com/{REPO}/pull/5", "number": 5, "headRefOid": "a" * 40,
            "state": "OPEN", "title": "fix: 遠端缺 lesson", "body": "說明\nCovers: t_ep1/AC-99.S1\n\nKanban: t_dev1",
        }
        code, out = self.submit(pr_title=None, pr_body=None)
        self.assertEqual((code, out["ok"], out["stage"]), (1, False, "preflight"))
        self.assertTrue(any("Lesson" in f for f in out["preflight"]["findings"]), out)
        self.assertTrue(any("AC-99.S1" in f for f in out["preflight"]["findings"]), out)
        self.assertEqual(self._mutating_calls(), [])

    def test_conflicting_kanban_owner_fails_closed_before_push(self) -> None:
        self.make({"app/a.py": "x = 2\n"})
        self.pr = {
            "url": f"https://github.com/{REPO}/pull/5", "number": 5, "headRefOid": "a" * 40,
            "state": "OPEN", "title": "feat: other", "body": "x\n\nKanban: t_other1",
        }
        code, out = self.submit(task="t_dev1")
        self.assertEqual((code, out["ok"], out["stage"]), (1, False, "pr_owner_conflict"))
        self.assertEqual(self._mutating_calls(), [])
        self.pr = None
        self.calls.clear()
        code, out = self.submit(task="t_dev1", pr_body="寫錯卡\n\nKanban: t_other1")
        self.assertEqual((code, out["ok"], out["stage"]), (1, False, "pr_owner_conflict"))
        self.assertEqual(self._mutating_calls(), [])

    def test_closed_or_merged_pr_refuses_before_preflight_or_push(self) -> None:
        self.make({"app/a.py": "x = 2\n"})
        for st in ("CLOSED", "MERGED"):
            self.calls.clear()
            self.pr = {
                "url": f"https://github.com/{REPO}/pull/5", "number": 5, "headRefOid": "a" * 40,
                "state": st, "title": "feat: done", "body": "Kanban: t_dev1",
            }
            code, out = self.submit()
            self.assertEqual((code, out["ok"], out["stage"], out["pr_state"]), (1, False, "pr_state", st))
            self.assertEqual(self._mutating_calls(), [])


class DoctorAndStats(unittest.TestCase):
    def test_doctor_surfaces_repo_agents_md_conflicts(self) -> None:
        repo = Path(tempfile.mkdtemp(prefix="devflow-prd-doctor-"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        (repo / "AGENTS.md").write_text("devops 為唯一 source/publisher。\n", encoding="utf-8")
        root = _root("devflow-prd-doctor-root-", repo)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        df = load_devflow(root)
        df.binding_plan = lambda slug: ({"id": "p_1"}, [], [])
        df.deploy_readiness = lambda slug: []
        buf = io.StringIO()
        with redirect_stdout(buf):
            df.cmd_repo_doctor(SimpleNamespace(slug="b"))
        rep = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual([w["id"] for w in rep["agents_md_warnings"]], ["sole_publisher_conflict"])
        self.assertEqual(rep["agents_md_warnings"][0]["file"], "AGENTS.md")

    def test_stats_buckets_adj_cards_by_trigger_source(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="devflow-prd-stats-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        db = root / "boards" / "b" / "kanban.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, created_by TEXT,
                    result TEXT, created_at INTEGER, started_at INTEGER);
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
                CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT,
                    payload TEXT, created_at INTEGER);
            """)
            c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", [
                ("t_1", "[ADJ] 計畫規則修正 t_e", "EPIC: t_e", "done", "devflow-tick", None, 1, None),
                ("t_2", "[ADJ] 流程阻塞 t_x", "EPIC: t_e\n處理：t_x\n問題", "done", "devflow-tick", None, 2, None),
                ("t_3", "[ADJ] 流程阻塞 t_y", "處理：t_y\n停在待分類（`triage`）", "ready", "devflow-tick", None, 3, None),
                ("t_4", "[ADJ] 處理失敗卡 t_z", "處理：t_z", "done", "default", None, 4, None),
                ("t_5", "[ADJ] PR 修正", "來源 ADV t_a", "done", "default", None, 5, None),
                ("t_6", "[ADJ] 隨手調整", "x", "done", "default", None, 6, None),
                ("t_7", "[ADJ] 計畫規則修正 t_old", "", "archived", "devflow-tick", None, 7, None),
            ])
        from importlib import import_module
        sys.path.insert(0, str(KIT))
        stats_lib = import_module("lib.stats")
        b = stats_lib.collect_board_stats(root / "boards", "b", REPO)
        self.assertEqual(b["adjustments"]["total"], 6)  # archived cards are excluded
        self.assertEqual(b["adjustments"]["by_source"], {
            "plan_rule": 1, "flow_block": 1, "stalled_triage": 1, "failed_card": 1, "pr_adv": 1, "manual_or_other": 1})
        rep = stats_lib.collect_all_stats(root / "boards", {"b": {"repo": REPO}}, state_dir=root)
        self.assertEqual(rep["totals"]["adj_by_source"], b["adjustments"]["by_source"])
        text = stats_lib.format_stats_text(rep)
        self.assertIn("adj_sources:", text)
        self.assertIn("plan_rule=1", text)


if __name__ == "__main__":
    unittest.main()
