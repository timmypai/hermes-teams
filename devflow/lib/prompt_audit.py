"""Prompt hygiene & repo AGENTS.md conflict auditing (declarative, zero repo hardcoding)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lib import lint_engine

WORKFLOW_RULES_DIR = Path(__file__).resolve().parents[1] / "rules" / "workflow"

# Phrases that must NEVER appear in developer auto-loaded prompts (direct contradictions with devflow).
FORBIDDEN_DEVELOPER_PHRASES: tuple[tuple[str, str], ...] = (
    ("Push to the main line", "與 devflow 嚴禁直接 push 到 main（僅走 PR）直接衝突"),
    ("Use the crapper tool", "與 devflow 禁止在 developer 容器安裝執行 crapper 直接衝突"),
    ("Use the dryer tool", "與 devflow 禁止在 developer 容器安裝執行 dryer 直接衝突"),
    ("Use the mutator tool", "與 devflow 禁止在 developer 容器安裝執行 mutator 直接衝突"),
    ("Redesign From First Principles", "鼓勵從頭重寫設計，與 Allowed-Scope 最小改動原則直接衝突"),
)


def scan_repo_agents_conflicts(
    repo_dir: Path | str | None,
    *,
    rules_dir: Path | str | None = None,
) -> list[dict[str, str]]:
    """Read-only scan of `<repo>` using declarative `rules/workflow/*.toml` rules."""
    if not repo_dir:
        return []
    root = Path(repo_dir)
    if not root.is_dir():
        return []
    rdir = Path(rules_dir) if rules_dir is not None else WORKFLOW_RULES_DIR
    rules = lint_engine.load_rules(rdir)
    return [
        {
            "id": f["id"],
            "file": f["file"],
            "match": f["match"],
            "detail": f["detail"],
        }
        for f in lint_engine.scan_repo(rules, root)
    ]


def role_auto_load_chars(kit_root: Path, roles_data: dict[str, Any] | None = None) -> dict[str, int]:
    """Measure total auto-loaded prompt characters per role (SOUL + auto_load skills + codex AGENTS/skills)."""
    data = roles_data or json.loads((kit_root / "roles.json").read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for role, spec in (data.get("roles") or {}).items():
        total = 0
        soul = kit_root / "souls" / f"{role}.md"
        if soul.is_file():
            total += len(soul.read_text(encoding="utf-8"))
        for sk in spec.get("auto_load") or []:
            p = kit_root / "skills" / sk / "SKILL.md"
            if p.is_file():
                total += len(p.read_text(encoding="utf-8"))
        if role == "developer":
            codex_agents = kit_root / "codex" / "AGENTS.md"
            if codex_agents.is_file():
                total += len(codex_agents.read_text(encoding="utf-8"))
            for csk in spec.get("codex_skills") or []:
                p = kit_root / "skills" / csk / "SKILL.md"
                if p.is_file():
                    total += len(p.read_text(encoding="utf-8"))
        out[role] = total
    return out


def audit_kit_prompts(kit_root: Path) -> dict[str, Any]:
    """Audit all kit souls, skills, declarative workflow lint rules, and codex instructions."""
    roles_data = json.loads((kit_root / "roles.json").read_text(encoding="utf-8"))
    policy = roles_data.get("policy") or {}
    budget = int(policy.get("max_role_prompt_chars") or 12000)
    sizes = role_auto_load_chars(kit_root, roles_data)
    findings: list[str] = []

    for role, chars in sorted(sizes.items()):
        if chars > budget:
            findings.append(f"role {role!r} auto_load prompt {chars} 字元超過上限 {budget}")

    # Verify declarative workflow TOML rules exist and pass their built-in fixtures
    rules_dir = kit_root / "rules" / "workflow"
    if not rules_dir.is_dir() or not list(rules_dir.glob("*.toml")):
        findings.append("缺少宣告式規則：rules/workflow/*.toml")
    else:
        for err in lint_engine.self_test_dir(rules_dir):
            findings.append(f"rules/workflow 自測失敗：{err}")

    # Verify devflow-chat and devflow-cli exist and are NOT auto-loaded into background workers
    for sk in ("devflow-chat", "devflow-cli"):
        if not (kit_root / "skills" / sk / "SKILL.md").is_file():
            findings.append(f"缺少拆層後的標準 skill：skills/{sk}/SKILL.md")
    for role, spec in (roles_data.get("roles") or {}).items():
        auto = set(spec.get("auto_load") or [])
        if spec.get("worker", True) and (auto & {"devflow-chat", "devflow-cli"}):
            findings.append(f"worker {role!r} 不應 auto_load devflow-chat / devflow-cli（應按需載入）")

    # Verify developer loaded prompts contain zero forbidden conflicting phrases
    dev_spec = (roles_data.get("roles") or {}).get("developer") or {}
    dev_files = [
        kit_root / "souls" / "developer.md",
        kit_root / "codex" / "AGENTS.md",
        *(kit_root / "skills" / s / "SKILL.md" for s in dev_spec.get("auto_load") or []),
        *(kit_root / "skills" / s / "SKILL.md" for s in dev_spec.get("codex_skills") or []),
    ]
    for p in dev_files:
        if not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8")
        for phrase, reason in FORBIDDEN_DEVELOPER_PHRASES:
            if phrase in txt:
                findings.append(f"{p.relative_to(kit_root)} 含有衝突語句 {phrase!r}：{reason}")

    # Verify no contradiction between "ban file paths in card body" and required Allowed-Scope
    path_ban_re = re.compile(r"禁止[^\n。；]{0,60}檔案路徑")
    for p in sorted((kit_root / "skills").glob("*/SKILL.md")):
        txt = p.read_text(encoding="utf-8")
        if path_ban_re.search(txt):
            findings.append(f"{p.relative_to(kit_root)} 含「禁止寫檔案路徑」與必填 Allowed-Scope 矛盾")

    # Verify devflow-chat is the SSOT for approve/approve-plan detailed protocol
    chat_md = kit_root / "skills" / "devflow-chat" / "SKILL.md"
    if chat_md.is_file():
        chat_txt = chat_md.read_text(encoding="utf-8")
        for req in ("devflow approve ", "--request", "嚴禁改查", "絕不執行 `devflow approve`",
                    "devflow approve-plan ", "絕不執行 `devflow approve-plan`"):
            if req not in chat_txt:
                findings.append(f"skills/devflow-chat/SKILL.md 缺少必要核准規則：{req}")

    # Verify PR-level [ADV] is told NOT to open a competing [ADJ] next to the reviewer's request_changes
    intake_md = kit_root / "skills" / "devflow-intake" / "SKILL.md"
    if intake_md.is_file():
        intake_txt = intake_md.read_text(encoding="utf-8")
        if "嚴禁在 PR 層 `[ADV]` 另建 `[ADJ]`" not in intake_txt:
            findings.append("skills/devflow-intake/SKILL.md 缺少「嚴禁在 PR 層 [ADV] 另建 [ADJ]」（應交 reviewer 單線退件）")

    # Verify no hardcoded project names or historical 8-hex card IDs in live docs/souls/skills/rules
    card_id_re = re.compile(r"\bt_[0-9a-f]{8}\b")
    scan_targets = [
        kit_root / "README.md",
        *sorted((kit_root / "souls").glob("*.md")),
        *sorted((kit_root / "skills").glob("*/SKILL.md")),
        *sorted(rules_dir.glob("*.toml")),
    ]
    for p in scan_targets:
        if not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8")
        if "teams-transcription" in txt:
            findings.append(f"{p.relative_to(kit_root)} 含硬編碼專案名")
        if card_id_re.search(txt):
            findings.append(f"{p.relative_to(kit_root)} 含歷史卡片 ID")

    return {"ok": not findings, "budget": budget, "sizes": sizes, "findings": findings}
