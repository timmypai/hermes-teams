"""Declarative policy and per-repo configuration loader for devflow.

Resolution order (highest precedence first):
  1. Environment overrides (DEVFLOW_*)
  2. Runtime host/kit config (<DEVFLOW_ROOT>/config.json, optional)
  3. Kit defaults in roles.json (`policy` / `host` sections)
  4. Safe built-in fallback defaults

Per-repo overrides live in `<repo>/.devflow/config.env`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

VALID_MERGE_METHODS = frozenset({"squash", "merge", "rebase"})
VALID_PLAN_RISK_MODES = frozenset({"off", "needs_input", "advisor"})

DEFAULT_TEST_PATHS: list[str] = [
    "tests/**",
    "test/**",
    "**/*.test.*",
    "**/*.spec.*",
    ".devflow/ui/**",
    ".devflow/verify.sh",
]

DEFAULT_ARTIFACT_PATHS: list[str] = [
    ".devflow/lessons/**",
    ".devflow/scenarios.json",
]

DEFAULT_CARD_EXECUTORS: dict[str, list[dict[str, str]]] = {
    "RES": [
        {"assignee": "researcher", "workspace": "scratch"},
        {"assignee": "devops", "workspace": "dir"},
        {"assignee": "reviewer", "workspace": "dir"},
    ],
}

DEFAULT_MODES: dict[str, dict[str, Any]] = {
    "deliver": {
        "allow_write": True,
        "require_scenarios": True,
        "require_ship": True,
        "allow_dev_cards": True,
        "delivery_card_kinds": ["DEV", "OPS", "SHIP"],
        "card_executors": {
            "RES": [
                {"assignee": "researcher", "workspace": "scratch"},
                {"assignee": "devops", "workspace": "dir"},
            ]
        },
        "res_assignees": ["researcher", "devops"],
        "res_workspaces": ["scratch", "dir"],
    },
    "investigate": {
        "allow_write": False,
        "require_scenarios": False,
        "require_ship": False,
        "allow_dev_cards": False,
        "delivery_card_kinds": ["RES"],
        "card_executors": {
            "RES": [
                {"assignee": "researcher", "workspace": "scratch"},
                {"assignee": "devops", "workspace": "dir"},
            ]
        },
        "res_assignees": ["researcher", "devops"],
        "res_workspaces": ["scratch", "dir"],
    },
    "review": {
        "allow_write": False,
        "require_scenarios": False,
        "require_ship": False,
        "allow_dev_cards": False,
        "delivery_card_kinds": ["RES", "SHIP"],
        "card_executors": {
            "RES": [
                {"assignee": "researcher", "workspace": "scratch"},
                {"assignee": "reviewer", "workspace": "dir"},
            ]
        },
        "res_assignees": ["researcher", "reviewer"],
        "res_workspaces": ["scratch", "dir"],
    },
}

DEFAULT_PIPELINES: dict[str, dict[str, Any]] = {
    "deliver": {
        "steps": ["repro", "design", "implement", "ci", "review", "ship", "accept"],
        "default_start": "repro",
        "default_stop": "accept",
        "step_cards": {"repro": "RES", "design": "ARCH", "implement": ["DEV", "OPS"], "ship": "SHIP", "accept": "EPIC"},
    },
    "investigate": {
        "steps": ["repro", "accept"],
        "default_start": "repro",
        "default_stop": "accept",
        "step_cards": {"repro": "RES", "accept": "EPIC"},
    },
    "review": {
        "steps": ["review", "accept"],
        "default_start": "review",
        "default_stop": "accept",
        "step_cards": {"review": "RES", "accept": "EPIC"},
    },
}

DEFAULT_HANDOFF_SCHEMAS: dict[str, dict[str, Any]] = {
    "repro": {
        "required": ["root_cause", "evidence"],
        "optional": [
            "affected_paths", "non_goals", "open_questions",
            "recommendation", "findings", "sources", "risks", "unverified",
        ],
    },
    "design": {
        "required": ["decisions", "target_files"],
        "optional": ["interfaces", "non_goals", "risks", "allowed_scope", "file_plan", "pr_slices", "adr_markdown"],
    },
    "implement": {
        "required": ["pr_url", "head_sha", "tests"],
        "optional": ["published_pr", "changed_files", "followups"],
    },
    "implement_local": {
        "required": ["branch", "head_sha", "tests", "changed_files"],
        "optional": ["base_ref", "summary", "followups"],
    },
    "ship": {
        "required": [],
        "required_when": [
            {"when": {"shipped": False}, "required": ["reason"]},
            {"default": True, "required": ["merged_sha"]},
        ],
        "optional": [
            "shipped", "reason", "merged_sha", "deployed_sha",
            "deploy_run_url", "integration_run_url", "acceptance_report", "verify", "followups",
        ],
    },
}

DEFAULT_CARD_HANDOFF_RULES: dict[str, Any] = {
    "cards": ["DEV", "OPS"],
    "done_heading": "## 本卡完成",
    "done_text": "完成條件：PR 已開、exact-head CI 綠。審查在本卡自己的 review 欄進行，不是 [SHIP] 的事；合併與部署由 [SHIP] 負責。",
    "excluded_sections": ["本卡完成"],
    "excluded_fields": ["Non-Goals", "Out-of-Scope", "Allowed-Scope", "Verify", "EPIC"],
    "negation_prefixes": [
        r"不(?:做|負責|含|包含|需|需要|要求|得|能|可|判|視為|宣稱|涵蓋|代替|替代|進行|涉|碰)",
        r"未(?:完成|取得|經|做|合併|部署|審查|批准|驗收|上線|通過)",
        r"(?:非|不是|不屬|不在)本卡(?:職責|範圍|任務|完成條件)?",
        r"排除",
        r"(?:交)?由\s*\[?SHIP\]?\s*負責",
        r"留待(?:後續|修正版|真人|\[?SHIP\]?)",
    ],
    "post_handoff_clauses": [
        {
            "id": "downstream_review_approval",
            "stage": "review",
            "label": "下游獨立審查／批准條件",
            "patterns": [
                r"(?:獨立|正式|原生|exact[- ]?head|非作者)\s*(?:GitHub\s*)?(?:審查|review)",
                r"(?:審查|審核|review)\s*(?:通過|核准|批准|放行|同意|成立|結果|結論|限|交由)",
                r"\b(?:APPROVED|CHANGES_REQUESTED)\b",
                r"(?:kanban_request_review|review\s*欄(?:完成|通過|核准|執行|審查))",
            ],
        },
        {
            "id": "downstream_merge_deploy",
            "stage": "ship",
            "label": "下游合併／部署條件",
            "patterns": [
                r"devflow\s+ship\s+merge",
                r"(?:合併|merge)\s*(?:PR|至\s*main|到\s*main|並\s*部署|後\s*部署|上線|出貨)",
                r"(?:PR|pull\s*request|分支|本卡|變更|修正)\s*(?:須|需|要|應|已)?\s*(?:合併|merge|部署上線)",
                r"(?:部署|deploy)\s*(?:上線|至\s*(?:正式|線上|Oracle|主機)|後\s*(?:驗收|驗證|上線|交接))",
                r"\[SHIP\]\s*(?:出貨|合併|部署)",
            ],
        },
        {
            "id": "downstream_human_acceptance",
            "stage": "accept",
            "label": "下游真人／線上驗收條件",
            "patterns": [
                r"(?:真人|使用者|本人|人工)\s*(?:實測|聽驗|線上)?\s*(?:驗收|測試|確認)\s*(?:通過|完成|成功)",
                r"(?:等待|完成|通過)\s*(?:真人|本人|使用者)\s*(?:驗收|聽驗)",
                r"真實\s*Teams\s*會議\s*(?:驗收|聽驗|確認)",
            ],
        },
    ],
}

DEFAULT_POLICY: dict[str, Any] = {
    "port_base": 18100,
    "card_chars": 600,
    "judge_chars": 2000,
    "adj_per_card": 2,
    "ship_patch_limit": 2,
    "runner_stale_seconds": 300,
    "runner_wait_seconds": 120,
    "max_review_rounds": 2,
    "max_epic_cards": 12,
    "max_diff_files": 30,
    "advisor_retry_seconds": 30,
    "advisor_max_retries": 6,
    "default_merge_method": "squash",
    "auto_plan_advisor": False,
    "plan_risk_mode": "off",
    "auto_flow_adj": False,
    "require_human_approval_for_protected": True,
    "require_allowed_scope": True,
    "max_role_prompt_chars": 12000,
    "acceptance_source_budget": 80000,
    "acceptance_evidence_budget": 20000,
    "default_test_paths": list(DEFAULT_TEST_PATHS),
    "default_artifact_paths": list(DEFAULT_ARTIFACT_PATHS),
    "card_executors": {k: [dict(item) for item in v] for k, v in DEFAULT_CARD_EXECUTORS.items()},
    "modes": {k: dict(v) for k, v in DEFAULT_MODES.items()},
    "pipelines": {k: dict(v) for k, v in DEFAULT_PIPELINES.items()},
    "handoff_schemas": {k: dict(v) for k, v in DEFAULT_HANDOFF_SCHEMAS.items()},
    "card_handoff_rules": dict(DEFAULT_CARD_HANDOFF_RULES),
}


def _unquote_env(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def env_values(text: str) -> dict[str, str]:
    pairs = (line.partition("=") for line in (text or "").splitlines() if not line.lstrip().startswith("#"))
    return {k.strip(): _unquote_env(v) for k, sep, v in pairs if sep and k.strip()}


def load_runtime_config(root: Path) -> dict[str, Any]:
    path = root / "config.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_policy(root: Path, roles: dict[str, Any]) -> dict[str, Any]:
    """Merge built-in defaults, roles.json `policy`, optional `<root>/config.json` `policy`, and env."""
    cfg = dict(DEFAULT_POLICY)
    for src in (roles.get("policy"), load_runtime_config(root).get("policy")):
        if isinstance(src, dict):
            for k, v in src.items():
                if v is not None:
                    cfg[k] = v
    int_env = {
        "DEVFLOW_PORT_BASE": "port_base",
        "DEVFLOW_CARD_CHARS": "card_chars",
        "DEVFLOW_ADJ_PER_CARD": "adj_per_card",
        "DEVFLOW_SHIP_PATCH_LIMIT": "ship_patch_limit",
        "DEVFLOW_MAX_REVIEW_ROUNDS": "max_review_rounds",
        "DEVFLOW_MAX_EPIC_CARDS": "max_epic_cards",
        "DEVFLOW_MAX_DIFF_FILES": "max_diff_files",
        "DEVFLOW_ACCEPTANCE_SOURCE_BUDGET": "acceptance_source_budget",
        "DEVFLOW_ACCEPTANCE_EVIDENCE_BUDGET": "acceptance_evidence_budget",
    }
    for env_key, pol_key in int_env.items():
        raw = os.environ.get(env_key, "").strip()
        if raw.isdigit():
            cfg[pol_key] = int(raw)
    if (mm := os.environ.get("DEVFLOW_DEFAULT_MERGE_METHOD", "").strip().lower()) in VALID_MERGE_METHODS:
        cfg["default_merge_method"] = mm
    for env_key, pol_key in (
        ("DEVFLOW_AUTO_PLAN_ADVISOR", "auto_plan_advisor"),
        ("DEVFLOW_AUTO_FLOW_ADJ", "auto_flow_adj"),
        ("DEVFLOW_REQUIRE_HUMAN_APPROVAL_FOR_PROTECTED", "require_human_approval_for_protected"),
        ("DEVFLOW_REQUIRE_ALLOWED_SCOPE", "require_allowed_scope"),
    ):
        raw = os.environ.get(env_key, "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            cfg[pol_key] = True
        elif raw in ("0", "false", "no", "off"):
            cfg[pol_key] = False
    if (prm := os.environ.get("DEVFLOW_PLAN_RISK_MODE", "").strip().lower()) in VALID_PLAN_RISK_MODES:
        cfg["plan_risk_mode"] = prm
    elif str(cfg.get("plan_risk_mode") or "").strip().lower() not in VALID_PLAN_RISK_MODES:
        cfg["plan_risk_mode"] = "off"
    return cfg


def resolve_host_ssh(root: Path, roles: dict[str, Any]) -> str:
    """Resolve the SSH target for `devflow repo access` without hardcoding a specific host IP."""
    for candidate in (
        os.environ.get("DEVFLOW_HOST_SSH", "").strip(),
        str((load_runtime_config(root).get("host") or {}).get("ssh") or "").strip(),
        str((roles.get("host") or {}).get("ssh") or "").strip(),
    ):
        if candidate:
            return candidate
    return "<user>@<host>"


def load_repo_config(repo_path: Path | str | None, *, fallback_text: str | None = None) -> dict[str, str]:
    """Read `<repo>/.devflow/config.env`, falling back to remote `fallback_text` if provided."""
    if repo_path:
        p = Path(repo_path) / ".devflow" / "config.env"
        if p.is_file():
            return env_values(p.read_text(encoding="utf-8", errors="replace"))
    if fallback_text:
        return env_values(fallback_text)
    return {}


def repo_merge_method(repo_cfg: dict[str, str], policy: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return `(method, error)`. Fails closed when `DEVFLOW_MERGE_METHOD` is set to an unknown value."""
    raw = (repo_cfg.get("DEVFLOW_MERGE_METHOD") or "").strip().lower()
    if not raw:
        default = str(policy.get("default_merge_method") or "squash").lower()
        return (default, None) if default in VALID_MERGE_METHODS else (None, f"invalid default merge method {default!r}")
    if raw not in VALID_MERGE_METHODS:
        allowed = ", ".join(sorted(VALID_MERGE_METHODS))
        return None, f"invalid DEVFLOW_MERGE_METHOD={raw!r} in .devflow/config.env (allowed: {allowed})"
    return raw, None


def repo_protected_paths(repo_cfg: dict[str, str]) -> list[str]:
    """Optional comma-separated extra protected path globs from `.devflow/config.env`."""
    raw = (repo_cfg.get("DEVFLOW_PROTECTED_PATHS") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _repo_path_list(
    repo_cfg: dict[str, str],
    env_key: str,
    policy: dict[str, Any] | None,
    policy_key: str,
) -> list[str]:
    raw = (repo_cfg.get(env_key) or "").strip()
    if raw:
        if raw.lower() in ("none", "off", "false", "0"):
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]
    if policy is not None:
        return [str(p).strip() for p in (policy.get(policy_key) or []) if str(p).strip()]
    return []


def repo_test_paths(repo_cfg: dict[str, str], policy: dict[str, Any] | None = None) -> list[str]:
    """Optional comma-separated declared test path globs from `.devflow/config.env` (`DEVFLOW_TEST_PATHS`),
    falling back to declarative `policy.default_test_paths` when `policy` is given.
    Set `DEVFLOW_TEST_PATHS=none` (or `off`/`false`) in a repo to disable default test path exemptions."""
    return _repo_path_list(repo_cfg, "DEVFLOW_TEST_PATHS", policy, "default_test_paths")


def repo_artifact_paths(repo_cfg: dict[str, str], policy: dict[str, Any] | None = None) -> list[str]:
    """Optional comma-separated declared devflow artifact globs from `.devflow/config.env`
    (`DEVFLOW_ARTIFACT_PATHS`), falling back to `policy.default_artifact_paths` when `policy` is given.
    Set `DEVFLOW_ARTIFACT_PATHS=none` (or `off`/`false`/`0`) in a repo to disable default artifact exemptions."""
    return _repo_path_list(repo_cfg, "DEVFLOW_ARTIFACT_PATHS", policy, "default_artifact_paths")


def valid_modes(policy: dict[str, Any] | None = None) -> frozenset[str]:
    modes = (policy or {}).get("modes")
    if isinstance(modes, dict) and modes:
        return frozenset(str(k).strip().lower() for k in modes if str(k).strip())
    return frozenset(DEFAULT_MODES)


def mode_config(policy: dict[str, Any] | None, mode: str) -> dict[str, Any]:
    """Resolve declarative workflow boundaries for `mode` (`deliver` | `investigate` | `review`)."""
    key = (mode or "deliver").strip().lower()
    modes = (policy or {}).get("modes")
    base = DEFAULT_MODES.get(key) or DEFAULT_MODES["deliver"]
    override = modes.get(key) if isinstance(modes, dict) and isinstance(modes.get(key), dict) else {}
    return {**base, **override}


def _executor_pairs(raw: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if not isinstance(raw, list):
        return pairs
    for item in raw:
        if not isinstance(item, dict):
            continue
        asg = str(item.get("assignee") or "").strip()
        ws = str(item.get("workspace") or "").strip()
        if asg and ws and (asg, ws) not in pairs:
            pairs.append((asg, ws))
    return pairs


def allowed_executors_for_card(
    policy: dict[str, Any] | None,
    mode_cfg: dict[str, Any] | None,
    kind: str,
    default_assignee: str,
    default_workspace: str,
) -> list[tuple[str, str]]:
    """Return allowed `(assignee, workspace)` executor pairs for card `kind`."""
    k = (kind or "").strip().upper()
    m_exec = (mode_cfg or {}).get("card_executors")
    if isinstance(m_exec, dict) and k in m_exec:
        if pairs := _executor_pairs(m_exec.get(k)):
            return pairs
    if k == "RES" and mode_cfg:
        raw_asg = [str(x).strip() for x in (mode_cfg.get("res_assignees") or []) if str(x).strip()]
        raw_ws = [str(x).strip() for x in (mode_cfg.get("res_workspaces") or []) if str(x).strip()]
        if "card_executors" not in mode_cfg and (raw_asg or raw_ws):
            asgs = raw_asg or [default_assignee]
            wss = raw_ws or [default_workspace]
            return [(a, w) for a in asgs for w in wss]
    p_exec = (policy or {}).get("card_executors")
    if isinstance(p_exec, dict) and k in p_exec:
        if pairs := _executor_pairs(p_exec.get(k)):
            return pairs
    return [(default_assignee, default_workspace)]


def delivery_card_kinds(mode_cfg: dict[str, Any] | None) -> frozenset[str]:
    """Card kinds that enter delivery commitment (triggering full AC coverage checks)."""
    raw = (mode_cfg or {}).get("delivery_card_kinds")
    if isinstance(raw, list):
        kinds = frozenset(str(x).strip().upper() for x in raw if str(x).strip())
        if kinds:
            return kinds
    return frozenset({"DEV", "OPS", "SHIP"})


def obsolete_hooks(roles: dict[str, Any] | None) -> list[dict[str, str]]:
    """Return normalized `{event, command}` pairs declared under `roles.json` `obsolete_hooks`."""
    out: list[dict[str, str]] = []
    for item in (roles or {}).get("obsolete_hooks") or []:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip()
        command = str(item.get("command") or "").strip()
        if event and command:
            out.append({"event": event, "command": command})
    return out


def _strip_hooks_yaml_fallback(text: str, targets: set[tuple[str, str]]) -> tuple[str, int]:
    """Deterministic stdlib fallback for stripping obsolete `hooks.<event>` entries when `ruamel.yaml` is absent."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    removed = 0
    in_hooks = False
    cur_event: str | None = None
    event_indent = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped.startswith("#") or not stripped.strip():
            out.append(line)
            i += 1
            continue
        if indent == 0:
            in_hooks = stripped.rstrip("\r\n") == "hooks:"
            cur_event = None
            out.append(line)
            i += 1
            continue
        if in_hooks and indent > 0 and not stripped.startswith("-") and ":" in stripped and (
            cur_event is None or indent <= event_indent
        ):
            cur_event = stripped.split(":", 1)[0].strip()
            event_indent = indent
            out.append(line)
            i += 1
            continue
        if in_hooks and cur_event and indent > event_indent and stripped.startswith("- "):
            item_indent = indent
            block = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                nxt_s = nxt.lstrip(" ")
                nxt_i = len(nxt) - len(nxt_s)
                if not nxt_s.strip():
                    block.append(nxt)
                    j += 1
                    continue
                if nxt_i <= item_indent:
                    break
                block.append(nxt)
                j += 1
            cmd = ""
            first_rest = stripped[2:].strip()
            if first_rest.startswith("command:"):
                cmd = _unquote_env(first_rest.partition(":")[2])
            else:
                for b in block[1:]:
                    bs = b.lstrip(" ")
                    if bs.startswith("command:"):
                        cmd = _unquote_env(bs.partition(":")[2])
                        break
            if (cur_event, cmd) in targets:
                removed += 1
            else:
                out.extend(block)
            i = j
            continue
        out.append(line)
        i += 1
    return "".join(out), removed


def retire_obsolete_hooks(
    home: Path,
    hooks_spec: list[dict[str, str]],
    *,
    yaml_cls: Any = None,
    backup_fn: Any = None,
    log_fn: Any = None,
) -> int:
    """Idempotently remove stale hooks from `config.yaml` and `shell-hooks-allowlist.json`."""
    targets = {(str(h.get("event") or "").strip(), str(h.get("command") or "").strip()) for h in hooks_spec}
    targets = {(e, c) for e, c in targets if e and c}
    if not targets:
        return 0
    if yaml_cls is None:
        try:
            from ruamel.yaml import YAML as _YamlCls  # type: ignore[import-not-found]
            yaml_cls = _YamlCls
        except ImportError:
            yaml_cls = None
    removed_total = 0
    cfg_path = home / "config.yaml"
    if cfg_path.is_file():
        cfg_removed = 0
        new_cfg_text: str | None = None
        if yaml_cls is not None:
            yaml = yaml_cls()
            if hasattr(yaml, "preserve_quotes"):
                yaml.preserve_quotes = True
            data = yaml.load(cfg_path.read_text(encoding="utf-8")) or {}
            hooks = data.get("hooks") if isinstance(data, dict) else None
            if isinstance(hooks, dict):
                for event, cmd in sorted(targets):
                    items = hooks.get(event)
                    if not isinstance(items, list):
                        continue
                    kept = [
                        it for it in items
                        if not (isinstance(it, dict) and str(it.get("command") or "").strip() == cmd)
                    ]
                    diff = len(items) - len(kept)
                    if diff:
                        cfg_removed += diff
                        if kept:
                            hooks[event] = kept
                        else:
                            hooks.pop(event, None)
                if cfg_removed and not hooks:
                    data.pop("hooks", None)
                if cfg_removed:
                    if backup_fn is not None:
                        backup_fn(cfg_path)
                    with cfg_path.open("w", encoding="utf-8") as fh:
                        yaml.dump(data, fh)
        else:
            raw_text = cfg_path.read_text(encoding="utf-8")
            new_cfg_text, cfg_removed = _strip_hooks_yaml_fallback(raw_text, targets)
            if cfg_removed:
                if backup_fn is not None:
                    backup_fn(cfg_path)
                cfg_path.write_text(new_cfg_text, encoding="utf-8")
        if cfg_removed and log_fn is not None:
            log_fn(f"retired {cfg_removed} obsolete hook(s) from {cfg_path}")
        removed_total += cfg_removed

    allow_path = home / "shell-hooks-allowlist.json"
    if allow_path.is_file():
        try:
            raw_allow = json.loads(allow_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw_allow = None
        if isinstance(raw_allow, dict) and isinstance(raw_allow.get("approvals"), list):
            kept_allow = [
                it for it in raw_allow["approvals"]
                if not (
                    isinstance(it, dict)
                    and (str(it.get("event") or "").strip(), str(it.get("command") or "").strip()) in targets
                )
            ]
            allow_removed = len(raw_allow["approvals"]) - len(kept_allow)
            if allow_removed:
                if backup_fn is not None:
                    backup_fn(allow_path)
                raw_allow["approvals"] = kept_allow
                allow_path.write_text(json.dumps(raw_allow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if log_fn is not None:
                    log_fn(f"retired {allow_removed} obsolete hook approval(s) from {allow_path}")
                removed_total += allow_removed
    return removed_total


def repo_deploys(repo_cfg: dict[str, str]) -> bool:
    """Single source of truth for whether a repo deploys via `deploy.yml` (default True)."""
    raw = (repo_cfg.get("DEVFLOW_DEPLOY") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def repo_max_diff_files(repo_cfg: dict[str, str], policy: dict[str, Any]) -> int:
    raw = (repo_cfg.get("DEVFLOW_MAX_DIFF_FILES") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return int(policy.get("max_diff_files") or DEFAULT_POLICY["max_diff_files"])


def repo_review_test_cmd(repo_cfg: dict[str, str]) -> str:
    return (repo_cfg.get("DEVFLOW_REVIEW_TEST_CMD") or "").strip()


def repo_deploy_manual_only(repo_cfg: dict[str, str]) -> bool:
    return (repo_cfg.get("DEVFLOW_DEPLOY_MANUAL_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")


def repo_allow_custom_delivery(repo_cfg: dict[str, str]) -> bool:
    return (repo_cfg.get("DEVFLOW_ALLOW_CUSTOM_DELIVERY") or "").strip().lower() in ("1", "true", "yes", "on")


def repo_require_allowed_scope(repo_cfg: dict[str, str], policy: dict[str, Any]) -> bool:
    """Whether open [DEV]/[OPS] cards must declare a bounded `Allowed-Scope:`."""
    raw = (repo_cfg.get("DEVFLOW_REQUIRE_ALLOWED_SCOPE") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(policy.get("require_allowed_scope", True))


def repo_require_human_approval(repo_cfg: dict[str, str], policy: dict[str, Any]) -> bool:
    """Whether protected-file PRs require explicit exact-head human approval before `ship merge`."""
    raw = (repo_cfg.get("DEVFLOW_REQUIRE_HUMAN_APPROVAL") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(policy.get("require_human_approval_for_protected", True))


def effective_plan_risk_mode(policy: dict[str, Any]) -> str:
    """Resolve `off` | `needs_input` | `advisor` (`auto_plan_advisor=true` implies `advisor`)."""
    if bool(policy.get("auto_plan_advisor", False)):
        return "advisor"
    mode = str(policy.get("plan_risk_mode") or "off").strip().lower()
    return mode if mode in VALID_PLAN_RISK_MODES else "off"


def pipelines_config(policy: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Return declarative pipeline definitions (`deliver`, `investigate`, `review`), falling back to defaults."""
    out = {k: dict(v) for k, v in DEFAULT_PIPELINES.items()}
    raw = (policy or {}).get("pipelines")
    if isinstance(raw, dict):
        for name, spec in raw.items():
            key = str(name or "").strip().lower()
            if key and isinstance(spec, dict) and spec.get("steps"):
                base = dict(out.get(key) or {})
                base.update(spec)
                out[key] = base
    return out


def _normalize_handoff_schema(spec: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "required": [str(x).strip() for x in (spec.get("required") or []) if str(x).strip()],
        "optional": [str(x).strip() for x in (spec.get("optional") or []) if str(x).strip()],
    }
    raw_when = spec.get("required_when")
    if isinstance(raw_when, list) and raw_when:
        clauses: list[dict[str, Any]] = []
        for item in raw_when:
            if not isinstance(item, dict):
                continue
            clause: dict[str, Any] = {
                "required": [str(x).strip() for x in (item.get("required") or []) if str(x).strip()]
            }
            if isinstance(item.get("when"), dict) and item["when"]:
                clause["when"] = dict(item["when"])
            if item.get("default"):
                clause["default"] = True
            clauses.append(clause)
        if clauses:
            out["required_when"] = clauses
    return out


def handoff_schemas(policy: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Return declarative handoff schemas keyed by pipeline step."""
    out = {k: _normalize_handoff_schema(v) for k, v in DEFAULT_HANDOFF_SCHEMAS.items()}
    raw = (policy or {}).get("handoff_schemas")
    if isinstance(raw, dict):
        for name, spec in raw.items():
            key = str(name or "").strip().lower()
            if key and isinstance(spec, dict):
                out[key] = _normalize_handoff_schema(spec)
    return out


def card_handoff_rules(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized declarative author-handoff rules and post-handoff lint clauses."""
    raw = (policy or {}).get("card_handoff_rules")
    src = raw if isinstance(raw, dict) else {}

    def _str_list(val: Any, fallback: list[str]) -> list[str]:
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        return list(fallback)

    def _clause_list(val: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(val, list):
            return [dict(item) for item in fallback]
        out: list[dict[str, Any]] = []
        for item in val:
            if not isinstance(item, dict):
                continue
            pats = [str(p) for p in (item.get("patterns") or []) if isinstance(p, str) and p.strip()]
            if not pats:
                continue
            out.append({
                "id": str(item.get("id") or "").strip() or f"rule_{len(out) + 1}",
                "stage": str(item.get("stage") or "downstream").strip(),
                "label": str(item.get("label") or item.get("id") or "下游階段條件").strip(),
                "patterns": pats,
            })
        return out

    done_heading = str(src.get("done_heading") or DEFAULT_CARD_HANDOFF_RULES["done_heading"]).strip()
    done_text = str(src.get("done_text") or DEFAULT_CARD_HANDOFF_RULES["done_text"]).strip()
    return {
        "cards": [c.upper() for c in _str_list(src.get("cards"), DEFAULT_CARD_HANDOFF_RULES["cards"])],
        "done_heading": done_heading,
        "done_text": done_text,
        "excluded_sections": _str_list(src.get("excluded_sections"), DEFAULT_CARD_HANDOFF_RULES["excluded_sections"]),
        "excluded_fields": _str_list(src.get("excluded_fields"), DEFAULT_CARD_HANDOFF_RULES["excluded_fields"]),
        "negation_prefixes": _str_list(src.get("negation_prefixes"), DEFAULT_CARD_HANDOFF_RULES["negation_prefixes"]),
        "post_handoff_clauses": _clause_list(
            src.get("post_handoff_clauses"), DEFAULT_CARD_HANDOFF_RULES["post_handoff_clauses"]
        ),
    }
