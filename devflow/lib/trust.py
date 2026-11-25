"""Trust boundary helpers: dedicated judgment credential selection, unified protected-file rules,
base-vs-head harness hash lock, and exact-head human approval records."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .scope import match_glob

# Canonical delivery machinery every devflow repo shares (or deploys via runner hooks).
# Note: `.devflow/verify.sh` is intentionally NOT listed here — adding CLI/lib acceptance
# scenarios is everyday [DEV] work, verified by `verify-base` + `check acceptance`.
DELIVERY_FILES_RE = re.compile(
    r"^(\.devflow/(deploy\.sh|deploy-build\.sh|deploy-post\.sh|capture\.sh|redact\.py|"
    r"up\.sh|playwright\.sh|operate\.sh|acceptance\.py|verify-base\.sh)|"
    r"\.github/workflows/deploy\.yml)$"
)

# Verification harness, declarative repo policy config, and GitHub workflows whose edits change
# what CI/preflight/judgment checks prove or enforce.
CONFIG_AND_GATE_RE = re.compile(
    r"^(\.devflow/(config\.env|check\.py|check\.sh|test\.sh|rules/)|\.github/workflows/)"
)

# Single source of truth for protected harness / gate files shared by preflight, check pr, and ship merge.
TRUSTED_HARNESS_RE = re.compile(CONFIG_AND_GATE_RE.pattern + "|" + DELIVERY_FILES_RE.pattern)

# Exact-head human approval directive under EPIC `## 使用者決定`:
#   - 核准保護檔變更：PR #12 <40-hex-sha>
HUMAN_APPROVAL_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?核准保護檔變更[：:][ \t]*PR[ \t]*#?(\d+)[ \t]+([0-9a-fA-F]{40})[ \t]*$",
    re.M,
)

# Exact plan-risk consent directive under EPIC `## 使用者決定`:
#   - 核准計畫風險：t_ep01 <12-hex-plan-hash>
PLAN_RISK_APPROVAL_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?核准計畫風險[：:][ \t]*(t_[0-9A-Za-z]+)[ \t]+([0-9a-fA-F]{12})[ \t]*$",
    re.M,
)

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PLAN_HASH_RE = re.compile(r"^[0-9a-f]{12}$")


def sha256_text(text: str | bytes) -> str:
    data = text.encode("utf-8", errors="replace") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()[:16]


def judgment_gh_role(roles: dict[str, Any]) -> tuple[str, str | None, bool]:
    """Resolve which GitHub credential posts `devflow-judgment`.

    Returns `(role_name, custom_gh_dir_or_none, isolated)`:
      - If `DEVFLOW_JUDGMENT_GH_DIR` env is set and exists -> `("judgment", path, True)`
      - Else if `roles["gh"]` defines `"judgment"` and differs from `"builder"` -> `("judgment", None, True)`
      - Else falls back to `"builder"` with `isolated=False` so existing 2-account deployments keep working
        while explicitly reporting `judgment_token_isolated=False`.
    """
    gh_dirs = roles.get("gh") or {}
    env_dir = os.environ.get("DEVFLOW_JUDGMENT_GH_DIR", "").strip()
    if env_dir:
        return "judgment", env_dir, Path(env_dir).resolve() != Path(str(gh_dirs.get("builder") or "")).resolve()
    if "judgment" in gh_dirs and gh_dirs["judgment"]:
        isolated = str(gh_dirs["judgment"]) != str(gh_dirs.get("builder") or "")
        return "judgment", None, isolated
    return "builder", None, False


def is_protected_file(filename: str, extra_patterns: list[str] | None = None) -> bool:
    if TRUSTED_HARNESS_RE.match(filename):
        return True
    return any(match_glob(filename, pat) for pat in (extra_patterns or []))


def protected_file_changes(
    files: list[dict],
    *,
    extra_patterns: list[str] | None = None,
    base_hashes: dict[str, str | None] | None = None,
    head_hashes: dict[str, str | None] | None = None,
) -> list[dict]:
    """Inspect changed files (including rename source paths) against trusted harness + repo protected paths."""
    out: list[dict] = []
    for f in sorted(files, key=lambda x: (x.get("previous_filename") or "", x["filename"])):
        name = f["filename"]
        prev = f.get("previous_filename") or None
        name_protected = is_protected_file(name, extra_patterns)
        prev_protected = bool(prev and is_protected_file(prev, extra_patterns))
        if not (name_protected or prev_protected):
            continue
        item: dict[str, Any] = {"filename": name, "status": f.get("status", "modified")}
        if prev:
            item["previous_filename"] = prev
        old_key = prev or name
        if base_hashes is not None and old_key in base_hashes:
            item["base_sha256"] = base_hashes[old_key]
        if head_hashes is not None and name in head_hashes:
            item["head_sha256"] = head_hashes[name]
        if prev and prev != name:
            # Moving a file into or out of a protected path always alters the protected tree.
            item["hash_changed"] = True
        elif item.get("base_sha256") and item.get("head_sha256"):
            item["hash_changed"] = item["base_sha256"] != item["head_sha256"]
        else:
            item["hash_changed"] = True
        out.append(item)
    return out


def epic_human_approvals(epic_body: str | None) -> set[tuple[int, str]]:
    """Exact `(pr_number, full_40_sha_lower)` pairs explicitly authorized under EPIC `## 使用者決定`."""
    m = re.search(r"^##\s*使用者決定[ \t]*\n(.*?)(?=^## |\Z)", epic_body or "", flags=re.M | re.S)
    if not m:
        return set()
    return {(int(hit.group(1)), hit.group(2).lower()) for hit in HUMAN_APPROVAL_RE.finditer(m.group(1))}


def epic_plan_risk_approvals(epic_body: str | None) -> set[tuple[str, str]]:
    """Exact `(epic_id, plan_hash_lower)` pairs explicitly accepted under EPIC `## 使用者決定`."""
    m = re.search(r"^##\s*使用者決定[ \t]*\n(.*?)(?=^## |\Z)", epic_body or "", flags=re.M | re.S)
    if not m:
        return set()
    return {(hit.group(1), hit.group(2).lower()) for hit in PLAN_RISK_APPROVAL_RE.finditer(m.group(1))}


def load_human_approvals(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_human_approval(
    path: Path,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    note: str = "",
    approved_by: str = "operator",
) -> dict[str, Any]:
    """Atomically record exact-head human approval for a PR touching protected files."""
    head = (head_sha or "").strip().lower()
    if not FULL_SHA_RE.fullmatch(head):
        raise ValueError(f"head_sha must be a full 40-char lowercase hex SHA, got {head_sha!r}")
    key = f"{repo}#{pr_number}"
    data = load_human_approvals(path)
    rec: dict[str, Any] = {
        "repo": repo,
        "pr": int(pr_number),
        "head_sha": head,
        "approved_at": int(time.time()),
        "approved_by": approved_by,
        "note": note,
    }
    data[key] = rec
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return rec


def is_human_approved(
    approvals_path: Path,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    epic_body: str | None = None,
) -> tuple[bool, str]:
    """Verify exact-head human approval via `state/human-approvals.json` or EPIC `## 使用者決定`.

    Fails closed when `head_sha` is empty, malformed, or does not equal the recorded full 40-hex SHA.
    """
    head = (head_sha or "").strip().lower()
    if not FULL_SHA_RE.fullmatch(head):
        return False, ""
    rec = load_human_approvals(approvals_path).get(f"{repo}#{pr_number}")
    if isinstance(rec, dict) and str(rec.get("head_sha") or "").lower() == head:
        return True, "state/human-approvals.json"
    for num, approved_sha in epic_human_approvals(epic_body):
        if num == int(pr_number) and approved_sha == head:
            return True, "EPIC ## 使用者決定"
    return False, ""
