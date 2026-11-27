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

    def _run_check_card(self, **kwargs) -> tuple[int, dict]:
        buf = io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(buf):
                self.df.cmd_check_card(types.SimpleNamespace(body_file=None, parent=[], **kwargs))
        except SystemExit as exc:
            code = int(exc.code or 0)
        return code, json.loads(buf.getvalue())

    def _full_dev_plan(self, header: str) -> dict[str, dict]:
        ep = self._epic(header, with_scenarios=True)
        dev_bundle = self._dev("t_dev1", acs="AC-1, AC-2", parents=[], with_ship=True)
        ep["parents"] = ["t_ship1"]
        return {"t_ep1": ep, **dev_bundle}

    def test_EPIC_authorization_fails_closed_on_conflicts_comments_and_invalid_values(self) -> None:
        # 1. Inline # comments on Allow-Write: false and Mode: investigate are honored rather than ignored.
        found_comment_dev = self.df.plan_rules("b", "t_ep1", self._full_dev_plan("Allow-Write: false # read-only\n"))
        self.assertTrue(any("Allow-Write: false" in f and "[DEV]" in f for f in found_comment_dev), found_comment_dev)
        ep_comment_ro = self._epic("Allow-Write: false # read-only\n", with_scenarios=False)
        res_ro = self._res("t_res1", assignee="devops", ws="dir", status="ready",
                           body="## 目標\n取證\n覆蓋：AC-1, AC-2\nEPIC: t_ep1\n")
        ep_comment_ro["parents"] = ["t_res1"]
        self.assertEqual(self.df.plan_rules("b", "t_ep1", {"t_ep1": ep_comment_ro, "t_res1": res_ro}), [])

        found_comment_mode = self.df.plan_rules("b", "t_ep1", self._full_dev_plan("Mode: investigate # read-only\n"))
        self.assertTrue(any("Mode: investigate" in f and "[DEV]" in f for f in found_comment_mode), found_comment_mode)

        # 2. Conflicting or malformed Mode / Allow-Write declarations NEVER restore write permission in plan_rules.
        bad_headers = [
            ("Allow-Write: true\nAllow-Write: false\n", "Allow-Write"),
            ("Allow-Write: true # one\nAllow-Write: false # two\n", "Allow-Write"),
            ("Allow-Write: maybe\n", "Allow-Write"),
            ("Allow-Write: false extra-tokens\n", "Allow-Write"),
            ("Allow-Write:   \n", "Allow-Write"),
            ("Mode: deliver\nMode: investigate\n", "Mode"),
            ("Mode: bogus\n", "Mode"),
            ("Mode: investigate\nAllow-Write: true\n", "衝突"),
        ]
        for header, expected_token in bad_headers:
            found = self.df.plan_rules("b", "t_ep1", self._full_dev_plan(header))
            self.assertTrue(any(expected_token in f for f in found), (header, found))
            self.assertTrue(any("禁止建立 [DEV] 卡" in f for f in found), (header, found))

        # 3. Real temporary SQLite board: cmd_check_card propagates referenced EPIC auth errors & blocks DEV.
        db_dir = Path(self.root) / "boards" / "b"
        db_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_dir / "kanban.db")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT)")
        conn.commit()
        old_boards = self.df.BOARDS
        self.df.BOARDS = Path(self.root) / "boards"
        self.addCleanup(setattr, self.df, "BOARDS", old_boards)
        self.addCleanup(conn.close)

        dev_body = (
            f"## 目標\n改碼\n\n## 本卡完成\n{self.df.DONE_TEXT}\n\n"
            "## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n"
        )
        res_body = "## 目標\n取證\nEPIC: t_ep1\n"

        for header, expected_token in bad_headers:
            ep_body = self._epic(header, with_scenarios=True)["body"]
            conn.execute("DELETE FROM tasks WHERE id = 't_ep1'")
            conn.execute("INSERT INTO tasks VALUES (?, ?)", ("t_ep1", ep_body))
            conn.commit()

            code_dev, res_dev = self._run_check_card(
                board="b", title="[DEV] 嘗試偷渡寫卡", body=dev_body, assignee="developer", workspace="worktree"
            )
            self.assertEqual(code_dev, 1, (header, res_dev))
            self.assertFalse(res_dev["ok"], (header, res_dev))
            self.assertTrue(any("授權設定無效" in f for f in res_dev["findings"]), (header, res_dev))
            self.assertTrue(any(expected_token in f for f in res_dev["findings"]), (header, res_dev))
            self.assertTrue(any("禁止建立 [DEV] 卡" in f for f in res_dev["findings"]), (header, res_dev))

            # Even non-writing [RES] card surfaces referenced EPIC authorization errors in check card.
            code_res, res_check = self._run_check_card(
                board="b", title="[RES] 附掛問題 EPIC", body=res_body, assignee="devops", workspace="dir"
            )
            self.assertEqual(code_res, 1, (header, res_check))
            self.assertFalse(res_check["ok"], (header, res_check))
            self.assertTrue(any("參考的 t_ep1 授權設定無效" in f for f in res_check["findings"]), (header, res_check))

        # Allow-Write: false # read-only in SQLite DB blocks DEV and allows paired RES.
        conn.execute("DELETE FROM tasks WHERE id = 't_ep1'")
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?)",
            ("t_ep1", self._epic("Allow-Write: false # read-only\n", with_scenarios=False)["body"]),
        )
        conn.commit()
        code_dev_ro, res_dev_ro = self._run_check_card(
            board="b", title="[DEV] 嘗試繞過註解", body=dev_body, assignee="developer", workspace="worktree"
        )
        self.assertEqual(code_dev_ro, 1, res_dev_ro)
        self.assertTrue(any("Allow-Write: false" in f for f in res_dev_ro["findings"]), res_dev_ro)

        code_res_ro, res_check_ro = self._run_check_card(
            board="b", title="[RES] 唯讀查證", body=res_body, assignee="devops", workspace="dir"
        )
        self.assertEqual(code_res_ro, 0, res_check_ro)
        self.assertTrue(res_check_ro["ok"], res_check_ro)

        # Valid EPIC header works and check card on invalid EPIC header itself still fails cleanly without duplication.
        code_ep_bad, res_ep_bad = self._run_check_card(
            board="b",
            title="[EPIC] 衝突宣告",
            body=self._epic("Allow-Write: true\nAllow-Write: false\n", with_scenarios=True)["body"],
            assignee="orchestrator",
            workspace="dir",
        )
        self.assertEqual(code_ep_bad, 1, res_ep_bad)
        self.assertTrue(any("Allow-Write" in f for f in res_ep_bad["findings"]), res_ep_bad)

    def test_tick_board_skips_competing_plan_adj_when_waking_epic_parents_are_all_done(self) -> None:
        ep_done_parent = self._epic("", with_scenarios=False)
        res_done = self._res("t_res1", assignee="devops", ws="dir", status="done",
                             body="## 目標\n已取證\n覆蓋：AC-1\nEPIC: t_ep1\n")
        ep_done_parent["parents"] = ["t_res1"]
        cards_waking = {"t_ep1": ep_done_parent, "t_res1": res_done}

        # Direct plan_rules still strictly catches missing AC coverage once discovery ended.
        direct = self.df.plan_rules("b", "t_ep1", cards_waking)
        self.assertTrue(any("AC-2 沒有任何卡片負責" in f for f in direct), direct)

        orig_kanban = self.df.kanban
        orig_plan_cards = self.df.plan_cards
        orig_tick_prs = self.df.tick_prs
        orig_release_held = self.df.release_held
        orig_release_adv = self.df.release_rate_limited_advisors
        orig_route_q = self.df.route_questions
        orig_run = self.df.run
        orig_repair = self.df.repair_bodies
        self.addCleanup(setattr, self.df, "kanban", orig_kanban)
        self.addCleanup(setattr, self.df, "plan_cards", orig_plan_cards)
        self.addCleanup(setattr, self.df, "tick_prs", orig_tick_prs)
        self.addCleanup(setattr, self.df, "release_held", orig_release_held)
        self.addCleanup(setattr, self.df, "release_rate_limited_advisors", orig_release_adv)
        self.addCleanup(setattr, self.df, "route_questions", orig_route_q)
        self.addCleanup(setattr, self.df, "run", orig_run)
        self.addCleanup(setattr, self.df, "repair_bodies", orig_repair)

        active_plan = cards_waking
        epic_list_view = {**ep_done_parent}
        created: list[list[str]] = []

        def fake_kanban(board: str, cmd: str, *args: str) -> str:
            if cmd == "list":
                return json.dumps([epic_list_view, *[c for k, c in active_plan.items() if k != "t_ep1"]])
            if cmd == "show":
                tid = args[0]
                t = active_plan.get(tid) or epic_list_view
                return json.dumps({"task": t, "parents": t.get("parents") or [], "comments": []})
            return ""

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and "create" in cmd:
                created.append(cmd)
                return types.SimpleNamespace(returncode=0, stdout=f"Created t_adj{len(created):05d}\n", stderr="")
            return orig_run(cmd, **kw)

        self.df.kanban = fake_kanban
        self.df.plan_cards = lambda board, eid: active_plan
        self.df.tick_prs = lambda *a: False
        self.df.release_held = lambda *a: None
        self.df.release_rate_limited_advisors = lambda *a: False
        self.df.route_questions = lambda *a: False
        self.df.repair_bodies = lambda *a: []
        self.df.run = fake_run

        st: dict = {}
        # 1. All parents done -> EPIC is waking up -> tick_board must NOT spawn a competing [ADJ].
        self.assertFalse(self.df.tick_board("b", st))
        self.assertEqual(created, [])

        # 2. Stale list snapshot saw status='todo', but fresh_cards already transitioned EPIC to 'ready'
        #    with an open parent -> tick_board must also skip spawning a competing [ADJ].
        ep_live_ready = {**self._epic("", with_scenarios=True), "status": "ready", "parents": ["t_dev1"]}
        dev_no_ship = self._dev("t_dev1", acs="AC-1, AC-2", with_ship=False)
        active_plan = {"t_ep1": ep_live_ready, **dev_no_ship}
        self.assertFalse(self.df.tick_board("b", {}))
        self.assertEqual(created, [])

        # 3. Positive control: sleeping EPIC (todo) with an open parent violating rules ([DEV] missing [SHIP])
        #    still triggers check_plan and opens one [ADJ].
        ep_sleeping_bad = {**self._epic("", with_scenarios=True), "status": "todo", "parents": ["t_dev1"]}
        active_plan = {"t_ep1": ep_sleeping_bad, **dev_no_ship}
        self.assertFalse(self.df.tick_board("b", {}))
        self.assertEqual(len(created), 1)
        self.assertIn("[ADJ] 計畫規則修正 t_ep1", created[0])


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
