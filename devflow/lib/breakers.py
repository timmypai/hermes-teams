"""Loop breakers and cost/round budgets to stop endless review ping-pong and runaway EPICs."""
from __future__ import annotations

from datetime import datetime, timezone

REVIEW_EFFECTIVE_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})


def _iso_to_ts(raw: str | None) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def count_review_rejections(
    reviews: list[dict],
    reviewer_login: str,
    *,
    since_iso: str | None = None,
) -> tuple[int, bool]:
    """Count `CHANGES_REQUESTED` reviews by `reviewer_login` (strictly after `since_iso` when set)
    and whether the latest effective review is APPROVED."""
    mine = [
        r for r in (reviews or [])
        if (r.get("user") or "").lower() == (reviewer_login or "").lower()
        and r.get("state") in REVIEW_EFFECTIVE_STATES
    ]
    ordered = sorted(mine, key=lambda r: (_iso_to_ts(r.get("submitted_at")) or 0.0, r.get("submitted_at") or ""))
    wm_ts = _iso_to_ts(since_iso)
    rejections = 0
    for r in ordered:
        if r.get("state") != "CHANGES_REQUESTED":
            continue
        if wm_ts is not None:
            r_ts = _iso_to_ts(r.get("submitted_at"))
            if r_ts is not None and r_ts <= wm_ts:
                continue
        rejections += 1
    approved_latest = bool(ordered and ordered[-1].get("state") == "APPROVED")
    return rejections, approved_latest


def review_round_breaker(
    *,
    github_rejections: int,
    kanban_rejections: int,
    approved_latest: bool,
    max_rounds: int,
    reset_at: str | None = None,
) -> dict:
    """Evaluate whether a PR/card has hit the review ping-pong circuit breaker.

    If `approved_latest` is True, the breaker does not trip.
    Otherwise, `rounds = max(github_rejections, kanban_rejections)`.
    Tripped when `max_rounds > 0 and rounds >= max_rounds`.
    """
    rounds = max(int(github_rejections or 0), int(kanban_rejections or 0))
    tripped = (not approved_latest) and max_rounds > 0 and rounds >= max_rounds
    reason = ""
    if tripped:
        reason = (
            f"review 退件已達 {rounds} 輪（上限 {max_rounds} 輪）：停止盲目往返修補，"
            "請先由 architect／orchestrator 重檢根因或以 needs_input 請使用者定錨"
        )
    out = {
        "rounds": rounds,
        "max_rounds": max_rounds,
        "tripped": tripped,
        "reason": reason,
        "action": "stop_ping_pong_escalate" if tripped else "continue",
    }
    if reset_at:
        out["reset_at"] = reset_at
    return out


def epic_card_cap_finding(epic: str, cards: dict[str, dict], card_type_fn, max_epic_cards: int) -> str | None:
    """Return an actionable finding when an EPIC accumulates too many non-archived work cards."""
    if max_epic_cards <= 0:
        return None
    work_cards = [
        tid for tid, c in cards.items()
        if tid != epic
        and c.get("status") != "archived"
        and card_type_fn(c.get("title") or "") in ("RES", "ARCH", "DEV", "OPS", "SHIP")
    ]
    if len(work_cards) <= max_epic_cards:
        return None
    return (
        f"{epic}: 本 EPIC 已有 {len(work_cards)} 張工作卡（上限 {max_epic_cards}）："
        "請收斂範圍或拆分為獨立 EPIC，停止繼續追加卡片"
    )
