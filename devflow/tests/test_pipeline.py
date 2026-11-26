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
        self.assertEqual(set(schemas), {"repro", "design", "implement", "ship"})
        self.assertIn("target_files", schemas["design"]["required"])

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
        cards_running["t_dev1"]["status"] = "done"
        self.assertTrue(pipe_lib.slice_stop_reached(cards_running, "t_ep1", s1, self.policy))


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


if __name__ == "__main__":
    unittest.main()
