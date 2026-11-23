"""devflow check plan on [DEV]/[OPS] cards: goal mode, the fixed `## 本卡完成`, and which cards are in a plan.

`kanban show --json` omits goal_mode, so plan_cards reads it from the board's kanban.db; the board
is a real SQLite file with Hermes' `tasks.goal_mode` and `body` columns and `show` is an in-memory fake.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from test_judgment import KIT, load_devflow

EPIC, DEV, SHIP = "t_epic", "t_dev", "t_ship"
ASSIGNEE = {"EPIC": "orchestrator", "ADJ": "orchestrator", "DEV": "developer", "SHIP": "devops"}
WORKSPACE = {"EPIC": "dir", "ADJ": "dir", "DEV": "worktree", "SHIP": "dir"}


class Plan(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        shutil.copy(KIT / "roles.json", root / "roles.json")
        (root / "state").mkdir()
        self.repo = root / "repo"
        (root / "state" / "repos.json").write_text(json.dumps(
            {"b": {"repo": "o/r", "path": str(self.repo), "contract": False}}))
        self.db = root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        self.db.parent.mkdir(parents=True)
        os.environ["DEVFLOW_HERMES_DATA"] = str(root / "data")
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA")
        self.df = load_devflow(root)
        self.done = f"## 本卡完成\n{self.df.DONE_TEXT}\n"
        # id -> (title, body, parents, children, workspace_path)
        self.cards = {
            EPIC: ("[EPIC] e", "## 驗收\n- AC-1 x", [SHIP], [], None),
            DEV: ("[DEV] d", f"## 目標\nd\n{self.done}## 驗收\n覆蓋：AC-1\nEPIC: {EPIC}", [], [SHIP], None),
            SHIP: ("[SHIP] s", f"EPIC: {EPIC}", [DEV], [EPIC], None),
        }
        self.df.kanban = self.show
        self.creators: dict[str, str] = {}   # id -> Hermes created_by
        self.statuses: dict[str, str] = {}   # id -> status (default todo)

    def show(self, board, cmd, tid, *args):
        title, body, parents, children, path = self.cards[tid]
        kind = title[1:title.index("]")]
        return json.dumps({"task": {"id": tid, "title": title, "body": body, "status": self.statuses.get(tid, "todo"),
                                    "assignee": ASSIGNEE[kind],
                                    "workspace_kind": WORKSPACE[kind], "workspace_path": path, "completion_contract": None,
                                    "created_by": self.creators.get(tid, "orchestrator")},
                           "parents": parents, "children": children, "runs": []})

    def board(self, goal: set[str]) -> None:
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT, goal_mode INTEGER NOT NULL DEFAULT 0)")
            c.executemany("INSERT INTO tasks VALUES (?, ?, ?)", [(t, v[1], int(t in goal)) for t, v in self.cards.items()])

    def findings(self, about: str) -> list[str]:
        cards = self.df.plan_cards("b", EPIC)
        return [f for f in self.df.plan_rules("b", EPIC, cards) if about in f]

    def test_dev_card_without_goal_mode_is_a_finding(self):
        self.board(set())
        self.assertEqual([f.split(":")[0] for f in self.findings("goal_mode")], [DEV])

    def test_dev_card_in_goal_mode_with_the_fixed_done_text_passes(self):
        self.board({DEV})
        self.assertEqual(self.findings("goal_mode") + self.findings("本卡完成"), [])

    def test_a_done_condition_that_needs_the_handoff_is_a_finding_and_tick_restores_the_text(self):
        """teams-transcription t_6d121ad4: the judge refused every handoff while this section asked for one."""
        body = ("## 目標\n續作 PR #1\n## 本卡完成\n### 作者階段\n作者以 kanban_request_review 交 reviewer，交接成功以原生讀回為準。\n"
                "### Reviewer 階段\nreviewer 批准後才完成。\n## 驗收\n覆蓋：AC-1\n- AC-1 x\nEPIC: t_epic")
        self.cards[DEV] = ("[DEV] d", body, [], [SHIP], None)
        self.board({DEV})
        self.assertEqual([f.split(":")[0] for f in self.findings("本卡完成")], [DEV])
        fixed = self.df.repaired_done(body)
        self.assertIsNone(self.df.done_problem("[DEV] d", fixed))
        self.assertTrue(fixed.startswith(f"## 目標\n續作 PR #1\n\n{self.done}"))
        self.assertIn("## 驗收\n覆蓋：AC-1\n- AC-1 x\nEPIC: t_epic", fixed)
        self.assertNotIn("reviewer 批准", fixed)

    def test_the_old_done_text_is_repaired_on_pending_cards(self):
        """Its 「交 review 是完成後的下一步」 read as "complete the card, [SHIP] reviews": t_48d094f6 and
        t_1463bbcb closed with no review (2026-11-19)."""
        old = "PR 已開、exact-head CI 綠。交 review 是完成後的下一步，不是完成條件；合併與部署由 [SHIP] 負責。"
        body = f"## 目標\nd\n## 本卡完成\n{old}\n## 驗收\n覆蓋：AC-1\nEPIC: {EPIC}"
        self.assertIsNotNone(self.df.done_problem("[OPS] o", body))
        fixed = self.df.repaired_done(body)
        self.assertNotIn("完成後的下一步", fixed)
        self.assertIn("審查在本卡自己的 review 欄", fixed)

    def test_repair_never_folds_the_following_lines_into_the_done_section(self):
        for body in ("EPIC: t_e01\n## 本卡完成\n已交 review\n## 驗收\n覆蓋：AC-1",   # no `## 目標`
                     "EPIC: t_e01\n覆蓋：AC-1",                                     # no heading at all
                     "## 驗收\n- AC-1 x\n## 目標\n做 x\n## 提示\n- y"):              # 目標 not first
            with self.subTest(body=body):
                fixed = self.df.repaired_done(body)
                self.assertIsNone(self.df.done_problem("[DEV] d", fixed), fixed)
                self.assertNotIn("已交 review", fixed)
                for line in body.splitlines():
                    if "本卡完成" not in line and "已交 review" not in line:
                        self.assertIn(line, fixed)

    def test_tick_drops_acs_copied_from_the_epic_and_keeps_the_ids(self):
        epic = "## 目標\ng\n## 驗收\n- AC-1 [cli] 部署成功且健康檢查通過\n  - S1 x\n- AC-2 [ui] 看到登入畫面"
        own = "- 本卡另需：診斷輸出保留錯誤原文"
        dev = (f"## 目標\nd\n{self.done}## 驗收\n- AC-1 [cli] 部署成功且健康檢查通過\n"
               f"- **AC-2**：[ui] 看到登入畫面\n- AC-2 [ui] 看到登入畫面，另加條件\n{own}\nEPIC: {EPIC}")
        self.cards[EPIC] = ("[EPIC] e", epic, [SHIP], [], None)
        self.cards[DEV] = ("[DEV] d", dev, [], [SHIP], None)
        self.board({DEV})
        edits = []
        self.df.kanban = lambda b, cmd, *a: edits.append(a) if cmd == "edit" else self.show(b, cmd, *a)
        self.assertEqual(self.df.repair_bodies("b", EPIC, self.df.plan_cards("b", EPIC)), [DEV])
        body = edits[0][2]
        self.assertIn("覆蓋：AC-1, AC-2", body)
        self.assertNotIn("部署成功且健康檢查通過", body)
        self.assertIn("看到登入畫面，另加條件", body)             # changed wording is the card's own check
        self.assertIn(own, body)
        self.assertIsNone(self.df.done_problem("[DEV] d", body))
        self.assertEqual(self.df.strip_copied_acs(body, self.df.epic_acs(epic)), body)   # idempotent

    def test_a_long_body_is_a_finding_but_an_epics_acceptance_is_not_counted(self):
        history = "PR #1 head 4927c1a、run 44306911907、review 5957244689 已核對。" * 40
        self.cards[SHIP] = ("[SHIP] s", f"## 目標\n部署\n## 提示\n{history}\nEPIC: {EPIC}", [DEV], [EPIC], None)
        self.cards[EPIC] = ("[EPIC] e", "## 目標\ng\n## 驗收\n- AC-1 x\n" + "  - S1 Given 很長的情境\n" * 200, [SHIP], [], None)
        self.board({DEV})
        self.assertEqual([f.split(":")[0] for f in self.findings("上限")], [SHIP])

    def test_a_done_section_past_the_judges_2000_characters_is_a_finding(self):
        body = f"## 目標\n{'很長的限制。' * 400}\n{self.done}## 驗收\n覆蓋：AC-1\nEPIC: {EPIC}"
        self.cards[DEV] = ("[DEV] d", body, [], [SHIP], None)
        self.board({DEV})
        self.assertIn("前 2000 字", "".join(self.findings("本卡完成")))
        self.assertIsNotNone(self.df.done_problem("[DEV] d", self.df.repaired_done(body)))  # 目標 itself is too long

    def test_an_adj_and_the_card_it_spawned_outside_the_repo_are_in_the_plan(self):
        """An [ADJ] names the EPIC without `EPIC:` and is not linked to it; the maintenance card it
        spawned names neither (2026-11-18). Cards of another EPIC stay out."""
        self.cards.update({
            "t_adj": ("[ADJ] 修 goal 送審循環", f"原 EPIC {EPIC}、原 OPS t_dev", ["t_fix"], [], str(self.repo)),
            "t_fix": ("[DEV] Hermes review_handoff 修補", "恢復 owner: t_adj", [], ["t_adj", "t_x"],
                      "/opt/data/maintenance/hermes-review-handoff"),
            "t_x": ("[DEV] other", "EPIC: t_0bad0bad", ["t_fix"], [], None),
        })
        self.board({DEV, "t_fix", "t_x"})
        cards = self.df.plan_cards("b", EPIC)
        self.assertEqual(sorted(cards), sorted([EPIC, DEV, SHIP, "t_adj", "t_fix"]))
        outside = [f for f in self.df.plan_rules("b", EPIC, cards) if "不在本專案" in f]
        self.assertEqual([f.split(":")[0] for f in outside], ["t_fix"])

    def test_an_adj_opened_by_a_worker_is_a_finding(self):
        """teams-transcription 2026-11-18: the devops [SHIP] opened its own [ADJ] cards."""
        for tid, who in (("t_adj1", "devops"), ("t_adj2", "devflow-tick"), ("t_adj3", "default")):
            self.cards[tid] = (f"[ADJ] {who}", f"EPIC: {EPIC}\n## 目標\n調整", [], [], None)
            self.creators[tid] = who
        self.board({DEV})
        self.assertEqual([f.split(":")[0] for f in self.findings("[ADJ] 由")], ["t_adj1"])

    def test_a_card_repaired_by_more_adj_than_the_limit_is_a_finding_counting_explicit_marks_only(self):
        """t_3b14e30d: eight [ADJ]. Bodies cite the EPIC, the SHIP and history; only marks count."""
        self.cards[SHIP] = ("[SHIP] s", f"EPIC: {EPIC}", [DEV], [EPIC], None)
        marks = {"t_adj1": ("[ADJ] 流程阻塞 t_ship", f"EPIC: {EPIC}"),
                 "t_adj2": ("[ADJ] 處理失敗卡 t_ship", f"EPIC: {EPIC}"),
                 "t_adj3": ("[ADJ] 改 smoke", f"EPIC: {EPIC}\n處理：t_dev、t_ship\n內文"),
                 "t_adj4": ("[ADJ] 收斂", f"EPIC: {EPIC}\n原 SHIP t_ship、原 OPS t_dev 的歷史"),
                 "t_adj5": ("[ADJ] 流程阻塞 t_ship", f"EPIC: {EPIC}")}
        for tid, (title, body) in marks.items():
            self.cards[tid] = (title, body, [], [], None)
        self.statuses.update(t_adj1="done", t_adj2="done", t_adj5="archived")
        self.board({DEV})
        found = self.findings("張 [ADJ] 處理這張卡")
        self.assertEqual([f.split(":")[0] for f in found], [SHIP])
        self.assertIn("t_adj1, t_adj2, t_adj3", found[0])
        self.statuses["t_adj3"] = "archived"
        self.assertEqual(self.findings("張 [ADJ] 處理這張卡"), [])   # two marks: within the limit

    def test_plan_cards_reads_full_hermes_sqlite_without_calling_kanban_show(self):
        """#1a: real boards load the whole plan in one SQLite read instead of N `kanban show` subprocesses."""
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT,
                  workspace_kind TEXT, workspace_path TEXT, completion_contract TEXT, created_by TEXT,
                  result TEXT, goal_mode INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT);""")
            c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                (EPIC, "[EPIC] e", "## 驗收\n- AC-1 [cli] x", "todo", "orchestrator", "dir", str(self.repo), None, "default", None, 0),
                (DEV, "[DEV] d", f"## 目標\nd\n{self.done}## 驗收\n覆蓋：AC-1\nEPIC: {EPIC}", "ready", "developer", "worktree", str(self.repo), None, "orchestrator", None, 1),
                (SHIP, "[SHIP] s", f"EPIC：{EPIC}", "todo", "devops", "dir", str(self.repo), None, "orchestrator", None, 0),
                ("t_arch", "[DEV] old", f"EPIC: {EPIC}", "archived", "developer", "worktree", None, None, "orchestrator", None, 1),
                ("t_pradv", "[ADV] PR 判斷 o/r#1", f"EPIC: {EPIC}", "done", "default", "scratch", None, None, "devflow-judgment", None, 0),
                ("t_other", "[DEV] other", "EPIC: t_0bad0bad", "todo", "developer", "worktree", None, None, "orchestrator", None, 1),
            ])
            c.executemany("INSERT INTO task_links VALUES (?,?)", [
                (DEV, SHIP), (SHIP, EPIC), ("t_pradv", DEV), ("t_other", DEV),
            ])
        show_calls = []
        self.df.kanban = lambda *a: show_calls.append(a) or (_ for _ in ()).throw(AssertionError(f"unexpected kanban call {a}"))
        cards = self.df.plan_cards("b", EPIC)
        self.assertEqual(show_calls, [])
        self.assertEqual(sorted(cards), sorted([EPIC, DEV, SHIP]))
        self.assertTrue(cards[DEV]["goal_mode"])
        self.assertEqual(cards[DEV]["parents"], [])       # PR advisor & other-EPIC card excluded
        self.assertEqual(cards[DEV]["children"], [SHIP])
        self.assertEqual(cards[SHIP]["parents"], [DEV])

    def test_pr_card_covering_only_manual_acs_is_a_finding(self):
        """#3: teams-transcription t_b80d91b0 (PR #12) covered only [manual] ACs and failed check acceptance at review."""
        epic_body = ("## 目標\ng\n## 驗收\n- AC-4 [manual] 真人語音\n- AC-5 [manual] 撤權\n"
                     "- AC-6 [cli] 工程前置\n  - S1 單場授權\n    - Given g\n    - When w\n    - Then t\n")
        self.cards[EPIC] = ("[EPIC] e", epic_body, [SHIP], [], None)
        self.cards[DEV] = ("[DEV] d", f"## 目標\nd\n{self.done}## 驗收\n覆蓋：AC-4, AC-5\nEPIC: {EPIC}", [], [SHIP], None)
        self.board({DEV})
        found = self.findings("全為 [manual]")
        self.assertEqual([f.split(":")[0] for f in found], [DEV])
        # Covering at least one non-manual AC passes.
        self.cards[DEV] = ("[DEV] d", f"## 目標\nd\n{self.done}## 驗收\n覆蓋：AC-4, AC-6\nEPIC: {EPIC}", [], [SHIP], None)
        self.assertEqual(self.findings("全為 [manual]"), [])

    def test_repair_bodies_enables_goal_mode_in_place_for_pending_dev_ops(self):
        """Item 1: tick enables goal_mode in place on pending [DEV]/[OPS] instead of archiving/recreating."""
        self.board(set())  # DEV lacks goal_mode
        enabled = []
        self.df.enable_goal_mode = lambda board, tid: enabled.append((board, tid)) or True
        cards = self.df.plan_cards("b", EPIC)
        self.assertFalse(cards[DEV]["goal_mode"])
        fixed = self.df.repair_bodies("b", EPIC, cards)
        self.assertEqual(enabled, [("b", DEV)])
        self.assertEqual(fixed, [DEV])
        self.assertTrue(cards[DEV]["goal_mode"])
        self.assertEqual([f for f in self.df.plan_rules("b", EPIC, cards) if "goal_mode" in f], [])

    def test_ship_accumulating_more_than_two_post_hoc_pr_cards_after_start_is_a_finding(self):
        """Item 4: teams-transcription t_12402f67 accumulated 8 post-hoc PR cards after starting."""
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT,
                  workspace_kind TEXT, workspace_path TEXT, completion_contract TEXT, created_by TEXT,
                  result TEXT, goal_mode INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER, started_at INTEGER);
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT);""")
            epic_body = "## 驗收\n- AC-1 [cli] x\n  - S1 s\n    - Given g\n    - When w\n    - Then t\n"
            c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                (EPIC, "[EPIC] e", epic_body, "todo", "orchestrator", "dir", str(self.repo), None, "default", None, 0, 100, 100),
                (SHIP, "[SHIP] s", f"EPIC: {EPIC}", "todo", "devops", "dir", str(self.repo), None, "orchestrator", None, 0, 100, 1000),
                ("t_pre1", "[DEV] pre1", f"## 目標\na\n{self.done}覆蓋：AC-1\nEPIC: {EPIC}", "done", "developer", "worktree", str(self.repo), None, "orchestrator", None, 1, 200, 300),
                ("t_pre2", "[DEV] pre2", f"## 目標\nb\n{self.done}覆蓋：AC-1\nEPIC: {EPIC}", "done", "developer", "worktree", str(self.repo), None, "orchestrator", None, 1, 250, 350),
                ("t_pre3", "[DEV] pre3", f"## 目標\nc\n{self.done}覆蓋：AC-1\nEPIC: {EPIC}", "done", "developer", "worktree", str(self.repo), None, "orchestrator", None, 1, 300, 400),
                ("t_p1", "[DEV] p1", f"## 目標\np1\n{self.done}覆蓋：AC-1\nEPIC: {EPIC}", "done", "developer", "worktree", str(self.repo), None, "devops", None, 1, 1100, 1105),
                ("t_p2", "[OPS] p2", f"## 目標\np2\n{self.done}覆蓋：AC-1\nEPIC: {EPIC}", "done", "devops", "worktree", str(self.repo), None, "devops", None, 1, 1200, 1205),
            ])
            c.executemany("INSERT INTO task_links VALUES (?,?)", [
                ("t_pre1", SHIP), ("t_pre2", SHIP), ("t_pre3", SHIP), ("t_p1", SHIP), ("t_p2", SHIP), (SHIP, EPIC),
            ])
        # 3 pre-start + 2 post-start PR cards: within limit (2).
        cards = self.df.plan_cards("b", EPIC)
        self.assertEqual([f for f in self.df.plan_rules("b", EPIC, cards) if "事後追加" in f], [])
        # Adding a 3rd post-start PR card triggers the structural whack-a-mole finding.
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "t_p3", "[OPS] p3", f"## 目標\np3\n{self.done}覆蓋：AC-1\nEPIC: {EPIC}", "ready", "devops", "worktree", str(self.repo), None, "devops", None, 1, 1300, None))
            c.execute("INSERT INTO task_links VALUES (?,?)", ("t_p3", SHIP))
        cards = self.df.plan_cards("b", EPIC)
        found = [f for f in self.df.plan_rules("b", EPIC, cards) if "事後追加" in f]
        self.assertEqual([f.split(":")[0] for f in found], [SHIP])
        self.assertIn("t_p1, t_p2, t_p3", found[0])
        # Negative cases: explicit refusal, incidental mention, ID prefix, wrong SHIP id, and approval outside
        # `## 使用者決定` must NEVER exempt the patch-cap finding.
        for bad_section in (
            f"\n## 使用者決定\n- 使用者 2026-11-23 決定：不核准 {SHIP} 再追加補丁卡 t_p3。\n",
            f"\n## 使用者決定\n- 不核准追加補丁：{SHIP} t_p3\n",
            f"\n## 使用者決定\n- 原出貨卡 {SHIP} 沿用目前部署，t_p3 尚未確認\n",
            f"\n## 使用者決定\n- 核准追加補丁：{SHIP} t_p30\n",
            "\n## 使用者決定\n- 核准追加補丁：t_other t_p3\n",
            f"\n## 備註\n- 核准追加補丁：{SHIP} t_p3\n",
        ):
            cards[EPIC]["body"] = epic_body + bad_section
            self.assertEqual([f.split(":")[0] for f in self.df.plan_rules("b", EPIC, cards) if "事後追加" in f],
                             [SHIP], bad_section)
        # Positive case: exact `- 核准追加補丁：<SHIP> <PATCH>` under `## 使用者決定` authorizes only that patch card.
        cards[EPIC]["body"] = epic_body + f"\n## 使用者決定\n- 核准追加補丁：{SHIP} t_p3\n"
        self.assertEqual([f for f in self.df.plan_rules("b", EPIC, cards) if "事後追加" in f], [])
        # A 4th unapproved open post-hoc patch card still fires the cap even though t_p3 was authorized.
        cards["t_p4"] = {
            **cards["t_p3"], "id": "t_p4", "title": "[OPS] p4", "status": "ready", "created_at": 1400,
        }
        cards[SHIP]["parents"] = [*cards[SHIP]["parents"], "t_p4"]
        self.assertEqual([f.split(":")[0] for f in self.df.plan_rules("b", EPIC, cards) if "事後追加" in f], [SHIP])
        # When all post-hoc PR cards are already `done` (no open patch card holding [SHIP]), [SHIP] is not blocked.
        cards[EPIC]["body"] = epic_body
        cards["t_p3"]["status"] = "done"
        cards["t_p4"]["status"] = "done"
        self.assertEqual([f for f in self.df.plan_rules("b", EPIC, cards) if "事後追加" in f], [])

    def test_work_card_opened_by_non_planner_is_a_finding(self):
        """Work cards ([RES]/[ARCH]/[DEV]/[OPS]/[SHIP]) must be planned by orchestrator (or devops for
        ship-failure fix cards), never spawned directly by `default` chat or other worker profiles."""
        self.creators[DEV] = "default"
        self.board({DEV})
        found = self.findings("工作卡只由 orchestrator")
        self.assertEqual([f.split(":")[0] for f in found], [DEV])
        for allowed in ("orchestrator", "devops"):
            self.creators[DEV] = allowed
            self.assertEqual(self.findings("工作卡只由 orchestrator"), [])


if __name__ == "__main__":
    unittest.main()
