"""Card scope contract parsing, body hygiene checks, and PR goal-diff alignment."""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

ALLOWED_SCOPE_RE = re.compile(r"^\s*Allowed-Scope[：:]\s*(.+)$", re.M | re.I)
OUT_OF_SCOPE_RE = re.compile(r"^\s*Out-of-Scope[：:]\s*(.+)$", re.M | re.I)
VERIFY_CMD_RE = re.compile(r"^\s*Verify[：:]\s*(.+)$", re.M | re.I)
KANBAN_TASK_RE = re.compile(r"^\s*Kanban[：:]\s*(t_[0-9A-Za-z]+)\b", re.M | re.I)
EPIC_SECTION_RE = re.compile(r"^##\s*(目標|驗收|使用者原話|使用者決定|不能碰)[ \t]*\n(.*?)(?=^## |\Z)", re.M | re.S)

# Deterministic leaks that must never appear in human-facing card bodies (Item #8).
LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("commit SHA", re.compile(r"\b[0-9a-f]{40}\b")),
    ("CI run URL/ID", re.compile(r"/actions/runs/\d+\b|\brun_id\s*[=:]\s*\d+\b", re.I)),
    ("內部容器/核心旗標", re.compile(r"(?:--no-sandbox|\bCAP_SYS_[A-Z_]+\b|\bseccomp\b|\b(?:uid|gid)=\d+\b)", re.I)),
)


def split_globs(raw: str) -> list[str]:
    return [tok.strip().strip("`") for tok in re.split(r"[,，\s]+", raw or "") if tok.strip().strip("`")]


def parse_card_contract(body: str | None) -> dict:
    """Extract optional structured scope & verification fields from a card body."""
    text = body or ""
    allowed = [g for m in ALLOWED_SCOPE_RE.finditer(text) for g in split_globs(m.group(1))]
    out_of_scope = [g for m in OUT_OF_SCOPE_RE.finditer(text) for g in split_globs(m.group(1))]
    verify = [m.group(1).strip() for m in VERIFY_CMD_RE.finditer(text) if m.group(1).strip()]
    return {
        "allowed_scope": list(dict.fromkeys(allowed)),
        "out_of_scope": list(dict.fromkeys(out_of_scope)),
        "verify": verify,
    }


def parse_epic_sections(body: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in EPIC_SECTION_RE.finditer(body or ""):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def match_glob(path: str, pattern: str) -> bool:
    """Match a POSIX repo-relative file path against a glob or directory prefix."""
    p = path.lstrip("./")
    pat = pattern.lstrip("./")
    if not pat:
        return False
    if pat.endswith("/"):
        return p.startswith(pat)
    if "**" in pat:
        # PurePosixPath.match handles leading wildcards; also support dir/**/*.py via fnmatch fallback
        prefix, _, suffix = pat.partition("**")
        prefix = prefix.rstrip("/")
        suffix = suffix.lstrip("/")
        if prefix and not (p == prefix or p.startswith(prefix + "/")):
            return False
        if not suffix:
            return True
        return fnmatch.fnmatch(p, f"*{suffix}") or PurePosixPath(p).match(pat)
    return fnmatch.fnmatch(p, pat) or p == pat or p.startswith(pat.rstrip("/") + "/")


def check_files_against_scope(
    filenames: list[str],
    *,
    allowed_scope: list[str] | None = None,
    out_of_scope: list[str] | None = None,
    max_diff_files: int | None = None,
) -> list[str]:
    """Deterministic diff scope checks: Out-of-Scope forbidden, Allowed-Scope bounded, file count capped."""
    findings: list[str] = []
    files = sorted(set(filenames))
    if out_of_scope:
        violated = [f for f in files if any(match_glob(f, pat) for pat in out_of_scope)]
        if violated:
            findings.append(
                f"改動觸及卡片禁止範圍（Out-of-Scope）：{', '.join(violated[:10])}"
                + (f" 等共 {len(violated)} 檔" if len(violated) > 10 else "")
            )
    if allowed_scope:
        # Allow standard test/lesson/scenario paths alongside declared Allowed-Scope unless explicitly forbidden
        outside = [
            f for f in files
            if not any(match_glob(f, pat) for pat in allowed_scope)
            and not f.startswith((".devflow/lessons/", ".devflow/scenarios.json"))
        ]
        if outside:
            findings.append(
                f"改動超出卡片宣告範圍（Allowed-Scope={', '.join(allowed_scope)}）：{', '.join(outside[:10])}"
                + (f" 等共 {len(outside)} 檔" if len(outside) > 10 else "")
            )
    if max_diff_files and max_diff_files > 0 and len(files) > max_diff_files:
        findings.append(
            f"單一 PR 改動 {len(files)} 個檔案，超過上限 {max_diff_files} 檔（請拆小或確認是否夾帶無關重構）"
        )
    return findings


def body_hygiene_leaks(text: str) -> list[str]:
    """Return labels of technical leaks found in human-facing card body text."""
    return [label for label, pat in LEAK_PATTERNS if pat.search(text or "")]


def extract_kanban_task_id(pr_body: str | None) -> str | None:
    m = KANBAN_TASK_RE.search(pr_body or "")
    return m.group(1) if m else None


def build_goal_context(
    *,
    epic_id: str | None,
    epic_title: str | None,
    epic_body: str | None,
    card_id: str | None = None,
    card_title: str | None = None,
    card_body: str | None = None,
) -> dict | None:
    """Assemble structured goal context passed into `check_pr` / `judge()` for scope-diff verification."""
    if not epic_body and not card_body:
        return None
    epic_secs = parse_epic_sections(epic_body)
    contract = parse_card_contract(card_body)
    ctx = {
        "epic_id": epic_id,
        "epic_title": epic_title or "",
        "user_said": (epic_secs.get("使用者原話") or "")[:2000],
        "user_decisions": (epic_secs.get("使用者決定") or "")[:1500],
        "epic_goal": (epic_secs.get("目標") or "")[:1000],
        "epic_ac": (epic_secs.get("驗收") or "")[:2500],
        "do_not_touch": (epic_secs.get("不能碰") or "")[:1000],
        "card_id": card_id,
        "card_title": card_title or "",
        "card_body": (card_body or "")[:1500],
        "allowed_scope": contract["allowed_scope"],
        "out_of_scope": contract["out_of_scope"],
    }
    return ctx
