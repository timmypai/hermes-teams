"""devflow resume against a fake board whose dispatcher claims a card the instant it is dispatchable.

Hermes semantics modelled: unblock lands in `review` when the newest lifecycle event carries a review
status; the review lane skips unassigned cards; `reopen-review` -> `ready` with the implementer.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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

    def test_stalled_triage_and_tick_phase5_hold_real_mutual_exclusion_with_barriers(self):
        # Set up isolated DB so stalled_triage re-read & lock acquisition operate against real SQLite rows
        hermes = self.root / "hermes"
        board_dir = hermes / "kanban" / "boards" / "b"
        board_dir.mkdir(parents=True)
        db = board_dir / "kanban.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
                block_kind TEXT, assignee TEXT, goal_mode INTEGER DEFAULT 1
            );
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER
            );
            CREATE TABLE kanban_notify_subs (
                task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
                notifier_profile TEXT, delivery_mode TEXT, last_event_id INTEGER,
                last_ping_event_id INTEGER, created_at INTEGER
            );
        """)
        dev_body = f"## 目標\nx\n\n## 本卡完成\n{self.df.DONE_TEXT}\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n"
        conn.execute("INSERT INTO tasks VALUES ('t_ep1', '[EPIC] e', '## 目標\ng\n## 驗收\n- AC-1 [api] x\n', 'todo', NULL, 'orchestrator', 0)")
        conn.execute("INSERT INTO tasks VALUES ('t_1', '[DEV] x', ?, 'triage', NULL, 'developer', 1)", (dev_body,))
        conn.execute("INSERT INTO tasks VALUES ('t_ship', '[SHIP] s', 'EPIC: t_ep1', 'todo', NULL, 'devops', 0)")
        conn.commit()
        conn.close()

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

        # 1) Barrier test: stalled_triage acquires try_locked BEFORE triage_event; concurrent thread
        # calling locked() cannot enter until stalled_triage finishes open_triage_adj.
        entered_triage_event = threading.Event()
        concurrent_started = threading.Event()
        concurrent_acquired = threading.Event()
        overlap_observed: list[bool] = []

        df.triage_event = lambda *a: (entered_triage_event.set(), time.sleep(0.15), 1)[-1]
        df.open_triage_adj = lambda *a: overlap_observed.append(concurrent_acquired.is_set()) or "t_adj1"

        def concurrent_locker():
            entered_triage_event.wait(timeout=2)
            concurrent_started.set()
            with df.locked(df.card_lock_name("b", "t_1")):
                concurrent_acquired.set()

        t = threading.Thread(target=concurrent_locker)
        t.start()
        tasks = {
            "t_1": {"id": "t_1", "title": "[DEV] x", "status": "triage", "block_kind": None},
            "t_ship": {"id": "t_ship", "title": "[SHIP] s", "status": "todo", "block_kind": None},
        }
        retry = df.stalled_triage("b", tasks, {"t_1": ["t_ship"]}, {})
        t.join(timeout=2)
        self.assertFalse(retry)
        self.assertTrue(concurrent_started.is_set())
        self.assertTrue(concurrent_acquired.is_set())
        self.assertEqual(overlap_observed, [False])  # lock was strictly held during open_triage_adj!

        # 2) Post-snapshot mutation: if another thread grabs lock first and updates DB to 'ready',
        # fresh re-read inside try_locked skips creating [ADJ].
        with sqlite3.connect(db) as c:
            c.execute("UPDATE tasks SET status = 'ready' WHERE id = 't_1'")
            c.commit()
        created2: list[str] = []
        df.triage_event = lambda *a: 2
        df.open_triage_adj = lambda *a: created2.append("adj") or "t_adj2"
        retry2 = df.stalled_triage("b", tasks, {"t_1": ["t_ship"]}, {})
        self.assertFalse(retry2)
        self.assertEqual(created2, [])

        # 3) Phase 5 tick_board barrier: while check_plan runs, child lock cannot be acquired by answer/resume
        entered_check_plan = threading.Event()
        p5_acquired = threading.Event()
        p5_overlap: list[bool] = []

        df.kanban = lambda b, cmd, *a: json.dumps([
            {"id": "t_ep1", "title": "[EPIC] e", "status": "todo", "assignee": "orchestrator"},
        ]) if cmd == "list" else (
            json.dumps({"task": {"id": "t_ep1", "status": "todo", "assignee": "orchestrator"},
                        "parents": ["t_1"], "comments": []})
            if cmd == "show" else ""
        )
        df.plan_cards = lambda b, tid: {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo", "assignee": "orchestrator",
                      "workspace_kind": "dir", "body": "## 目標\ng\n## 驗收\n- AC-1 [api] x\n",
                      "parents": ["t_1"], "children": [], "goal_mode": False},
            "t_1": {"id": "t_1", "title": "[DEV] x", "status": "ready", "assignee": "developer",
                    "workspace_kind": "worktree", "body": dev_body,
                    "parents": [], "children": ["t_ep1"], "goal_mode": True},
        }

        def slow_check_plan(*a, **kw):
            entered_check_plan.set()
            time.sleep(0.15)
            p5_overlap.append(p5_acquired.is_set())
            return {"findings": [], "actionable_rules": [], "gating_risks": [], "note": ""}

        df.check_plan = slow_check_plan
        df.open_plan_adj = lambda *a: (None, False)

        def p5_locker():
            entered_check_plan.wait(timeout=2)
            with df.locked(df.card_lock_name("b", "t_1")):
                p5_acquired.set()

        t2 = threading.Thread(target=p5_locker)
        t2.start()
        tick_retry = df.tick_board("b", {})
        t2.join(timeout=2)
        self.assertFalse(tick_retry)
        self.assertTrue(p5_acquired.is_set())
        self.assertEqual(p5_overlap, [False])


if __name__ == "__main__":
    unittest.main()
