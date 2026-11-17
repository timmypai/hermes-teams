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
from contextlib import closing
from pathlib import Path

from test_judgment import KIT

INBOX_CHAT = {"platform": "local", "chat_id": "local-inbox", "user_id": "uid:1", "chat_type": "dm"}
EPIC_CHAT = {"platform": "local", "chat_id": "local-epic", "user_id": "uid:1", "chat_type": "dm"}
ONESHOT_CHAT = {**INBOX_CHAT, "chat_id": "local-oneshot"}
DEVOPS_CHAT = {"platform": "local", "chat_id": "local-devops", "user_id": "uid:1", "chat_type": "dm"}


class Board:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT, assignee TEXT);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT);
            CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
              user_id TEXT, user_id_alt TEXT, chat_type TEXT, notifier_profile TEXT, delivery_mode TEXT,
              created_at INTEGER, last_event_id INTEGER, last_ping_event_id INTEGER);""")
        self.sent: list[tuple[str, int, str]] = []   # (task, event id, chat) the notifier delivered
        self.clock = 0

    def card(self, tid: str, title: str, *, body: str = "", status: str = "todo", assignee: str = "default") -> None:
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,NULL,?)", (tid, title, body, status, assignee))
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
        # The default profile's chats: Bot Chat is the inbox even when an automated oneshot run is newer.
        with closing(sqlite3.connect(root / "data" / "state.db")) as s:
            s.execute("CREATE TABLE sessions (title TEXT, source TEXT, origin_json TEXT, last_activity_at REAL)")
            s.executemany("INSERT INTO sessions VALUES (?,?,?,?)", [
                ("Bot Chat", "tui", json.dumps(INBOX_CHAT), 100),
                ("建立 EPIC", "oneshot", json.dumps(ONESHOT_CHAT), 200),
                ("app chat", "api_server", json.dumps({**INBOX_CHAT, "chat_id": "run_app"}), 150),
                ("Epic chat", "desktop", json.dumps(EPIC_CHAT), 50)])
            s.commit()
        # devops has its own Bot Chat; developer has none (its questions stay in Hermes' chat).
        (root / "data" / "profiles" / "devops").mkdir(parents=True)
        with closing(sqlite3.connect(root / "data" / "profiles" / "devops" / "state.db")) as s:
            s.execute("CREATE TABLE sessions (title TEXT, source TEXT, origin_json TEXT, last_activity_at REAL)")
            s.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("Bot Chat", "gui", json.dumps(DEVOPS_CHAT), 10))
            s.commit()
        self.profiles: list[tuple] = []

    def kanban(self, board, cmd, tid, *args):
        opts = dict(zip(args[::2], args[1::2]))
        if cmd == "notify-unsubscribe":
            self.board.db.execute("DELETE FROM kanban_notify_subs WHERE task_id=? AND chat_id=?", (tid, opts["--chat-id"]))
            self.board.db.commit()
            return ""
        assert cmd == "notify-subscribe", cmd
        self.board.subscribe(tid, {"platform": opts["--platform"], "chat_id": opts["--chat-id"],
                                   "user_id": opts.get("--user-id"), "chat_type": opts.get("--chat-type")})
        self.profiles.append((tid, opts["--chat-id"], opts["--notifier-profile"]))
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
        epic = "t_e01c0ffe"   # real ids are t_ + hex; [ADJ] bodies name them as `EPIC: t_…`
        self.board.card(epic, "[EPIC] goal")
        self.board.subscribe(epic, EPIC_CHAT)
        self.board.card("t_dev", "[DEV] step")
        self.board.db.execute("INSERT INTO task_links VALUES ('t_dev', ?)", (epic,))
        self.board.card("t_adj", "[ADJ] fix", body=f"EPIC: {epic}\nretry t_dev")
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

    def test_a_card_pinged_only_in_a_oneshot_session_asks_in_bot_chat(self):
        self.board.card("t_auto", "[DEV] made by an automated oneshot run")
        self.board.subscribe("t_auto", ONESHOT_CHAT)
        ev = self.board.block("t_auto")
        self.board.notify()                       # nobody reads the oneshot session
        self.tick()
        self.tick()
        self.assertEqual(self.board.sent, [("t_auto", ev, "local-oneshot"), ("t_auto", ev, "local-inbox")])

    def test_an_epic_followed_only_by_a_oneshot_session_routes_its_cards_to_bot_chat(self):
        epic = "t_e02c0ffe"
        self.board.card(epic, "[EPIC] made by an automated oneshot run")
        self.board.subscribe(epic, ONESHOT_CHAT)
        self.board.card("t_dev", "[DEV] step")
        self.board.subscribe("t_dev", ONESHOT_CHAT)   # inherited from the creator
        self.board.db.execute("INSERT INTO task_links VALUES ('t_dev', ?)", (epic,))
        ev = self.board.block("t_dev")
        self.board.notify()
        self.tick()
        self.assertEqual(self.board.sent, [("t_dev", ev, "local-oneshot"), ("t_dev", ev, "local-inbox")])

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

    def test_a_missing_permission_is_asked_like_a_question(self):
        self.board.card("t_ship", "[SHIP] needs a host runner")
        ev = self.board.block("t_ship", "capability")
        self.tick()
        self.assertEqual(self.board.sent, [("t_ship", ev, "local-inbox")])

    def test_a_lost_compare_and_set_is_retried_next_tick(self):
        self.board.card("t_cli", "[DEV] made from the CLI")
        ev = self.board.block("t_cli")
        real = self.df.replay_from
        self.df.replay_from = lambda *a: False    # the notifier moved the cursor first
        self.df.route_questions("b", self.st)
        self.df.replay_from = real
        self.tick()
        self.assertEqual(self.board.sent, [("t_cli", ev, "local-inbox")])

    def test_inbox_is_bot_chat_never_an_automated_oneshot_session(self):
        chat, source = self.df.inbox()
        self.assertEqual((chat["chat_id"], source), ("local-inbox", "bot-chat"))
        self.assertNotIn("local-oneshot", [c["chat_id"] for c in self.df.known_chats()])

    def test_question_is_asked_once_in_the_assignee_bot_chat_and_hermes_also_hears(self):
        self.board.card("t_ship", "[SHIP] needs a runner", assignee="devops")
        ev = self.board.block("t_ship", "capability")
        self.tick()
        self.tick()
        self.assertEqual(sorted(self.board.sent), [("t_ship", ev, "local-devops"), ("t_ship", ev, "local-inbox")])
        self.assertIn(("t_ship", "local-devops", "devops"), self.profiles)
        self.assertEqual([(p["task"], p["ask_in"]) for p in self.df.pending("devops")], [("t_ship", "devops")])

    def test_assignee_chat_is_unsubscribed_after_the_answer(self):
        self.board.card("t_ship", "[SHIP] needs a runner", assignee="devops")
        self.board.block("t_ship")
        self.tick()
        self.board.unblock("t_ship")
        self.tick()
        chats = [c for (c,) in self.board.db.execute("SELECT chat_id FROM kanban_notify_subs WHERE task_id='t_ship'")]
        self.assertEqual(chats, ["local-inbox"])
        self.assertEqual(self.df.pending("devops"), [])

    def test_assignee_without_bot_chat_is_asked_in_hermes_chat(self):
        self.board.card("t_dev", "[DEV] x", assignee="developer")
        ev = self.board.block("t_dev")
        self.tick()
        self.assertEqual(self.board.sent, [("t_dev", ev, "local-inbox")])
        self.assertEqual([p["ask_in"] for p in self.df.pending()], ["default"])


if __name__ == "__main__":
    unittest.main()
