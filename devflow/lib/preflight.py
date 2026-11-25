"""Developer preflight CLI gate (`devflow preflight`): deterministic local checks before PR/review."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .policy import (
    env_values,
    load_repo_config,
    repo_max_diff_files,
    repo_protected_paths,
    repo_require_allowed_scope,
    repo_review_test_cmd,
    repo_test_paths,
)
from .scope import allowed_scope_problems, check_files_against_scope, parse_card_contract, touched_paths
from .standard import check_standard_delivery_dir, dependent_standard_paths
from .trust import is_protected_file, protected_file_changes, sha256_text

FIX_TITLE_RE = re.compile(r"^\s*(fix|revert)(\([^)]*\))?!?:", re.I)
LESSON_LINE_RE = re.compile(r"^\s*(Lesson:\s*L-\d+|No-lesson:\s*\S.{9,})", re.M)
COVERS_RE = re.compile(r"^\s*Covers:\s*(.+)$", re.M)
SCENARIO_ID_RE = re.compile(r"^(t_[0-9A-Za-z]+)/(AC-\d+)\.(S\d+)$")


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_dir), *args], text=True, capture_output=True)


def _git_bytes(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True)


def _parse_status_porcelain_z(raw: bytes) -> list[str]:
    """Parse `git status --porcelain -z` records without C-quoting or line splitting."""
    if not raw:
        return []
    tokens = raw.split(b"\x00")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    entries: list[str] = []
    i = 0
    while i < len(tokens):
        rec = os.fsdecode(tokens[i])
        if rec:
            entries.append(rec)
            # In porcelain v1 -z, renames/copies (XY has R or C) emit an extra orig_path token.
            if len(rec) >= 2 and ("R" in rec[:2] or "C" in rec[:2]):
                i += 2
                continue
        i += 1
    return entries


def _parse_name_status_z(raw: bytes) -> list[dict]:
    """Parse `git diff --name-status -z` NUL-delimited records, preserving exact paths (Unicode/tabs/newlines)."""
    if not raw:
        return []
    tokens = raw.split(b"\x00")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    status_map = {"A": "added", "M": "modified", "D": "removed", "R": "renamed", "C": "copied"}
    files: list[dict] = []
    i = 0
    while i < len(tokens):
        code = os.fsdecode(tokens[i])
        if not code:
            i += 1
            continue
        st = code[0]
        if st in ("R", "C") and i + 2 < len(tokens):
            files.append({
                "previous_filename": os.fsdecode(tokens[i + 1]),
                "filename": os.fsdecode(tokens[i + 2]),
                "status": status_map[st],
            })
            i += 3
        elif i + 1 < len(tokens):
            files.append({
                "filename": os.fsdecode(tokens[i + 1]),
                "status": status_map.get(st, "modified"),
            })
            i += 2
        else:
            break
    return files


def _git_blob_hash(repo_dir: Path, rev: str, path: str) -> str | None:
    """Hash raw git blob bytes at `rev:path` without text decoding or newline normalization."""
    p = _git_bytes(repo_dir, "show", f"{rev}:{path}")
    return sha256_text(p.stdout) if p.returncode == 0 else None


def _file_hash(repo_dir: Path, path: str) -> str | None:
    p = repo_dir / path
    if not p.is_file():
        return None
    try:
        return sha256_text(p.read_bytes())
    except OSError:
        return None


def run_preflight(
    repo_dir: Path,
    *,
    policy: dict[str, Any],
    base_ref: str = "HEAD~1",
    card_body: str | None = None,
    epic_id: str | None = None,
    valid_scenarios: set[str] | None = None,
    pr_title: str | None = None,
    pr_body: str | None = None,
    run_tests: bool = True,
    allow_dirty: bool = False,
    template_dir: Path | None = None,
    review_loop: dict[str, Any] | None = None,
    extra_findings: list[str] | None = None,
    require_scope_for_card: bool = True,
) -> dict[str, Any]:
    """Run all deterministic local preflight checks inside a developer worktree."""
    findings: list[str] = []
    checks: dict[str, str] = {}

    if not (repo_dir / ".git").exists():
        return {
            "ok": False,
            "checks": {"git_repo": "fail"},
            "findings": [f"{repo_dir} 不是 git worktree/repo 根目錄"],
            "files": [],
            "protected_changes": [],
        }

    # 1. Clean worktree check (NUL-delimited, handles non-ASCII and control characters)
    status_p = _git_bytes(repo_dir, "status", "--porcelain", "-z")
    dirty_entries = _parse_status_porcelain_z(status_p.stdout) if status_p.returncode == 0 else []
    if dirty_entries and not allow_dirty:
        checks["clean_worktree"] = "fail"
        findings.append(f"worktree 有 {len(dirty_entries)} 個未提交變更，請先 commit 或清除暫存檔再送審")
    else:
        checks["clean_worktree"] = "ok"

    # 2. Resolve changed files relative to base_ref using -z (no C-quoting; preserves exact paths & renames)
    diff_p = _git_bytes(repo_dir, "diff", "--name-status", "-z", f"{base_ref}...HEAD")
    if diff_p.returncode != 0:
        diff_p = _git_bytes(repo_dir, "diff", "--name-status", "-z", base_ref, "HEAD")
    files: list[dict] = []
    if diff_p.returncode == 0:
        files = _parse_name_status_z(diff_p.stdout)
        checks["git_diff"] = "ok"
    else:
        checks["git_diff"] = "fail"
        err = (diff_p.stderr or diff_p.stdout).decode("utf-8", errors="replace").strip()[-200:]
        findings.append(f"無法比對 base ref {base_ref!r}：{err}")

    # 3. Scope contract + diff file count cap (checks BOTH rename source and destination paths)
    repo_cfg = load_repo_config(repo_dir)
    contract = parse_card_contract(card_body)
    max_files = repo_max_diff_files(repo_cfg, policy)
    test_paths = repo_test_paths(repo_cfg)
    all_touched = touched_paths(files)
    scope_findings: list[str] = []
    if card_body is not None:
        scope_findings.extend(allowed_scope_problems(
            contract["allowed_scope"],
            required=require_scope_for_card and repo_require_allowed_scope(repo_cfg, policy) and bool(card_body.strip()),
        ))
    scope_findings.extend(check_files_against_scope(
        all_touched,
        allowed_scope=contract["allowed_scope"],
        out_of_scope=contract["out_of_scope"],
        test_paths=test_paths,
        max_diff_files=max_files,
        file_count=len(files),
    ))
    findings.extend(scope_findings)
    checks["scope"] = "fail" if scope_findings else "ok"

    if review_loop is not None:
        if review_loop.get("tripped"):
            checks["review_loop"] = "fail"
            findings.append(review_loop.get("reason") or "review 退件已達熔斷上限，請先由 orchestrator／architect 介入")
        else:
            checks["review_loop"] = "ok"

    if extra_findings:
        checks["scenarios"] = "fail"
        findings.extend(extra_findings)

    # 4. Protected / trusted harness hash lock (raw bytes, only for protected paths)
    extra_protected = repo_protected_paths(repo_cfg)
    base_hashes: dict[str, str | None] = {}
    head_hashes: dict[str, str | None] = {}
    for f in files:
        old_path = f.get("previous_filename") or f["filename"]
        new_path = f["filename"]
        if is_protected_file(old_path, extra_protected) or is_protected_file(new_path, extra_protected):
            base_hashes[old_path] = _git_blob_hash(repo_dir, base_ref, old_path)
            head_hashes[new_path] = _file_hash(repo_dir, new_path)
    protected = protected_file_changes(
        files,
        extra_patterns=extra_protected,
        base_hashes=base_hashes,
        head_hashes=head_hashes,
    )
    checks["protected_files"] = "warn" if protected else "ok"

    # 4b. Standard delivery template compliance (enforces canonical devflow delivery scripts across all repos)
    standard_delivery: dict[str, Any] = {"ok": True, "checked": [], "missing": [], "drifted": [], "findings": []}
    base_cfg_p = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"{base_ref}:.devflow/config.env"],
        text=True,
        capture_output=True,
    )
    base_cfg = env_values(base_cfg_p.stdout) if base_cfg_p.returncode == 0 else {}
    target_standard = dependent_standard_paths(all_touched, base_cfg=base_cfg, head_cfg=repo_cfg)
    if template_dir is not None and template_dir.is_dir() and target_standard:
        standard_delivery = check_standard_delivery_dir(
            repo_dir, template_dir, repo_cfg=repo_cfg, only_paths=target_standard
        )
        findings.extend(standard_delivery["findings"])
        checks["standard_delivery"] = "ok" if standard_delivery["ok"] else "fail"
    else:
        checks["standard_delivery"] = "ok"

    # 5. PR title & body checks (fix -> Lesson, Covers -> valid scenario IDs)
    pr_findings: list[str] = []
    if pr_title and FIX_TITLE_RE.match(pr_title):
        if not LESSON_LINE_RE.search(pr_body or ""):
            pr_findings.append("fix/revert PR 說明缺少 `Lesson: L-NNNN` 或至少 10 字的 `No-lesson: <理由>`")
    if pr_body:
        covers = [x for m in COVERS_RE.finditer(pr_body) for x in re.split(r"[,，\s]+", m.group(1)) if x]
        for cid in covers:
            m = SCENARIO_ID_RE.match(cid)
            if not m:
                pr_findings.append(f"PR Covers: {cid!r} 格式錯誤（應為 <EPIC>/AC-n.Sm）")
            elif epic_id and m.group(1) != epic_id:
                pr_findings.append(f"PR Covers: {cid} 所屬 EPIC ({m.group(1)}) 與本卡 EPIC ({epic_id}) 不符")
            elif valid_scenarios is not None and cid not in valid_scenarios:
                pr_findings.append(f"PR Covers: {cid} 不在 EPIC 驗收情境清單中")
    findings.extend(pr_findings)
    checks["pr_metadata"] = "fail" if pr_findings else "ok"

    # 6. Local checks & targeted tests (optional)
    test_outputs: list[dict] = []
    if run_tests:
        cmds: list[tuple[str, list[str] | str, bool]] = []
        check_sh = repo_dir / ".devflow" / "check.sh"
        if check_sh.is_file():
            cmds.append(("check.sh", ["bash", str(check_sh)], False))
        # Prefer card Verify -> repo DEVFLOW_REVIEW_TEST_CMD -> .devflow/test.sh
        if contract["verify"]:
            for idx, cmd in enumerate(contract["verify"], 1):
                cmds.append((f"verify_{idx}", cmd, True))
        elif (review_cmd := repo_review_test_cmd(repo_cfg)):
            cmds.append(("review_test", review_cmd, True))
        elif (repo_dir / ".devflow" / "test.sh").is_file():
            cmds.append(("test.sh", ["bash", str(repo_dir / ".devflow" / "test.sh")], False))

        for label, cmd, use_shell in cmds:
            p = subprocess.run(cmd, cwd=repo_dir, shell=use_shell, text=True, capture_output=True)
            test_outputs.append({
                "name": label,
                "returncode": p.returncode,
                "tail": (p.stderr or p.stdout).strip()[-400:],
            })
            if p.returncode != 0:
                checks[label] = "fail"
                findings.append(f"本機檢查 `{label}` 失敗（exit {p.returncode}）：{(p.stderr or p.stdout).strip()[-240:]}")
            else:
                checks[label] = "ok"

    return {
        "ok": not findings,
        "checks": checks,
        "findings": findings,
        "files": [f["filename"] for f in files],
        "touched_files": all_touched,
        "file_count": len(files),
        "max_diff_files": max_files,
        "protected_changes": protected,
        "standard_delivery": standard_delivery,
        "needs_human_approval": bool(protected) or bool(standard_delivery.get("custom_allowed")),
        "tests": test_outputs,
        **({"review_loop": review_loop} if review_loop is not None else {}),
    }
