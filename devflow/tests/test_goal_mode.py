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

    def show(self, board, cmd, tid, *args):
        title, body, parents, children, path = self.cards[tid]
        kind = title[1:title.index("]")]
        return json.dumps({"task": {"id": tid, "title": title, "body": body, "status": "todo", "assignee": ASSIGNEE[kind],
                                    "workspace_kind": WORKSPACE[kind], "workspace_path": path, "completion_contract": None},
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


if __name__ == "__main__":
    unittest.main()
