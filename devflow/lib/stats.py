"""Read-only workflow telemetry (`devflow stats`) over Kanban SQLite and devflow state files."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from . import kanban_adapter

CARD_TAG_RE = re.compile(r"^\s*\[([A-Z]+)\]")
WORK_KINDS = ("RES", "ARCH", "DEV", "OPS", "SHIP")
PR_KINDS = ("DEV", "OPS")


def _card_kind(title: str | None) -> str:
    m = CARD_TAG_RE.match(title or "")
    return m.group(1) if m else "OTHER"


def _load_json_ro(path: Path, warnings: list[str], label: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        warnings.append(f"{label}: unreadable ({type(err).__name__})")
        return {}
    if not isinstance(data, dict):
        warnings.append(f"{label}: expected JSON object")
        return {}
    return data


def _extract_root_cause(result_raw: str | None) -> str | None:
    text = (result_raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        for key in ("root_cause", "rootCause"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:120]
        meta = parsed.get("metadata")
        if isinstance(meta, dict):
            val = meta.get("root_cause")
            if isinstance(val, str) and val.strip():
                return val.strip()[:120]
    m = re.search(r'"root_cause"\s*:\s*"([^"]{1,120})"', text)
    return m.group(1).strip() if m else None


def collect_board_stats(
    boards_dir: Path,
    slug: str,
    repo: str,
    *,
    judgments: dict[str, Any] | None = None,
    plan_risk_state: dict[str, Any] | None = None,
    human_approvals: dict[str, Any] | None = None,
    max_review_rounds: int = 2,
) -> dict[str, Any]:
    """Compute bounded read-only delivery metrics for one registered board."""
    db = boards_dir / slug / "kanban.db"
    by_kind: dict[str, int] = {}
    epics = {"total": 0, "done": 0, "open": 0, "archived": 0}
    work_cards_non_archived = 0
    pr_cards_completed = 0
    pr_cards_non_archived = 0
    first_pass_count = 0
    total_changes_requested = 0
    tripped_cards = 0
    adj_total = 0
    adj_done = 0
    adj_open = 0
    root_causes: Counter[str] = Counter()
    ship_total = 0
    ship_done = 0
    ship_open = 0
    post_hoc_patch_total = 0
    total_cards = 0
    db_available = False
    error: str | None = None

    if db.is_file():
        try:
            with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "tasks" in tables:
                    db_available = True
                    t_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
                    want_cols = [c for c in ("id", "title", "status", "created_by", "result", "created_at") if c in t_cols]
                    rows = [dict(r) for r in conn.execute(f"SELECT {', '.join(want_cols)} FROM tasks")]

                    cr_by_task: dict[str, int] = {}
                    if "task_events" in tables:
                        for er in conn.execute(
                            "SELECT task_id, COUNT(*) AS cnt FROM task_events WHERE kind = 'changes_requested' GROUP BY task_id"
                        ):
                            cr_by_task[str(er["task_id"])] = int(er["cnt"])

                    for r in rows:
                        tid = str(r.get("id") or "")
                        status = str(r.get("status") or "").lower()
                        kind = _card_kind(r.get("title"))
                        total_cards += 1
                        by_kind[kind] = by_kind.get(kind, 0) + 1

                        if kind == "EPIC":
                            epics["total"] += 1
                            if status == "done":
                                epics["done"] += 1
                            elif status == "archived":
                                epics["archived"] += 1
                            else:
                                epics["open"] += 1
                        if kind in WORK_KINDS and status != "archived":
                            work_cards_non_archived += 1
                        if kind in PR_KINDS:
                            cr = cr_by_task.get(tid, 0)
                            if status != "archived":
                                pr_cards_non_archived += 1
                                total_changes_requested += cr
                                if status != "done" and max_review_rounds > 0:
                                    post_wm = kanban_adapter.card_changes_requested_count(boards_dir, slug, tid)
                                    if post_wm >= max_review_rounds:
                                        tripped_cards += 1
                            if status == "done":
                                pr_cards_completed += 1
                                if cr == 0:
                                    first_pass_count += 1
                            if str(r.get("created_by") or "").lower() == "devops" and status != "archived":
                                post_hoc_patch_total += 1
                        elif kind == "ADJ" and status != "archived":
                            adj_total += 1
                            if status == "done":
                                adj_done += 1
                            else:
                                adj_open += 1
                            if rc := _extract_root_cause(r.get("result")):
                                root_causes[rc] += 1
                        elif kind == "SHIP" and status != "archived":
                            ship_total += 1
                            if status == "done":
                                ship_done += 1
                            else:
                                ship_open += 1
        except sqlite3.Error as err:
            error = f"sqlite_error: {type(err).__name__}"

    # Gate telemetry from state JSON files (matched by repo or board slug)
    j_prefix = f"{repo.lower()}#"
    j_entries = [
        v for k, v in (judgments or {}).items()
        if isinstance(k, str) and k.lower().startswith(j_prefix) and isinstance(v, dict)
    ]
    j_findings = sum(1 for v in j_entries if v.get("findings"))
    j_human = sum(1 for v in j_entries if v.get("needs_human_approval"))

    pending_map = (human_approvals or {}).get("_pending")
    pending_human = 0
    if isinstance(pending_map, dict):
        pending_human = sum(
            1 for v in pending_map.values()
            if isinstance(v, dict) and str(v.get("repo") or "").lower() == repo.lower()
        )

    pending_plan_risk = sum(
        1 for v in (plan_risk_state or {}).values()
        if isinstance(v, dict) and v.get("board") == slug and v.get("status") == "pending"
    )

    non_archived_epics = epics["done"] + epics["open"]
    avg_work_per_epic = round(work_cards_non_archived / non_archived_epics, 2) if non_archived_epics else 0.0
    first_pass_rate = round(first_pass_count / pr_cards_completed, 4) if pr_cards_completed else None
    avg_cr = round(total_changes_requested / pr_cards_non_archived, 2) if pr_cards_non_archived else 0.0

    out: dict[str, Any] = {
        "slug": slug,
        "repo": repo,
        "db_available": db_available,
        "epics": epics,
        "cards": {
            "total": total_cards,
            "by_kind": dict(sorted(by_kind.items())),
            "avg_work_cards_per_epic": avg_work_per_epic,
        },
        "review": {
            "pr_cards_completed": pr_cards_completed,
            "first_pass_count": first_pass_count,
            "first_pass_rate": first_pass_rate,
            "total_changes_requested": total_changes_requested,
            "avg_changes_requested_per_pr_card": avg_cr,
            "currently_tripped_cards": tripped_cards,
        },
        "adjustments": {
            "total": adj_total,
            "done": adj_done,
            "open": adj_open,
            "top_root_causes": [{"root_cause": rc, "count": cnt} for rc, cnt in root_causes.most_common(5)],
        },
        "shipping": {
            "total": ship_total,
            "done": ship_done,
            "open": ship_open,
            "post_hoc_patch_total": post_hoc_patch_total,
        },
        "gates": {
            "pr_judgments_total": len(j_entries),
            "pr_judgments_with_findings": j_findings,
            "pr_judgments_needs_human_approval": j_human,
            "pending_human_approvals": pending_human,
            "pending_plan_risk_gates": pending_plan_risk,
        },
    }
    if error:
        out["error"] = error
    return out


def collect_all_stats(
    boards_dir: Path,
    repos_state: dict[str, dict[str, Any]],
    *,
    state_dir: Path,
    slug: str | None = None,
    max_review_rounds: int = 2,
) -> dict[str, Any]:
    """Collect read-only telemetry across one or all registered boards plus aggregate totals."""
    warnings: list[str] = []
    judgments = _load_json_ro(state_dir / "pr-judgments.json", warnings, "pr-judgments.json")
    plan_risk = _load_json_ro(state_dir / "plan-risk.json", warnings, "plan-risk.json")
    human_approvals = _load_json_ro(state_dir / "human-approvals.json", warnings, "human-approvals.json")

    targets = (
        [(slug, repos_state[slug])] if slug is not None
        else sorted(repos_state.items(), key=lambda kv: kv[0])
    )
    boards = [
        collect_board_stats(
            boards_dir,
            s,
            str(meta.get("repo") or ""),
            judgments=judgments,
            plan_risk_state=plan_risk,
            human_approvals=human_approvals,
            max_review_rounds=max_review_rounds,
        )
        for s, meta in targets
    ]

    tot_epics_done = sum(b["epics"]["done"] for b in boards)
    tot_epics_open = sum(b["epics"]["open"] for b in boards)
    tot_pr_done = sum(b["review"]["pr_cards_completed"] for b in boards)
    tot_first_pass = sum(b["review"]["first_pass_count"] for b in boards)
    totals = {
        "boards": len(boards),
        "epics_done": tot_epics_done,
        "epics_open": tot_epics_open,
        "pr_cards_completed": tot_pr_done,
        "first_pass_rate": round(tot_first_pass / tot_pr_done, 4) if tot_pr_done else None,
        "total_changes_requested": sum(b["review"]["total_changes_requested"] for b in boards),
        "currently_tripped_cards": sum(b["review"]["currently_tripped_cards"] for b in boards),
        "total_adj": sum(b["adjustments"]["total"] for b in boards),
        "post_hoc_patch_total": sum(b["shipping"]["post_hoc_patch_total"] for b in boards),
        "pending_human_approvals": sum(b["gates"]["pending_human_approvals"] for b in boards),
        "pending_plan_risk_gates": sum(b["gates"]["pending_plan_risk_gates"] for b in boards),
    }
    return {"boards": boards, "totals": totals, "warnings": warnings}


def format_stats_text(report: dict[str, Any]) -> str:
    """Render concise, human-readable telemetry summary for terminal output."""
    lines: list[str] = []
    boards = report.get("boards") or []
    if not boards:
        lines.append("no registered repos")
    for b in boards:
        ep = b["epics"]
        rev = b["review"]
        adj = b["adjustments"]
        shp = b["shipping"]
        gt = b["gates"]
        fp_str = f"{rev['first_pass_rate'] * 100:.0f}%" if rev["first_pass_rate"] is not None else "n/a"
        status_tag = f" [error: {b['error']}]" if b.get("error") else ("" if b["db_available"] else " [no db]")
        lines.append(
            f"{b['slug']} ({b['repo']}){status_tag}: "
            f"EPIC done={ep['done']}/open={ep['open']} "
            f"avg_work={b['cards']['avg_work_cards_per_epic']} | "
            f"PR first_pass={fp_str} ({rev['first_pass_count']}/{rev['pr_cards_completed']}) "
            f"cr={rev['total_changes_requested']} tripped={rev['currently_tripped_cards']} | "
            f"ADJ={adj['total']} patch={shp['post_hoc_patch_total']} | "
            f"gates: pending_approval={gt['pending_human_approvals']} pending_plan_risk={gt['pending_plan_risk_gates']}"
        )
        if adj["top_root_causes"]:
            top = ", ".join(f"{x['root_cause']}({x['count']})" for x in adj["top_root_causes"][:3])
            lines.append(f"  top root_causes: {top}")
    if len(boards) > 1:
        t = report["totals"]
        fp_all = f"{t['first_pass_rate'] * 100:.0f}%" if t["first_pass_rate"] is not None else "n/a"
        lines.append(
            f"TOTAL ({t['boards']} boards): EPIC done={t['epics_done']}/open={t['epics_open']} | "
            f"first_pass={fp_all} cr={t['total_changes_requested']} tripped={t['currently_tripped_cards']} | "
            f"ADJ={t['total_adj']} patch={t['post_hoc_patch_total']} | "
            f"pending_approval={t['pending_human_approvals']} pending_plan_risk={t['pending_plan_risk_gates']}"
        )
    for w in report.get("warnings") or []:
        lines.append(f"warning: {w}")
    return "\n".join(lines)
