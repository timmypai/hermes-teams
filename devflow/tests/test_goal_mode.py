"""devflow check plan: pending [DEV]/[OPS] cards must run in Hermes goal mode.

`kanban show --json` omits goal_mode, so plan_cards reads it from the board's kanban.db; the board
is a real SQLite file with Hermes' `tasks.goal_mode` column and `show` is an in-memory fake.
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


class GoalMode(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        shutil.copy(KIT / "roles.json", root / "roles.json")
        (root / "state").mkdir()
        (root / "state" / "repos.json").write_text(json.dumps(
            {"b": {"repo": "o/r", "path": str(root / "repo"), "contract": False}}))
        self.db = root / "data" / "kanban" / "boards" / "b" / "kanban.db"
        self.db.parent.mkdir(parents=True)
        os.environ["DEVFLOW_HERMES_DATA"] = str(root / "data")
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA")
        self.df = load_devflow(root)
        self.cards = {
            EPIC: ("[EPIC] e", "## 驗收\n- AC-1 x", [SHIP], []),
            DEV: ("[DEV] d", f"覆蓋：AC-1\nEPIC: {EPIC}", [], [SHIP]),
            SHIP: ("[SHIP] s", f"EPIC: {EPIC}", [DEV], [EPIC]),
        }
        self.df.kanban = self.show

    def show(self, board, cmd, tid, *args):
        title, body, parents, children = self.cards[tid]
        kind = "dir" if "SHIP" in title else ("worktree" if "DEV" in title else "scratch")
        assignee = {"EPIC": "orchestrator", "DEV": "developer", "SHIP": "devops"}[title[1:title.index("]")]]
        return json.dumps({"task": {"id": tid, "title": title, "body": body, "status": "todo", "assignee": assignee,
                                    "workspace_kind": kind, "completion_contract": None},
                           "parents": parents, "children": children, "runs": []})

    def board(self, goal: set[str]) -> None:
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal_mode INTEGER NOT NULL DEFAULT 0)")
            c.executemany("INSERT INTO tasks VALUES (?, ?)", [(t, int(t in goal)) for t in self.cards])

    def goal_findings(self) -> list[str]:
        cards = self.df.plan_cards("b", EPIC)
        return [f for f in self.df.plan_rules("b", EPIC, cards) if "goal_mode" in f]

    def test_dev_card_without_goal_mode_is_a_finding(self):
        self.board(set())
        self.assertEqual([f.split(":")[0] for f in self.goal_findings()], [DEV])

    def test_dev_card_in_goal_mode_passes(self):
        self.board({DEV})
        self.assertEqual(self.goal_findings(), [])


if __name__ == "__main__":
    unittest.main()
