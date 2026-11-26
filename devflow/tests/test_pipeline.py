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


if __name__ == "__main__":
    unittest.main()
