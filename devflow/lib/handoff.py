"""Structured handoff validation, ARCH target-file inheritance, and atomic Allowed-Scope merging."""
from __future__ import annotations

import fnmatch
import re
from functools import lru_cache
from typing import Any

from . import policy as policy_lib
from . import scope as scope_lib

_EPIC_ACCEPT_HEADING_RE = re.compile(r"^##\s*驗收[ \t]*$", re.M)
_NEXT_HEADING_RE = re.compile(r"^##\s+\S", re.M)
_BULLET_PREFIX_RE = re.compile(r"^\s*[-*]\s+")
CARD_KIND_STEPS: dict[str, str] = {
    "RES": "repro",
    "ARCH": "design",
    "DEV": "implement",
    "OPS": "implement",
    "SHIP": "ship",
}


def step_for_card_kind(kind: str) -> str | None:
    """Map a card prefix (`RES`/`ARCH`/`DEV`/`OPS`/`SHIP`) to its handoff schema step name."""
    return CARD_KIND_STEPS.get((kind or "").strip().upper())


def validate_card_handoff(
    kind: str,
    metadata: Any,
    policy: dict[str, Any] | None = None,
    *,
    step: str | None = None,
) -> list[str]:
    """Validate structured `metadata` for a completed or checkpointed card based on its card kind."""
    upper = (kind or "").strip().upper()
    step_override = (step or "").strip().lower()
    if step_override == "implement_local":
        if upper not in ("DEV", "OPS"):
            return [f"[{upper or '?'}] 卡型不支援 `implement_local` 本機實作 checkpoint"]
        return validate_handoff("implement_local", metadata, policy)
    if upper == "ARCH":
        if step_override and step_override != "design":
            return [f"[ARCH] 不支援步驟 '{step_override}'（預期 design）"]
        _, errs = validate_arch_handoff(metadata, policy)
        return errs
    default_step = step_for_card_kind(upper)
    if step_override:
        if not default_step or step_override != default_step:
            return [f"[{upper or '?'}] 卡型預期步驟 '{default_step or 'none'}'，與指定 '{step_override}' 不符"]
        return validate_handoff(default_step, metadata, policy)
    if not default_step:
        return []
    return validate_handoff(default_step, metadata, policy)


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
    required = list(schema.get("required") or [])
    when_clauses = schema.get("required_when") or []
    if when_clauses:
        matched_req: list[str] | None = None
        default_req: list[str] | None = None
        for clause in when_clauses:
            if not isinstance(clause, dict):
                continue
            cond = clause.get("when")
            if isinstance(cond, dict) and cond:
                if all(k in metadata and metadata.get(k) == v for k, v in cond.items()):
                    matched_req = list(clause.get("required") or [])
                    break
            elif clause.get("default") and default_req is None:
                default_req = list(clause.get("required") or [])
        for req in (matched_req if matched_req is not None else (default_req or [])):
            if req not in required:
                required.append(req)

    errs: list[str] = []
    for req in required:
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


def validate_arch_handoff(
    metadata: Any,
    policy: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate completed `[ARCH]` handoff `metadata` and return `(planned_files, errors)`.

    Enforces structured `design` schema (`decisions` + `target_files`, or legacy `file_plan`/
    `allowed_scope` with extracted concrete paths) so empty/missing handoff metadata cannot
    silently bypass downstream `Allowed-Scope` inheritance.
    """
    files = extract_arch_target_files(metadata)
    errs = validate_handoff("design", metadata, policy)
    if files:
        errs = [e for e in errs if "缺少必填欄位 `target_files`" not in e]
        broad = [p for p in files if scope_lib.is_overly_broad_scope(p)]
        if broad and not any("過寬" in e for e in errs):
            errs.append(f"[design] 規劃檔案過寬（{', '.join(broad)}），不得使用全專案萬用模式")
    elif isinstance(metadata, dict) and not any("target_files" in e for e in errs):
        errs.append("[design] handoff metadata 未包含任何有效 `target_files` / `file_plan` 路徑")
    return files, errs


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


_TOKEN_KIND_STARSTAR = "STARSTAR"
_TOKEN_KIND_TREE_DESC = "TREE_DESC"
_TOKEN_KIND_WILD = "WILD"
_TOKEN_KIND_LIT = "LIT"


def _seg_lit_prefix(seg: str) -> str:
    wilds = [seg.find(ch) for ch in ("*", "?", "[") if ch in seg]
    return seg[: min(wilds)] if wilds else seg


def _seg_lit_suffix(seg: str) -> str:
    i = len(seg) - 1
    while i >= 0:
        ch = seg[i]
        if ch in ("*", "?"):
            break
        if ch == "]":
            if "[" in seg[:i]:
                break
        i -= 1
    else:
        return seg
    return seg[i + 1 :]


def _can_span_slash(seg: str) -> bool:
    if "*" in seg or "?" in seg:
        return True
    i = 0
    n = len(seg)
    while i < n:
        if seg[i] == "[":
            j = i + 1
            if j < n and seg[j] in ("!", "^"):
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j < n and fnmatch.fnmatchcase("/", seg[i : j + 1]):
                return True
            i = j + 1 if j < n else i + 1
        else:
            i += 1
    return False


def _segs_can_overlap(sa: str, sb: str, k_a: str, k_b: str) -> bool:
    if k_a == _TOKEN_KIND_LIT and k_b == _TOKEN_KIND_LIT:
        return sa == sb
    if k_a == _TOKEN_KIND_LIT:
        return fnmatch.fnmatchcase(sa, sb)
    if k_b == _TOKEN_KIND_LIT:
        return fnmatch.fnmatchcase(sb, sa)
    pa, pb = _seg_lit_prefix(sa), _seg_lit_prefix(sb)
    if pa and pb and not (pa.startswith(pb) or pb.startswith(pa)):
        return False
    ea, eb = _seg_lit_suffix(sa), _seg_lit_suffix(sb)
    if ea and eb and not (ea.endswith(eb) or eb.endswith(ea)):
        return False
    return True


def _span_head_token(seg: str) -> str:
    """Portion of a slash-spanning segment up to and including its first slash-spanning wildcard."""
    i = 0
    n = len(seg)
    while i < n:
        ch = seg[i]
        if ch in ("*", "?"):
            return seg[: i + 1] + "*"
        if ch == "[":
            j = i + 1
            if j < n and seg[j] in ("!", "^"):
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j < n and fnmatch.fnmatchcase("/", seg[i : j + 1]):
                return seg[: j + 1] + "*"
            i = j + 1 if j < n else i + 1
            continue
        i += 1
    return seg


def _span_tail_token(seg: str) -> str:
    """Portion of a slash-spanning segment from its last slash-spanning wildcard to end."""
    lit = _seg_lit_suffix(seg)
    return "*" + lit if lit else "*"


def _compile_overlap_spec(raw_pat: str) -> tuple[tuple[tuple[str, str], ...], bool, str]:
    """Compile normalized pattern tokens, `is_tree`, and required trailing literal suffix."""
    raw = (raw_pat or "").strip().strip("`'\"")
    trailing_slash = raw.endswith("/")
    pat = _normalize_glob_token(raw)
    if not pat:
        return (), False, ""
    has_wild = any(ch in pat for ch in ("*", "?", "["))
    if "**" in pat:
        prefix, _, suffix = pat.partition("**")
        prefix = prefix.rstrip("/")
        suffix = suffix.lstrip("/")
        tokens: list[tuple[str, str]] = []
        if prefix:
            for seg in prefix.split("/"):
                if not seg:
                    continue
                tokens.append((_TOKEN_KIND_WILD if any(c in seg for c in ("*", "?", "[")) else _TOKEN_KIND_LIT, seg))
        tokens.append((_TOKEN_KIND_STARSTAR, "**"))
        if not suffix:
            return tuple(tokens), True, ""
        suf_raw_trailing = suffix.endswith("/")
        suf_segs = [s for s in suffix.rstrip("/").split("/") if s]
        for idx, seg in enumerate(suf_segs):
            # `scope.match_glob` checks `fnmatch(p, f"*{suffix}")`, so the first post-`**`
            # segment may match the tail of a longer segment without a `/` boundary.
            eff = ("*" + seg) if (idx == 0 and not seg.startswith("*")) else seg
            if eff == "**":
                tokens.append((_TOKEN_KIND_STARSTAR, "**"))
            else:
                tokens.append((_TOKEN_KIND_WILD if any(c in eff for c in ("*", "?", "[")) else _TOKEN_KIND_LIT, eff))
        if suf_raw_trailing:
            tokens.append((_TOKEN_KIND_TREE_DESC, "/"))
            return tuple(tokens), True, ""
        return tuple(tokens), False, _seg_lit_suffix(suf_segs[-1]) if suf_segs else ""

    if trailing_slash:
        tokens = [
            (_TOKEN_KIND_WILD if any(c in seg for c in ("*", "?", "[")) else _TOKEN_KIND_LIT, seg)
            for seg in pat.split("/") if seg
        ]
        tokens.append((_TOKEN_KIND_TREE_DESC, "/"))
        return tuple(tokens), True, ""

    if not has_wild:
        tokens = [(_TOKEN_KIND_LIT, seg) for seg in pat.split("/") if seg]
        tokens.append((_TOKEN_KIND_STARSTAR, "**"))
        return tuple(tokens), True, ""

    segs = [s for s in pat.split("/") if s]
    tokens = [
        (_TOKEN_KIND_WILD if any(c in seg for c in ("*", "?", "[")) else _TOKEN_KIND_LIT, seg)
        for seg in segs
    ]
    end_lit = _seg_lit_suffix(segs[-1]) if segs else ""
    return tuple(tokens), False, end_lit


def _globs_overlap(a: str, b: str) -> bool:
    """Segment-aware over-approximation of `scope.match_glob` intersection with zero false negatives."""
    pa = _normalize_glob_token(a)
    pb = _normalize_glob_token(b)
    if not pa or not pb:
        return False
    if pa == pb or scope_lib.match_glob(pa, pb) or scope_lib.match_glob(pb, pa):
        return True
    toks_a, tree_a, end_a = _compile_overlap_spec(a)
    toks_b, tree_b, end_b = _compile_overlap_spec(b)
    if not toks_a or not toks_b:
        return False
    if not tree_a and not tree_b and end_a and end_b:
        if not (end_a.endswith(end_b) or end_b.endswith(end_a)):
            return False

    na, nb = len(toks_a), len(toks_b)

    @lru_cache(maxsize=None)
    def dp(ia: int, ib: int) -> bool:
        if ia == na and ib == nb:
            return True
        if ia < na and toks_a[ia][0] == _TOKEN_KIND_STARSTAR:
            if dp(ia + 1, ib) or (ib < nb and dp(ia, ib + 1)):
                return True
        if ib < nb and toks_b[ib][0] == _TOKEN_KIND_STARSTAR:
            if dp(ia, ib + 1) or (ia < na and dp(ia + 1, ib)):
                return True
        if ia == na or ib == nb:
            return False

        ka, sa = toks_a[ia]
        kb, sb = toks_b[ib]

        if ka == _TOKEN_KIND_TREE_DESC:
            # Trailing `/` requires >= 1 concrete segment on B (or absorption into B's wildcard tail).
            if kb in (_TOKEN_KIND_TREE_DESC,):
                return dp(ia + 1, ib + 1) or dp(ia + 1, ib) or dp(ia, ib + 1)
            if dp(ia + 1, ib + 1) or dp(ia, ib + 1):
                return True
            if kb == _TOKEN_KIND_WILD and _can_span_slash(sb) and dp(ia + 1, ib):
                return True
            return False
        if kb == _TOKEN_KIND_TREE_DESC:
            if dp(ia + 1, ib + 1) or dp(ia + 1, ib):
                return True
            if ka == _TOKEN_KIND_WILD and _can_span_slash(sa) and dp(ia, ib + 1):
                return True
            return False

        if _segs_can_overlap(sa, sb, ka, kb) and dp(ia + 1, ib + 1):
            return True

        # A slash-spanning segment on one side can absorb >= 1 segments on the other side.
        if ka == _TOKEN_KIND_WILD and _can_span_slash(sa):
            head_a = _span_head_token(sa)
            tail_a = _span_tail_token(sa)
            if _segs_can_overlap(head_a, sb, _TOKEN_KIND_WILD, kb):
                for jb in range(ib + 1, nb):
                    kjb, sjb = toks_b[jb]
                    if kjb in (_TOKEN_KIND_STARSTAR, _TOKEN_KIND_TREE_DESC):
                        if dp(ia + 1, jb) or dp(ia + 1, jb + 1):
                            return True
                    elif _segs_can_overlap(tail_a, sjb, _TOKEN_KIND_WILD, kjb) and dp(ia + 1, jb + 1):
                        return True
        if kb == _TOKEN_KIND_WILD and _can_span_slash(sb):
            head_b = _span_head_token(sb)
            tail_b = _span_tail_token(sb)
            if _segs_can_overlap(sa, head_b, ka, _TOKEN_KIND_WILD):
                for ja in range(ia + 1, na):
                    kja, sja = toks_a[ja]
                    if kja in (_TOKEN_KIND_STARSTAR, _TOKEN_KIND_TREE_DESC):
                        if dp(ja, ib + 1) or dp(ja + 1, ib + 1):
                            return True
                    elif _segs_can_overlap(sja, tail_b, kja, _TOKEN_KIND_WILD) and dp(ja + 1, ib + 1):
                        return True
        return False

    return dp(0, 0)


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
