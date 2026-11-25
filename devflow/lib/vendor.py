"""Pinned external skill repository synchronization (fail-closed SHA verification)."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _git(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


def _git_bytes(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True)


def _git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def _verify_tree_contents(vendor_dir: Path, commit_sha: str) -> None:
    """Cryptographically verify every tracked file/symlink on disk against `commit_sha`'s Git tree,
    independent of Git index flags (`--assume-unchanged`, `--skip-worktree`, or stat cache)."""
    ls_p = _git_bytes(["git", "-C", str(vendor_dir), "ls-tree", "-r", "-z", commit_sha])
    if ls_p.returncode != 0:
        err = (ls_p.stderr or ls_p.stdout).decode("utf-8", errors="replace").strip()[:200]
        raise RuntimeError(f"git ls-tree failed for {commit_sha[:12]}: {err}")
    for entry in ls_p.stdout.split(b"\x00"):
        if not entry:
            continue
        meta_b, sep, rel_b = entry.partition(b"\t")
        if not sep:
            continue
        parts = meta_b.decode("ascii", errors="replace").split()
        if len(parts) < 3:
            continue
        mode, obj_type, expected_oid = parts[0], parts[1], parts[2].lower()
        if obj_type != "blob":
            continue
        rel = os.fsdecode(rel_b)
        path = vendor_dir / rel
        if mode == "120000":
            if not path.is_symlink():
                raise RuntimeError(f"pinned content mismatch for {rel}: expected symlink")
            raw = os.fsencode(os.readlink(path))
        else:
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"pinned content mismatch for {rel}: missing regular file")
            raw = path.read_bytes()
        actual_oid = _git_blob_sha1(raw)
        if actual_oid != expected_oid:
            raise RuntimeError(
                f"pinned content mismatch for {rel}: expected blob {expected_oid[:12]}, got {actual_oid[:12]}"
            )


def validate_pinned_sha(sha: str | None) -> str:
    """Validate and normalize a full 40-character hex commit SHA."""
    norm = (sha or "").strip().lower()
    if not FULL_SHA_RE.fullmatch(norm):
        raise ValueError(f"mattpocock_skills.sha must be a 40-char hex commit SHA, got {sha!r}")
    return norm


def sync_pinned_git_repo(
    vendor_dir: Path,
    repo: str,
    pinned_sha: str,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> str:
    """Ensure `vendor_dir` is a clean detached checkout at exact commit `pinned_sha`.

    Fails closed (raises RuntimeError/ValueError) if:
    - `pinned_sha` is not a full 40-hex commit SHA;
    - `vendor_dir` exists and is non-empty without a `.git` directory;
    - fetching or checking out `pinned_sha` fails;
    - post-checkout `git rev-parse HEAD` does not equal `pinned_sha` or working tree is dirty.
    """
    want = validate_pinned_sha(pinned_sha)
    repo_url = (repo or "").strip()
    if not repo_url:
        raise ValueError("mattpocock_skills.repo must be non-empty")

    git_dir = vendor_dir / ".git"
    if not git_dir.exists():
        if vendor_dir.exists() and any(vendor_dir.iterdir()):
            raise RuntimeError(f"{vendor_dir} exists and is non-empty without .git; refusing to overwrite")
        vendor_dir.mkdir(parents=True, exist_ok=True)
        init_p = _git(["git", "-C", str(vendor_dir), "init", "-q"])
        if init_p.returncode != 0:
            raise RuntimeError(f"git init failed: {(init_p.stderr or init_p.stdout).strip()}")
        rem_p = _git(["git", "-C", str(vendor_dir), "remote", "add", "origin", repo_url])
        if rem_p.returncode != 0:
            raise RuntimeError(f"git remote add failed: {(rem_p.stderr or rem_p.stdout).strip()}")
    else:
        # Keep origin URL synchronized when the existing checkout already points to a git repo.
        cur_url = _git(["git", "-C", str(vendor_dir), "remote", "get-url", "origin"])
        if cur_url.returncode != 0:
            _git(["git", "-C", str(vendor_dir), "remote", "add", "origin", repo_url])
        elif cur_url.stdout.strip() != repo_url:
            _git(["git", "-C", str(vendor_dir), "remote", "set-url", "origin", repo_url])

    has_commit = _git(["git", "-C", str(vendor_dir), "cat-file", "-e", f"{want}^{{commit}}"]).returncode == 0
    if not has_commit:
        fetch_p = _git(["git", "-C", str(vendor_dir), "fetch", "-q", "--depth", "1", "origin", want])
        if fetch_p.returncode != 0:
            fetch_p = _git(["git", "-C", str(vendor_dir), "fetch", "-q", "origin"])
        if fetch_p.returncode != 0:
            raise RuntimeError(
                f"failed to fetch pinned commit {want[:12]} from {repo_url}: "
                f"{(fetch_p.stderr or fetch_p.stdout).strip()[:300]}"
            )
        if _git(["git", "-C", str(vendor_dir), "cat-file", "-e", f"{want}^{{commit}}"]).returncode != 0:
            raise RuntimeError(f"pinned commit {want} not found in {repo_url}")

    # Always reset the index unconditionally (clears --assume-unchanged / --skip-worktree flags),
    # force-checkout the exact pinned tree, and purge all untracked/ignored/nested directories (-ffdx).
    empty_p = _git(["git", "-C", str(vendor_dir), "read-tree", "--empty"])
    if empty_p.returncode != 0:
        raise RuntimeError(f"git read-tree --empty failed: {(empty_p.stderr or empty_p.stdout).strip()[:300]}")
    co_p = _git(["git", "-C", str(vendor_dir), "checkout", "-q", "--detach", "--force", want])
    if co_p.returncode != 0:
        raise RuntimeError(f"git checkout {want[:12]} failed: {(co_p.stderr or co_p.stdout).strip()[:300]}")
    cl_p = _git(["git", "-C", str(vendor_dir), "clean", "-q", "-ffdx"])
    if cl_p.returncode != 0:
        raise RuntimeError(f"git clean failed: {(cl_p.stderr or cl_p.stdout).strip()[:300]}")

    actual_p = _git(["git", "-C", str(vendor_dir), "rev-parse", "HEAD"])
    actual = actual_p.stdout.strip().lower() if actual_p.returncode == 0 else ""
    if actual != want:
        raise RuntimeError(f"pinned checkout verification failed: expected {want}, got {actual or 'none'}")
    _verify_tree_contents(vendor_dir, want)
    dirty_after = _git(
        ["git", "-C", str(vendor_dir), "status", "--porcelain", "--ignored=matching", "--untracked-files=all"]
    ).stdout.strip()
    if dirty_after:
        raise RuntimeError(f"vendor repo {vendor_dir} is not clean after checkout: {dirty_after[:200]}")

    if log_fn:
        log_fn(f"mattpocock/skills @ {actual[:7]} (pinned {want[:12]})")
    return actual


def discover_mattpocock_skills(vendor_dir: Path) -> dict[str, Path]:
    """Return `{skill_name: skill_dir}` for verified `skills/*/*/SKILL.md` entries.

    When `vendor_dir` is a git checkout, restricts discovery strictly to files tracked in
    `HEAD` (`git ls-tree`) as defense-in-depth against untracked/ignored injections.
    """
    found: dict[str, Path] = {}
    if (vendor_dir / ".git").exists():
        ls_p = _git(["git", "-C", str(vendor_dir), "ls-tree", "-r", "--name-only", "HEAD", "--", "skills"])
        if ls_p.returncode != 0:
            raise RuntimeError(f"failed to list tracked skills in {vendor_dir}: {(ls_p.stderr or ls_p.stdout).strip()[:200]}")
        for rel in sorted(ls_p.stdout.splitlines()):
            parts = Path(rel).parts
            if len(parts) == 4 and parts[0] == "skills" and parts[3] == "SKILL.md":
                skill_md = vendor_dir / rel
                if skill_md.is_file():
                    found.setdefault(skill_md.parent.name, skill_md.parent)
        return found
    skills_root = vendor_dir / "skills"
    if not skills_root.is_dir():
        return found
    for skill_md in sorted(skills_root.glob("*/*/SKILL.md")):
        found.setdefault(skill_md.parent.name, skill_md.parent)
    return found
