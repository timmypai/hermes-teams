"""Encapsulated adapter for Hermes Kanban SQLite & internal write helpers.

Centralizes all direct `kanban.db` queries and `hermes_cli.kanban_db` calls that used to be
scattered as inline Python strings across `bin/devflow`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _parse_ts(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 0 else None
    s = str(val).strip()
    if not s:
        return None
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _adj_targets_card(adj_title: str, adj_body: str, tid: str) -> bool:
    """True when an [ADJ] card explicitly targets `tid` (flow-block, failure-fix, or plan-rule repair)."""
    if not adj_title or not adj_title.lstrip().startswith("[ADJ]"):
        return False
    pat = re.compile(rf"(?<![0-9A-Za-z_]){re.escape(tid)}(?![0-9A-Za-z_])")
    if pat.search(adj_title):
        return True
    for m in re.finditer(r"^\s*處理[：:][ \t]*([^\n]+)$", adj_body or "", re.M):
        if pat.search(m.group(1)):
            return True
    if adj_title.lstrip().startswith("[ADJ] 計畫規則修正") and pat.search(adj_body or ""):
        return True
    return False


def card_review_reset_watermark(boards_dir: Path, board: str, tid: str) -> tuple[int, float]:
    """Return `(reset_event_id, reset_timestamp)` for `tid`'s review-round breaker.

    A reset watermark advances ONLY on deliberate intervention:
      1. A completed (`status='done'`) `[ADJ]` card on the same board that explicitly targets `tid`.
      2. A human-anchored `unblocked` event on `tid` following a `needs_input` or `capability` block.
    Transient retries never reset the counter.
    """
    db = boards_dir / board / "kanban.db"
    if not tid or not db.exists():
        return 0, 0.0
    wm_ev = 0
    wm_ts = 0.0
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # 1. Human-anchored unblock after needs_input / capability block on tid
            if "task_events" in tables:
                ev_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_events)")}
                sel = "id, kind, payload" + (", created_at" if "created_at" in ev_cols else "")
                waiting_human = False
                for ev in conn.execute(
                    f"SELECT {sel} FROM task_events WHERE task_id = ? ORDER BY id ASC", (tid,)
                ):
                    kind = ev["kind"]
                    if kind in ("blocked", "block_loop_detected"):
                        try:
                            payload = json.loads(ev["payload"] or "{}")
                        except ValueError:
                            payload = {}
                        bkind = (payload.get("kind") or "") if isinstance(payload, dict) else ""
                        if bkind in ("needs_input", "capability"):
                            waiting_human = True
                    elif kind == "unblocked" and waiting_human:
                        waiting_human = False
                        wm_ev = max(wm_ev, int(ev["id"]))
                        if "created_at" in ev_cols:
                            ts = _parse_ts(ev["created_at"])
                            if ts is not None:
                                wm_ts = max(wm_ts, ts)
            # 2. Completed [ADJ] card targeting tid
            if "tasks" in tables:
                t_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
                if {"id", "title", "status"}.issubset(t_cols):
                    sel = "id, title" + (", body" if "body" in t_cols else "") + (", completed_at" if "completed_at" in t_cols else "")
                    for row in conn.execute(f"SELECT {sel} FROM tasks WHERE status = 'done'"):
                        body = row["body"] if "body" in t_cols else ""
                        if not _adj_targets_card(row["title"] or "", body or "", tid):
                            continue
                        if "completed_at" in t_cols:
                            ts = _parse_ts(row["completed_at"])
                            if ts is not None:
                                wm_ts = max(wm_ts, ts)
                        if "task_events" in tables:
                            ev_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_events)")}
                            q = "SELECT MAX(id) AS mid" + (", MAX(created_at) AS mts" if "created_at" in ev_cols else "") + " FROM task_events WHERE task_id = ?"
                            er = conn.execute(q, (row["id"],)).fetchone()
                            if er and er["mid"] is not None:
                                wm_ev = max(wm_ev, int(er["mid"]))
                            if er and "mts" in er.keys():
                                ts = _parse_ts(er["mts"])
                                if ts is not None:
                                    wm_ts = max(wm_ts, ts)
    except sqlite3.Error:
        return 0, 0.0
    return wm_ev, wm_ts


def board_rows(boards_dir: Path, board: str, sql: str, args: tuple = ()) -> list[dict]:
    with closing(sqlite3.connect(f"file:{boards_dir / board / 'kanban.db'}?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args)]


def task_block_kind(boards_dir: Path, board: str, tid: str) -> str | None:
    rows = board_rows(boards_dir, board, "SELECT block_kind FROM tasks WHERE id = ?", (tid,))
    return rows[0]["block_kind"] if rows else None


def goal_modes(boards_dir: Path, board: str, ids: list[str]) -> set[str]:
    db = boards_dir / board / "kanban.db"
    if not ids or not db.exists():
        return set()
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        marks = ",".join("?" * len(ids))
        return {r[0] for r in conn.execute(f"SELECT id FROM tasks WHERE goal_mode AND id IN ({marks})", ids)}


def card_changes_requested_count(boards_dir: Path, board: str, tid: str) -> int:
    """Count `changes_requested` events on `tid` strictly after its latest intervention reset
    watermark (completed targeting `[ADJ]` or human `needs_input`/`capability` unblock)."""
    db = boards_dir / board / "kanban.db"
    if not db.exists():
        return 0
    wm_ev, wm_ts = card_review_reset_watermark(boards_dir, board, tid)
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            ev_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_events)")}
            sel = "id" + (", created_at" if "created_at" in ev_cols else "")
            rows = conn.execute(
                f"SELECT {sel} FROM task_events WHERE task_id = ? AND kind = 'changes_requested'",
                (tid,),
            ).fetchall()
            count = 0
            for r in rows:
                if wm_ev > 0 and int(r["id"]) <= wm_ev:
                    continue
                if wm_ts > 0.0 and "created_at" in ev_cols:
                    ev_ts = _parse_ts(r["created_at"])
                    if ev_ts is not None and (ev_ts < wm_ts or (ev_ts == wm_ts and wm_ev == 0)):
                        continue
                count += 1
            return count
    except sqlite3.Error:
        return 0


def set_board_project(run_fn: Callable, hermes_py: str, slug: str, project_id: str) -> None:
    run_fn([
        hermes_py, "-c",
        "import sys; from hermes_cli import kanban_db as kb; "
        "kb.write_board_metadata(sys.argv[1], project_id=sys.argv[2])",
        slug, project_id,
    ])


def leave_triage(run_fn: Callable, hermes_py: str, boards_dir: Path, board: str, task_id: str) -> None:
    run_fn([
        hermes_py, "-c",
        "import sys; from pathlib import Path; from hermes_cli import kanban_db_connect as k, "
        "kanban_db as kb; c = k.connect(Path(sys.argv[1])); "
        "sys.exit(0 if kb.specify_triage_task(c, sys.argv[2]) else 1)",
        str(boards_dir / board / "kanban.db"), task_id,
    ])


def replay_from(run_fn: Callable, hermes_py: str, boards_dir: Path, board: str, sub: dict, event_id: int) -> bool:
    p = run_fn([
        hermes_py, "-c",
        "import sys; from pathlib import Path; "
        "from hermes_cli import kanban_db_connect as k, kanban_db_notify as n; "
        "c = k.connect(Path(sys.argv[1])); "
        "print(int(n.rewind_notify_cursor(c, task_id=sys.argv[2], platform=sys.argv[3], chat_id=sys.argv[4], "
        "thread_id=sys.argv[5] or None, claimed_cursor=int(sys.argv[6]), old_cursor=int(sys.argv[7])))); c.close()",
        str(boards_dir / board / "kanban.db"), sub["task_id"], sub["platform"], sub["chat_id"],
        sub["thread_id"] or "", str(sub["last_event_id"]), str(event_id - 1),
    ])
    return p.stdout.strip() == "1"


def enable_goal_mode(run_fn: Callable, hermes_py: str, boards_dir: Path, board: str, tid: str) -> bool:
    db = boards_dir / board / "kanban.db"
    if not db.exists():
        return False
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hermes_cli import kanban_db_connect as k, kanban_db as kb\n"
        "c = k.connect(Path(sys.argv[1]))\n"
        "try:\n"
        "    with kb.write_txn(c):\n"
        "        cur = c.execute(\n"
        "            \"UPDATE tasks SET goal_mode = 1 WHERE id = ? \"\n"
        "            \"AND status NOT IN ('done', 'archived') AND NOT goal_mode\",\n"
        "            (sys.argv[2],),\n"
        "        )\n"
        "        print(cur.rowcount)\n"
        "finally:\n"
        "    c.close()\n"
    )
    try:
        p = run_fn([hermes_py, "-c", code, str(db), tid], check=False)
    except FileNotFoundError:
        with closing(sqlite3.connect(db)) as c, c:
            cur = c.execute(
                "UPDATE tasks SET goal_mode = 1 WHERE id = ? AND status NOT IN ('done', 'archived') AND NOT goal_mode",
                (tid,),
            )
            return cur.rowcount == 1
    if p.returncode != 0:
        print(f"devflow: enable_goal_mode failed for {board}/{tid}: {(p.stderr or p.stdout).strip()[-200:]}",
              file=sys.stderr)
        return False
    return p.stdout.strip() == "1"


def mark_plan_needs_input(
    run_fn: Callable, hermes_py: str, boards_dir: Path, board: str, epic: str, reason: str,
) -> tuple[int | None, bool]:
    db = boards_dir / board / "kanban.db"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hermes_cli import kanban_db_connect as k, kanban_db as kb\n"
        "c = k.connect(Path(sys.argv[1]))\n"
        "try:\n"
        "    with kb.write_txn(c):\n"
        "        cur = c.execute(\n"
        "            \"UPDATE tasks SET status = CASE WHEN status IN ('todo', 'ready', 'scheduled') THEN 'blocked' ELSE status END, \"\n"
        "            \"block_kind = 'needs_input' WHERE id = ? AND status NOT IN ('done', 'archived')\",\n"
        "            (sys.argv[2],),\n"
        "        )\n"
        "        if cur.rowcount != 1:\n"
        "            sys.exit(2)\n"
        "        kb._append_event(c, sys.argv[2], 'blocked', {'kind': 'needs_input', 'reason': sys.argv[3]})\n"
        "        eid = c.execute('SELECT last_insert_rowid()').fetchone()[0]\n"
        "    print(eid)\n"
        "finally:\n"
        "    c.close()\n"
    )
    p = None
    try:
        p = run_fn([hermes_py, "-c", code, str(db), epic, reason], check=False)
    except FileNotFoundError:
        p = None
    if p is not None and p.returncode == 0 and p.stdout.strip().isdigit():
        return int(p.stdout.strip()), False
    if p is not None and p.returncode == 2:
        return None, False
    if db.exists():
        try:
            with closing(sqlite3.connect(db)) as c, c:
                cur = c.execute(
                    "UPDATE tasks SET status = CASE WHEN status IN ('todo', 'ready', 'scheduled') THEN 'blocked' ELSE status END, "
                    "block_kind = 'needs_input' WHERE id = ? AND status NOT IN ('done', 'archived')",
                    (epic,),
                )
                if cur.rowcount != 1:
                    return (0, False) if (p is not None and p.returncode == 0) else (None, False)
                ev = c.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, ?)",
                    (epic, json.dumps({"kind": "needs_input", "reason": reason}, ensure_ascii=False), int(time.time())),
                )
                return int(ev.lastrowid or 0), False
        except sqlite3.Error as err:
            if p is not None and p.returncode == 0:
                return 0, False
            print(f"devflow: plan rule escalation sqlite fallback for {board}/{epic} failed: {err}", file=sys.stderr)
            return None, True
    if p is not None and p.returncode != 0:
        print(f"devflow: plan rule escalation for {board}/{epic} failed: "
              f"{(p.stderr or p.stdout).strip()[-300:]}", file=sys.stderr)
        return None, True
    return (0, False) if p is not None else (None, False)


def mark_triage_needs_input(
    run_fn: Callable, hermes_py: str, boards_dir: Path, board: str, task_id: str, reason: str,
) -> tuple[int | None, bool]:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hermes_cli import kanban_db_connect as k, kanban_db as kb\n"
        "c = k.connect(Path(sys.argv[1]))\n"
        "try:\n"
        "    with kb.write_txn(c):\n"
        "        cur = c.execute(\n"
        "            \"UPDATE tasks SET block_kind = 'needs_input' WHERE id = ? AND status = 'triage' \"\n"
        "            \"AND (block_kind IS NULL OR block_kind NOT IN ('needs_input', 'capability'))\",\n"
        "            (sys.argv[2],),\n"
        "        )\n"
        "        if cur.rowcount != 1:\n"
        "            sys.exit(2)\n"
        "        kb._append_event(c, sys.argv[2], 'blocked', {'kind': 'needs_input', 'reason': sys.argv[3]})\n"
        "        eid = c.execute('SELECT last_insert_rowid()').fetchone()[0]\n"
        "    print(eid)\n"
        "finally:\n"
        "    c.close()\n"
    )
    p = run_fn([hermes_py, "-c", code, str(boards_dir / board / "kanban.db"), task_id, reason], check=False)
    if p.returncode != 0:
        if p.returncode != 2:
            print(f"devflow: stalled triage escalation for {board}/{task_id} failed: "
                  f"{(p.stderr or p.stdout).strip()[-300:]}", file=sys.stderr)
            return None, True
        return None, False
    out = p.stdout.strip()
    return (int(out), False) if out.isdigit() else (None, True)


def hold_task_dependency(
    run_fn: Callable, hermes_py: str, boards_dir: Path, board: str, task_id: str, reason: str,
) -> tuple[bool, bool]:
    """Durably park a pending [SHIP] card in `blocked (dependency)` while its EPIC awaits a user
    plan-risk decision. Returns `(held_or_already_blocked, transient_failure)`."""
    db = boards_dir / board / "kanban.db"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hermes_cli import kanban_db_connect as k, kanban_db as kb\n"
        "c = k.connect(Path(sys.argv[1]))\n"
        "try:\n"
        "    with kb.write_txn(c):\n"
        "        cur = c.execute(\n"
        "            \"UPDATE tasks SET status = 'blocked', block_kind = 'dependency' \"\n"
        "            \"WHERE id = ? AND status IN ('todo', 'ready', 'scheduled')\",\n"
        "            (sys.argv[2],),\n"
        "        )\n"
        "        if cur.rowcount == 1:\n"
        "            kb._append_event(c, sys.argv[2], 'blocked', {'kind': 'dependency', 'reason': sys.argv[3]})\n"
        "        print(cur.rowcount)\n"
        "finally:\n"
        "    c.close()\n"
    )
    p = None
    try:
        p = run_fn([hermes_py, "-c", code, str(db), task_id, reason], check=False)
    except FileNotFoundError:
        p = None
    if p is not None and p.returncode == 0 and p.stdout.strip().isdigit():
        return p.stdout.strip() == "1", False
    if db.exists():
        try:
            with closing(sqlite3.connect(db)) as c, c:
                cur = c.execute(
                    "UPDATE tasks SET status = 'blocked', block_kind = 'dependency' "
                    "WHERE id = ? AND status IN ('todo', 'ready', 'scheduled')",
                    (task_id,),
                )
                if cur.rowcount == 1:
                    c.execute(
                        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, ?)",
                        (task_id, json.dumps({"kind": "dependency", "reason": reason}, ensure_ascii=False), int(time.time())),
                    )
                return cur.rowcount == 1, False
        except sqlite3.Error as err:
            print(f"devflow: hold_task_dependency sqlite fallback for {board}/{task_id} failed: {err}", file=sys.stderr)
            return False, True
    if p is not None and p.returncode != 0:
        print(f"devflow: hold_task_dependency for {board}/{task_id} failed: "
              f"{(p.stderr or p.stdout).strip()[-300:]}", file=sys.stderr)
        return False, True
    return False, False
