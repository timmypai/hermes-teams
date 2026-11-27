"""Tests for unified constraint-based DAG boundaries, paired card executors, and obsolete hook cleanup."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

from test_judgment import KIT, REPO, load_devflow

if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))


def _make_board_root(prefix: str, repo_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copy(KIT / "roles.json", root / "roles.json")
    (root / "state").mkdir()
    (root / "state" / "repos.json").write_text(
        json.dumps({
            "b": {
                "slug": "b",
                "repo": REPO,
                "path": str(repo_dir),
                "port": 18100,
                "default_branch": "main",
                "contract": True,
            }
        }),
        encoding="utf-8",
    )
    return root


class ConstraintDagBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_dir = Path(tempfile.mkdtemp(prefix="devflow-cdag-repo-"))
        self.addCleanup(shutil.rmtree, self.repo_dir, ignore_errors=True)
        (self.repo_dir / ".devflow").mkdir()
        (self.repo_dir / ".devflow" / "config.env").write_text(
            "DEVFLOW_DEPLOY=true\nDEVFLOW_MERGE_METHOD=merge\n", encoding="utf-8"
        )
        self.root = _make_board_root("devflow-cdag-root-", self.repo_dir)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.df = load_devflow(self.root)

    def _epic(self, header: str = "", *, with_scenarios: bool = False) -> dict:
        s1 = (
            "  - S1\n    - Given pre\n    - When act\n    - Then ok1\n"
            if with_scenarios else ""
        )
        s2 = (
            "  - S2\n    - Given pre2\n    - When act2\n    - Then ok2\n"
            if with_scenarios else ""
        )
        body = (
            f"{header}## 目標\n修復分享 PPT 檔案時語音中止\n\n"
            "## 驗收\n"
            f"- AC-1 [lib] 重現停音真因\n{s1}"
            f"- AC-2 [lib] 修正並保留原有語音輸出\n{s2}"
        )
        return {
            "id": "t_ep1",
            "title": "[EPIC] PPT Live",
            "status": "todo",
            "assignee": "orchestrator",
            "workspace_kind": "dir",
            "parents": [],
            "children": [],
            "goal_mode": False,
            "body": body,
        }

    def _res(self, tid: str = "t_res1", *, assignee: str = "researcher", ws: str = "scratch",
             status: str = "ready", body: str = "## 目標\n查證\nEPIC: t_ep1\n") -> dict:
        return {
            "id": tid,
            "title": "[RES] 查證停音原因",
            "status": status,
            "assignee": assignee,
            "workspace_kind": ws,
            "parents": [],
            "children": ["t_ep1"],
            "goal_mode": False,
            "body": body,
        }

    def _dev(self, tid: str = "t_dev1", *, acs: str = "AC-1, AC-2", parents: list[str] | None = None,
             with_ship: bool = True) -> dict[str, dict]:
        body = (
            f"## 目標\n修補送音判斷\n\n## 本卡完成\n{self.df.DONE_TEXT}\n\n"
            f"## 驗收\n覆蓋：{acs}\nAllowed-Scope: app/**\nEPIC: t_ep1\n"
        )
        children = ["t_ship1"] if with_ship else ["t_ep1"]
        out: dict[str, dict] = {
            tid: {
                "id": tid,
                "title": "[DEV] 修正 PPT Live 停音",
                "status": "ready",
                "assignee": "developer",
                "workspace_kind": "worktree",
                "parents": parents or [],
                "children": children,
                "goal_mode": True,
                "completion_contract": REPO,
                "body": body,
            }
        }
        if with_ship:
            out["t_ship1"] = {
                "id": "t_ship1",
                "title": "[SHIP] 部署驗證",
                "status": "todo",
                "assignee": "devops",
                "workspace_kind": "dir",
                "parents": [tid],
                "children": ["t_ep1"],
                "goal_mode": False,
                "body": "## 目標\n出貨\nEPIC: t_ep1\n",
            }
        return out

    def test_discovery_phase_allows_parallel_res_without_premature_ac_mapping_or_ship(self) -> None:
        for header in ("", "Mode: deliver\n"):
            ep = self._epic(header, with_scenarios=False)
            r1 = self._res("t_res1", assignee="researcher", ws="scratch", status="running")
            r2 = self._res("t_res2", assignee="devops", ws="dir", status="ready")
            ep["parents"] = ["t_res1", "t_res2"]
            cards = {"t_ep1": ep, "t_res1": r1, "t_res2": r2}
            self.assertEqual(self.df.plan_rules("b", "t_ep1", cards), [], header)

    def test_paired_executor_rejects_mismatched_assignee_workspace_in_plan_and_check_card(self) -> None:
        ep = self._epic("")
        for bad_asg, bad_ws in (("researcher", "dir"), ("devops", "scratch"), ("reviewer", "dir")):
            r = self._res("t_res1", assignee=bad_asg, ws=bad_ws, status="ready")
            ep_copy = {**ep, "parents": ["t_res1"]}
            found = self.df.plan_rules("b", "t_ep1", {"t_ep1": ep_copy, "t_res1": r})
            self.assertTrue(found, (bad_asg, bad_ws))

        # Live check card matches paired policy: devops/dir [RES] passes, researcher/dir [RES] fails.
        ok_buf = io.StringIO()
        with contextlib.redirect_stdout(ok_buf):
            self.df.cmd_check_card(types.SimpleNamespace(
                board="b",
                title="[RES] 查線上去識別快照",
                body="## 目標\n唯讀查快照\nEPIC: t_ep1\n",
                body_file=None,
                assignee="devops",
                workspace="dir",
                parent=[],
            ))
        self.assertTrue(json.loads(ok_buf.getvalue())["ok"])

        bad_buf = io.StringIO()
        with contextlib.redirect_stdout(bad_buf), self.assertRaises(SystemExit) as ctx:
            self.df.cmd_check_card(types.SimpleNamespace(
                board="b",
                title="[RES] 錯誤指派",
                body="## 目標\n查快照\nEPIC: t_ep1\n",
                body_file=None,
                assignee="researcher",
                workspace="dir",
                parent=[],
            ))
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(json.loads(bad_buf.getvalue())["ok"])

    def test_commitment_boundary_immediately_catches_missing_ac_coverage_before_dev_runs(self) -> None:
        ep = self._epic("", with_scenarios=True)
        res_done = self._res("t_res1", assignee="devops", ws="dir", status="done")
        dev_bundle = self._dev("t_dev1", acs="AC-1", parents=["t_res1"], with_ship=True)
        ep["parents"] = ["t_ship1"]
        cards = {"t_ep1": ep, "t_res1": res_done, **dev_bundle}
        found = self.df.plan_rules("b", "t_ep1", cards)
        self.assertTrue(any("AC-2 沒有任何卡片負責" in f for f in found), found)

        # Adding AC-2 coverage satisfies commitment rules cleanly.
        dev_full = self._dev("t_dev1", acs="AC-1, AC-2", parents=["t_res1"], with_ship=True)
        self.assertEqual(self.df.plan_rules("b", "t_ep1", {"t_ep1": ep, "t_res1": res_done, **dev_full}), [])

    def test_discovery_finished_without_delivery_or_full_ac_coverage_fails_closed(self) -> None:
        ep = self._epic("", with_scenarios=False)
        res_done = self._res("t_res1", assignee="devops", ws="dir", status="done",
                             body="## 目標\n已完成\nEPIC: t_ep1\n")
        ep["parents"] = ["t_res1"]
        found = self.df.plan_rules("b", "t_ep1", {"t_ep1": ep, "t_res1": res_done})
        self.assertTrue(any("AC-1 沒有任何卡片負責" in f for f in found), found)
        self.assertTrue(any("AC-2 沒有任何卡片負責" in f for f in found), found)

    def test_allow_write_ceiling_validation_and_enforcement(self) -> None:
        # Allow-Write: false allows pure read-only RES covering ACs without scenarios or SHIP,
        # and refuses [DEV]/[OPS] in both plan_rules and check card (even without Mode: investigate).
        ep_ro = self._epic("Allow-Write: false\n", with_scenarios=False)
        res_ro = self._res("t_res1", assignee="devops", ws="dir", status="ready",
                           body="## 目標\n取證\n覆蓋：AC-1, AC-2\nEPIC: t_ep1\n")
        ep_ro["parents"] = ["t_res1"]
        self.assertEqual(self.df.plan_rules("b", "t_ep1", {"t_ep1": ep_ro, "t_res1": res_ro}), [])

        dev_bundle = self._dev("t_dev1", acs="AC-1, AC-2", parents=["t_res1"], with_ship=False)
        ep_with_dev = {**ep_ro, "parents": ["t_dev1"]}
        found_dev = self.df.plan_rules("b", "t_ep1", {"t_ep1": ep_with_dev, "t_res1": res_ro, **dev_bundle})
        self.assertTrue(any("Allow-Write: false" in f and "[DEV]" in f for f in found_dev), found_dev)

        # Invalid Allow-Write value or contradiction with Mode: investigate fails closed.
        ep_bad = self._epic("Allow-Write: maybe\n", with_scenarios=True)
        found_bad = self.df.plan_rules("b", "t_ep1", {"t_ep1": {**ep_bad, "parents": ["t_res1"]}, "t_res1": res_ro})
        self.assertTrue(any("Allow-Write 'maybe' 無效" in f for f in found_bad), found_bad)

        ep_conflict = self._epic("Mode: investigate\nAllow-Write: true\n", with_scenarios=False)
        found_conflict = self.df.plan_rules(
            "b", "t_ep1", {"t_ep1": {**ep_conflict, "parents": ["t_res1"]}, "t_res1": res_ro}
        )
        self.assertTrue(any("衝突" in f and "Allow-Write: true" in f for f in found_conflict), found_conflict)

        # check card also enforces Allow-Write: false when EPIC is in the board DB.
        db_dir = Path(self.root) / "boards" / "b"
        db_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_dir / "kanban.db")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO tasks VALUES (?, ?)", ("t_ep1", ep_ro["body"]))
        conn.commit()
        conn.close()
        old_boards = self.df.BOARDS
        self.df.BOARDS = Path(self.root) / "boards"
        self.addCleanup(setattr, self.df, "BOARDS", old_boards)

        ro_buf = io.StringIO()
        with contextlib.redirect_stdout(ro_buf), self.assertRaises(SystemExit) as ctx:
            self.df.cmd_check_card(types.SimpleNamespace(
                board="b",
                title="[DEV] 偷開改碼卡",
                body=f"## 目標\n改碼\n\n## 本卡完成\n{self.df.DONE_TEXT}\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n",
                body_file=None,
                assignee="developer",
                workspace="worktree",
                parent=[],
            ))
        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(any("Allow-Write: false" in f for f in json.loads(ro_buf.getvalue())["findings"]))


class ObsoleteHooksCleanup(unittest.TestCase):
    def test_retire_obsolete_hooks_removes_stage_card_and_preserves_dispatch_tick(self) -> None:
        from lib import policy as policy_lib

        roles_data = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        spec = policy_lib.obsolete_hooks(roles_data)
        self.assertEqual(
            spec,
            [{"event": "pre_tool_call", "command": "/opt/data/devflow/bin/devflow stage-card"}],
        )

        home = Path(tempfile.mkdtemp(prefix="devflow-hook-clean-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / "config.yaml").write_text(
            "hooks_auto_accept: true\n"
            "hooks:\n"
            "  on_kanban_dispatch_tick:\n"
            "    - command: /opt/data/devflow/bin/devflow tick --hook\n"
            "      timeout: 30\n"
            "  pre_tool_call:\n"
            "    - command: /opt/data/devflow/bin/devflow stage-card\n"
            "      matcher: kanban_create\n"
            "      timeout: 5\n"
            "      fail_closed: true\n"
            "tts:\n"
            "  provider: openai\n",
            encoding="utf-8",
        )
        (home / "shell-hooks-allowlist.json").write_text(
            json.dumps({
                "approvals": [
                    {"event": "on_kanban_dispatch_tick", "command": "/opt/data/devflow/bin/devflow tick --hook"},
                    {"event": "pre_tool_call", "command": "/opt/data/devflow/bin/devflow stage-card"},
                ]
            }),
            encoding="utf-8",
        )
        backed: list[Path] = []
        logs: list[str] = []
        removed = policy_lib.retire_obsolete_hooks(
            home, spec, backup_fn=backed.append, log_fn=logs.append
        )
        self.assertEqual(removed, 2)
        cfg_after = (home / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("stage-card", cfg_after)
        self.assertIn("/opt/data/devflow/bin/devflow tick --hook", cfg_after)
        self.assertIn("provider: openai", cfg_after)
        allow_after = json.loads((home / "shell-hooks-allowlist.json").read_text(encoding="utf-8"))
        self.assertEqual(len(allow_after["approvals"]), 1)
        self.assertEqual(allow_after["approvals"][0]["command"], "/opt/data/devflow/bin/devflow tick --hook")
        self.assertEqual(len(backed), 2)
        self.assertEqual(len(logs), 2)

        # Idempotent on second run
        self.assertEqual(policy_lib.retire_obsolete_hooks(home, spec), 0)

    def test_retire_obsolete_hooks_supports_yaml_cls_path(self) -> None:
        from lib import policy as policy_lib

        class FakeYaml:
            preserve_quotes = False

            def load(self, text: str):
                return json.loads(text)

            def dump(self, data: dict, fh) -> None:
                json.dump(data, fh)

        home = Path(tempfile.mkdtemp(prefix="devflow-hook-yamlcls-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / "config.yaml").write_text(
            json.dumps({
                "hooks": {
                    "on_kanban_dispatch_tick": [{"command": "/opt/data/devflow/bin/devflow tick --hook"}],
                    "pre_tool_call": [{"command": "/opt/data/devflow/bin/devflow stage-card", "fail_closed": True}],
                }
            }),
            encoding="utf-8",
        )
        removed = policy_lib.retire_obsolete_hooks(
            home,
            [{"event": "pre_tool_call", "command": "/opt/data/devflow/bin/devflow stage-card"}],
            yaml_cls=FakeYaml,
        )
        self.assertEqual(removed, 1)
        parsed = json.loads((home / "config.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("pre_tool_call", parsed["hooks"])
        self.assertIn("on_kanban_dispatch_tick", parsed["hooks"])


if __name__ == "__main__":
    unittest.main()
