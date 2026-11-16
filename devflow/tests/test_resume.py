"""devflow resume against a fake board whose dispatcher claims a card the instant it is dispatchable.

Hermes semantics modelled: unblock lands in `review` when the newest lifecycle event carries a review
status; the review lane skips unassigned cards; `reopen-review` -> `ready` with the implementer.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_judgment import KIT, load_devflow


class Board:
    def __init__(self, assignee: str, block_source: str | None):
        self.task = {"status": "blocked", "assignee": assignee}
        self.events = [{"kind": "review_requested", "payload": {"implementer": "developer", "reviewer": "reviewer"}},
                       {"kind": "blocked", "payload": {"resume_status": block_source} if block_source else {}}]
        self.claims: list[tuple[str, str]] = []   # (lane, profile) the dispatcher started

    def dispatch(self) -> None:
        """A tick racing every CLI call: claims any dispatchable card at once."""
        t = self.task
        if t["status"] in ("ready", "review") and t["assignee"]:
            self.claims.append((t["status"], t["assignee"]))
            t["status"] = "running"

    def __call__(self, board, cmd, task_id, *args):
        t = self.task
        if cmd == "show":
            return json.dumps({"task": dict(t), "events": self.events})
        if cmd == "assign":
            t["assignee"] = None if args[0] == "none" else args[0]
        elif cmd == "unblock":
            src = self.events[-1]["payload"].get("resume_status")
            t["status"] = "review" if src == "review" else "ready"
            self.events.append({"kind": "unblocked", "payload": {}})
        elif cmd == "reopen-review":
            assert t["status"] == "review"
            t["status"], t["assignee"] = "ready", self.events[0]["payload"]["implementer"]
        elif cmd == "block":
            t["status"] = "blocked"
        self.dispatch()
        return ""


class Resume(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        shutil.copy(KIT / "roles.json", root / "roles.json")
        self.df = load_devflow(root)
        patcher = mock.patch.object(self.df.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_resume(self, board: Board) -> list[tuple[str, str]]:
        self.df.kanban = board
        self.df.resume("b", "t_1")
        return board.claims

    def test_implementer_rework_after_review_block_never_runs_in_review_lane(self):
        self.assertEqual(self.run_resume(Board("developer", "review")), [("ready", "developer")])

    def test_reviewer_resume_after_review_block_stays_in_review_lane(self):
        self.assertEqual(self.run_resume(Board("reviewer", "review")), [("review", "reviewer")])

    def test_implementer_resume_after_ready_block_runs_in_ready_lane(self):
        self.assertEqual(self.run_resume(Board("developer", None)), [("ready", "developer")])


if __name__ == "__main__":
    unittest.main()
