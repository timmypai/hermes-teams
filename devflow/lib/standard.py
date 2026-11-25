"""Deterministic standard delivery template compliance checker & synchronizer.

Ensures every onboarded repo follows devflow's canonical deployment & diagnostics
pipeline (`.devflow/deploy.sh`, `.devflow/capture.sh`, `.devflow/redact.py`, and
`.github/workflows/deploy.yml`) instead of inventing opaque custom deploy wrappers.
Project-specific differences belong strictly in `.devflow/config.env`, `Dockerfile`,
`deploy/compose.yml`, `.devflow/deploy-build.sh`, `.devflow/test.sh`, and `.devflow/verify.sh`.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .policy import repo_allow_custom_delivery, repo_deploy_manual_only

STANDARD_CORE_FILES: tuple[str, ...] = (
    ".devflow/capture.sh",
    ".devflow/redact.py",
)
STANDARD_DEPLOY_FILES: tuple[str, ...] = (
    ".devflow/deploy.sh",
    ".github/workflows/deploy.yml",
)
ALL_STANDARD_DELIVERY_FILES: tuple[str, ...] = STANDARD_CORE_FILES + STANDARD_DEPLOY_FILES

_PUSH_TRIGGER_RE = re.compile(
    r"^on:\n  push:\n    branches: \[[^\n]+\]\n  workflow_dispatch:\n",
    re.M,
)


def _norm_text(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n") + "\n"


def templates_digest(template_dir: Path) -> str:
    """Stable short digest over all canonical delivery templates present in `template_dir`.

    Returns empty string when `template_dir` does not exist so callers without a
    populated kit template directory retain backwards-compatible fingerprints.
    """
    if not template_dir.is_dir():
        return ""
    h = hashlib.sha256()
    found = 0
    for rel in ALL_STANDARD_DELIVERY_FILES:
        src = template_dir / rel
        if not src.is_file():
            continue
        found += 1
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(_norm_text(src.read_text(encoding="utf-8", errors="replace")).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16] if found else ""


def required_standard_files(repo_cfg: dict[str, str] | None = None) -> tuple[str, ...]:
    cfg = repo_cfg or {}
    deploys = (cfg.get("DEVFLOW_DEPLOY") or "true").strip().lower() != "false"
    return ALL_STANDARD_DELIVERY_FILES if deploys else STANDARD_CORE_FILES


def dependent_standard_paths(
    touched_paths: list[str],
    *,
    base_cfg: dict[str, str] | None = None,
    head_cfg: dict[str, str] | None = None,
) -> list[str]:
    """Return canonical delivery paths that must be validated for `touched_paths`.

    Includes any directly touched required file PLUS dependent canonical files when
    `.devflow/config.env` changes declarative deployment settings (`DEVFLOW_DEPLOY`
    or `DEVFLOW_DEPLOY_MANUAL_ONLY`), even if those dependent files were untouched.
    """
    base = base_cfg or {}
    head = head_cfg or {}
    req_set = set(required_standard_files(head))
    paths: list[str] = [p for p in touched_paths if p in req_set]
    if ".devflow/config.env" in touched_paths:
        base_deploy = (base.get("DEVFLOW_DEPLOY") or "true").strip().lower() != "false"
        head_deploy = (head.get("DEVFLOW_DEPLOY") or "true").strip().lower() != "false"
        base_manual = repo_deploy_manual_only(base)
        head_manual = repo_deploy_manual_only(head)
        if head_deploy and not base_deploy:
            for p in STANDARD_DEPLOY_FILES:
                if p not in paths:
                    paths.append(p)
        if head_deploy and (head_manual != base_manual):
            if ".github/workflows/deploy.yml" not in paths:
                paths.append(".github/workflows/deploy.yml")
    return paths


def render_standard_file(template_dir: Path, rel: str, *, repo_cfg: dict[str, str] | None = None) -> str | None:
    """Render the canonical template text for `rel`, applying declarative trigger options (no hardcoding)."""
    src = template_dir / rel
    if not src.is_file():
        return None
    text = src.read_text(encoding="utf-8")
    if rel == ".github/workflows/deploy.yml" and repo_deploy_manual_only(repo_cfg or {}):
        text = _PUSH_TRIGGER_RE.sub("on:\n  workflow_dispatch:\n", text, count=1)
    return _norm_text(text)


def check_standard_delivery_contents(
    actual_by_path: dict[str, str | None],
    template_dir: Path,
    *,
    repo_cfg: dict[str, str] | None = None,
    only_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Compare provided `{rel_path: content_or_none}` against canonical kit templates."""
    cfg = repo_cfg or {}
    custom_allowed = repo_allow_custom_delivery(cfg)
    req = required_standard_files(cfg)
    targets = [p for p in req if only_paths is None or p in set(only_paths)]
    missing: list[str] = []
    drifted: list[str] = []
    checked: list[str] = []
    for rel in targets:
        expected = render_standard_file(template_dir, rel, repo_cfg=cfg)
        if expected is None:
            continue
        checked.append(rel)
        got = actual_by_path.get(rel)
        if got is None:
            missing.append(rel)
        elif _norm_text(got) != expected:
            drifted.append(rel)

    findings: list[str] = []
    if not custom_allowed:
        if missing:
            findings.append(
                f"缺少 devflow 公版交付檔：{', '.join(missing)}（請執行 `devflow contract <slug> . --sync-standard` 補齊）"
            )
        if drifted:
            findings.append(
                f"偏離 devflow 公版交付檔：{', '.join(drifted)}（所有 repo 一律遵循公版部署與採證流程，"
                "專案客製請放 `.devflow/config.env`、`deploy/compose.yml` 或 `.devflow/deploy-build.sh`，"
                "並以 `devflow contract <slug> . --sync-standard` 還原公版）"
            )
    return {
        "ok": not findings,
        "checked": checked,
        "missing": missing,
        "drifted": drifted,
        "custom_allowed": custom_allowed,
        "findings": findings,
    }


def check_standard_delivery_dir(
    repo_dir: Path,
    template_dir: Path,
    *,
    repo_cfg: dict[str, str] | None = None,
    only_paths: list[str] | None = None,
) -> dict[str, Any]:
    req = required_standard_files(repo_cfg)
    targets = [p for p in req if only_paths is None or p in set(only_paths)]
    actual: dict[str, str | None] = {}
    for rel in targets:
        p = repo_dir / rel
        actual[rel] = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    return check_standard_delivery_contents(actual, template_dir, repo_cfg=repo_cfg, only_paths=targets)


def sync_standard_files(
    repo_dir: Path,
    template_dir: Path,
    *,
    repo_cfg: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Restore/upgrade canonical delivery files in `repo_dir` without touching project-owned files."""
    updated: list[str] = []
    unchanged: list[str] = []
    for rel in required_standard_files(repo_cfg):
        expected = render_standard_file(template_dir, rel, repo_cfg=repo_cfg)
        if expected is None:
            continue
        out = repo_dir / rel
        if out.is_file() and _norm_text(out.read_text(encoding="utf-8", errors="replace")) == expected:
            unchanged.append(rel)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(expected, encoding="utf-8")
        if out.suffix == ".sh":
            out.chmod(0o755)
        updated.append(rel)
    return {"updated": updated, "unchanged": unchanged}
