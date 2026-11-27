"""Declarative interruptible/resumable workflow pipelines (`policy.pipelines` + `lib/pipeline.py`)."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import pipeline as pipe_lib  # noqa: E402
from lib import policy as policy_lib  # noqa: E402
from test_judgment import REPO, load_devflow  # noqa: E402


class DeclarativePipelineSpec(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        self.policy = policy_lib.load_policy(KIT, self.roles)

    def test_roles_json_declares_pipelines_and_handoff_schemas(self) -> None:
        pipes = policy_lib.pipelines_config(self.policy)
        self.assertEqual(set(pipes), {"deliver", "investigate", "review"})
        self.assertEqual(
            pipes["deliver"]["steps"],
            ["repro", "design", "implement", "ci", "review", "ship", "accept"],
        )
        schemas = policy_lib.handoff_schemas(self.policy)
        self.assertEqual(set(schemas), {"repro", "design", "implement", "implement_local", "ship"})
        self.assertIn("target_files", schemas["design"]["required"])
        self.assertEqual(schemas["implement_local"]["required"], ["branch", "head_sha", "tests", "changed_files"])

    def test_default_deliver_pipeline_requires_ship_and_scenarios(self) -> None:
        body = "Mode: deliver\n## 目標\n修好問題\n## 驗收\n- AC-1 [api] 回傳 200\n"
        spec = pipe_lib.parse_epic_pipeline(body, self.policy, epic_mode="deliver")
        self.assertTrue(spec["valid"], spec)
        self.assertEqual(spec["pipeline"], "deliver")
        self.assertEqual(spec["start_at"], "repro")
        self.assertEqual(spec["stop_after"], "accept")
        self.assertFalse(spec["is_partial"])
        mode_cfg = policy_lib.mode_config(self.policy, "deliver")
        self.assertTrue(pipe_lib.requires_ship_for_epic(spec, mode_cfg))
        self.assertTrue(pipe_lib.requires_scenarios_for_epic(spec, mode_cfg))

    def test_stop_after_ci_or_implement_allows_skipping_ship_replay_ad4f89e6(self) -> None:
        """Replay t_ad4f89e6: user requests code+PR+CI only (`Stop-After: ci` / `implement`),
        so omitting [SHIP] must be valid without spawning invalid [ADJ]."""
        mode_cfg = policy_lib.mode_config(self.policy, "deliver")
        for stop in ("implement", "ci", "review"):
            body = f"Mode: deliver\nStop-After: {stop}\n## 目標\n只到 {stop}\n"
            spec = pipe_lib.parse_epic_pipeline(body, self.policy, epic_mode="deliver")
            self.assertTrue(spec["valid"], (stop, spec))
            self.assertTrue(spec["is_partial"])
            self.assertFalse(pipe_lib.requires_ship_for_epic(spec, mode_cfg), stop)
            self.assertTrue(pipe_lib.requires_scenarios_for_epic(spec, mode_cfg), stop)

    def test_stop_after_repro_or_design_skips_scenarios_and_ship_replay_39c1b18f(self) -> None:
        """Replay early-phase stop: stopping at repro or design skips downstream scenarios & [SHIP]."""
        mode_cfg = policy_lib.mode_config(self.policy, "deliver")
        for stop in ("repro", "design"):
            body = f"Pipeline: deliver\nStart-At: repro\nStop-After: {stop}\n## 目標\n先定錨\n"
            spec = pipe_lib.parse_epic_pipeline(body, self.policy, epic_mode="deliver")
            self.assertTrue(spec["valid"], spec)
            self.assertFalse(pipe_lib.requires_ship_for_epic(spec, mode_cfg))
            self.assertFalse(pipe_lib.requires_scenarios_for_epic(spec, mode_cfg))

    def test_invalid_pipeline_step_or_reversed_bounds_fail_closed(self) -> None:
        mode_cfg = policy_lib.mode_config(self.policy, "deliver")
        bad_cases = [
            ("Pipeline: nonexistent\n## 目標\nx\n", "deliver"),
            ("Mode: deliver\nStart-At: bogus\n## 目標\nx\n", "deliver"),
            ("Mode: deliver\nStop-After: bogus\n## 目標\nx\n", "deliver"),
            ("Mode: deliver\nStart-At: ship\nStop-After: repro\n## 目標\nx\n", "deliver"),
            ("Mode: investigate\nPipeline: deliver\n## 目標\nx\n", "investigate"),
            ("Mode: deliver\nPaused-At: unknown_step\n## 目標\nx\n", "deliver"),
        ]
        for body, mode in bad_cases:
            spec = pipe_lib.parse_epic_pipeline(body, self.policy, epic_mode=mode)
            self.assertFalse(spec["valid"], body)
            self.assertTrue(spec["problems"], body)
            # Fail closed on strict deliver defaults when invalid
            if mode == "deliver":
                self.assertTrue(pipe_lib.requires_ship_for_epic(spec, mode_cfg), body)
                self.assertTrue(pipe_lib.requires_scenarios_for_epic(spec, mode_cfg), body)

    def test_advance_epic_body_steps_forward_and_clears_paused_at(self) -> None:
        body = (
            "Mode: deliver\n"
            "Start-At: repro\n"
            "Stop-After: ci\n"
            "Paused-At: ci\n"
            "## 目標\n完成 CI 後再決定要不要出貨\n"
        )
        # Advance explicitly to ship
        b1, s1 = pipe_lib.advance_epic_body(body, self.policy, stop_after="ship", epic_mode="deliver")
        self.assertTrue(s1["valid"])
        self.assertEqual(s1["start_at"], "review")
        self.assertEqual(s1["stop_after"], "ship")
        self.assertIsNone(s1["paused_at"])
        self.assertNotIn("Paused-At:", b1)
        self.assertIn("Stop-After: ship", b1)
        self.assertIn("## 目標\n完成 CI 後再決定要不要出貨", b1)

        # Advance to end
        b2, s2 = pipe_lib.advance_epic_body(b1, self.policy, to_end=True, epic_mode="deliver")
        self.assertTrue(s2["valid"])
        self.assertEqual(s2["start_at"], "accept")
        self.assertEqual(s2["stop_after"], "accept")

        # Advancing past end or backwards fails closed
        with self.assertRaises(ValueError):
            pipe_lib.advance_epic_body(b2, self.policy, to_end=True, epic_mode="deliver")
        with self.assertRaises(ValueError):
            pipe_lib.advance_epic_body(body, self.policy, stop_after="repro", epic_mode="deliver")
        with self.assertRaises(ValueError):
            pipe_lib.advance_epic_body(body, self.policy, stop_after="ship", to_end=True, epic_mode="deliver")

    def test_user_paused_card_detection(self) -> None:
        self.assertTrue(pipe_lib.is_user_paused_card({"status": "scheduled", "block_kind": None}))
        self.assertTrue(pipe_lib.is_user_paused_card({"status": "blocked", "block_kind": "needs_input"}))
        self.assertTrue(pipe_lib.is_user_paused_card({"status": "triage", "block_kind": "needs_input"}))
        self.assertFalse(pipe_lib.is_user_paused_card({"status": "blocked", "block_kind": "dependency"}))
        self.assertFalse(pipe_lib.is_user_paused_card({"status": "blocked", "block_kind": "transient"}))
        self.assertFalse(pipe_lib.is_user_paused_card({"status": "ready", "block_kind": None}))
        self.assertFalse(pipe_lib.is_user_paused_card({"status": "triage", "block_kind": None}))

    def test_before_start_step_active_paused_at_and_slice_stop_reached(self) -> None:
        body = "Mode: deliver\nStart-At: implement\nStop-After: ci\n## 目標\n只做到 ci\n"
        spec = pipe_lib.parse_epic_pipeline(body, self.policy, epic_mode="deliver")
        self.assertTrue(spec["valid"])
        self.assertTrue(pipe_lib.is_card_before_start("RES", spec, self.policy))
        self.assertTrue(pipe_lib.is_card_before_start("ARCH", spec, self.policy))
        self.assertFalse(pipe_lib.is_card_before_start("DEV", spec, self.policy))
        self.assertTrue(pipe_lib.is_step_active("implement", spec))
        self.assertTrue(pipe_lib.is_step_active("ci", spec))
        self.assertFalse(pipe_lib.is_step_active("review", spec))
        self.assertFalse(pipe_lib.is_step_active("ship", spec))

        marked1, s1 = pipe_lib.mark_epic_paused_at(body, "ci", self.policy, epic_mode="deliver")
        self.assertEqual(s1["paused_at"], "ci")
        marked2, s2 = pipe_lib.mark_epic_paused_at(marked1, "ci", self.policy, epic_mode="deliver")
        self.assertEqual(marked1, marked2)
        self.assertEqual(s2["paused_at"], "ci")

        cards_running = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo"},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] d", "status": "ready"},
            "t_ship1": {"id": "t_ship1", "title": "[SHIP] s", "status": "blocked"},
        }
        self.assertFalse(pipe_lib.slice_stop_reached(cards_running, "t_ep1", s1, self.policy))
        # Review-lane DEV requires verified CI step_evidence for Stop-After: ci
        cards_running["t_dev1"]["status"] = "review"
        self.assertFalse(pipe_lib.slice_stop_reached(cards_running, "t_ep1", s1, self.policy))
        self.assertFalse(pipe_lib.slice_stop_reached(
            cards_running, "t_ep1", s1, self.policy,
            step_evidence={"t_dev1": {"step": "ci", "ok": False, "pr_number": 7, "head_sha": "a" * 40}},
        ))
        self.assertFalse(pipe_lib.slice_stop_reached(
            cards_running, "t_ep1", s1, self.policy,
            step_evidence={"t_dev1": {"step": "implement", "ok": True, "pr_number": 7, "head_sha": "a" * 40}},
        ))
        self.assertTrue(pipe_lib.slice_stop_reached(
            cards_running, "t_ep1", s1, self.policy,
            step_evidence={"t_dev1": {"step": "ci", "ok": True, "pr_number": 7, "head_sha": "a" * 40}},
        ))
        # Multi-DEV slice still waits if a second DEV is running
        cards_running["t_dev2"] = {"id": "t_dev2", "title": "[DEV] d2", "status": "running"}
        self.assertFalse(pipe_lib.slice_stop_reached(
            cards_running, "t_ep1", s1, self.policy,
            step_evidence={"t_dev1": {"step": "ci", "ok": True, "pr_number": 7, "head_sha": "a" * 40}},
        ))
        cards_running.pop("t_dev2")
        # Under Stop-After: review, a review-lane DEV is NOT done even with CI evidence
        rev_spec = pipe_lib.parse_epic_pipeline("Mode: deliver\nStop-After: review\n## 目標\nr\n", self.policy, epic_mode="deliver")
        self.assertFalse(pipe_lib.slice_stop_reached(
            cards_running, "t_ep1", rev_spec, self.policy,
            step_evidence={"t_dev1": {"step": "ci", "ok": True, "pr_number": 7, "head_sha": "a" * 40}},
        ))
        cards_running["t_dev1"]["status"] = "done"
        self.assertTrue(pipe_lib.slice_stop_reached(cards_running, "t_ep1", s1, self.policy))
        self.assertTrue(pipe_lib.slice_stop_reached(cards_running, "t_ep1", rev_spec, self.policy))

        # Under Stop-After: implement, only blocked(dependency) with local_only implement evidence reaches slice stop
        impl_spec = pipe_lib.parse_epic_pipeline(
            "Mode: deliver\nStart-At: implement\nStop-After: implement\n## 目標\n只停實作\n",
            self.policy,
            epic_mode="deliver",
        )
        local_ev = {
            "t_dev1": {
                "step": "implement",
                "ok": True,
                "local_only": True,
                "head_sha": "d" * 40,
                "branch": "wt/t_dev1",
            }
        }
        for bad_state in (
            {"status": "running", "block_kind": None},
            {"status": "ready", "block_kind": None},
            {"status": "review", "block_kind": None},
            {"status": "blocked", "block_kind": "needs_input"},
        ):
            c_probe = {
                "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo"},
                "t_dev1": {"id": "t_dev1", "title": "[DEV] d", **bad_state},
                "t_ship1": {"id": "t_ship1", "title": "[SHIP] s", "status": "blocked"},
            }
            self.assertFalse(pipe_lib.slice_stop_reached(c_probe, "t_ep1", impl_spec, self.policy, step_evidence=local_ev), bad_state)
        c_cp_held = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo"},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] d", "status": "blocked", "block_kind": "dependency"},
            "t_ship1": {"id": "t_ship1", "title": "[SHIP] s", "status": "blocked"},
        }
        # Missing/non-local evidence fails closed
        self.assertFalse(pipe_lib.slice_stop_reached(c_cp_held, "t_ep1", impl_spec, self.policy))
        self.assertFalse(pipe_lib.slice_stop_reached(
            c_cp_held, "t_ep1", impl_spec, self.policy,
            step_evidence={"t_dev1": {**local_ev["t_dev1"], "local_only": False}},
        ))
        self.assertTrue(pipe_lib.slice_stop_reached(c_cp_held, "t_ep1", impl_spec, self.policy, step_evidence=local_ev))
        # local_only checkpoint evidence must NEVER satisfy Stop-After: ci or Stop-After: review
        self.assertFalse(pipe_lib.slice_stop_reached(c_cp_held, "t_ep1", s1, self.policy, step_evidence=local_ev))
        self.assertFalse(pipe_lib.slice_stop_reached(c_cp_held, "t_ep1", rev_spec, self.policy, step_evidence=local_ev))


class BinDevflowPipelineIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-pipe-int-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        repo_dir = self.root / "repo"
        (repo_dir / ".devflow").mkdir(parents=True)
        (repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY=true\n", encoding="utf-8")
        (self.root / "state").mkdir(parents=True)
        (self.root / "state" / "repos.json").write_text(
            json.dumps({"b": {"slug": "b", "repo": REPO, "path": str(repo_dir), "default_branch": "main",
                              "port": 18100, "contract": True}}),
            encoding="utf-8",
        )
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        self.df = load_devflow(self.root)

    def _base_cards(self, epic_header: str, *, dev_status: str = "ready", dev_block: str | None = None,
                    dev_goal: bool = True, with_ship: bool = False) -> dict:
        epic_body = (
            f"{epic_header}## 目標\n修好 API\n\n## 驗收\n- AC-1 [api] 回傳 200\n"
            "  - S1 [smoke]\n    - Given ready\n    - When GET /ok\n    - Then 200\n"
        )
        dev_body = (
            f"## 目標\n修 /ok\n\n## 本卡完成\n{self.df.DONE_TEXT}\n\n"
            "## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n"
        )
        cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] API fix", "status": "todo", "assignee": "orchestrator",
                      "workspace_kind": "dir", "parents": ["t_ship1" if with_ship else "t_dev1"],
                      "children": [], "goal_mode": False, "body": epic_body},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] 修 /ok", "status": dev_status, "block_kind": dev_block,
                      "assignee": "developer", "workspace_kind": "worktree", "parents": [],
                      "children": ["t_ship1"] if with_ship else ["t_ep1"],
                      "goal_mode": dev_goal, "completion_contract": REPO, "body": dev_body},
        }
        if with_ship:
            cards["t_ship1"] = {"id": "t_ship1", "title": "[SHIP] 部署", "status": "todo", "assignee": "devops",
                                "workspace_kind": "dir", "parents": ["t_dev1"], "children": ["t_ep1"],
                                "goal_mode": False, "body": "## 目標\n出貨\nEPIC: t_ep1\n"}
        return cards

    def test_plan_rules_stop_after_ci_allows_no_ship_and_paused_card_immunity(self) -> None:
        # With Stop-After: ci, no [SHIP] is valid
        cards = self._base_cards("Mode: deliver\nStop-After: ci\n")
        self.assertEqual(self.df.plan_rules("b", "t_ep1", cards), [])

        # Default deliver without Stop-After still requires [SHIP]
        cards_full = self._base_cards("Mode: deliver\n")
        self.assertTrue(any("[SHIP]" in f for f in self.df.plan_rules("b", "t_ep1", cards_full)))

        # User-paused [DEV] (scheduled or needs_input) does not emit per-card rule findings
        paused_sched = self._base_cards("Mode: deliver\n", dev_status="scheduled", dev_goal=False)
        self.assertEqual([f for f in self.df.plan_rules("b", "t_ep1", paused_sched) if f.startswith("t_dev1:")], [])
        paused_wait = self._base_cards("Mode: deliver\n", dev_status="blocked", dev_block="needs_input", dev_goal=False)
        self.assertEqual([f for f in self.df.plan_rules("b", "t_ep1", paused_wait) if f.startswith("t_dev1:")], [])

    def test_plan_rules_rejects_invalid_stop_after(self) -> None:
        cards = self._base_cards("Mode: deliver\nStop-After: made_up\n")
        found = self.df.plan_rules("b", "t_ep1", cards)
        self.assertTrue(any("Stop-After" in f for f in found), found)
        self.assertTrue(any("[SHIP]" in f for f in found), found)  # fail closed

    def test_cmd_epic_advance_updates_body_and_fails_at_end(self) -> None:
        cards = self._base_cards("Mode: deliver\nStart-At: repro\nStop-After: ci\nPaused-At: ci\n")
        comments: list[str] = []

        def fake_kanban(board, cmd, task_id, *args):
            if cmd == "show":
                return json.dumps({"task": cards[task_id], "parents": [], "children": [], "events": []})
            if cmd == "edit":
                cards[task_id]["body"] = args[args.index("--body") + 1]
                return ""
            if cmd == "comment":
                comments.append(args[0])
                return ""
            raise AssertionError((cmd, args))

        self.df.kanban = fake_kanban
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.df.cmd_epic_advance(types.SimpleNamespace(board="b", epic="t_ep1", stop_after=None, to_end=True))
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(out["pipeline"]["start_at"], "review")
        self.assertEqual(out["pipeline"]["stop_after"], "accept")
        self.assertNotIn("Paused-At:", cards["t_ep1"]["body"])
        self.assertEqual(len(comments), 1)
        # Advancing again from terminal step fails closed
        with self.assertRaises(SystemExit):
            self.df.cmd_epic_advance(types.SimpleNamespace(board="b", epic="t_ep1", stop_after=None, to_end=True))

    def test_omitted_mode_with_investigate_pipeline_fails_closed(self) -> None:
        cards = self._base_cards("Pipeline: investigate\n")
        found = self.df.plan_rules("b", "t_ep1", cards)
        self.assertTrue(any("不一致" in f and "Pipeline" in f for f in found), found)
        self.assertTrue(any("[SHIP]" in f for f in found), found)

    def test_stop_after_blocks_out_of_slice_cards_until_epic_advance(self) -> None:
        import os
        import sqlite3
        from unittest import mock

        hermes = self.root / "hermes"
        board_dir = hermes / "kanban" / "boards" / "b"
        board_dir.mkdir(parents=True)
        old_data = os.environ.get("DEVFLOW_HERMES_DATA")
        os.environ["DEVFLOW_HERMES_DATA"] = str(hermes)
        self.addCleanup(
            lambda: os.environ.__setitem__("DEVFLOW_HERMES_DATA", old_data)
            if old_data is not None else os.environ.pop("DEVFLOW_HERMES_DATA", None)
        )
        df = load_devflow(self.root)
        patcher = mock.patch.object(df.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

        epic_body = (
            "Mode: deliver\nStop-After: design\n## 目標\n先定案再施工\n\n## 驗收\n- AC-1 [api] 回傳 200\n"
            "  - S1 [smoke]\n    - Given ready\n    - When GET /ok\n    - Then 200\n"
        )
        arch_body = "## 目標\n設計邊界\nEPIC: t_ep1\n"
        dev_body = f"## 目標\n實作\n\n## 本卡完成\n{df.DONE_TEXT}\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n"
        ship_body = "## 目標\n出貨\nEPIC: t_ep1\n"

        db = board_dir / "kanban.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT,
                assignee TEXT, workspace_kind TEXT, workspace_path TEXT,
                completion_contract TEXT, created_by TEXT, result TEXT, goal_mode INTEGER
            );
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
        """)
        rows = [
            ("t_ep1", "[EPIC] API", epic_body, "todo", None, "orchestrator", "dir", "", None, "default", None, 0),
            ("t_arch1", "[ARCH] 設計", arch_body, "ready", None, "architect", "dir", "", None, "orchestrator", None, 0),
            ("t_dev1", "[DEV] 實作", dev_body, "ready", None, "developer", "worktree", "", REPO, "orchestrator", None, 1),
            ("t_ship1", "[SHIP] 出貨", ship_body, "todo", None, "devops", "dir", "", None, "orchestrator", None, 0),
        ]
        conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO task_links VALUES (?,?)", [("t_arch1", "t_dev1"), ("t_dev1", "t_ship1"), ("t_ship1", "t_ep1")])
        conn.commit()
        conn.close()

        cards = df.plan_cards("b", "t_ep1")
        # Out-of-slice runnable DEV and SHIP are rejected in plan_rules before being parked
        found = df.plan_rules("b", "t_ep1", cards)
        self.assertTrue(any(f.startswith("t_dev1:") and "Stop-After=design" in f for f in found), found)
        self.assertTrue(any(f.startswith("t_ship1:") and "Stop-After=design" in f for f in found), found)

        # Parking out-of-slice cards holds both DEV and SHIP in blocked(dependency)
        pipe_spec = df.pipeline_lib.parse_epic_pipeline(cards["t_ep1"]["body"], df.POLICY, epic_mode="deliver")
        self.assertFalse(df.hold_out_of_slice_cards("b", "t_ep1", cards, pipe_spec))
        cards = df.plan_cards("b", "t_ep1")
        self.assertEqual(cards["t_dev1"]["status"], "blocked")
        self.assertEqual(cards["t_dev1"]["block_kind"], "dependency")
        self.assertEqual(cards["t_ship1"]["status"], "blocked")
        self.assertEqual(cards["t_ship1"]["block_kind"], "dependency")
        # Once parked at boundary (and ARCH still open, before DEV/SHIP run), plan_rules passes
        self.assertEqual(df.plan_rules("b", "t_ep1", cards), [])

        resumed: list[str] = []

        def fake_kanban(board, cmd, tid, *args):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                if cmd == "show":
                    r = dict(c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
                    return json.dumps({"task": r, "events": [], "parents": [], "children": []})
                if cmd == "edit":
                    c.execute("UPDATE tasks SET body = ? WHERE id = ?", (args[args.index("--body") + 1], tid))
                    return ""
                if cmd == "comment":
                    return ""
                if cmd == "assign":
                    to = None if args[0] == "none" else args[0]
                    c.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (to, tid))
                    return ""
                if cmd == "unblock":
                    c.execute("UPDATE tasks SET status = 'ready', block_kind = NULL WHERE id = ?", (tid,))
                    c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'unblocked', '{}', 2)", (tid,))
                    resumed.append(tid)
                    return ""
            raise AssertionError((cmd, tid, args))

        df.kanban = fake_kanban
        # Advance to Stop-After: ci -> releases t_dev1 ONLY; t_ship1 remains parked!
        with contextlib.redirect_stdout(io.StringIO()):
            df.cmd_epic_advance(types.SimpleNamespace(board="b", epic="t_ep1", stop_after="ci", to_end=False))
        self.assertEqual(resumed, ["t_dev1"])
        after_ci = df.plan_cards("b", "t_ep1")
        self.assertEqual(after_ci["t_dev1"]["status"], "ready")
        self.assertEqual(after_ci["t_ship1"]["status"], "blocked")

        # Advance to end -> releases t_ship1 too
        resumed.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            df.cmd_epic_advance(types.SimpleNamespace(board="b", epic="t_ep1", stop_after=None, to_end=True))
        self.assertEqual(resumed, ["t_ship1"])
        after_end = df.plan_cards("b", "t_ep1")
        self.assertEqual(after_end["t_ship1"]["status"], "ready")

    def test_start_at_before_start_and_runtime_stop_gates(self) -> None:
        import os
        import sqlite3
        from unittest import mock

        hermes = self.root / "hermes-gate"
        board_dir = hermes / "kanban" / "boards" / "b"
        board_dir.mkdir(parents=True)
        old_data = os.environ.get("DEVFLOW_HERMES_DATA")
        os.environ["DEVFLOW_HERMES_DATA"] = str(hermes)
        self.addCleanup(
            lambda: os.environ.__setitem__("DEVFLOW_HERMES_DATA", old_data)
            if old_data is not None else os.environ.pop("DEVFLOW_HERMES_DATA", None)
        )
        df = load_devflow(self.root)

        # 1. Start-At: implement rejects runnable [RES] before Start-At
        cards_early = self._base_cards("Mode: deliver\nStart-At: implement\nStop-After: ci\n")
        cards_early["t_res1"] = {"id": "t_res1", "title": "[RES] old", "status": "ready", "assignee": "researcher",
                                 "workspace_kind": "scratch", "parents": [], "children": ["t_dev1"],
                                 "goal_mode": False, "body": "EPIC: t_ep1\n"}
        cards_early["t_dev1"]["parents"] = ["t_res1"]
        found = df.plan_rules("b", "t_ep1", cards_early)
        self.assertTrue(any(f.startswith("t_res1:") and "Start-At=implement" in f for f in found), found)

        # Advance from Stop-After: ci -> Stop-After: review preserves live [DEV]/[OPS] card in review lane
        adv_epic_body = (
            "Mode: deliver\nStop-After: ci\n## 目標\n進到 review\n\n## 驗收\n- AC-1 [api] 200\n"
            "  - S1 [smoke]\n    - Given a\n    - When b\n    - Then c\n"
        )
        adv_cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] API fix", "status": "todo", "assignee": "orchestrator",
                      "workspace_kind": "dir", "parents": ["t_dev1"], "children": [], "goal_mode": False,
                      "body": adv_epic_body},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] 修 /ok", "status": "review", "block_kind": None,
                       "assignee": "reviewer", "workspace_kind": "worktree", "parents": [], "children": ["t_ep1"],
                       "goal_mode": True, "completion_contract": REPO,
                       "body": f"## 目標\n修\n\n## 本卡完成\n{df.DONE_TEXT}\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n"},
        }
        self.assertEqual(df.plan_rules("b", "t_ep1", adv_cards), [])
        adv_body_rev, adv_spec_rev = pipe_lib.advance_epic_body(adv_epic_body, df.POLICY, stop_after="review", epic_mode="deliver")
        self.assertEqual(adv_spec_rev["start_at"], "review")
        adv_cards["t_ep1"]["body"] = adv_body_rev
        self.assertEqual(df.plan_rules("b", "t_ep1", adv_cards), [])
        # Advancing past review to ship while DEV is still open catches DEV as before Start-At=ship
        adv_body_ship, _ = pipe_lib.advance_epic_body(adv_body_rev, df.POLICY, stop_after="ship", epic_mode="deliver")
        adv_cards["t_ep1"]["body"] = adv_body_ship
        ship_found = df.plan_rules("b", "t_ep1", adv_cards)
        self.assertTrue(any(f.startswith("t_dev1:") and "Start-At=ship" in f for f in ship_found), ship_found)

        # 2. _maybe_mark_slice_paused stamps Paused-At: ci idempotently once [DEV] completes
        epic_body = (
            "Mode: deliver\nStop-After: ci\n## 目標\n只做到 ci\n\n## 驗收\n- AC-1 [api] 200\n"
            "  - S1 [smoke]\n    - Given a\n    - When b\n    - Then c\n"
        )
        dev_done = {"id": "t_dev1", "title": "[DEV] d", "status": "done", "body": "EPIC: t_ep1\n"}
        cards_slice = {"t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo", "body": epic_body}, "t_dev1": dev_done}
        edits: list[str] = []
        comments: list[str] = []
        df.kanban = lambda b, cmd, tid, *a: (
            edits.append(a[a.index("--body") + 1]) if cmd == "edit"
            else (comments.append(a[0]) if cmd == "comment" else "")
        )
        pipe = df.pipeline_lib.parse_epic_pipeline(epic_body, df.POLICY, epic_mode="deliver")
        self.assertTrue(df._maybe_mark_slice_paused("b", "t_ep1", cards_slice, pipe))
        self.assertIn("Paused-At: ci", cards_slice["t_ep1"]["body"])
        self.assertEqual(len(edits), 1)
        self.assertIn("devflow epic advance", comments[0])
        pipe2 = df.pipeline_lib.parse_epic_pipeline(cards_slice["t_ep1"]["body"], df.POLICY, epic_mode="deliver")
        self.assertFalse(df._maybe_mark_slice_paused("b", "t_ep1", cards_slice, pipe2))
        self.assertEqual(len(edits), 1)

        # 3. Setup SQLite DB for cmd_pr_submit, tick_prs, and cmd_ship_merge boundary checks
        db = board_dir / "kanban.db"
        repo_dir = self.root / "repo"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT, assignee TEXT, completion_contract TEXT);
            CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, metadata TEXT);
        """)
        conn.execute("INSERT INTO tasks VALUES ('t_ep1', '[EPIC] e', 'Mode: deliver\nStop-After: implement\n## 目標\nx\n', 'todo', NULL, 'orchestrator', NULL)")
        conn.execute("INSERT INTO tasks VALUES ('t_dev1', '[DEV] d', 'EPIC: t_ep1\n', 'running', NULL, 'developer', ?)", (REPO,))
        conn.commit()
        conn.close()

        # cmd_pr_submit blocks push/PR when Stop-After: implement
        df._run_card_preflight = lambda a, **kw: ({}, {"ok": True, "checks": {}, "findings": []}, repo_dir, "Kanban: t_dev1")
        with mock.patch.object(df.preflight_lib, "_git", return_value=types.SimpleNamespace(returncode=0, stdout="feat/x\n", stderr="")), \
             mock.patch.object(df, "run", return_value=types.SimpleNamespace(returncode=0, stdout="[]", stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            with self.assertRaises(SystemExit) as ctx:
                df.cmd_pr_submit(types.SimpleNamespace(
                    slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                    pr_title="feat: x", pr_body="Kanban: t_dev1", pr_body_file=None,
                    no_test=True, allow_dirty=True, dry_run=True,
                ))
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(json.loads(buf.getvalue())["stage"], "pipeline_stop")

        # tick_prs skips gate() when EPIC sets Stop-After: ci
        with sqlite3.connect(db) as c:
            c.execute("UPDATE tasks SET body = 'Mode: deliver\nStop-After: ci\n## 目標\nx\n' WHERE id = 't_ep1'")
        gate_calls: list[int] = []
        df.gate = lambda *a: gate_calls.append(1)
        df.kanban = lambda b, cmd, tid, *a: json.dumps({
            "task": {"status": "review", "body": "EPIC: t_ep1\n"},
            "runs": [{"status": "review", "metadata": {"published_pr": f"https://github.com/{REPO}/pull/7"}}],
        })
        df.tick_prs("b", REPO, [{"id": "t_dev1", "title": "[DEV] d", "assignee": "reviewer", "status": "review", "completion_contract": REPO}])
        self.assertEqual(gate_calls, [])

        # cmd_ship_merge blocks with pipeline_stop_blocked (exit 7) when EPIC sets Stop-After: review
        with sqlite3.connect(db) as c:
            c.execute("UPDATE tasks SET body = 'Mode: deliver\nStop-After: review\n## 目標\nx\n' WHERE id = 't_ep1'")
        df.pr_meta = lambda r, n: {"state": "OPEN", "headRefOid": "a" * 40, "title": "t", "body": "Kanban: t_dev1"}
        df.deploy_readiness = lambda s: []
        df.review_problem = lambda *a: None
        df.resolve_pr_judgment_inputs = lambda *a: ({"epic_id": "t_ep1", "card_id": "t_dev1"}, {}, "fp1")
        df.gate = lambda *a: ({"fingerprint": "fp1", "findings": [], "needs_human_approval": False}, "ok")
        buf_ship = io.StringIO()
        with contextlib.redirect_stdout(buf_ship):
            with self.assertRaises(SystemExit) as ctx_ship:
                df.cmd_ship_merge(types.SimpleNamespace(board="b", pr=7))
        self.assertEqual(ctx_ship.exception.code, 7)
        self.assertTrue(json.loads(buf_ship.getvalue())["pipeline_stop_blocked"])

    def test_stop_after_ci_pauses_review_lane_dev_only_after_exact_head_ci_and_retains_review_handoff(self) -> None:
        import os
        import sqlite3
        from unittest import mock

        hermes = self.root / "hermes-ci-slice"
        board_dir = hermes / "kanban" / "boards" / "b"
        board_dir.mkdir(parents=True)
        old_data = os.environ.get("DEVFLOW_HERMES_DATA")
        os.environ["DEVFLOW_HERMES_DATA"] = str(hermes)
        self.addCleanup(
            lambda: os.environ.__setitem__("DEVFLOW_HERMES_DATA", old_data)
            if old_data is not None else os.environ.pop("DEVFLOW_HERMES_DATA", None)
        )
        df = load_devflow(self.root)
        epic_body = (
            "Mode: deliver\nStop-After: ci\n## 目標\n只先跑到 CI\n\n## 驗收\n- AC-1 [api] 200\n"
            "  - S1 [smoke]\n    - Given ready\n    - When GET /ok\n    - Then 200\n"
        )
        dev_body = (
            f"## 目標\n修\n\n## 本卡完成\n{df.DONE_TEXT}\n\n"
            "## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n"
        )
        head_sha = "c" * 40
        db = board_dir / "kanban.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
                block_kind TEXT, assignee TEXT, workspace_kind TEXT,
                workspace_path TEXT, completion_contract TEXT,
                created_by TEXT, result TEXT, goal_mode INTEGER
            );
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, metadata TEXT);
        """)
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_ep1", "[EPIC] CI stop", epic_body, "todo", None, "orchestrator", "dir", None, None, "user", None, 0),
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_dev1", "[DEV] 修 /ok", dev_body, "review", None, "reviewer", "worktree", None, REPO, "orchestrator", None, 1),
        )
        conn.execute("INSERT INTO task_links VALUES ('t_dev1', 't_ep1')")
        conn.execute(
            "INSERT INTO task_runs VALUES (1, 't_dev1', 'review', ?)",
            (json.dumps({"pr_url": f"https://github.com/{REPO}/pull/7", "pr_number": 7}),),
        )
        conn.commit()
        conn.close()

        edits: list[str] = []
        comments: list[str] = []
        gate_calls: list[int] = []

        def fake_kanban(b: str, cmd: str, tid: str, *args: str) -> str:
            if cmd == "show":
                with sqlite3.connect(db) as c:
                    c.row_factory = sqlite3.Row
                    t = dict(c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
                    runs = [
                        {"status": r["status"], "metadata": json.loads(r["metadata"])}
                        for r in c.execute("SELECT status, metadata FROM task_runs WHERE task_id = ?", (tid,))
                    ]
                return json.dumps({"task": t, "runs": runs})
            if cmd == "edit":
                body = args[args.index("--body") + 1]
                edits.append(body)
                with sqlite3.connect(db) as c:
                    c.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, tid))
                    c.commit()
                return ""
            if cmd == "comment":
                comments.append(args[0])
                return ""
            return ""

        df.kanban = fake_kanban
        df.gate = lambda *a: (gate_calls.append(1) or {"fingerprint": "fp", "findings": []}, "ok")

        cards = df.plan_cards("b", "t_ep1")
        ep_pipe = pipe_lib.parse_epic_pipeline(cards["t_ep1"]["body"], df.POLICY, epic_mode="deliver")

        # 1. Pending CI -> does NOT stamp Paused-At: ci and signals retry
        with mock.patch.object(df, "pr_meta", return_value={"state": "OPEN", "headRefOid": head_sha}), \
             mock.patch.object(df, "workflow_runs", return_value=[{
                 "id": 101, "path": ".github/workflows/ci.yml", "event": "pull_request",
                 "head_sha": head_sha, "status": "in_progress", "conclusion": None,
             }]):
            retry_flags: list[bool] = []
            self.assertFalse(df._maybe_mark_slice_paused("b", "t_ep1", cards, ep_pipe, retry_out=retry_flags))
            self.assertTrue(retry_flags)
            self.assertEqual(edits, [])

        # 2. Failed CI -> does NOT stamp Paused-At: ci
        with mock.patch.object(df, "pr_meta", return_value={"state": "OPEN", "headRefOid": head_sha}), \
             mock.patch.object(df, "workflow_runs", return_value=[{
                 "id": 102, "path": ".github/workflows/ci.yml", "event": "pull_request",
                 "head_sha": head_sha, "status": "completed", "conclusion": "failure",
             }]):
            self.assertFalse(df._maybe_mark_slice_paused("b", "t_ep1", cards, ep_pipe))
            self.assertEqual(edits, [])

        # 3. Exact-head CI succeeded with DEV in review -> stamps Paused-At: ci and skips tick_prs gate
        with mock.patch.object(df, "pr_meta", return_value={"state": "OPEN", "headRefOid": head_sha}), \
             mock.patch.object(df, "workflow_runs", return_value=[{
                 "id": 103, "path": ".github/workflows/ci.yml", "event": "pull_request",
                 "head_sha": head_sha, "status": "completed", "conclusion": "success",
             }]):
            self.assertTrue(df._maybe_mark_slice_paused("b", "t_ep1", cards, ep_pipe))
        self.assertEqual(len(edits), 1)
        self.assertIn("Paused-At: ci", cards["t_ep1"]["body"])
        self.assertTrue(any("--stop-after review" in c for c in comments), comments)

        # Idempotent once Paused-At: ci is recorded (no further GitHub probes)
        paused_pipe = pipe_lib.parse_epic_pipeline(cards["t_ep1"]["body"], df.POLICY, epic_mode="deliver")
        with mock.patch.object(df, "pr_meta", side_effect=AssertionError("must not probe when already paused")):
            self.assertFalse(df._maybe_mark_slice_paused("b", "t_ep1", cards, paused_pipe))

        # tick_prs still skips gate() while Stop-After: ci is active
        df.tick_prs("b", REPO, [cards["t_dev1"]])
        self.assertEqual(gate_calls, [])

        # 4. Advancing to Stop-After: review keeps t_dev1 in review/reviewer handoff, enables tick_prs gate(),
        #    and does NOT mark Paused-At: review until t_dev1 becomes done.
        with contextlib.redirect_stdout(io.StringIO()):
            df.cmd_epic_advance(types.SimpleNamespace(board="b", epic="t_ep1", stop_after="review", to_end=False))
        cards_rev = df.plan_cards("b", "t_ep1")
        self.assertEqual(cards_rev["t_dev1"]["status"], "review")
        self.assertEqual(cards_rev["t_dev1"]["assignee"], "reviewer")
        rev_pipe = pipe_lib.parse_epic_pipeline(cards_rev["t_ep1"]["body"], df.POLICY, epic_mode="deliver")
        self.assertFalse(df._maybe_mark_slice_paused("b", "t_ep1", cards_rev, rev_pipe))
        self.assertNotIn("Paused-At: review", cards_rev["t_ep1"]["body"])
        df.tick_prs("b", REPO, [cards_rev["t_dev1"]])
        self.assertEqual(gate_calls, [1])

        with sqlite3.connect(db) as c:
            c.execute("UPDATE tasks SET status = 'done', assignee = 'reviewer' WHERE id = 't_dev1'")
            c.commit()
        cards_rev_done = df.plan_cards("b", "t_ep1")
        self.assertTrue(df._maybe_mark_slice_paused("b", "t_ep1", cards_rev_done, rev_pipe))
        self.assertIn("Paused-At: review", cards_rev_done["t_ep1"]["body"])

    def test_stop_after_implement_checkpoint_pause_advance_and_resume_to_pr_submit(self) -> None:
        import os
        import sqlite3
        import subprocess
        from unittest import mock

        hermes = self.root / "hermes-impl-slice"
        board_dir = hermes / "kanban" / "boards" / "b"
        board_dir.mkdir(parents=True)
        old_data = os.environ.get("DEVFLOW_HERMES_DATA")
        os.environ["DEVFLOW_HERMES_DATA"] = str(hermes)
        self.addCleanup(
            lambda: os.environ.__setitem__("DEVFLOW_HERMES_DATA", old_data)
            if old_data is not None else os.environ.pop("DEVFLOW_HERMES_DATA", None)
        )
        df = load_devflow(self.root)

        repo_dir = self.root / "repo"
        remote = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
        for cfg_k, cfg_v in (("user.name", "Dev"), ("user.email", "dev@example.com")):
            subprocess.run(["git", "-C", str(repo_dir), "config", cfg_k, cfg_v], check=True, capture_output=True)
        (repo_dir / "app").mkdir(parents=True, exist_ok=True)
        (repo_dir / "app" / "a.py").write_text("X = 1\n", encoding="utf-8")
        check_sh = repo_dir / ".devflow" / "check.sh"
        check_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        check_sh.chmod(0o755)
        subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "chore: base"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "push", "-u", "origin", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "fetch", "origin", "main"], check=True, capture_output=True)

        subprocess.run(["git", "-C", str(repo_dir), "checkout", "-b", "wt/t_dev1"], check=True, capture_output=True)
        (repo_dir / "app" / "a.py").write_text("X = 2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", "app/a.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "feat: fix x (t_dev1)"], check=True, capture_output=True)
        head_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

        epic_body = (
            "Mode: deliver\nStart-At: implement\nStop-After: implement\n"
            "## 目標\n只先完成本機實作\n\n"
            "## 驗收\n- AC-1 [api] 回傳 200\n"
            "  - S1 [smoke]\n    - Given ready\n    - When GET /ok\n    - Then 200\n"
        )
        dev_body = (
            f"## 目標\n修\n\n## 本卡完成\n{df.DONE_TEXT}\n\n"
            "## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n"
        )
        ship_body = "## 目標\n合併\nEPIC: t_ep1\n"
        db = board_dir / "kanban.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
                block_kind TEXT, assignee TEXT, workspace_kind TEXT,
                workspace_path TEXT, completion_contract TEXT,
                created_by TEXT, result TEXT, goal_mode INTEGER
            );
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, metadata TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
        """)
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_ep1", "[EPIC] Local impl stop", epic_body, "todo", None, "orchestrator", "dir", None, None, "user", None, 0),
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_dev1", "[DEV] 修 /ok", dev_body, "running", None, "developer", "worktree", str(repo_dir), REPO, "orchestrator", None, 1),
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_ship1", "[SHIP] 部署 /ok", ship_body, "blocked", "dependency", "devops", "dir", None, None, "orchestrator", None, 0),
        )
        conn.execute("INSERT INTO task_links VALUES ('t_dev1', 't_ship1')")
        conn.execute("INSERT INTO task_links VALUES ('t_ship1', 't_ep1')")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_ship1', 'blocked', ?, 1)",
            (json.dumps({"kind": "dependency", "reason": df.pipeline_stop_hold_reason("t_ep1", "implement")}, ensure_ascii=False),),
        )
        conn.commit()
        conn.close()

        edits: list[str] = []
        comments: list[tuple[str, str]] = []
        unblocks: list[str] = []

        def fake_kanban(b: str, cmd: str, tid: str, *args: str) -> str:
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                if cmd == "show":
                    t = dict(c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
                    runs = [
                        {"status": r["status"], "metadata": json.loads(r["metadata"])}
                        for r in c.execute("SELECT status, metadata FROM task_runs WHERE task_id = ?", (tid,))
                    ]
                    events = [
                        {"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"] or "{}")}
                        for r in c.execute("SELECT id, kind, payload FROM task_events WHERE task_id = ? ORDER BY id ASC", (tid,))
                    ]
                    parents = [r["parent_id"] for r in c.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (tid,))]
                    children = [r["child_id"] for r in c.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (tid,))]
                    return json.dumps({"task": t, "runs": runs, "events": events, "parents": parents, "children": children})
                if cmd == "edit":
                    body = args[args.index("--body") + 1]
                    edits.append(body)
                    c.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, tid))
                    return ""
                if cmd == "comment":
                    comments.append((tid, args[0]))
                    return ""
                if cmd == "assign":
                    to = None if args[0] == "none" else args[0]
                    c.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (to, tid))
                    return ""
                if cmd == "unblock":
                    c.execute("UPDATE tasks SET status = 'ready', block_kind = NULL WHERE id = ?", (tid,))
                    c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'unblocked', '{}', 2)", (tid,))
                    unblocks.append(tid)
                    return ""
            raise AssertionError((b, cmd, tid, args))

        df.kanban = fake_kanban

        # 1. Under Stop-After: implement, cmd_pr_submit blocks push/PR and never pushes or creates PR
        blocked_runs: list[list[str]] = []

        def fake_gh_only_list(cmd, gh=None, check=True, cwd=None, input=None):
            blocked_runs.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "list"]:
                return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
            raise AssertionError(f"must not run {cmd} under Stop-After: implement")

        buf_pr1 = io.StringIO()
        with mock.patch.object(df, "run", side_effect=fake_gh_only_list), \
             contextlib.redirect_stdout(buf_pr1):
            with self.assertRaises(SystemExit) as ctx_pr1:
                df.cmd_pr_submit(types.SimpleNamespace(
                    slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                    pr_title="feat: fix x (t_dev1)",
                    pr_body="Fix X\n\nCovers: t_ep1/AC-1.S1\nKanban: t_dev1",
                    pr_body_file=None, no_test=False, allow_dirty=False, dry_run=False,
                ))
        self.assertEqual(ctx_pr1.exception.code, 1)
        pr1_out = json.loads(buf_pr1.getvalue())
        self.assertEqual(pr1_out["stage"], "pipeline_stop")
        self.assertIn("devflow checkpoint", pr1_out["error"])
        self.assertEqual(len(blocked_runs), 1)

        # 2. Cannot pause before checkpoint & dependency block
        cards_init = df.plan_cards("b", "t_ep1")
        pipe_init = pipe_lib.parse_epic_pipeline(cards_init["t_ep1"]["body"], df.POLICY, epic_mode="deliver")
        self.assertFalse(df._maybe_mark_slice_paused("b", "t_ep1", cards_init, pipe_init))

        # 3. Record local-only implementation checkpoint
        buf_cp = io.StringIO()
        with contextlib.redirect_stdout(buf_cp):
            df.cmd_checkpoint(types.SimpleNamespace(
                slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                no_test=False, test=[], summary="updated app/a.py",
            ))
        cp_out = json.loads(buf_cp.getvalue())
        self.assertTrue(cp_out["ok"])
        self.assertEqual(cp_out["stage"], "implement_checkpoint")
        self.assertTrue(cp_out["local_only"])
        self.assertEqual(cp_out["head_sha"], head_sha)
        self.assertEqual(cp_out["changed_files"], ["app/a.py"])
        self.assertEqual(cp_out["tests"], ["check.sh=pass"])
        self.assertTrue(cp_out["block_reason"].startswith("pipeline-implement-checkpoint:t_ep1"))

        # Check handoff passes as implement_local (auto-detected & explicit --step), while full implement still requires PR
        buf_hoff = io.StringIO()
        with contextlib.redirect_stdout(buf_hoff):
            df.cmd_check_handoff(types.SimpleNamespace(
                board="b", task="t_dev1", metadata=None, metadata_file=None, step=None,
            ))
        hoff_out = json.loads(buf_hoff.getvalue())
        self.assertTrue(hoff_out["ok"])
        self.assertEqual(hoff_out["step"], "implement_local")
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            df.cmd_check_handoff(types.SimpleNamespace(
                board="b", task="t_dev1", metadata=None, metadata_file=None, step="implement",
            ))

        # 4. Developer parks t_dev1 with kanban_block(kind="dependency", reason=block_reason); contract=REPO preserved, no PR
        with sqlite3.connect(db) as c:
            c.execute("UPDATE tasks SET status = 'blocked', block_kind = 'dependency' WHERE id = 't_dev1'")
            c.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_dev1', 'blocked', ?, 2)",
                (json.dumps({"kind": "dependency", "reason": cp_out["block_reason"]}, ensure_ascii=False),),
            )
            c.commit()
        cards_cp = df.plan_cards("b", "t_ep1")
        self.assertEqual(cards_cp["t_dev1"]["completion_contract"], REPO)

        # Must stay parked while Stop-After: implement is active
        self.assertFalse(df.release_pipeline_stop_holds("b", "t_ep1", cards_cp, pipe_init))
        self.assertEqual(cards_cp["t_dev1"]["status"], "blocked")
        self.assertEqual(df.plan_rules("b", "t_ep1", cards_cp), [])

        # 5. Tampering with HEAD after checkpoint fails closed until restored
        (repo_dir / "app" / "a.py").write_text("X = 3\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", "app/a.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "feat: uncheckpointed"], check=True, capture_output=True)
        ev_tamper, _ = df._collect_slice_step_evidence("b", "t_ep1", cards_cp, pipe_init)
        self.assertEqual(ev_tamper, {})
        self.assertFalse(df._maybe_mark_slice_paused("b", "t_ep1", cards_cp, pipe_init))
        subprocess.run(["git", "-C", str(repo_dir), "reset", "--hard", head_sha], check=True, capture_output=True)

        # 6. Exact slice pause: stamps Paused-At: implement once and posts advance prompt
        self.assertTrue(df._maybe_mark_slice_paused("b", "t_ep1", cards_cp, pipe_init))
        self.assertIn("Paused-At: implement", cards_cp["t_ep1"]["body"])
        self.assertEqual(len(edits), 1)
        ep_comments = [msg for tid, msg in comments if tid == "t_ep1"]
        self.assertEqual(len(ep_comments), 1)
        self.assertIn("Paused-At: implement", ep_comments[0])
        self.assertIn("--stop-after ci", ep_comments[0])
        pipe_paused = pipe_lib.parse_epic_pipeline(cards_cp["t_ep1"]["body"], df.POLICY, epic_mode="deliver")
        self.assertFalse(df._maybe_mark_slice_paused("b", "t_ep1", cards_cp, pipe_paused))

        # 7. Advance EPIC to ci -> clears Paused-At, resumes t_dev1 cleanly (keeps t_ship1 parked)
        with contextlib.redirect_stdout(io.StringIO()):
            df.cmd_epic_advance(types.SimpleNamespace(board="b", epic="t_ep1", stop_after="ci", to_end=False))
        self.assertEqual(unblocks, ["t_dev1"])
        cards_ci = df.plan_cards("b", "t_ep1")
        self.assertNotIn("Paused-At:", cards_ci["t_ep1"]["body"])
        self.assertIn("Start-At: ci", cards_ci["t_ep1"]["body"])
        self.assertIn("Stop-After: ci", cards_ci["t_ep1"]["body"])
        self.assertEqual(cards_ci["t_dev1"]["status"], "ready")
        self.assertEqual(cards_ci["t_dev1"]["workspace_path"], str(repo_dir))
        self.assertEqual(cards_ci["t_dev1"]["completion_contract"], REPO)
        self.assertEqual(cards_ci["t_ship1"]["status"], "blocked")

        # 8. Under Stop-After: ci before PR creation, ci checkpoint fails closed; then cmd_pr_submit creates PR #99
        with mock.patch.object(df, "run", side_effect=fake_gh_only_list), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx_cp_ci:
            df.cmd_checkpoint(types.SimpleNamespace(
                slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                no_test=False, test=[], summary="",
            ))
        self.assertEqual(ctx_cp_ci.exception.code, 1)

        pr_calls: list[list[str]] = []
        pr_created = False

        def fake_gh_submit(cmd, gh=None, check=True, cwd=None, input=None):
            nonlocal pr_created
            pr_calls.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "list"]:
                if not pr_created:
                    return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
                return types.SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([{
                        "url": f"https://github.com/{REPO}/pull/99",
                        "number": 99,
                        "headRefOid": head_sha,
                        "state": "OPEN",
                        "title": "feat: fix x (t_dev1)",
                        "body": "Fix X\n\nCovers: t_ep1/AC-1.S1\nKanban: t_dev1",
                    }]),
                    stderr="",
                )
            if "push" in cmd:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd[:3] == ["gh", "pr", "create"]:
                pr_created = True
                return types.SimpleNamespace(returncode=0, stdout=f"https://github.com/{REPO}/pull/99\n", stderr="")
            raise AssertionError(cmd)

        buf_pr2 = io.StringIO()
        with mock.patch.object(df, "run", side_effect=fake_gh_submit), \
             contextlib.redirect_stdout(buf_pr2):
            df.cmd_pr_submit(types.SimpleNamespace(
                slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                pr_title="feat: fix x (t_dev1)",
                pr_body="Fix X\n\nCovers: t_ep1/AC-1.S1\nKanban: t_dev1",
                pr_body_file=None, no_test=False, allow_dirty=False, dry_run=False,
            ))
        pr2_out = json.loads(buf_pr2.getvalue())
        self.assertTrue(pr2_out["ok"])
        self.assertEqual(pr2_out["stage"], "submitted")
        self.assertTrue(pr2_out["created"])
        self.assertEqual(pr2_out["pr_number"], 99)
        self.assertEqual(pr2_out["head_sha"], head_sha)
        self.assertFalse(pr2_out["review_active"])
        self.assertEqual(pr2_out["next_action"], "wait_ci_then_checkpoint")
        self.assertTrue(any("push" in c for c in pr_calls), pr_calls)
        self.assertTrue(any(c[:3] == ["gh", "pr", "create"] for c in pr_calls), pr_calls)

        # 9. Stop-After: ci checkpoint & cmd_check_pr gate + hold_out_of_slice_cards + epic advance to review
        # 9a. cmd_check_pr refuses while review step is inactive (even when worker env binds task)
        old_kt = os.environ.get("HERMES_KANBAN_TASK")
        os.environ["HERMES_KANBAN_TASK"] = "t_dev1"
        try:
            buf_cpr = io.StringIO()
            with contextlib.redirect_stdout(buf_cpr), self.assertRaises(SystemExit) as ctx_cpr:
                df.cmd_check_pr(types.SimpleNamespace(board="b", pr=99))
            self.assertEqual(ctx_cpr.exception.code, 1)
            self.assertEqual(json.loads(buf_cpr.getvalue())["stage"], "pipeline_stop")
        finally:
            if old_kt is not None:
                os.environ["HERMES_KANBAN_TASK"] = old_kt
            else:
                os.environ.pop("HERMES_KANBAN_TASK", None)

        # 9b. Stop-After: ci checkpoint fails closed if ci.yml is not completed/success yet
        buf_ci_wait = io.StringIO()
        with mock.patch.object(df, "run", side_effect=fake_gh_submit), \
             mock.patch.object(df, "workflow_runs", return_value=[{
                 "id": 501, "path": ".github/workflows/ci.yml", "event": "pull_request",
                 "head_sha": head_sha, "status": "in_progress", "conclusion": None,
             }]), \
             contextlib.redirect_stdout(buf_ci_wait), self.assertRaises(SystemExit) as ctx_ci_wait:
            df.cmd_checkpoint(types.SimpleNamespace(
                slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                no_test=False, test=[], summary="",
            ))
        self.assertEqual(ctx_ci_wait.exception.code, 1)
        self.assertEqual(json.loads(buf_ci_wait.getvalue())["stage"], "ci_not_green")

        # 9c. Exact-head ci.yml green -> cmd_checkpoint records ci_checkpoint & pipeline-ci-checkpoint reason
        buf_ci_ok = io.StringIO()
        with mock.patch.object(df, "run", side_effect=fake_gh_submit), \
             mock.patch.object(df, "workflow_runs", return_value=[{
                 "id": 502, "path": ".github/workflows/ci.yml", "event": "pull_request",
                 "head_sha": head_sha, "status": "completed", "conclusion": "success",
             }]), \
             contextlib.redirect_stdout(buf_ci_ok):
            df.cmd_checkpoint(types.SimpleNamespace(
                slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                no_test=False, test=[], summary="PR #99 CI green",
            ))
        ci_ok = json.loads(buf_ci_ok.getvalue())
        self.assertTrue(ci_ok["ok"])
        self.assertEqual(ci_ok["stage"], "ci_checkpoint")
        self.assertTrue(ci_ok["block_reason"].startswith("pipeline-ci-checkpoint:t_ep1"), ci_ok)

        with sqlite3.connect(db) as c:
            c.execute("UPDATE tasks SET status = 'blocked', block_kind = 'dependency' WHERE id = 't_dev1'")
            c.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_dev1', 'blocked', ?, 10)",
                (json.dumps({"kind": "dependency", "reason": ci_ok["block_reason"]}, ensure_ascii=False),),
            )
            c.commit()
        cards_ci_paused = df.plan_cards("b", "t_ep1")
        with mock.patch.object(df, "pr_meta", return_value={"state": "OPEN", "headRefOid": head_sha}), \
             mock.patch.object(df, "workflow_runs", return_value=[{
                 "id": 502, "path": ".github/workflows/ci.yml", "event": "pull_request",
                 "head_sha": head_sha, "status": "completed", "conclusion": "success",
             }]):
            self.assertTrue(df._maybe_mark_slice_paused("b", "t_ep1", cards_ci_paused, pipe_lib.parse_epic_pipeline(
                cards_ci_paused["t_ep1"]["body"], df.POLICY, epic_mode="deliver"
            )))
        self.assertIn("Paused-At: ci", df.plan_cards("b", "t_ep1")["t_ep1"]["body"])

        # Advance to review -> releases ci checkpoint hold cleanly to developer with request_review note
        with contextlib.redirect_stdout(io.StringIO()):
            df.cmd_epic_advance(types.SimpleNamespace(board="b", epic="t_ep1", stop_after="review", to_end=False))
        self.assertEqual(df.plan_cards("b", "t_ep1")["t_dev1"]["status"], "ready")
        self.assertTrue(any("kanban_request_review" in msg for tid, msg in comments if tid == "t_dev1"), comments)

    def test_stop_after_holds_ready_epic_gates_final_acceptance_and_blocks_worker_spoofing(self) -> None:
        import os
        import sqlite3
        import subprocess
        from unittest import mock

        hermes = self.root / "hermes-epic-stop"
        board_dir = hermes / "kanban" / "boards" / "b"
        board_dir.mkdir(parents=True)
        old_data = os.environ.get("DEVFLOW_HERMES_DATA")
        os.environ["DEVFLOW_HERMES_DATA"] = str(hermes)
        self.addCleanup(
            lambda: os.environ.__setitem__("DEVFLOW_HERMES_DATA", old_data)
            if old_data is not None else os.environ.pop("DEVFLOW_HERMES_DATA", None)
        )
        df = load_devflow(self.root)

        repo_dir = self.root / "repo-spoof"
        remote = self.root / "origin-spoof.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
        for k, v in (("user.name", "Dev"), ("user.email", "dev@example.com")):
            subprocess.run(["git", "-C", str(repo_dir), "config", k, v], check=True, capture_output=True)
        (repo_dir / "app").mkdir(parents=True)
        (repo_dir / "app" / "x.py").write_text("X = 1\n", encoding="utf-8")
        check_sh = repo_dir / ".devflow" / "check.sh"
        check_sh.parent.mkdir(parents=True, exist_ok=True)
        check_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        check_sh.chmod(0o755)
        subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "push", "-u", "origin", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "fetch", "origin", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "checkout", "-b", "wt/t_dev1"], check=True, capture_output=True)
        (repo_dir / "app" / "x.py").write_text("X = 2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", "app/x.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "feat: x"], check=True, capture_output=True)

        db = board_dir / "kanban.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
                block_kind TEXT, assignee TEXT, workspace_kind TEXT,
                workspace_path TEXT, completion_contract TEXT,
                created_by TEXT, result TEXT, goal_mode INTEGER
            );
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, metadata TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
        """)
        epic_ship_body = (
            "Mode: deliver\nStop-After: ship\n"
            "## 目標\n停在出貨後等待人手動驗收\n\n"
            "## 驗收\n- AC-1 [api] 回傳 200\n"
            "  - S1 [smoke]\n    - Given ok\n    - When GET /ok\n    - Then 200\n"
        )
        arch_body = "## 目標\n架構\nEPIC: t_ep1\n"
        dev_body = f"## 目標\n改\n\n## 本卡完成\n{df.DONE_TEXT}\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/x.py\nEPIC: t_ep1\n"
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("t_ep1", "[EPIC] Stop Ship", epic_ship_body, "ready", None, "orchestrator", "dir", None, None, "user", None, 0))
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("t_ship1", "[SHIP] 出貨", "EPIC: t_ep1\n", "done", None, "devops", "dir", None, None, "orchestrator", None, 0))
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("t_arch1", "[ARCH] 設計", arch_body, "done", None, "architect", "dir", None, None, "orchestrator", None, 0))
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("t_dev1", "[DEV] 實作", dev_body, "review", None, "reviewer", "worktree", str(repo_dir), REPO, "orchestrator", None, 1))
        conn.execute("INSERT INTO task_links VALUES ('t_ship1', 't_ep1')")
        conn.execute("INSERT INTO task_links VALUES ('t_arch1', 't_dev1')")
        conn.execute("INSERT INTO task_runs VALUES (1, 't_arch1', 'done', ?)",
                     (json.dumps({"decisions": ["keep x.py"], "target_files": ["app/x.py", "app/y.py"]}),))
        conn.commit()
        conn.close()

        # 1. check_acceptance(review=False) fails closed while Stop-After != accept, while review=True remains allowed
        with mock.patch.object(df, "entry", return_value={"slug": "b", "repo": REPO, "path": str(repo_dir), "default_branch": "main"}):
            rep_final = df.check_acceptance("b", "t_ep1", [1], review=False, evidence=[], tmp=self.root / "t1")
            self.assertEqual(rep_final["criteria"], [])
            self.assertTrue(any("Stop-After=ship" in p and "拒絕最終驗收" in p for p in rep_final["problems"]), rep_final)
            with mock.patch.object(df, "pr_meta", return_value={"number": 1, "state": "OPEN", "headRefOid": "a" * 40, "body": "Covers: t_ep1/AC-1.S1"}), \
                 mock.patch.object(df, "branch_file", return_value=None), \
                 mock.patch.object(df, "workflow_runs", return_value=[]):
                rep_rev = df.check_acceptance("b", "t_ep1", [1], review=True, evidence=[], tmp=self.root / "t2")
                self.assertEqual(len(rep_rev["criteria"]), 1)

        # 2. hold_out_of_slice_cards NEVER holds fresh 1-card intake EPIC, DOES hold finished Stop-After: ship EPIC
        held_calls: list[tuple[str, str]] = []

        def fake_hold(run_fn, hpy, boards, brd, cid, reason, *, allow_review=False):
            held_calls.append((cid, reason))
            with sqlite3.connect(db) as c:
                prev = c.execute("SELECT status FROM tasks WHERE id = ?", (cid,)).fetchone()
                payload = {"kind": "dependency", "reason": reason}
                if allow_review and prev and prev[0] == "review":
                    payload["source_status"] = "review"
                    payload["resume_status"] = "review"
                c.execute("UPDATE tasks SET status = 'blocked', block_kind = 'dependency' WHERE id = ?", (cid,))
                c.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, 20)",
                    (cid, json.dumps(payload, ensure_ascii=False)),
                )
                c.commit()
            return True, False

        df.kanban = lambda b, cmd, tid, *args: ""
        fresh_cards = {"t_ep1": {"id": "t_ep1", "title": "[EPIC] fresh", "body": "Mode: deliver\nStop-After: ship\n", "status": "ready"}}
        with mock.patch.object(df.kanban_adapter, "hold_task_dependency", side_effect=fake_hold):
            self.assertFalse(df.hold_out_of_slice_cards("b", "t_ep1", fresh_cards, pipe_lib.parse_epic_pipeline(
                "Mode: deliver\nStop-After: ship\n", df.POLICY, epic_mode="deliver"
            )))
            self.assertEqual(held_calls, [])

            # Also verifies Stop-After: ship holds finished ready [EPIC] AND holds stray review [DEV] when review is inactive
            ci_only_cards = {
                "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "body": epic_ship_body, "status": "ready"},
                "t_dev1": {"id": "t_dev1", "title": "[DEV] 實作", "body": dev_body, "status": "review"},
            }
            df.hold_out_of_slice_cards("b", "t_ep1", ci_only_cards, pipe_lib.parse_epic_pipeline(
                "Mode: deliver\nStop-After: ci\n", df.POLICY, epic_mode="deliver"
            ))
            self.assertIn("t_dev1", [cid for cid, _ in held_calls])

            held_calls.clear()
            ship_cards = {
                "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "body": epic_ship_body, "status": "ready"},
                "t_ship1": {"id": "t_ship1", "title": "[SHIP] 出貨", "body": "EPIC: t_ep1\n", "status": "done"},
            }
            df.hold_out_of_slice_cards("b", "t_ep1", ship_cards, pipe_lib.parse_epic_pipeline(
                epic_ship_body, df.POLICY, epic_mode="deliver"
            ))
            self.assertEqual([cid for cid, _ in held_calls], ["t_ep1"])

        # Advancing Stop-After: ship -> accept releases held [EPIC]
        resumed_ids: list[str] = []
        with mock.patch.object(df, "resume", side_effect=lambda b, tid, note=None: resumed_ids.append(tid) or "ok"):
            ship_cards["t_ep1"]["status"] = "blocked"
            ship_cards["t_ep1"]["block_kind"] = "dependency"
            df.release_pipeline_stop_holds("b", "t_ep1", ship_cards, pipe_lib.parse_epic_pipeline(
                "Mode: deliver\nStart-At: accept\nStop-After: accept\n", df.POLICY, epic_mode="deliver"
            ))
            self.assertEqual(resumed_ids, ["t_ep1"])

            # Also releases held [EPIC] when Stop-After: repro advances to design with no child card covering design
            resumed_ids.clear()
            repro_only_cards = {
                "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "body": "Mode: deliver\nStart-At: design\nStop-After: design\n",
                          "status": "blocked", "block_kind": "dependency"},
                "t_res1": {"id": "t_res1", "title": "[RES] 調查", "body": "EPIC: t_ep1\n", "status": "done"},
            }
            with mock.patch.object(df, "_card_has_pipeline_stop_hold", side_effect=lambda b, e, tid, card=None: tid == "t_ep1"):
                df.release_pipeline_stop_holds("b", "t_ep1", repro_only_cards, pipe_lib.parse_epic_pipeline(
                    "Mode: deliver\nStart-At: design\nStop-After: design\n", df.POLICY, epic_mode="deliver"
                ))
            self.assertEqual(resumed_ids, ["t_ep1"])

        # 2b. PR #41 Findings #1 & #2: REAL hold_task_dependency(allow_review=True) + plan_cards() -> SQLite event lookup
        # Test both inline hermes_py SQL script (via stubbed hermes_cli) and SQLite fallback, then release on advance to review.
        import subprocess as _subproc
        stub_pkg = self.root / "stub_hermes_cli"
        (stub_pkg / "hermes_cli").mkdir(parents=True, exist_ok=True)
        (stub_pkg / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (stub_pkg / "hermes_cli" / "kanban_db_connect.py").write_text(
            "import sqlite3\ndef connect(p):\n    return sqlite3.connect(p)\n", encoding="utf-8",
        )
        (stub_pkg / "hermes_cli" / "kanban_db.py").write_text(
            "import contextlib, json, time\n"
            "@contextlib.contextmanager\n"
            "def write_txn(c):\n"
            "    with c:\n"
            "        yield\n"
            "def _append_event(c, tid, kind, payload):\n"
            "    c.execute('INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)',\n"
            "              (tid, kind, json.dumps(payload, ensure_ascii=False), int(time.time())))\n",
            encoding="utf-8",
        )

        with sqlite3.connect(db) as c:
            c.execute("DELETE FROM task_events WHERE task_id = 't_dev1'")
            c.execute("UPDATE tasks SET status = 'review', block_kind = NULL, assignee = 'reviewer' WHERE id = 't_dev1'")
            c.execute(
                "UPDATE tasks SET status = 'ready', block_kind = NULL, body = ? WHERE id = 't_ep1'",
                ("Mode: deliver\nStart-At: implement\nStop-After: ci\n## 目標\nx\n## 驗收\n- AC-1 [api] ok\n",),
            )
            c.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_dev1', 'review_requested', ?, 30)",
                (json.dumps({"implementer": "developer"}),),
            )
            c.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("t_dev_sub", "[DEV] 副線", dev_body, "review", None, "reviewer", "worktree", str(repo_dir), REPO, "orchestrator", None, 1),
            )
            c.execute("INSERT INTO task_links VALUES ('t_arch1', 't_dev_sub')")
            c.commit()

        # Path A: SQLite fallback via hold_out_of_slice_cards (HERMES_PY missing)
        ci_pipe_real = pipe_lib.parse_epic_pipeline("Mode: deliver\nStop-After: ci\n", df.POLICY, epic_mode="deliver")
        with mock.patch.object(df, "HERMES_PY", str(self.root / "missing-hermes-py")):
            df.hold_out_of_slice_cards("b", "t_ep1", {"t_ep1": df.plan_cards("b", "t_ep1")["t_ep1"], "t_dev1": df.plan_cards("b", "t_ep1")["t_dev1"]}, ci_pipe_real)

        # Path B: Real inline hermes_py script execution against stubbed hermes_cli -> SQLite
        def _run_with_stub_pkg(cmd, **kw):
            env = dict(os.environ)
            env["PYTHONPATH"] = str(stub_pkg) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            return _subproc.run(cmd, env=env, text=True, capture_output=True, check=kw.get("check", True))

        held_sub, fail_sub = df.kanban_adapter.hold_task_dependency(
            _run_with_stub_pkg, sys.executable, df.BOARDS, "b", "t_dev_sub",
            df.pipeline_stop_hold_reason("t_ep1", "ci"), allow_review=True,
        )
        self.assertTrue(held_sub and not fail_sub, (held_sub, fail_sub))

        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            for tid in ("t_dev1", "t_dev_sub"):
                trow = dict(c.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone())
                self.assertEqual(trow, {"status": "blocked", "block_kind": "dependency"}, tid)
                evrow = dict(c.execute("SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1", (tid,)).fetchone())
                epay = json.loads(evrow["payload"])
                self.assertEqual(epay.get("source_status"), "review", (tid, epay))
                self.assertEqual(epay.get("resume_status"), "review", (tid, epay))
            c.execute("DELETE FROM tasks WHERE id = 't_dev_sub'")
            c.execute("DELETE FROM task_links WHERE child_id = 't_dev_sub'")
            c.commit()

        # Reload through REAL plan_cards() (no `events` key on cards!) and verify release_pipeline_stop_holds keeps t_dev1 held under Stop-After: ci
        reloaded_cards = df.plan_cards("b", "t_ep1")
        self.assertNotIn("events", reloaded_cards["t_dev1"])
        with mock.patch.object(df, "resume", side_effect=AssertionError("review-held card must stay blocked while review step is inactive")):
            df.release_pipeline_stop_holds("b", "t_ep1", reloaded_cards, ci_pipe_real)
        self.assertEqual(reloaded_cards["t_dev1"]["status"], "blocked")

        # Advance EPIC to review via REAL cmd_epic_advance -> unblocks t_dev1 back into review
        def sqlite_kanban(board, cmd, *args):
            with sqlite3.connect(db) as kc:
                kc.row_factory = sqlite3.Row
                if cmd == "show":
                    tid = args[0]
                    t = dict(kc.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
                    evs = []
                    for r in kc.execute("SELECT id, kind, payload, created_at FROM task_events WHERE task_id = ? ORDER BY id ASC", (tid,)):
                        evs.append({"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"] or "{}"), "created_at": r["created_at"]})
                    return json.dumps({"task": t, "events": evs})
                if cmd == "edit":
                    tid, flag, val = args[0], args[1], args[2]
                    if flag == "--body":
                        kc.execute("UPDATE tasks SET body = ? WHERE id = ?", (val, tid))
                        kc.commit()
                    return ""
                if cmd == "comment":
                    tid, msg = args[0], args[1]
                    kc.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'comment', ?, 40)",
                               (tid, json.dumps({"body": msg}, ensure_ascii=False)))
                    kc.commit()
                    return ""
                if cmd == "assign":
                    tid, who = args[0], args[1]
                    kc.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (None if who == "none" else who, tid))
                    kc.commit()
                    return ""
                if cmd == "unblock":
                    tid = args[0]
                    rows = kc.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1", (tid,)).fetchall()
                    p_old = json.loads(rows[0]["payload"] or "{}") if rows else {}
                    dest = "review" if p_old.get("resume_status") == "review" else "ready"
                    kc.execute("UPDATE tasks SET status = ?, block_kind = NULL WHERE id = ?", (dest, tid))
                    kc.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'unblocked', ?, 41)",
                               (tid, json.dumps({"to": dest})))
                    kc.commit()
                    return ""
            return ""

        with mock.patch.object(df, "kanban", side_effect=sqlite_kanban), \
             contextlib.redirect_stdout(io.StringIO()):
            df.cmd_epic_advance(types.SimpleNamespace(board="b", epic="t_ep1", stop_after="review", to_end=False))
        with sqlite3.connect(db) as c:
            row_after = c.execute("SELECT status, block_kind, assignee FROM tasks WHERE id = 't_dev1'").fetchone()
            self.assertEqual(tuple(row_after), ("review", None, "reviewer"))

        # 3. Worker identity lock & --no-test spoofing prevention in cmd_checkpoint / cmd_pr_submit
        with sqlite3.connect(db) as c:
            c.execute(
                "UPDATE tasks SET body = ? WHERE id = 't_ep1'",
                ("Mode: deliver\nStart-At: implement\nStop-After: implement\n## 目標\nx\n## 驗收\n- AC-1 [cli] ok\n  - S1\n    - Given a\n    - When b\n    - Then c\n",),
            )
            c.commit()
        old_kt = os.environ.get("HERMES_KANBAN_TASK")
        os.environ["HERMES_KANBAN_TASK"] = "t_other_card"
        try:
            with mock.patch.object(df, "entry", return_value={"slug": "b", "repo": REPO, "path": str(repo_dir), "default_branch": "main"}):
                buf_m1 = io.StringIO()
                with contextlib.redirect_stdout(buf_m1), self.assertRaises(SystemExit) as cm1:
                    df.cmd_checkpoint(types.SimpleNamespace(
                        slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                        no_test=False, test=[], summary="",
                    ))
                self.assertEqual(cm1.exception.code, 1)
                self.assertEqual(json.loads(buf_m1.getvalue())["stage"], "worker_task_mismatch")

                buf_m2 = io.StringIO()
                with contextlib.redirect_stdout(buf_m2), self.assertRaises(SystemExit) as cm2:
                    df.cmd_pr_submit(types.SimpleNamespace(
                        slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                        pr_title="feat: x", pr_body="Kanban: t_dev1", pr_body_file=None,
                        no_test=False, allow_dirty=False, dry_run=True,
                    ))
                self.assertEqual(cm2.exception.code, 1)
                self.assertEqual(json.loads(buf_m2.getvalue())["stage"], "worker_task_mismatch")
        finally:
            if old_kt is not None:
                os.environ["HERMES_KANBAN_TASK"] = old_kt
            else:
                os.environ.pop("HERMES_KANBAN_TASK", None)

        # --no-test + --test "fake" rejected when repo defines .devflow/check.sh (even outside worker env)
        with mock.patch.object(df, "entry", return_value={"slug": "b", "repo": REPO, "path": str(repo_dir), "default_branch": "main"}):
            buf_nt = io.StringIO()
            with contextlib.redirect_stdout(buf_nt), self.assertRaises(SystemExit) as cm_nt:
                df.cmd_checkpoint(types.SimpleNamespace(
                    slug="b", task="t_dev1", dir=str(repo_dir), base=None,
                    no_test=True, test=["fake -> pass"], summary="spoof",
                ))
            self.assertEqual(cm_nt.exception.code, 1)
            self.assertEqual(json.loads(buf_nt.getvalue())["stage"], "test_required")

        # 4. cmd_check_card and _validate_edit_op reject invalid [EPIC] headers & [DEV] narrowing away [ARCH] target_files
        buf_cc = io.StringIO()
        with contextlib.redirect_stdout(buf_cc), self.assertRaises(SystemExit) as cm_cc:
            df.cmd_check_card(types.SimpleNamespace(
                title="[EPIC] Bad Pipe",
                body="Mode: deliver\nStop-After: bogus_step\n## 目標\nx\n",
                body_file=None, assignee=None, workspace=None, board=None, parent=[],
            ))
        self.assertEqual(cm_cc.exception.code, 1)
        self.assertTrue(any("Stop-After" in f for f in json.loads(buf_cc.getvalue())["findings"]))

        epic_edit_errs = df._validate_edit_op("b", {"op": "edit", "task": "t_ep1", "body": "Mode: deliver\nStart-At: ship\nStop-After: repro\n"})
        self.assertTrue(any("Start-At" in e for e in epic_edit_errs), epic_edit_errs)
        dev_edit_errs = df._validate_edit_op(
            "b", {"op": "edit", "task": "t_dev1", "body": dev_body},
        )
        self.assertTrue(any("app/y.py" in e for e in dev_edit_errs), dev_edit_errs)


if __name__ == "__main__":
    unittest.main()
