"""Encapsulated adapter for Hermes Kanban SQLite & internal write helpers.

Centralizes all direct `kanban.db` queries and `hermes_cli.kanban_db` calls that used to be
scattered as inline Python strings across `bin/devflow`.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Callable


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
    """Count `changes_requested` events recorded on a card in kanban.db (0 if table/db absent)."""
    db = boards_dir / board / "kanban.db"
    if not db.exists():
        return 0
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'changes_requested'",
                (tid,),
            ).fetchone()
            return int(row[0]) if row else 0
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
        "            \"UPDATE tasks SET status = CASE WHEN status IN ('todo', 'scheduled') THEN 'blocked' ELSE status END, \"\n"
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
                    "UPDATE tasks SET status = CASE WHEN status IN ('todo', 'scheduled') THEN 'blocked' ELSE status END, "
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
