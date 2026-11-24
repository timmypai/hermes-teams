"""Trust boundary helpers: dedicated judgment credential selection and base-vs-head harness hash lock."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .scope import match_glob

# Trusted acceptance & verification harness files whose modification changes what CI proves.
TRUSTED_HARNESS_RE = re.compile(
    r"^(\.devflow/(acceptance\.py|check\.py|check\.sh|verify-base\.sh|deploy\.sh|capture\.sh|redact\.py|"
    r"up\.sh|playwright\.sh|rules/)|\.github/workflows/)"
)


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
        prev = (f.get("previous_filename") or "").strip() or None
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
