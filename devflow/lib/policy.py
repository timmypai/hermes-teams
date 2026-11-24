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


def repo_max_diff_files(repo_cfg: dict[str, str], policy: dict[str, Any]) -> int:
    raw = (repo_cfg.get("DEVFLOW_MAX_DIFF_FILES") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return int(policy.get("max_diff_files") or DEFAULT_POLICY["max_diff_files"])


def repo_review_test_cmd(repo_cfg: dict[str, str]) -> str:
    return (repo_cfg.get("DEVFLOW_REVIEW_TEST_CMD") or "").strip()
