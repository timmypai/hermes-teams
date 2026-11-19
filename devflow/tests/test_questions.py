"""needs_input questions reach a chat: route_questions on a real SQLite board shaped like Hermes'.

Hermes semantics modelled: a new subscription starts at MAX(task_events.id) (never replays history);
the notifier sends events after a subscription's cursor, then records the ping; rewind is a
compare-and-set on the cursor.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from contextlib import closing, redirect_stderr
from pathlib import Path

from test_judgment import KIT

# The default profile's app (api_server) chats: the kanban notifier can wake these. Its local Bot Chat it cannot.
INBOX_CHAT = {"platform": "api_server", "chat_id": "local-inbox", "user_id": "uid:1", "chat_type": "dm"}
EPIC_CHAT = {"platform": "api_server", "chat_id": "local-epic", "user_id": "uid:1", "chat_type": "dm"}
BOT_CHAT = {"platform": "local", "chat_id": "20261110_bot", "user_id": "uid:1", "chat_type": "dm"}
ONESHOT_CHAT = {**INBOX_CHAT, "chat_id": "local-oneshot"}
DEVOPS_CHAT = {"platform": "local", "chat_id": "local-devops", "user_id": "uid:1", "chat_type": "dm"}


class Board:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT, assignee TEXT);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT,
              created_at INTEGER);
            CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
              user_id TEXT, user_id_alt TEXT, chat_type TEXT, notifier_profile TEXT, delivery_mode TEXT,
              created_at INTEGER, last_event_id INTEGER, last_ping_event_id INTEGER);
            CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT, body TEXT,
              created_at INTEGER);""")
        self.sent: list[tuple[str, int, str]] = []   # (task, event id, chat) the notifier delivered
        self.clock = 0

    def card(self, tid: str, title: str, *, body: str = "", status: str = "todo", assignee: str = "default") -> None:
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,NULL,?)", (tid, title, body, status, assignee))
        self.event(tid, "created")

    def event(self, tid: str, kind: str, payload: dict | None = None) -> int:
        cur = self.db.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                              (tid, kind, json.dumps(payload or {}), int(time.time())))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def block(self, tid: str, kind: str = "needs_input", reason: str = "問題：要不要公開？") -> int:
        self.db.execute("UPDATE tasks SET status='blocked', block_kind=? WHERE id=?", (kind, tid))
        return self.event(tid, "blocked", {"kind": kind, "reason": reason})

    def comment(self, tid: str, author: str, body: str) -> None:
        self.db.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                        (tid, author, body, int(time.time())))
        self.db.commit()

    def unblock(self, tid: str) -> None:
        self.db.execute("UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?", (tid,))
        self.event(tid, "unblocked")

    def subscribe(self, tid: str, chat: dict, profile: str = "default") -> None:
        """Hermes add_notify_sub: the cursor starts after every existing event."""
        self.clock += 1
        top = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        self.db.execute("INSERT INTO kanban_notify_subs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, chat["platform"], chat["chat_id"], chat.get("thread_id"), chat.get("user_id"), None,
                         chat.get("chat_type"), profile, "notify+wake", self.clock, top, 0))
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
        (root / "state" / "repos.json").write_text(json.dumps({"b": {"slug": "b", "repo": "o/r", "path": str(root / "repo")}}))
        self.df = load_devflow(root)
        (root / "data" / "kanban" / "boards" / "b").mkdir(parents=True)
        self.board = Board(root / "data" / "kanban" / "boards" / "b" / "kanban.db")
        self.addCleanup(self.board.db.close)
        self.df.kanban = self.kanban
        self.df.replay_from = self.replay_from
        self.st: dict = {}
        # Decisions says "only the user can answer" unless a test says otherwise: no network in tests.
        self.df.decide = lambda content, questions: {"user_decision": {"probability": 0.9}}
        # The default profile's chats: Bot Chat is the inbox even when an automated oneshot run is newer.
        with closing(sqlite3.connect(root / "data" / "state.db")) as s:
            s.execute("CREATE TABLE sessions (title TEXT, source TEXT, origin_json TEXT, last_activity_at REAL)")
            s.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
            s.executemany("INSERT INTO sessions VALUES (?,?,?,?)", [
                ("Bot Chat", "tui", json.dumps(BOT_CHAT), 300),
                ("建立 EPIC", "oneshot", json.dumps(ONESHOT_CHAT), 200),
                ("app chat", "api_server", json.dumps(INBOX_CHAT), 150),
                ("Epic chat", "api_server", json.dumps(EPIC_CHAT), 50)])
            s.commit()
        # devops has its own Bot Chat with terminal; researcher's has no terminal; developer has none.
        for p, tools in (("devops", "terminal\n    - clarify"), ("orchestrator", "terminal"), ("researcher", "file\n    - web")):
            (root / "data" / "profiles" / p).mkdir(parents=True)
            (root / "data" / "profiles" / p / "config.yaml").write_text(
                f"model: x\nplatform_toolsets:\n  cli:\n    - {tools}\n  api_server:\n    - terminal\nkanban:\n  a: 1\n")
            with closing(sqlite3.connect(root / "data" / "profiles" / p / "state.db")) as s:
                s.execute("CREATE TABLE sessions (title TEXT, source TEXT, origin_json TEXT, last_activity_at REAL)")
                s.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
                s.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                          ("Bot Chat", "gui", json.dumps({**DEVOPS_CHAT, "chat_id": f"local-{p}"}), 10))
                s.commit()
        self.profiles: list[tuple] = []
        self.data = root / "data"

    def kanban(self, board, cmd, tid, *args):
        opts = dict(zip(args[::2], args[1::2]))
        if cmd == "notify-unsubscribe":
            cur = self.board.db.execute("DELETE FROM kanban_notify_subs WHERE task_id=? AND chat_id=?", (tid, opts["--chat-id"]))
            self.board.db.commit()
            if cur.rowcount == 0:   # Hermes: "(no such subscription)", exit 1 -> devflow run() dies
                raise SystemExit("no such subscription")
            return ""
        assert cmd == "notify-subscribe", cmd
        self.board.subscribe(tid, {"platform": opts["--platform"], "chat_id": opts["--chat-id"],
                                   "user_id": opts.get("--user-id"), "chat_type": opts.get("--chat-type")},
                             opts["--notifier-profile"])
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

    def test_inbox_is_the_latest_app_chat_never_bot_chat_or_a_oneshot_session(self):
        chat, source = self.df.inbox()
        self.assertEqual((chat["chat_id"], source), ("local-inbox", "latest"))
        known = [c["chat_id"] for c in self.df.known_chats()]
        self.assertNotIn("20261110_bot", known)
        self.assertNotIn("local-oneshot", known)

    def test_a_card_asked_only_in_bot_chat_is_asked_again_in_the_app_chat(self):
        self.board.card("t_old", "[DEV] subscribed to Bot Chat before this change")
        self.board.subscribe("t_old", BOT_CHAT)
        ev = self.board.block("t_old")
        self.st["asked"] = [f"b/t_old/{ev}"]   # an older tick "asked" it in Bot Chat, which never delivered
        self.tick()
        self.tick()
        self.assertEqual([s for s in self.board.sent if s[2] == "local-inbox"], [("t_old", ev, "local-inbox")])

    def test_question_is_asked_once_in_the_assignee_bot_chat_and_hermes_also_hears(self):
        self.board.card("t_ship", "[SHIP] needs a runner", assignee="devops")
        ev = self.board.block("t_ship", "capability")
        self.tick()
        self.tick()
        self.assertEqual(sorted(self.board.sent), [("t_ship", ev, "local-devops"), ("t_ship", ev, "local-inbox")])
        self.assertIn(("t_ship", "local-devops", "devops"), self.profiles)
        self.assertEqual([(p["task"], p["ask_in"]) for p in self.df.pending("devops")], [("t_ship", "devops")])

    def test_an_archived_routed_card_does_not_stop_the_next_question(self):
        self.board.card("t_old", "[SHIP] answered then archived", assignee="devops")
        self.board.block("t_old")
        self.tick()
        self.board.db.execute("UPDATE tasks SET status='archived' WHERE id='t_old'")
        self.board.db.execute("DELETE FROM kanban_notify_subs WHERE task_id='t_old'")   # Hermes archive drops them
        self.board.card("t_new", "[SHIP] next question", assignee="devops")
        ev = self.board.block("t_new")
        self.tick()
        self.assertIn(("t_new", ev, "local-devops"), self.board.sent)

    def _say(self, profile: str, chat_id: str, role: str, content: str) -> None:
        db = self.data / ("state.db" if profile == "default" else f"profiles/{profile}/state.db")
        with closing(sqlite3.connect(db)) as s:
            s.execute("CREATE TABLE IF NOT EXISTS messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
            s.execute("INSERT INTO messages VALUES (?,?,?,?)", (chat_id, role, content, time.time()))
            s.commit()

    def test_pending_reports_what_each_chat_transcript_shows_not_the_ping_cursor(self):
        self.board.card("t_ship", "[SHIP] needs a runner", assignee="devops")
        self.board.block("t_ship")
        self.tick()                                     # notifier ran: pings recorded, cursors advanced
        def seen():
            return {c["chat"]: (c["woken"], c["replied"]) for c in self.df.pending()[0]["chats"]}
        self.assertEqual(seen(), {"api_server:local-inbox": (False, False), "local:local-devops": (False, False)})
        self._say("default", "local-inbox", "user", "[kanban] Task t_ship blocked; needs attention.")
        self._say("devops", "local-devops", "user", "[kanban] Task t_ship blocked; needs attention.")
        self.assertEqual(seen(), {"api_server:local-inbox": (True, False), "local:local-devops": (True, False)})
        self._say("devops", "local-devops", "assistant", "需要你決定（b t_ship）…")
        self.assertEqual(seen()["local:local-devops"], (True, True))

    # --- workflow questions go to an [ADJ], not to the user ---------------------------------------
    def _flow(self, p: float | None, create_ok: bool = True) -> tuple[list, list]:
        """Decisions answers `p` (None: unavailable); `hermes kanban create` is recorded and, like Hermes,
        puts the [ADJ] on the board; `hermes kanban comment` goes to self.comments."""
        judged, created = [], []
        self.comments: list[tuple[str, str]] = []

        def decide(content, questions):
            judged.append(content)
            if p is None:
                raise self.df.DecisionsError("no key")
            return {"user_decision": {"probability": p}}

        def run(cmd, **kw):
            if cmd[4] == "comment":
                if not create_ok:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="database is locked")
                self.comments.append((cmd[5], cmd[6]))
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            created.append(cmd)
            if not create_ok:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="database is locked")
            tid = f"t_ad{len(created):06x}"
            self.board.card(tid, cmd[5], body=cmd[cmd.index("--body") + 1], assignee="orchestrator")
            return types.SimpleNamespace(returncode=0, stdout=f"Created {tid}\n", stderr="")
        self.df.decide, self.df.run = decide, run
        return judged, created

    def _finish(self, tid: str) -> None:
        self.board.db.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        self.board.db.commit()

    def test_an_idle_board_is_ticked_again_after_a_kit_update(self):
        key = self.df.tick_key("b")
        self.assertEqual(self.df.tick_key("b"), key)                     # idle board, same kit: skipped
        roles = self.df.ROOT / "roles.json"
        roles.write_text(roles.read_text() + "\n")                        # deploy-kit.sh copied a new kit
        self.assertNotEqual(self.df.tick_key("b"), key)

    def test_a_tick_waits_for_another_boards_tick_instead_of_skipping(self):
        """devflow-smoke's tick held the shared lock whenever teams-transcription's hook fired (2026-11-18)."""
        ran = []
        self.df.tick_board = lambda board, st: ran.append(board)
        other = open(self.df.ROOT / "state" / "tick.lock", "w")        # another board's tick is running
        fcntl.flock(other, fcntl.LOCK_EX)
        waiting = threading.Thread(target=self.df.cmd_tick, args=(types.SimpleNamespace(hook=False, board="b"),))
        waiting.start()
        time.sleep(0.3)
        self.df.cmd_tick(types.SimpleNamespace(hook=False, board="b"))  # a second hook for b: one waiter is enough
        self.assertEqual(ran, [])
        fcntl.flock(other, fcntl.LOCK_UN)
        other.close()
        waiting.join(5)
        self.assertEqual(ran, ["b"])
        self.assertEqual(json.loads(self.df.TICK.read_text())["mtime"]["b"], self.df.tick_key("b"))

    def test_a_failed_adj_create_asks_the_user_and_routing_goes_on(self):
        self._ops_card()
        self._flow(0.05, create_ok=False)
        self.board.card("t_ship", "[SHIP] another question", assignee="devops")
        evs = {tid: self.board.block(tid) for tid in ("t_ops", "t_ship")}
        with redirect_stderr(io.StringIO()):
            self.tick()
        self.assertIn(("t_ops", evs["t_ops"], "local-devops"), self.board.sent)
        self.assertIn(("t_ship", evs["t_ship"], "local-devops"), self.board.sent)

    def _ops_card(self) -> str:
        epic = "t_e04c0ffe"
        self.board.card(epic, "[EPIC] goal")
        self.board.subscribe(epic, EPIC_CHAT)
        self.board.card("t_ops", "[OPS] step", assignee="devops")
        self.board.db.execute("INSERT INTO task_links VALUES ('t_ops', ?)", (epic,))
        return epic

    def test_a_workflow_question_opens_one_adj_and_does_not_wake_the_assignee(self):
        epic = self._ops_card()
        judged, created = self._flow(0.05)
        ev = self.board.block("t_ops", reason="問題：請 Hermes 管理端排除 goal judge 循環？選項：A / B")
        self.tick()
        self.tick()
        self.assertEqual(len(judged), 1)
        self.assertEqual(len(created), 1)
        cmd = created[0]
        self.assertEqual(cmd[cmd.index("--assignee") + 1], "orchestrator")
        self.assertIn(f"EPIC: {epic}", cmd[cmd.index("--body") + 1])
        self.assertIn("goal judge 循環", cmd[cmd.index("--body") + 1])
        self.assertEqual(cmd[cmd.index("--idempotency-key") + 1], f"flow-t_ops-{ev}")
        # Hermes' own notifier still pings the EPIC chat; Hermes answers it from `handled_by` (intake skill).
        self.assertNotIn(("t_ops", ev, "local-devops"), self.board.sent)
        self.df.TICK.write_text(json.dumps(self.st))
        self.assertEqual(self.df.pending()[0]["handled_by"], "t_ad000001")

    def test_a_card_blocked_a_third_time_asks_the_user(self):
        self._ops_card()
        judged, created = self._flow(0.05)
        evs = []
        for i in range(3):
            if i:
                self._finish(f"t_ad{i:06x}")     # the [ADJ] resumed it and finished; the worker blocked again
                self.board.unblock("t_ops")
            evs.append(self.board.block("t_ops"))
            self.tick()
        self.assertEqual(len(created), 2)
        self.assertEqual(len(judged), 2)
        self.assertEqual([e for _, e, chat in self.board.sent if chat == "local-devops"], [evs[2]])

    def test_adj_laps_count_every_creator_but_only_explicit_marks(self):
        """t_3b14e30d had 8 [ADJ]; Hermes and devops opened most, so a tick-only count never stopped it.
        An [ADJ] that only cites the card (as EPIC/history) is not a lap; an archived one is not either."""
        self._ops_card()
        judged, created = self._flow(0.05)
        self.board.card("t_adh1", "[ADJ] 處理失敗卡 t_ops", status="done", assignee="orchestrator")
        self.board.card("t_adh2", "[ADJ] 改 smoke", body="EPIC: t_e04c0ffe\n處理：t_ship, t_ops\n內文", status="done")
        self.board.card("t_adh3", "[ADJ] 收斂", body="原 OPS t_ops、原 SHIP t_ship 的歷史", status="done")
        self.board.card("t_adh4", "[ADJ] 流程阻塞 t_ops", status="archived")
        ev = self.board.block("t_ops")
        self.tick()
        self.assertEqual((created, judged), ([], []))
        self.assertIn(("t_ops", ev, "local-devops"), self.board.sent)

    def test_a_card_an_open_adj_repairs_gets_no_second_adj_and_the_question_goes_there(self):
        """2026-11-18: tick opened t_a4059a2e for the block Hermes' t_0c2c0548 was already handling."""
        self._ops_card()
        judged, created = self._flow(0.05)
        self.board.card("t_adj0", "[ADJ] 收斂停止的卡", body="EPIC: t_e04c0ffe\n處理：t_ops\n內文", status="running")
        ev = self.board.block("t_ops", reason="問題：請 orchestrator 重定義停止範圍？")
        self.tick()
        self.assertEqual((created, judged), ([], []))
        self.assertEqual([a for a, _ in self.comments], ["t_adj0"])
        self.assertIn("重定義停止範圍", self.comments[0][1])
        self.assertNotIn(("t_ops", ev, "local-devops"), self.board.sent)
        self.df.TICK.write_text(json.dumps(self.st))
        self.assertEqual(self.df.pending()[0]["handled_by"], "t_adj0")
        self.tick()
        self.assertEqual(len(self.comments), 1)   # once per block

    def test_a_question_the_open_adj_did_not_receive_is_asked_to_the_user(self):
        self._ops_card()
        judged, created = self._flow(0.05, create_ok=False)
        self.board.card("t_adj0", "[ADJ] 收斂停止的卡", body="EPIC: t_e04c0ffe\n處理：t_ops\n內文", status="running")
        ev = self.board.block("t_ops")
        with redirect_stderr(io.StringIO()):
            self.tick()
        self.assertEqual((created, judged, self.comments), ([], [], []))
        self.assertIn(("t_ops", ev, "local-devops"), self.board.sent)
        self.df.TICK.write_text(json.dumps(self.st))
        self.assertIsNone(self.df.pending()[0]["handled_by"])

    def test_an_open_adj_that_only_cites_the_card_does_not_take_its_question(self):
        self._ops_card()
        judged, created = self._flow(0.05)
        self.board.card("t_adj0", "[ADJ] 別的修正", body="EPIC: t_e04c0ffe\n處理：t_other\n參考 t_ops 的 run", status="running")
        self.board.block("t_ops")
        self.tick()
        self.assertEqual((len(created), self.comments), (1, []))
        self.assertIn("處理：t_ops", created[0][created[0].index("--body") + 1])

    def test_unsure_or_unavailable_decisions_asks_the_user(self):
        self._ops_card()
        for p, tid in ((0.5, "t_ops"), (None, "t_ops2")):
            with self.subTest(p=p):
                if tid != "t_ops":
                    self.board.card(tid, "[OPS] step 2", assignee="devops")
                    self.board.db.execute("INSERT INTO task_links VALUES (?, 't_e04c0ffe')", (tid,))
                _, created = self._flow(p)
                ev = self.board.block(tid)
                self.tick()
                self.assertEqual(created, [])
                self.assertIn((tid, ev, "local-devops"), self.board.sent)

    def test_comments_after_the_question_are_listed_except_the_assignees_own(self):
        self.board.card("t_res", "[RES] find the source", assignee="researcher")
        self.board.comment("t_res", "default", "提問前的留言")
        time.sleep(1.1)                            # comments are timed in whole seconds
        self.board.block("t_res", kind="capability")
        self.board.comment("t_res", "researcher", "我自己的進度")
        self.board.comment("t_res", "default", "使用者提供：上游 commit 0f50ec6c")
        self.assertEqual([(c["author"], c["text"]) for c in self.df.pending()[0]["comments_since"]],
                         [("default", "使用者提供：上游 commit 0f50ec6c")])

    def _resume_like_hermes(self):
        """devflow resume, reduced to its effect: a note, then the card unblocks (slowly, to overlap)."""
        path, notes = self.board.path, []

        def resume(board, tid, note):
            time.sleep(0.2)
            with closing(sqlite3.connect(path)) as c:
                c.execute("UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?", (tid,))
                c.execute("INSERT INTO task_events (task_id, kind, payload) VALUES (?, 'unblocked', '{}')", (tid,))
                c.commit()
            notes.append(note)
            return f"resumed {tid}"
        self.df.resume = resume
        return notes

    def test_two_answers_to_one_question_apply_once_even_at_the_same_time(self):
        notes = self._resume_like_hermes()
        self.board.card("t_ship", "[SHIP] needs a runner", assignee="devops")
        ev = self.board.block("t_ship")
        refused: list[int] = []

        def reply(where, text):
            try:
                self.df.answer("b", "t_ship", ev, text, where)
            except SystemExit as e:
                refused.append(e.code)
        threads = [threading.Thread(target=reply, args=a) for a in (("devops 頁面", "A"), ("App 對話", "B"))]
        err = io.StringIO()
        with redirect_stderr(err):
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(notes), 1)
        self.assertEqual(refused, [1])
        self.assertIn("events since: unblocked", err.getvalue())

    def test_an_answer_to_an_older_question_is_not_applied_to_the_new_one(self):
        notes = self._resume_like_hermes()
        self.board.card("t_ship", "[SHIP] needs a runner", assignee="devops")
        first = self.board.block("t_ship")
        self.board.unblock("t_ship")
        second = self.board.block("t_ship")
        err = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self.df.answer("b", "t_ship", first, "A", "devops 頁面")
        self.assertIn(f"now waits on question event {second}", err.getvalue())
        self.assertEqual(notes, [])
        self.df.answer("b", "t_ship", second, "B", "devops 頁面")
        self.assertEqual(len(notes), 1)

    def test_assignee_chat_is_unsubscribed_after_the_answer(self):
        self.board.card("t_ship", "[SHIP] needs a runner", assignee="devops")
        self.board.block("t_ship")
        self.tick()
        self.board.unblock("t_ship")
        self.tick()
        chats = [c for (c,) in self.board.db.execute("SELECT chat_id FROM kanban_notify_subs WHERE task_id='t_ship'")]
        self.assertEqual(chats, ["local-inbox"])
        self.assertEqual(self.df.pending("devops"), [])

    def test_reassigned_blocked_card_moves_its_question_to_the_new_bot(self):
        self.board.card("t_ship", "[SHIP] needs a runner", assignee="devops")
        ev = self.board.block("t_ship")
        self.tick()
        self.board.db.execute("UPDATE tasks SET assignee='orchestrator' WHERE id='t_ship'")
        self.board.db.commit()
        self.tick()
        self.tick()
        self.assertEqual(sorted(self.board.sent), [("t_ship", ev, "local-devops"), ("t_ship", ev, "local-inbox"),
                                                   ("t_ship", ev, "local-orchestrator")])
        chats = sorted(c for (c,) in self.board.db.execute("SELECT chat_id FROM kanban_notify_subs WHERE task_id='t_ship'"))
        self.assertEqual(chats, ["local-inbox", "local-orchestrator"])
        self.assertEqual([p["ask_in"] for p in self.df.pending()], ["orchestrator"])

    def test_assignee_without_a_usable_bot_chat_is_asked_in_hermes_chat(self):
        self.board.card("t_dev", "[DEV] x", assignee="developer")    # no Bot Chat
        self.board.card("t_res", "[RES] y", assignee="researcher")   # Bot Chat without terminal
        ev_dev, ev_res = self.board.block("t_dev"), self.board.block("t_res")
        self.tick()
        self.assertEqual(sorted(self.board.sent), [("t_dev", ev_dev, "local-inbox"), ("t_res", ev_res, "local-inbox")])
        self.assertEqual([p["ask_in"] for p in self.df.pending()], ["default", "default"])


if __name__ == "__main__":
    unittest.main()
