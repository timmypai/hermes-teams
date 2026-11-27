"""Declarative, interruptible, and resumable workflow pipeline state machine."""
from __future__ import annotations

import re
from typing import Any

from . import policy as policy_lib

PIPELINE_RE = re.compile(r"^\s*Pipeline[：:]\s*([A-Za-z0-9_-]+)\s*$", re.M | re.I)
START_AT_RE = re.compile(r"^\s*Start-At[：:]\s*([A-Za-z0-9_-]+)\s*$", re.M | re.I)
STOP_AFTER_RE = re.compile(r"^\s*Stop-After[：:]\s*([A-Za-z0-9_-]+)\s*$", re.M | re.I)
PAUSED_AT_RE = re.compile(r"^\s*Paused-At[：:]\s*([A-Za-z0-9_-]+)\s*$", re.M | re.I)
_EXPLICIT_MODE_RE = re.compile(r"^\s*Mode[：:]\s*([A-Za-z0-9_-]+)\s*$", re.M | re.I)


def parse_epic_pipeline(
    epic_body: str | None,
    policy: dict[str, Any] | None = None,
    *,
    epic_mode: str = "deliver",
) -> dict[str, Any]:
    """Parse and validate `Pipeline:`, `Start-At:`, `Stop-After:`, and `Paused-At:` on an EPIC body.

    Invalid pipelines, unknown step names, or `Start-At` later than `Stop-After` mark `valid=False`
    with explicit findings so downstream checks fail closed.
    """
    text = epic_body or ""
    pipelines = policy_lib.pipelines_config(policy)
    problems: list[str] = []

    raw_pipeline_m = PIPELINE_RE.search(text)
    raw_pipeline = raw_pipeline_m.group(1).strip().lower() if raw_pipeline_m else None
    mode_m = _EXPLICIT_MODE_RE.search(text)
    explicit_mode = mode_m.group(1).strip().lower() if mode_m else None
    resolved_mode = (epic_mode or explicit_mode or "deliver").strip().lower()

    if raw_pipeline is not None:
        if raw_pipeline not in pipelines:
            allowed = " / ".join(sorted(pipelines))
            problems.append(f"Pipeline '{raw_pipeline}' 無效（允許：{allowed}）")
            pipeline_name = resolved_mode if resolved_mode in pipelines else "deliver"
        else:
            pipeline_name = raw_pipeline
            if pipeline_name != resolved_mode:
                problems.append(
                    f"Pipeline '{pipeline_name}' 與有效 Mode '{resolved_mode}' 不一致（請明確宣告 Mode: {pipeline_name}）"
                )
    else:
        if resolved_mode not in pipelines:
            allowed = " / ".join(sorted(pipelines))
            problems.append(f"Pipeline '{resolved_mode}' 無效（允許：{allowed}）")
            pipeline_name = "deliver"
        else:
            pipeline_name = resolved_mode

    cfg = pipelines.get(pipeline_name) or pipelines["deliver"]
    steps = [str(s).strip().lower() for s in (cfg.get("steps") or []) if str(s).strip()]
    default_start = str(cfg.get("default_start") or (steps[0] if steps else "repro")).strip().lower()
    default_stop = str(cfg.get("default_stop") or (steps[-1] if steps else "accept")).strip().lower()

    raw_start_m = START_AT_RE.search(text)
    raw_stop_m = STOP_AFTER_RE.search(text)
    raw_paused_m = PAUSED_AT_RE.search(text)

    start_at = raw_start_m.group(1).strip().lower() if raw_start_m else default_start
    stop_after = raw_stop_m.group(1).strip().lower() if raw_stop_m else default_stop
    paused_at = raw_paused_m.group(1).strip().lower() if raw_paused_m else None

    if start_at not in steps:
        problems.append(f"Start-At '{start_at}' 不在 {pipeline_name} 步驟中（允許：{' / '.join(steps)}）")
    if stop_after not in steps:
        problems.append(f"Stop-After '{stop_after}' 不在 {pipeline_name} 步驟中（允許：{' / '.join(steps)}）")
    if paused_at is not None and paused_at not in steps:
        problems.append(f"Paused-At '{paused_at}' 不在 {pipeline_name} 步驟中（允許：{' / '.join(steps)}）")

    active_steps: list[str] = []
    next_step: str | None = None
    if start_at in steps and stop_after in steps:
        s_idx = steps.index(start_at)
        e_idx = steps.index(stop_after)
        if s_idx > e_idx:
            problems.append(f"Start-At ({start_at}) 晚於 Stop-After ({stop_after})，無法執行")
        else:
            active_steps = steps[s_idx : e_idx + 1]
            if e_idx + 1 < len(steps):
                next_step = steps[e_idx + 1]

    valid = not problems and bool(active_steps)
    is_partial = bool(steps) and (start_at != steps[0] or stop_after != steps[-1])
    return {
        "pipeline": pipeline_name,
        "steps": steps,
        "start_at": start_at,
        "stop_after": stop_after,
        "paused_at": paused_at,
        "active_steps": active_steps,
        "next_step": next_step,
        "is_partial": is_partial,
        "has_explicit_stop": raw_stop_m is not None,
        "valid": valid,
        "problems": problems,
    }


def requires_ship_for_epic(spec: dict[str, Any], mode_cfg: dict[str, Any]) -> bool:
    """Whether missing `[SHIP]` is a plan defect. Fails closed to strict mode_cfg when spec is invalid."""
    need_ship = bool(mode_cfg.get("require_ship", True))
    if not need_ship:
        return False
    if not spec.get("valid", True):
        return True
    if "ship" not in (spec.get("steps") or []):
        return False
    return "ship" in (spec.get("active_steps") or [])


def requires_scenarios_for_epic(spec: dict[str, Any], mode_cfg: dict[str, Any]) -> bool:
    """Whether executable AC scenarios are required for the planned active slice."""
    if not bool(mode_cfg.get("require_scenarios", True)):
        return False
    if not spec.get("valid", True):
        return True
    active = set(spec.get("active_steps") or [])
    # Stopping strictly before implementation (repro / design only) needs no scenarios yet.
    return not active.isdisjoint({"implement", "ci", "review", "ship", "accept"})


def _strip_pipeline_headers(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines():
        if START_AT_RE.match(line) or STOP_AFTER_RE.match(line) or PAUSED_AT_RE.match(line):
            continue
        kept.append(line)
    out = "\n".join(kept).lstrip("\n")
    return out + ("\n" if text.endswith("\n") and not out.endswith("\n") else "")


def advance_epic_body(
    epic_body: str | None,
    policy: dict[str, Any] | None = None,
    *,
    stop_after: str | None = None,
    to_end: bool = False,
    epic_mode: str = "deliver",
) -> tuple[str, dict[str, Any]]:
    """Advance an EPIC body forward from its current `Stop-After` (and clear any `Paused-At`)."""
    if stop_after and to_end:
        raise ValueError("stop_after 與 to_end 不可同時指定")
    spec = parse_epic_pipeline(epic_body, policy, epic_mode=epic_mode)
    if not spec["valid"]:
        raise ValueError("EPIC Pipeline 設定無效：" + "；".join(spec["problems"]))
    steps = spec["steps"]
    cur_stop = spec["stop_after"]
    cur_idx = steps.index(cur_stop)
    if cur_idx >= len(steps) - 1:
        raise ValueError(f"EPIC 流程已到最後步驟 '{cur_stop}'，無需再推進")

    if to_end:
        target_stop = steps[-1]
    elif stop_after:
        target_stop = stop_after.strip().lower()
        if target_stop not in steps:
            raise ValueError(f"Stop-After '{target_stop}' 不在 {spec['pipeline']} 步驟中（允許：{' / '.join(steps)}）")
        if steps.index(target_stop) <= cur_idx:
            raise ValueError(f"目標 Stop-After '{target_stop}' 必須晚於目前步驟 '{cur_stop}'")
    else:
        target_stop = steps[cur_idx + 1]

    new_start = steps[cur_idx + 1]
    stripped = _strip_pipeline_headers(epic_body or "")
    lines = stripped.splitlines()
    insert_at = 0
    for idx, line in enumerate(lines):
        if _EXPLICIT_MODE_RE.match(line) or PIPELINE_RE.match(line):
            insert_at = idx + 1
        elif line.strip():
            break
    header_lines = [f"Start-At: {new_start}", f"Stop-After: {target_stop}"]
    merged_lines = [*lines[:insert_at], *header_lines, *lines[insert_at:]]
    new_body = "\n".join(merged_lines).rstrip() + "\n"
    new_spec = parse_epic_pipeline(new_body, policy, epic_mode=epic_mode)
    return new_body, new_spec


def card_step_for_kind(
    kind: str,
    spec: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> str | None:
    """Return the pipeline step name owning `kind` (`DEV`/`OPS`/`SHIP`/etc.), or None for meta cards."""
    upper = (kind or "").strip().upper()
    if not upper or upper in ("EPIC", "ADJ", "ADV"):
        return None
    pipe_cfg = (policy_lib.pipelines_config(policy).get(spec.get("pipeline") or "") or {})
    step_cards = pipe_cfg.get("step_cards") or {}
    for step_name, raw_kinds in step_cards.items():
        kinds = [raw_kinds] if isinstance(raw_kinds, str) else list(raw_kinds or [])
        if any(str(k).strip().upper() == upper for k in kinds):
            return str(step_name).strip().lower()
    return None


def is_card_beyond_stop(
    kind: str,
    spec: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> bool:
    """True when `spec` is valid and `kind` belongs strictly after `spec['stop_after']`."""
    if not spec.get("valid", True):
        return False
    steps = list(spec.get("steps") or [])
    stop_after = str(spec.get("stop_after") or "").strip().lower()
    if stop_after not in steps:
        return False
    step = card_step_for_kind(kind, spec, policy)
    if not step or step not in steps:
        return False
    return steps.index(step) > steps.index(stop_after)


def card_step_span(
    kind: str,
    spec: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> tuple[int, int] | None:
    """Return inclusive `(start_idx, end_idx)` in `spec['steps']` owned by `kind`.

    A card type owns from its mapped step through any immediately following steps that have no
    separate card entry in `step_cards` (for example, `DEV`/`OPS` at `implement` remain active
    through `ci` and `review` before `SHIP` takes over at `ship`).
    """
    if not spec.get("valid", True):
        return None
    steps = list(spec.get("steps") or [])
    start_step = card_step_for_kind(kind, spec, policy)
    if not start_step or start_step not in steps:
        return None
    lo = steps.index(start_step)
    pipe_cfg = policy_lib.pipelines_config(policy).get(spec.get("pipeline") or "") or {}
    assigned_steps = {str(k).strip().lower() for k, v in (pipe_cfg.get("step_cards") or {}).items() if v}
    hi = lo
    while hi + 1 < len(steps) and steps[hi + 1] not in assigned_steps:
        hi += 1
    return lo, hi


def is_card_before_start(
    kind: str,
    spec: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> bool:
    """True when `spec` is valid and `kind`'s entire lifecycle span lies strictly before `spec['start_at']`."""
    span = card_step_span(kind, spec, policy)
    if span is None:
        return False
    steps = list(spec.get("steps") or [])
    start_at = str(spec.get("start_at") or "").strip().lower()
    if start_at not in steps:
        return False
    return span[1] < steps.index(start_at)


def is_step_active(step: str, spec: dict[str, Any]) -> bool:
    """True when `spec` is valid and `step` is inside the EPIC's active slice."""
    if not spec.get("valid", True):
        return False
    target = (step or "").strip().lower()
    return target in {str(s).strip().lower() for s in (spec.get("active_steps") or [])}


def mark_epic_paused_at(
    epic_body: str | None,
    step: str,
    policy: dict[str, Any] | None = None,
    *,
    epic_mode: str = "deliver",
) -> tuple[str, dict[str, Any]]:
    """Record idempotent `Paused-At: <step>` header on an EPIC body without duplicating headers."""
    text = epic_body or ""
    spec = parse_epic_pipeline(text, policy, epic_mode=epic_mode)
    if not spec["valid"]:
        raise ValueError("EPIC Pipeline 設定無效：" + "；".join(spec["problems"]))
    target = (step or "").strip().lower()
    if target not in spec["steps"]:
        raise ValueError(f"Paused-At '{target}' 不在 {spec['pipeline']} 步驟中")

    kept = [line for line in text.splitlines() if not PAUSED_AT_RE.match(line)]
    insert_at = 0
    for idx, line in enumerate(kept):
        if _EXPLICIT_MODE_RE.match(line) or PIPELINE_RE.match(line) or START_AT_RE.match(line) or STOP_AFTER_RE.match(line):
            insert_at = idx + 1
        elif line.strip():
            break
    kept.insert(insert_at, f"Paused-At: {target}")
    new_body = "\n".join(kept).rstrip() + "\n"
    return new_body, parse_epic_pipeline(new_body, policy, epic_mode=epic_mode)


def slice_stop_reached(
    cards: dict[str, dict[str, Any]],
    epic: str,
    spec: dict[str, Any],
    policy: dict[str, Any] | None = None,
    *,
    step_evidence: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """True when a partial pipeline (`stop_after != terminal`) has satisfied every card overlapping `active_steps`.

    Cards whose span continues beyond `stop_after` (such as `[DEV]`/`[OPS]` across `implement -> ci -> review`
    under `Stop-After: ci`) count as completed up to `stop_after` when they are already in `review` with
    verified exact-head `step_evidence` for `stop_after`.
    """
    if not spec.get("valid"):
        return False
    steps = list(spec.get("steps") or [])
    start_at = str(spec.get("start_at") or "").strip().lower()
    stop_after = str(spec.get("stop_after") or "").strip().lower()
    if not steps or start_at not in steps or stop_after not in steps or stop_after == steps[-1]:
        return False
    s_idx, e_idx = steps.index(start_at), steps.index(stop_after)
    ev_map = step_evidence or {}
    has_cards = False
    for cid, c in cards.items():
        if cid == epic or c.get("status") == "archived":
            continue
        title = str(c.get("title") or "").lstrip()
        kind = title[1:].split("]", 1)[0] if title.startswith("[") and "]" in title else ""
        span = card_step_span(kind, spec, policy)
        if span is None or span[1] < s_idx or span[0] > e_idx:
            continue
        has_cards = True
        status = str(c.get("status") or "").strip().lower()
        if status in ("done", "archived"):
            continue
        if span[0] <= e_idx < span[1]:
            ev = ev_map.get(cid)
            if (
                stop_after == "ci"
                and (
                    status == "review"
                    or (status == "blocked" and str(c.get("block_kind") or "").strip().lower() == "dependency")
                )
                and isinstance(ev, dict)
                and str(ev.get("step") or "").strip().lower() == "ci"
                and ev.get("ok") is True
                and bool(str(ev.get("head_sha") or "").strip())
                and ev.get("pr_number") is not None
            ):
                continue
            if (
                stop_after == "implement"
                and status == "blocked"
                and str(c.get("block_kind") or "").strip().lower() == "dependency"
                and isinstance(ev, dict)
                and str(ev.get("step") or "").strip().lower() == "implement"
                and ev.get("ok") is True
                and ev.get("local_only") is True
                and bool(str(ev.get("head_sha") or "").strip())
                and bool(str(ev.get("branch") or "").strip())
            ):
                continue
        return False
    return has_cards


def is_user_paused_card(task: dict[str, Any]) -> bool:
    """True when a card is explicitly parked by/for the user (`scheduled` or waiting on `needs_input`)."""
    status = str(task.get("status") or "").strip().lower()
    block_kind = str(task.get("block_kind") or "").strip().lower()
    if status == "scheduled":
        return True
    if status in ("blocked", "triage") and block_kind == "needs_input":
        return True
    return False
