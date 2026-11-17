"""needs_input questions reach a chat: route_questions on a real SQLite board shaped like Hermes'.

Hermes semantics modelled: a new subscription starts at MAX(task_events.id) (never replays history);
the notifier sends events after a subscription's cursor, then records the ping; rewind is a
compare-and-set on the cursor.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_judgment import KIT

INBOX_CHAT = {"platform": "local", "chat_id": "local-inbox", "user_id": "uid:1", "chat_type": "dm"}
EPIC_CHAT = {"platform": "local", "chat_id": "local-epic", "user_id": "uid:1", "chat_type": "dm"}


class Board:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT);
            CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
              user_id TEXT, user_id_alt TEXT, chat_type TEXT, notifier_profile TEXT, delivery_mode TEXT,
              created_at INTEGER, last_event_id INTEGER, last_ping_event_id INTEGER);""")
        self.sent: list[tuple[str, int, str]] = []   # (task, event id, chat) the notifier delivered
        self.clock = 0

    def card(self, tid: str, title: str, *, body: str = "", status: str = "todo") -> None:
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,NULL)", (tid, title, body, status))
        self.event(tid, "created")

    def event(self, tid: str, kind: str, payload: dict | None = None) -> int:
        cur = self.db.execute("INSERT INTO task_events (task_id, kind, payload) VALUES (?,?,?)",
                              (tid, kind, json.dumps(payload or {})))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def block(self, tid: str, kind: str = "needs_input") -> int:
        self.db.execute("UPDATE tasks SET status='blocked', block_kind=? WHERE id=?", (kind, tid))
        return self.event(tid, "blocked", {"kind": kind, "reason": "問題：要不要公開？"})

    def unblock(self, tid: str) -> None:
        self.db.execute("UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?", (tid,))
        self.event(tid, "unblocked")

    def subscribe(self, tid: str, chat: dict) -> None:
        """Hermes add_notify_sub: the cursor starts after every existing event."""
        self.clock += 1
        top = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        self.db.execute("INSERT INTO kanban_notify_subs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, chat["platform"], chat["chat_id"], chat.get("thread_id"), chat.get("user_id"), None,
                         chat.get("chat_type"), "default", "notify+wake", self.clock, top, 0))
        self.db.commit()

    def notify(self) -> None:
        """Hermes notifier: send blocked events after each cursor, then advance it and record the ping."""
        for tid, chat, cursor in self.db.execute(
                "SELECT task_id, chat_id, last_event_id FROM kanban_notify_subs").fetchall():
            rows = self.db.execute("SELECT id, kind FROM task_events WHERE task_id=? AND id>?", (tid, cursor)).fetchall()
            for eid, kind in rows:
                if kind == "blocked":
                    self.sent.append((tid, eid, chat))
                    self.db.execute("UPDATE kanban_notify_subs SET last_ping_event_id=? WHERE task_id=? AND chat_id=?",
                                    (eid, tid, chat))
            if rows:
                self.db.execute("UPDATE kanban_notify_subs SET last_event_id=? WHERE task_id=? AND chat_id=?",
                                (rows[-1][0], tid, chat))
        self.db.commit()


class RouteQuestions(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        os.environ["DEVFLOW_HERMES_DATA"] = str(root / "data")
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA")
        from test_judgment import load_devflow
        shutil.copy(KIT / "roles.json", root / "roles.json")
        (root / "state").mkdir()
        (root / "state" / "repos.json").write_text(json.dumps({"b": {"slug": "b", "repo": "o/r"}}))
        self.df = load_devflow(root)
        (root / "data" / "kanban" / "boards" / "b").mkdir(parents=True)
        self.board = Board(root / "data" / "kanban" / "boards" / "b" / "kanban.db")
        self.addCleanup(self.board.db.close)
        self.df.kanban = self.kanban
        self.df.replay_from = self.replay_from
        self.st: dict = {}
        # The inbox defaults to the newest chat a card was made in.
        self.board.card("t_old", "[EPIC] earlier work", status="done")
        self.board.subscribe("t_old", INBOX_CHAT)

    def kanban(self, board, cmd, tid, *args):
        assert cmd == "notify-subscribe", cmd
        opts = dict(zip(args[::2], args[1::2]))
        self.board.subscribe(tid, {"platform": opts["--platform"], "chat_id": opts["--chat-id"],
                                   "user_id": opts.get("--user-id"), "chat_type": opts.get("--chat-type")})
        return ""

    def replay_from(self, board, sub, event_id):
        cur = self.board.db.execute(
            "UPDATE kanban_notify_subs SET last_event_id=? WHERE task_id=? AND chat_id=? AND last_event_id=?",
            (event_id - 1, sub["task_id"], sub["chat_id"], sub["last_event_id"]))
        self.board.db.commit()
        return cur.rowcount == 1

    def tick(self) -> None:
        self.df.route_questions("b", self.st)
        self.board.notify()

    def test_block_before_any_chat_is_sent_once_to_the_inbox(self):
        self.board.card("t_cli", "[DEV] made from the CLI")
        ev = self.board.block("t_cli")
        self.tick()
        self.tick()
        self.assertEqual(self.board.sent, [("t_cli", ev, "local-inbox")])

    def test_card_in_an_epic_path_asks_in_the_epic_chat(self):
        self.board.card("t_epic", "[EPIC] goal")
        self.board.subscribe("t_epic", EPIC_CHAT)
        self.board.card("t_dev", "[DEV] step")
        self.board.db.execute("INSERT INTO task_links VALUES ('t_dev', 't_epic')")
        self.board.card("t_adj", "[ADJ] fix", body="EPIC: t_epic\nretry t_dev")
        ev_dev, ev_adj = self.board.block("t_dev"), self.board.block("t_adj")
        self.tick()
        self.assertEqual(sorted(self.board.sent), [("t_adj", ev_adj, "local-epic"), ("t_dev", ev_dev, "local-epic")])

    def test_a_question_already_sent_is_not_sent_again(self):
        self.board.card("t_chat", "[DEV] made in a chat")
        self.board.subscribe("t_chat", EPIC_CHAT)
        ev = self.board.block("t_chat")
        self.board.notify()                       # Hermes delivered it on its own
        self.tick()
        self.assertEqual(self.board.sent, [("t_chat", ev, "local-epic")])

    def test_blocking_again_after_an_answer_asks_again(self):
        self.board.card("t_cli", "[DEV] made from the CLI")
        first = self.board.block("t_cli")
        self.tick()
        self.board.unblock("t_cli")
        second = self.board.block("t_cli")
        self.tick()
        self.assertEqual([e for _, e, _ in self.board.sent], [first, second])

    def test_other_block_kinds_are_subscribed_but_not_replayed(self):
        self.board.card("t_cli", "[DEV] made from the CLI")
        self.board.block("t_cli", "dependency")
        self.tick()
        self.assertEqual(self.board.sent, [])
        subs = self.board.db.execute("SELECT chat_id FROM kanban_notify_subs WHERE task_id='t_cli'").fetchall()
        self.assertEqual(subs, [("local-inbox",)])

    def test_a_lost_compare_and_set_is_retried_next_tick(self):
        self.board.card("t_cli", "[DEV] made from the CLI")
        ev = self.board.block("t_cli")
        real = self.df.replay_from
        self.df.replay_from = lambda *a: False    # the notifier moved the cursor first
        self.df.route_questions("b", self.st)
        self.df.replay_from = real
        self.tick()
        self.assertEqual(self.board.sent, [("t_cli", ev, "local-inbox")])


if __name__ == "__main__":
    unittest.main()
