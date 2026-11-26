"""devflow resume against a fake board whose dispatcher claims a card the instant it is dispatchable.

Hermes semantics modelled: unblock lands in `review` when the newest lifecycle event carries a review
status; the review lane skips unassigned cards; `reopen-review` -> `ready` with the implementer.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
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

    def test_failed_reassign_leaves_the_card_assigned_for_a_retry(self):
        board = Board("developer", None)
        fail = {"assign developer": 1}  # the first re-assign fails, the restore succeeds

        def flaky(b, cmd, task_id, *args):
            key = " ".join((cmd, *args))
            if fail.get(key):
                fail[key] -= 1
                raise SystemExit(1)
            return board(b, cmd, task_id, *args)
        self.df.kanban = flaky
        with self.assertRaises(SystemExit):
            self.df.resume("b", "t_1")
        self.assertEqual((board.task["status"], board.task["assignee"], board.claims), ("blocked", "developer", []))
        self.df.kanban = board
        self.df.resume("b", "t_1")  # the retry `resume` would have refused on an unassigned card
        self.assertEqual(board.claims, [("ready", "developer")])

    def triage(self, open_parent: bool, claim_now: bool = True) -> Board:
        """A card Hermes parked in triage after blocking twice; the user's answer comes as the note."""
        board = Board("devops", None)
        board.task["status"] = "triage"
        comments = []
        inner = board.__call__

        def kanban(b, cmd, task_id, *args):
            if cmd == "comment":
                comments.append((board.task["status"], args[0]))
                return ""
            return inner(b, cmd, task_id, *args)

        def leave_triage(b, task_id):  # Hermes: triage -> todo, ready (and claimable) without open parents
            board.task["status"] = "todo" if open_parent else "ready"
            if not open_parent and claim_now:
                board.dispatch()
        self.df.kanban, self.df.leave_triage = kanban, leave_triage
        board.comments = comments
        self.df.resume("b", "t_1", "使用者回覆：A")
        return board

    def test_triage_card_without_open_parents_runs_with_the_answer_posted_first(self):
        board = self.triage(open_parent=False)
        self.assertEqual(board.comments, [("triage", "使用者回覆：A")])
        self.assertEqual(board.claims, [("ready", "devops")])

    def test_triage_card_waiting_on_a_parent_keeps_the_answer_and_waits_in_todo(self):
        board = self.triage(open_parent=True)
        self.assertEqual(board.comments, [("triage", "使用者回覆：A")])
        self.assertEqual((board.task["status"], board.task["assignee"], board.claims), ("todo", "devops", []))

    def test_triage_card_left_ready_before_a_claim_is_not_blocked_again(self):
        # t_12402f67 2026-11-22: the re-block repeated the reason and Hermes sent it back to triage.
        board = self.triage(open_parent=False, claim_now=False)
        self.assertEqual((board.task["status"], board.task["assignee"]), ("ready", "devops"))
        self.assertNotIn("unblocked", [e["kind"] for e in board.events])  # no block -> unblock cycle


class CommentOps(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        shutil.copy(KIT / "roles.json", root / "roles.json")
        self.df = load_devflow(root)

    def test_a_failed_op_stops_the_ops_that_depend_on_it(self):
        calls, comments = [], []

        def kanban(board, cmd, *args):
            if cmd == "comment":
                comments.append(args[1])
                return ""
            calls.append(cmd)
            if cmd == "link":
                raise SystemExit(1)
            return ""
        self.df.kanban = kanban
        ops = [{"op": "link", "parent": "t_new", "child": "t_c"}, {"op": "unlink", "parent": "t_old", "child": "t_c"},
               {"op": "archive", "task": "t_old"}]
        body = "```devflow-ops\n" + json.dumps(ops) + "\n```"
        applied: list[str] = []
        self.df.apply_comment_ops("b", "t_adj", [{"author": "orchestrator", "created_at": 1, "body": body}], applied)
        self.assertEqual(calls, ["link"])
        self.assertEqual(comments[0].count("\n- 未執行 "), 2)


class LockRaceProtection(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="devflow-lock-race-"))
        self.addCleanup(shutil.rmtree, self.root)
        repo_dir = self.root / "repo"
        repo_dir.mkdir(parents=True)
        (self.root / "state").mkdir(parents=True)
        (self.root / "state" / "repos.json").write_text(
            json.dumps({"b": {"slug": "b", "repo": "o/r", "path": str(repo_dir), "default_branch": "main",
                              "port": 18100, "contract": True}}),
            encoding="utf-8",
        )
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        self.df = load_devflow(self.root)
        patcher = mock.patch.object(self.df.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resume_reentrant_lock_and_cross_process_stalled_triage_skip(self):
        board = Board("developer", None)
        self.df.kanban = board
        # Same-process nested answer/resume lock never deadlocks
        with self.df.locked(self.df.card_lock_name("b", "t_1")):
            self.assertTrue(self.df.is_card_locked("b", "t_1"))
            self.df.resume("b", "t_1")
        self.assertFalse(self.df.is_card_locked("b", "t_1"))
        self.assertEqual(board.claims, [("ready", "developer")])

        # Cross-process flock on card lock causes stalled_triage to skip without creating [ADJ]
        lock_file = self.root / "state" / f"{self.df.card_lock_name('b', 't_1')}.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [
                sys.executable, "-c",
                "import fcntl, sys, time; fh = open(sys.argv[1], 'w'); "
                "fcntl.flock(fh, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(10)",
                str(lock_file),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            self.assertTrue(self.df.is_card_locked("b", "t_1"))
            tasks = {
                "t_1": {"id": "t_1", "title": "[DEV] x", "status": "triage", "block_kind": None},
                "t_ship": {"id": "t_ship", "title": "[SHIP] s", "status": "todo", "block_kind": None},
            }
            created: list[str] = []
            self.df.open_triage_adj = lambda *a: created.append("adj") or "t_adj1"
            self.df.triage_event = lambda *a: 1
            retry = self.df.stalled_triage("b", tasks, {"t_1": ["t_ship"]}, {})
            self.assertTrue(retry)
            self.assertEqual(created, [])

            # Phase 5 tick_board check skips waiting EPIC when any child card is locked
            self.df.kanban = lambda b, cmd, *a: json.dumps([
                {"id": "t_ep1", "title": "[EPIC] e", "status": "todo", "assignee": "orchestrator"},
            ]) if cmd == "list" else json.dumps({"task": {"id": "t_ep1", "status": "todo", "assignee": "orchestrator"}, "parents": ["t_1"], "comments": []})
            self.df.plan_cards = lambda b, tid: {"t_ep1": {"id": "t_ep1", "status": "todo"}, "t_1": {"id": "t_1"}}
            check_calls: list[str] = []
            self.df.check_plan = lambda *a, **kw: check_calls.append("called") or {}
            tick_retry = self.df.tick_board("b", {})
            self.assertTrue(tick_retry)
            self.assertEqual(check_calls, [])
        finally:
            if proc.stdout:
                proc.stdout.close()
            proc.terminate()
            proc.wait(timeout=5)
        self.assertFalse(self.df.is_card_locked("b", "t_1"))


if __name__ == "__main__":
    unittest.main()
