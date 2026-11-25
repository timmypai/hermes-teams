"""Pinned external skill repository synchronization (fail-closed SHA verification)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _git(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


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

    cur_head = _git(["git", "-C", str(vendor_dir), "rev-parse", "HEAD"])
    cur_dirty = _git(["git", "-C", str(vendor_dir), "status", "--porcelain"])
    if cur_head.returncode != 0 or cur_head.stdout.strip().lower() != want or cur_dirty.stdout.strip():
        co_p = _git(["git", "-C", str(vendor_dir), "checkout", "-q", "--detach", "--force", want])
        if co_p.returncode != 0:
            raise RuntimeError(f"git checkout {want[:12]} failed: {(co_p.stderr or co_p.stdout).strip()[:300]}")
        cl_p = _git(["git", "-C", str(vendor_dir), "clean", "-q", "-fdx"])
        if cl_p.returncode != 0:
            raise RuntimeError(f"git clean failed: {(cl_p.stderr or cl_p.stdout).strip()[:300]}")

    actual_p = _git(["git", "-C", str(vendor_dir), "rev-parse", "HEAD"])
    actual = actual_p.stdout.strip().lower() if actual_p.returncode == 0 else ""
    if actual != want:
        raise RuntimeError(f"pinned checkout verification failed: expected {want}, got {actual or 'none'}")
    dirty_after = _git(["git", "-C", str(vendor_dir), "status", "--porcelain"]).stdout.strip()
    if dirty_after:
        raise RuntimeError(f"vendor repo {vendor_dir} is not clean after checkout: {dirty_after[:200]}")

    if log_fn:
        log_fn(f"mattpocock/skills @ {actual[:7]} (pinned {want[:12]})")
    return actual


def discover_mattpocock_skills(vendor_dir: Path) -> dict[str, Path]:
    """Return `{skill_name: skill_dir}` for all vendored `skills/*/*/SKILL.md` entries."""
    found: dict[str, Path] = {}
    skills_root = vendor_dir / "skills"
    if not skills_root.is_dir():
        return found
    for skill_md in sorted(skills_root.glob("*/*/SKILL.md")):
        found.setdefault(skill_md.parent.name, skill_md.parent)
    return found
