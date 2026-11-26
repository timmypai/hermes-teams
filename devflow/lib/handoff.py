"""Structured handoff validation, ARCH target-file inheritance, and atomic Allowed-Scope merging."""
from __future__ import annotations

import re
from typing import Any

from lib import policy as policy_lib
from lib import scope as scope_lib

_EPIC_ACCEPT_HEADING_RE = re.compile(r"^##\s*驗收[ \t]*$", re.M)
_NEXT_HEADING_RE = re.compile(r"^##\s+\S", re.M)
_BULLET_PREFIX_RE = re.compile(r"^\s*[-*]\s+")


def _is_empty_handoff_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def validate_handoff(step: str, metadata: Any, policy: dict[str, Any] | None = None) -> list[str]:
    """Validate structured worker `metadata` against declarative `policy.handoff_schemas`."""
    key = (step or "").strip().lower()
    schemas = policy_lib.handoff_schemas(policy)
    if key not in schemas:
        return [f"未知的 handoff 步驟 '{step}'（允許：{' / '.join(sorted(schemas))}）"]
    if not isinstance(metadata, dict):
        return [f"[{key}] handoff metadata 須為 JSON 物件，收到 {type(metadata).__name__}"]

    schema = schemas[key]
    errs: list[str] = []
    for req in schema.get("required") or []:
        if req not in metadata or _is_empty_handoff_value(metadata.get(req)):
            errs.append(f"[{key}] handoff metadata 缺少必填欄位 `{req}`")

    if key == "design" and "target_files" in metadata:
        raw_targets = metadata.get("target_files")
        targets = extract_arch_target_files({"target_files": raw_targets})
        if raw_targets and not targets:
            errs.append("[design] `target_files` 必須包含有效 repo 相對路徑或 glob")
        broad = [p for p in targets if scope_lib.is_overly_broad_scope(p)]
        if broad:
            errs.append(
                f"[design] `target_files` 過寬（{', '.join(broad)}），不得使用全專案萬用模式"
            )
    return errs


def _normalize_glob_token(token: str) -> str:
    s = (token or "").strip().strip("`'\"")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/").rstrip("/")


def _tokens_from_field(value: Any) -> list[str]:
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        for line in value.splitlines():
            clean = _BULLET_PREFIX_RE.sub("", line).strip()
            if not clean:
                continue
            for tok in scope_lib.split_globs(clean):
                norm = _normalize_glob_token(tok)
                if norm and ("/" in norm or "*" in norm or "." in norm or norm in ("Dockerfile", "Makefile")):
                    out.append(norm)
        return out
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, dict):
                for dict_key in ("path", "file", "glob", "pattern", "target"):
                    if dict_key in item:
                        out.extend(_tokens_from_field(item[dict_key]))
            else:
                out.extend(_tokens_from_field(item))
    elif isinstance(value, dict):
        for dict_key in ("files", "paths", "modify", "create", "target_files"):
            if dict_key in value:
                out.extend(_tokens_from_field(value[dict_key]))
    return out


def extract_arch_target_files(metadata: dict[str, Any] | None) -> list[str]:
    """Extract planned repo-relative files/globs from an `[ARCH]` run's `metadata`."""
    if not isinstance(metadata, dict):
        return []
    found: list[str] = []
    for key in ("target_files", "allowed_scope", "file_plan"):
        if key in metadata:
            found.extend(_tokens_from_field(metadata.get(key)))
    return list(dict.fromkeys(found))


def missing_arch_scope_files(
    arch_files: list[str],
    allowed_scope: list[str] | None,
    *,
    test_paths: list[str] | None = None,
) -> list[str]:
    """Return planned `[ARCH]` target files not covered by downstream `[DEV]`/`[OPS]` `Allowed-Scope:`."""
    planned = [_normalize_glob_token(f) for f in (arch_files or []) if _normalize_glob_token(f)]
    if not planned:
        return []
    scope_pats = [_normalize_glob_token(p) for p in (allowed_scope or []) if _normalize_glob_token(p)]
    extra_tests = [_normalize_glob_token(p) for p in (test_paths or []) if _normalize_glob_token(p)]
    missing: list[str] = []
    for af in planned:
        if any(af == pat or scope_lib.match_glob(af, pat) for pat in scope_pats):
            continue
        if any(af == pat or scope_lib.match_glob(af, pat) for pat in extra_tests):
            continue
        missing.append(af)
    return missing


def _wildcard_prefix(pat: str) -> str:
    return pat.split("*", 1)[0].rstrip("/")


def _globs_overlap(a: str, b: str) -> bool:
    pa = _normalize_glob_token(a)
    pb = _normalize_glob_token(b)
    if not pa or not pb:
        return False
    if pa == pb or scope_lib.match_glob(pa, pb) or scope_lib.match_glob(pb, pa):
        return True
    a_is_wild = "*" in pa or a.strip().endswith("/")
    b_is_wild = "*" in pb or b.strip().endswith("/")
    a_pre = _wildcard_prefix(pa)
    b_pre = _wildcard_prefix(pb)
    if a_is_wild and a_pre and (pb == a_pre or pb.startswith(a_pre + "/")):
        return True
    if b_is_wild and b_pre and (pa == b_pre or pa.startswith(b_pre + "/")):
        return True
    return False


def merge_allowed_scope_into_body(
    body: str | None,
    additions: list[str],
    *,
    epic_body: str | None = None,
) -> tuple[str, list[str]]:
    """Atomically validate and merge approved path globs into a card body's `Allowed-Scope:`.

    Fails closed (`ValueError`) on empty additions, overly broad wildcards (`*`, `**`), or any
    overlap with the card's `Out-of-Scope:` or the owning EPIC's `## 不能碰` paths.
    """
    raw_items: list[str] = []
    for item in additions or []:
        raw_items.extend(scope_lib.split_globs(str(item or "")))
    clean_adds = list(dict.fromkeys(_normalize_glob_token(g) for g in raw_items if _normalize_glob_token(g)))
    if not clean_adds:
        raise ValueError("缺少要新增的 Allowed-Scope 路徑 glob")

    broad = [g for g in clean_adds if scope_lib.is_overly_broad_scope(g)]
    if broad:
        raise ValueError(f"拒絕過寬萬用路徑（{', '.join(broad)}）：不得加入全專案萬用模式")

    contract = scope_lib.parse_card_contract(body)
    forbidden = list(contract.get("out_of_scope") or [])
    if epic_body:
        sec = scope_lib.parse_epic_sections(epic_body).get("不能碰")
        forbidden.extend(scope_lib.extract_do_not_touch_paths(sec))
    forbidden = list(dict.fromkeys(forbidden))

    for add in clean_adds:
        for forb in forbidden:
            if _globs_overlap(add, forb):
                raise ValueError(f"新增範圍 `{add}` 與禁止範圍 `{forb}` 衝突，已拒絕寫入")

    merged = list(dict.fromkeys([*_existing_normalized(contract.get("allowed_scope") or []), *clean_adds]))
    scope_line = f"Allowed-Scope: {', '.join(merged)}"
    text = body or ""
    if scope_lib.ALLOWED_SCOPE_RE.search(text):
        seen_first = False
        new_lines: list[str] = []
        for line in text.splitlines():
            if scope_lib.ALLOWED_SCOPE_RE.match(line):
                if not seen_first:
                    new_lines.append(scope_line)
                    seen_first = True
                continue
            new_lines.append(line)
        new_body = "\n".join(new_lines)
        if text.endswith("\n") or not text:
            new_body += "\n"
        return new_body, merged

    # Insert Allowed-Scope cleanly inside `## 驗收` (before `EPIC:` if present), else append.
    lines = text.splitlines()
    accept_idx = next((i for i, ln in enumerate(lines) if _EPIC_ACCEPT_HEADING_RE.match(ln)), None)
    if accept_idx is not None:
        end_idx = len(lines)
        epic_line_idx: int | None = None
        for i in range(accept_idx + 1, len(lines)):
            if _NEXT_HEADING_RE.match(lines[i]):
                end_idx = i
                break
            if scope_lib.EPIC_REF if hasattr(scope_lib, "EPIC_REF") else False:  # pragma: no cover
                pass
            if re.match(r"^\s*EPIC[：:]", lines[i], re.I) and epic_line_idx is None:
                epic_line_idx = i
        insert_idx = epic_line_idx if epic_line_idx is not None else end_idx
        lines.insert(insert_idx, scope_line)
        new_body = "\n".join(lines).rstrip("\n") + "\n"
        return new_body, merged

    suffix = "\n" if text and not text.endswith("\n") else ""
    new_body = f"{text}{suffix}{scope_line}\n"
    return new_body, merged


def _existing_normalized(items: list[str]) -> list[str]:
    return [n for x in items if (n := _normalize_glob_token(x))]
