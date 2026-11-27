"""Structured handoff validation, ARCH target-file inheritance, and atomic Allowed-Scope merging."""
from __future__ import annotations

import re
import sys
from collections import deque
from pathlib import PurePosixPath
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


class _CharNFA:
    """Compact epsilon-NFA over characters for exact `scope.match_glob` intersection."""

    def __init__(self) -> None:
        self.eps: list[list[int]] = []
        self.step: list[list[tuple[tuple[str, Any], int]]] = []
        self.start = self.new_state()
        self.accept = self.new_state()

    def new_state(self) -> int:
        idx = len(self.eps)
        self.eps.append([])
        self.step.append([])
        return idx

    def add_eps(self, src: int, dst: int) -> None:
        self.eps[src].append(dst)

    def add_step(self, src: int, matcher: tuple[str, Any], dst: int) -> None:
        self.step[src].append((matcher, dst))

    def emit_literal(self, src: int, text: str) -> int:
        cur = src
        for ch in text:
            nxt = self.new_state()
            self.add_step(cur, ("lit", ch), nxt)
            cur = nxt
        return cur

    def emit_any_star(self, src: int) -> int:
        loop = self.new_state()
        self.add_eps(src, loop)
        self.add_step(loop, ("any", None), loop)
        return loop

    def emit_fnmatch(self, src: int, pat: str, *, allow_doublestar: bool = False) -> int:
        cur = src
        i = 0
        n = len(pat)
        while i < n:
            if allow_doublestar and pat.startswith("**/", i):
                after = self.new_state()
                self.add_eps(cur, after)
                mid = self.emit_any_star(cur)
                self.add_step(mid, ("lit", "/"), after)
                cur = after
                i += 3
                continue
            ch = pat[i]
            if ch == "*":
                cur = self.emit_any_star(cur)
                while i < n and pat[i] == "*":
                    i += 1
            elif ch == "?":
                nxt = self.new_state()
                self.add_step(cur, ("any", None), nxt)
                cur = nxt
                i += 1
            elif ch == "[":
                matcher, next_i = _parse_fnmatch_class(pat, i)
                nxt = self.new_state()
                self.add_step(cur, matcher, nxt)
                cur = nxt
                i = next_i
            else:
                nxt = self.new_state()
                self.add_step(cur, ("lit", ch), nxt)
                cur = nxt
                i += 1
        return cur


_MAX_CODEPOINT = sys.maxunicode


def _normalize_intervals(intervals: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    if not intervals:
        return ()
    ordered = sorted(intervals)
    out = [ordered[0]]
    for lo, hi in ordered[1:]:
        plo, phi = out[-1]
        if lo <= phi + 1:
            out[-1] = (plo, max(phi, hi))
        else:
            out.append((lo, hi))
    return tuple(out)


def _complement_intervals(intervals: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
    out: list[tuple[int, int]] = []
    cur = 0
    for lo, hi in intervals:
        if cur < lo:
            out.append((cur, lo - 1))
        cur = hi + 1
    if cur <= _MAX_CODEPOINT:
        out.append((cur, _MAX_CODEPOINT))
    return tuple(out)


def _intersect_intervals(
    iv1: tuple[tuple[int, int], ...],
    iv2: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    out: list[tuple[int, int]] = []
    i, j = 0, 0
    while i < len(iv1) and j < len(iv2):
        lo = max(iv1[i][0], iv2[j][0])
        hi = min(iv1[i][1], iv2[j][1])
        if lo <= hi:
            out.append((lo, hi))
        if iv1[i][1] < iv2[j][1]:
            i += 1
        else:
            j += 1
    return tuple(out)


def _parse_fnmatch_class(pat: str, i: int) -> tuple[tuple[str, Any], int]:
    """Parse Python `fnmatch`-style `[...]` character class at `i` with exact range semantics."""
    n = len(pat)
    j = i + 1
    if j < n and pat[j] == "!":
        j += 1
    if j < n and pat[j] == "]":
        j += 1
    while j < n and pat[j] != "]":
        j += 1
    if j >= n:
        return ("lit", "["), i + 1

    s = i + 1
    stuff = pat[s:j]
    neg = stuff.startswith("!")
    if "-" not in stuff:
        body = stuff[1:] if neg else stuff
        iv = _normalize_intervals([(ord(c), ord(c)) for c in body])
    else:
        chunks: list[str] = []
        pos = s
        k = s + 2 if pat[s] == "!" else s + 1
        while True:
            k = pat.find("-", k, j)
            if k < 0:
                break
            chunks.append(pat[pos:k])
            pos = k + 1
            k = k + 3
        chunk = pat[pos:j]
        if chunk:
            chunks.append(chunk)
        else:
            chunks[-1] += "-"
        # Match fnmatch._translate: strip descending ranges right-to-left.
        for idx in range(len(chunks) - 1, 0, -1):
            if chunks[idx - 1] and chunks[idx] and chunks[idx - 1][-1] > chunks[idx][0]:
                chunks[idx - 1] = chunks[idx - 1][:-1] + chunks[idx][1:]
                del chunks[idx]
        if neg and chunks and chunks[0].startswith("!"):
            chunks[0] = chunks[0][1:]
        raw_iv: list[tuple[int, int]] = []
        if len(chunks) == 1:
            for c in chunks[0]:
                raw_iv.append((ord(c), ord(c)))
        else:
            for m, ch in enumerate(chunks):
                lo_i = 1 if m > 0 else 0
                hi_i = len(ch) - 1 if m < len(chunks) - 1 else len(ch)
                for c in ch[lo_i:hi_i]:
                    raw_iv.append((ord(c), ord(c)))
                if m < len(chunks) - 1 and ch and chunks[m + 1]:
                    raw_iv.append((ord(ch[-1]), ord(chunks[m + 1][0])))
        iv = _normalize_intervals(raw_iv)

    if neg:
        iv = _complement_intervals(iv)
    if iv == ((0, _MAX_CODEPOINT),):
        return ("any", None), j + 1
    return ("cls", iv), j + 1


def _has_fnmatch_wild(pat: str) -> bool:
    if "*" in pat or "?" in pat:
        return True
    i = 0
    n = len(pat)
    while i < n:
        if pat[i] == "[":
            matcher, next_i = _parse_fnmatch_class(pat, i)
            if matcher[0] != "lit":
                return True
            i = next_i
        else:
            i += 1
    return False


def _compile_glob_nfas(raw_pat: str) -> tuple[_CharNFA, ...] | None:
    """Compile `raw_pat` into a conjunction of character NFAs matching `scope_lib.match_glob`."""
    raw = (raw_pat or "").strip().strip("`'\"")
    pat = raw.lstrip("./")
    if not pat:
        return None
    if pat.endswith("/"):
        nfa = _CharNFA()
        end = nfa.emit_any_star(nfa.emit_literal(nfa.start, pat))
        nfa.add_eps(end, nfa.accept)
        return (nfa,)

    if "**" in pat:
        prefix, _, suffix = pat.partition("**")
        prefix = prefix.rstrip("/")
        suffix = suffix.lstrip("/")
        terms: list[_CharNFA] = []
        if prefix:
            np = _CharNFA()
            pend = np.emit_literal(np.start, prefix)
            np.add_eps(pend, np.accept)
            np.add_eps(np.emit_any_star(np.emit_literal(pend, "/")), np.accept)
            terms.append(np)
        if suffix:
            ns = _CharNFA()
            mid = ns.emit_any_star(ns.start)
            ns.add_eps(ns.emit_fnmatch(mid, suffix), ns.accept)
            collapsed = re.sub(r"/+", "/", PurePosixPath(suffix).as_posix())
            if collapsed != suffix or "**" in suffix:
                mid2 = ns.emit_any_star(ns.start)
                ns.add_eps(ns.emit_fnmatch(mid2, collapsed, allow_doublestar=True), ns.accept)
            terms.append(ns)
        if not terms:
            na = _CharNFA()
            na.add_eps(na.emit_any_star(na.start), na.accept)
            terms.append(na)
        return tuple(terms)

    nfa = _CharNFA()
    nfa.add_eps(nfa.emit_fnmatch(nfa.start, pat), nfa.accept)
    if not _has_fnmatch_wild(pat):
        pend = nfa.emit_literal(nfa.start, pat)
        nfa.add_eps(pend, nfa.accept)
        nfa.add_eps(nfa.emit_any_star(nfa.emit_literal(pend, "/")), nfa.accept)
    return (nfa,)


def _meet_matchers(m1: tuple[str, Any], m2: tuple[str, Any]) -> tuple[str, Any] | None:
    k1, v1 = m1
    k2, v2 = m2
    if k1 == "any":
        return None if (k2 == "cls" and not v2) else m2
    if k2 == "any":
        return None if (k1 == "cls" and not v1) else m1
    if k1 == "lit" and k2 == "lit":
        return m1 if v1 == v2 else None
    if k1 == "lit":
        o = ord(v1)
        return m1 if any(lo <= o <= hi for lo, hi in v2) else None
    if k2 == "lit":
        o = ord(v2)
        return m2 if any(lo <= o <= hi for lo, hi in v1) else None
    inter = _intersect_intervals(v1, v2)
    return ("cls", inter) if inter else None


def _prepare_nfa(nfa: _CharNFA) -> tuple[list[bool], list[list[tuple[tuple[str, Any], int]]]]:
    n = len(nfa.eps)
    is_acc = [False] * n
    eff: list[list[tuple[tuple[str, Any], int]]] = [[] for _ in range(n)]
    for s in range(n):
        seen = {s}
        stack = [s]
        while stack:
            u = stack.pop()
            for v in nfa.eps[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        is_acc[s] = nfa.accept in seen
        seen_edges: set[tuple[tuple[str, Any], int]] = set()
        for u in seen:
            for m, d in nfa.step[u]:
                edge = (m, d)
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    eff[s].append(edge)
    return is_acc, eff


def _globs_overlap(a: str, b: str) -> bool:
    """Exact character-NFA conjunction intersection of `scope.match_glob` languages (zero false negatives)."""
    pa = (a or "").strip().strip("`'\"").lstrip("./")
    pb = (b or "").strip().strip("`'\"").lstrip("./")
    if not pa or not pb:
        return False
    if pa == pb:
        return True
    if (not _has_fnmatch_wild(pa) and scope_lib.match_glob(pa, pb)) or (
        not _has_fnmatch_wild(pb) and scope_lib.match_glob(pb, pa)
    ):
        return True

    na = _compile_glob_nfas(a)
    nb = _compile_glob_nfas(b)
    if na is None or nb is None:
        return False
    nfas = na + nb
    k = len(nfas)
    prepped = [_prepare_nfa(x) for x in nfas]
    start = tuple(x.start for x in nfas)
    if all(prepped[i][0][start[i]] for i in range(k)):
        return True

    visited: set[tuple[int, ...]] = {start}
    queue: deque[tuple[int, ...]] = deque([start])
    max_states = 50_000
    while queue:
        cur = queue.popleft()
        dfs: list[tuple[int, tuple[str, Any], list[int]]] = [(0, ("any", None), [])]
        while dfs:
            idx, acc_m, dsts = dfs.pop()
            if idx == k:
                nxt = tuple(dsts)
                if all(prepped[i][0][nxt[i]] for i in range(k)):
                    return True
                if nxt not in visited:
                    visited.add(nxt)
                    if len(visited) > max_states:
                        return True
                    queue.append(nxt)
                continue
            for m, d in prepped[idx][1][cur[idx]]:
                nm = _meet_matchers(acc_m, m)
                if nm is not None:
                    dfs.append((idx + 1, nm, [*dsts, d]))
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
