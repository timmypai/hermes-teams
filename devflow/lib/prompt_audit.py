"""Prompt hygiene, schema-vs-doc contract & repo AGENTS.md auditing (declarative, zero repo hardcoding)."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from lib import lint_engine, policy as policy_lib

WORKFLOW_RULES_DIR = Path(__file__).resolve().parents[1] / "rules" / "workflow"
KIT_CONTRACTS_FILE = Path("rules") / "kit" / "prompt-contracts.toml"


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


def _role_prompt_files(kit_root: Path, role: str, spec: dict[str, Any]) -> list[Path]:
    files = [
        kit_root / "souls" / f"{role}.md",
        *(kit_root / "skills" / s / "SKILL.md" for s in (spec.get("auto_load") or [])),
    ]
    if role == "developer":
        files.append(kit_root / "codex" / "AGENTS.md")
        files.extend(kit_root / "skills" / s / "SKILL.md" for s in (spec.get("codex_skills") or []))
    return [p for p in files if p.is_file()]


def _schema_doc_fields(schema: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return `(always_required, conditional_tokens)` derived from a declarative handoff schema."""
    always = [str(k).strip() for k in (schema.get("required") or []) if str(k).strip()]
    cond_tokens: list[str] = []
    for clause in schema.get("required_when") or []:
        if not isinstance(clause, dict):
            continue
        req = [str(k).strip() for k in (clause.get("required") or []) if str(k).strip()]
        if clause.get("default"):
            for k in req:
                if k not in always:
                    always.append(k)
        if isinstance(clause.get("when"), dict) and clause["when"]:
            for wk in clause["when"]:
                ks = str(wk).strip()
                if ks and ks not in cond_tokens:
                    cond_tokens.append(ks)
            for k in req:
                if k not in always and k not in cond_tokens:
                    cond_tokens.append(k)
    return always, cond_tokens


def _discover_registered_slugs(kit_root: Path, explicit: list[str] | tuple[str, ...] | None) -> list[str]:
    found: list[str] = []
    for raw in explicit or ():
        s = str(raw or "").strip()
        if s and s not in found:
            found.append(s)
    repos_file = kit_root / "state" / "repos.json"
    if explicit is None and repos_file.is_file():
        try:
            raw_map = json.loads(repos_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw_map = {}
        if isinstance(raw_map, dict):
            for k, val in raw_map.items():
                slug = str((val.get("slug") if isinstance(val, dict) else None) or k or "").strip()
                if slug and slug not in found:
                    found.append(slug)
    return found


def _extract_marked_block(text: str, start_marker: str, end_marker: str) -> str | None:
    s = text.find(start_marker)
    if s < 0:
        return None
    e = text.find(end_marker, s)
    if e < 0:
        return None
    return text[s : e + len(end_marker)]


def load_kit_contracts(kit_root: Path) -> dict[str, Any]:
    """Load declarative kit prompt/contract rules from `rules/kit/prompt-contracts.toml`."""
    contracts_path = kit_root / KIT_CONTRACTS_FILE
    if not contracts_path.is_file():
        return {}
    return tomllib.loads(contracts_path.read_text(encoding="utf-8"))


def audit_kit_prompts(
    kit_root: Path,
    *,
    registered_slugs: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Audit all kit souls, skills, declarative workflow/kit lint rules, schemas, and lexer parity."""
    roles_data = json.loads((kit_root / "roles.json").read_text(encoding="utf-8"))
    policy = roles_data.get("policy") or {}
    budget = int(policy.get("max_role_prompt_chars") or 12000)
    sizes = role_auto_load_chars(kit_root, roles_data)
    findings: list[str] = []

    for role, chars in sorted(sizes.items()):
        if chars > budget:
            findings.append(f"role {role!r} auto_load prompt {chars} 字元超過上限 {budget}")

    contracts_path = kit_root / KIT_CONTRACTS_FILE
    if not contracts_path.is_file():
        findings.append(f"缺少宣告式契約：{KIT_CONTRACTS_FILE}")
        contracts: dict[str, Any] = {}
    else:
        try:
            contracts = load_kit_contracts(kit_root)
        except (OSError, tomllib.TOMLDecodeError) as err:
            findings.append(f"無法解析 {KIT_CONTRACTS_FILE}：{err}")
            contracts = {}

    rules_dir = kit_root / "rules" / "workflow"
    if not rules_dir.is_dir() or not list(rules_dir.glob("*.toml")):
        findings.append("缺少宣告式規則：rules/workflow/*.toml")
    else:
        for err in lint_engine.self_test_dir(rules_dir):
            findings.append(f"rules/workflow 自測失敗：{err}")

    for sk in ("devflow-chat", "devflow-cli"):
        if not (kit_root / "skills" / sk / "SKILL.md").is_file():
            findings.append(f"缺少拆層後的標準 skill：skills/{sk}/SKILL.md")
    for role, spec in (roles_data.get("roles") or {}).items():
        auto = set(spec.get("auto_load") or [])
        if spec.get("worker", True) and (auto & {"devflow-chat", "devflow-cli"}):
            findings.append(f"worker {role!r} 不應 auto_load devflow-chat / devflow-cli（應按需載入）")

    roles_map = roles_data.get("roles") or {}
    for entry in contracts.get("forbid_in_role") or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip()
        phrase = str(entry.get("phrase") or "")
        reason = str(entry.get("reason") or "禁止語句")
        spec = roles_map.get(role) or {}
        for p in _role_prompt_files(kit_root, role, spec):
            txt = p.read_text(encoding="utf-8")
            if phrase and phrase in txt:
                findings.append(f"{p.relative_to(kit_root)} 含有衝突語句 {phrase!r}：{reason}")

    for entry in contracts.get("forbid_in_glob") or []:
        if not isinstance(entry, dict):
            continue
        glob_pat = str(entry.get("glob") or "").strip()
        regex_str = str(entry.get("regex") or "")
        detail = str(entry.get("detail") or "命中禁止樣式")
        if not glob_pat or not regex_str:
            continue
        pat = re.compile(regex_str)
        for p in sorted(kit_root.glob(glob_pat)):
            if not p.is_file():
                continue
            if pat.search(p.read_text(encoding="utf-8")):
                findings.append(f"{p.relative_to(kit_root)} {detail}")

    for entry in contracts.get("require_in_file") or []:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("path") or "").strip()
        p = kit_root / rel
        if not rel or not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8")
        for req in entry.get("required") or []:
            req_s = str(req)
            if req_s and req_s not in txt:
                findings.append(f"{rel} 缺少必要規則：{req_s}")

    schemas = policy_lib.handoff_schemas(policy)
    for entry in contracts.get("role_handoff_contract") or []:
        if not isinstance(entry, dict):
            continue
        step = str(entry.get("step") or "").strip().lower()
        schema = schemas.get(step)
        if not schema:
            findings.append(f"{KIT_CONTRACTS_FILE}: 未知的 handoff schema '{step}'")
            continue
        always_req, cond_tokens = _schema_doc_fields(schema)
        file_rels = [str(x).strip() for x in (entry.get("files") or []) if str(x).strip()]
        for rel in file_rels:
            p = kit_root / rel
            if not p.is_file():
                continue
            txt = p.read_text(encoding="utf-8")
            for field in always_req:
                if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])", txt):
                    findings.append(
                        f"{rel} 缺少 roles.json handoff_schemas.{step} 必填欄位 `{field}` 的明確指引"
                    )
        cond_rels = [str(x).strip() for x in (entry.get("conditional_files") or file_rels) if str(x).strip()]
        for rel in cond_rels:
            p = kit_root / rel
            if not p.is_file():
                continue
            txt = p.read_text(encoding="utf-8")
            for field in cond_tokens:
                if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])", txt):
                    findings.append(
                        f"{rel} 缺少 roles.json handoff_schemas.{step} 條件必填欄位 `{field}` 的明確指引"
                    )

    hygiene = contracts.get("hygiene") if isinstance(contracts.get("hygiene"), dict) else {}
    card_regex = str(hygiene.get("forbid_card_id_regex") or r"\bt_[0-9a-f]{8}\b")
    card_id_re = re.compile(card_regex)
    slugs = (
        _discover_registered_slugs(kit_root, registered_slugs)
        if hygiene.get("forbid_registered_slugs", True)
        else []
    )
    slug_res = [
        (slug, re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(slug)}(?![A-Za-z0-9_-])"))
        for slug in slugs
    ]
    scan_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for gpat in hygiene.get("scan_globs") or []:
        for p in sorted(kit_root.glob(str(gpat))):
            if p.is_file() and p not in seen_paths:
                seen_paths.add(p)
                scan_paths.append(p)
    for p in scan_paths:
        txt = p.read_text(encoding="utf-8")
        rel = p.relative_to(kit_root)
        for slug, slug_re in slug_res:
            if slug_re.search(txt):
                findings.append(f"{rel} 含硬編碼專案識別符 {slug!r}")
        if card_id_re.search(txt):
            findings.append(f"{rel} 含歷史看板卡號")

    lexer_files = [str(x).strip() for x in (hygiene.get("shared_lexer_files") or []) if str(x).strip()]
    markers = list(hygiene.get("shared_lexer_markers") or [])
    req_dir = str(hygiene.get("shared_lexer_require_dir") or "").strip()
    if len(lexer_files) >= 2 and len(markers) == 2 and (not req_dir or (kit_root / req_dir).is_dir()):
        start_m, end_m = str(markers[0]), str(markers[1])
        blocks: list[tuple[str, str | None]] = []
        for rel in lexer_files:
            p = kit_root / rel
            if not p.is_file():
                findings.append(f"缺少共用 lexer 檔案：{rel}")
                blocks.append((rel, None))
                continue
            blk = _extract_marked_block(p.read_text(encoding="utf-8"), start_m, end_m)
            if blk is None:
                findings.append(f"{rel} 缺少共用 lexer 界線標記 `{start_m}` / `{end_m}`")
            blocks.append((rel, blk))
        valid_blocks = [(rel, blk) for rel, blk in blocks if blk is not None]
        if len(valid_blocks) == len(lexer_files):
            ref_rel, ref_blk = valid_blocks[0]
            for rel, blk in valid_blocks[1:]:
                if blk != ref_blk:
                    findings.append(f"{rel} 與 {ref_rel} 的共用 lexer 區塊不一致（已發生漂移）")

    return {
        "ok": not findings,
        "budget": budget,
        "role_chars": sizes,
        "findings": findings,
    }
