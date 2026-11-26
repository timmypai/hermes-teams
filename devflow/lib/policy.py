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

DEFAULT_MODES: dict[str, dict[str, Any]] = {
    "deliver": {
        "require_scenarios": True,
        "require_ship": True,
        "allow_dev_cards": True,
        "res_assignees": ["researcher"],
        "res_workspaces": ["scratch"],
    },
    "investigate": {
        "require_scenarios": False,
        "require_ship": False,
        "allow_dev_cards": False,
        "res_assignees": ["researcher", "devops"],
        "res_workspaces": ["scratch", "dir"],
    },
    "review": {
        "require_scenarios": False,
        "require_ship": False,
        "allow_dev_cards": False,
        "res_assignees": ["researcher", "reviewer"],
        "res_workspaces": ["scratch", "dir"],
    },
}

DEFAULT_PIPELINES: dict[str, dict[str, Any]] = {
    "deliver": {
        "steps": ["repro", "design", "implement", "ci", "review", "ship", "accept"],
        "default_start": "repro",
        "default_stop": "accept",
        "step_cards": {"repro": "RES", "design": "ARCH", "implement": "DEV", "ship": "SHIP", "accept": "EPIC"},
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

DEFAULT_HANDOFF_SCHEMAS: dict[str, dict[str, list[str]]] = {
    "repro": {
        "required": ["root_cause", "evidence"],
        "optional": ["affected_paths", "non_goals", "open_questions"],
    },
    "design": {
        "required": ["decisions", "target_files"],
        "optional": ["interfaces", "non_goals", "risks", "allowed_scope", "file_plan", "pr_slices", "adr_markdown"],
    },
    "implement": {
        "required": ["pr_url", "head_sha", "tests"],
        "optional": ["published_pr", "changed_files", "followups"],
    },
    "ship": {
        "required": ["merged_sha"],
        "optional": ["deployed_sha", "acceptance_report", "followups"],
    },
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
    "default_test_paths": list(DEFAULT_TEST_PATHS),
    "modes": {k: dict(v) for k, v in DEFAULT_MODES.items()},
    "pipelines": {k: dict(v) for k, v in DEFAULT_PIPELINES.items()},
    "handoff_schemas": {k: dict(v) for k, v in DEFAULT_HANDOFF_SCHEMAS.items()},
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


def repo_test_paths(repo_cfg: dict[str, str], policy: dict[str, Any] | None = None) -> list[str]:
    """Optional comma-separated declared test path globs from `.devflow/config.env` (`DEVFLOW_TEST_PATHS`),
    falling back to declarative `policy.default_test_paths` when `policy` is given.
    Set `DEVFLOW_TEST_PATHS=none` (or `off`/`false`) in a repo to disable default test path exemptions."""
    raw = (repo_cfg.get("DEVFLOW_TEST_PATHS") or "").strip()
    if raw:
        if raw.lower() in ("none", "off", "false", "0"):
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]
    if policy is not None:
        return [str(p).strip() for p in (policy.get("default_test_paths") or []) if str(p).strip()]
    return []


def valid_modes(policy: dict[str, Any] | None = None) -> frozenset[str]:
    modes = (policy or {}).get("modes")
    if isinstance(modes, dict) and modes:
        return frozenset(str(k).strip().lower() for k in modes if str(k).strip())
    return frozenset(DEFAULT_MODES)


def mode_config(policy: dict[str, Any] | None, mode: str) -> dict[str, Any]:
    """Resolve declarative workflow rules for `mode` (`deliver` | `investigate` | `review`)."""
    key = (mode or "deliver").strip().lower()
    modes = (policy or {}).get("modes")
    base = DEFAULT_MODES.get(key) or DEFAULT_MODES["deliver"]
    override = modes.get(key) if isinstance(modes, dict) and isinstance(modes.get(key), dict) else {}
    return {**base, **override}


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


def handoff_schemas(policy: dict[str, Any] | None = None) -> dict[str, dict[str, list[str]]]:
    """Return declarative handoff schemas keyed by pipeline step."""
    out = {k: {sk: list(sv) for sk, sv in v.items()} for k, v in DEFAULT_HANDOFF_SCHEMAS.items()}
    raw = (policy or {}).get("handoff_schemas")
    if isinstance(raw, dict):
        for name, spec in raw.items():
            key = str(name or "").strip().lower()
            if key and isinstance(spec, dict):
                out[key] = {
                    "required": [str(x).strip() for x in (spec.get("required") or []) if str(x).strip()],
                    "optional": [str(x).strip() for x in (spec.get("optional") or []) if str(x).strip()],
                }
    return out
