"""Developer preflight CLI gate (`devflow preflight`): deterministic local checks before PR/review."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .policy import load_repo_config, repo_max_diff_files, repo_protected_paths, repo_review_test_cmd
from .scope import check_files_against_scope, parse_card_contract, touched_paths
from .trust import is_protected_file, protected_file_changes, sha256_text

FIX_TITLE_RE = re.compile(r"^\s*(fix|revert)(\([^)]*\))?!?:", re.I)
LESSON_LINE_RE = re.compile(r"^\s*(Lesson:\s*L-\d+|No-lesson:\s*\S.{9,})", re.M)
COVERS_RE = re.compile(r"^\s*Covers:\s*(.+)$", re.M)
SCENARIO_ID_RE = re.compile(r"^(t_[0-9A-Za-z]+)/(AC-\d+)\.(S\d+)$")


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_dir), *args], text=True, capture_output=True)


def _git_blob_hash(repo_dir: Path, rev: str, path: str) -> str | None:
    """Hash raw git blob bytes at `rev:path` without text decoding or newline normalization."""
    p = subprocess.run(["git", "-C", str(repo_dir), "show", f"{rev}:{path}"], capture_output=True)
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

    # 1. Clean worktree check
    status_p = _git(repo_dir, "status", "--porcelain")
    dirty_lines = [ln for ln in status_p.stdout.splitlines() if ln.strip()]
    if dirty_lines and not allow_dirty:
        checks["clean_worktree"] = "fail"
        findings.append(f"worktree 有 {len(dirty_lines)} 個未提交變更，請先 commit 或清除暫存檔再送審")
    else:
        checks["clean_worktree"] = "ok"

    # 2. Resolve changed files relative to base_ref (preserving rename/copy source paths)
    diff_p = _git(repo_dir, "diff", "--name-status", f"{base_ref}...HEAD")
    if diff_p.returncode != 0:
        diff_p = _git(repo_dir, "diff", "--name-status", base_ref, "HEAD")
    files: list[dict] = []
    if diff_p.returncode == 0:
        for ln in diff_p.stdout.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                st = parts[0][0]
                status_map = {"A": "added", "M": "modified", "D": "removed", "R": "renamed", "C": "copied"}
                if st in ("R", "C") and len(parts) >= 3:
                    files.append({
                        "filename": parts[2].strip(),
                        "previous_filename": parts[1].strip(),
                        "status": status_map[st],
                    })
                else:
                    files.append({"filename": parts[-1].strip(), "status": status_map.get(st, "modified")})
        checks["git_diff"] = "ok"
    else:
        checks["git_diff"] = "fail"
        findings.append(f"無法比對 base ref {base_ref!r}：{(diff_p.stderr or diff_p.stdout).strip()[-200:]}")

    # 3. Scope contract + diff file count cap (checks BOTH rename source and destination paths)
    repo_cfg = load_repo_config(repo_dir)
    contract = parse_card_contract(card_body)
    max_files = repo_max_diff_files(repo_cfg, policy)
    all_touched = touched_paths(files)
    scope_findings = check_files_against_scope(
        all_touched,
        allowed_scope=contract["allowed_scope"],
        out_of_scope=contract["out_of_scope"],
        max_diff_files=max_files,
        file_count=len(files),
    )
    findings.extend(scope_findings)
    checks["scope"] = "fail" if scope_findings else "ok"

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
        "needs_human_approval": bool(protected),
        "tests": test_outputs,
    }
