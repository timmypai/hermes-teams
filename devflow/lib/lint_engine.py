"""Unified zero-hardcode declarative TOML lint engine (stdlib-only).

Supports four rule kinds across kit workflow audits and project lint checks:
- `regex`: line/text regex scans on matched repo files
- `cross_file`: declarative multi-file policy consistency checks (e.g. config.env vs AGENTS.md)
- `import_boundary`: Python AST + JS/TS import boundary enforcement
- `diff_policy`: declarative diff file/line budget enforcement

Every rule file (`<id>.toml`) must carry `[[fixture]]` self-tests (`expect = "pass"` and `"fail"`).
"""
from __future__ import annotations

import ast
import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import policy as policy_lib

VALID_KINDS = frozenset({"regex", "cross_file", "import_boundary", "diff_policy", "diff_budget"})
DIFF_KINDS = frozenset({"diff_policy", "diff_budget"})
VALID_SEVERITIES = frozenset({"warn", "error"})
DEFAULT_SKIP_DIRS = frozenset({".git", ".worktrees", "node_modules", ".venv", "vendor", "dist", "build", "__pycache__"})
ALLOW_EXEMPT_RE = re.compile(r"devflow:\s*allow\s+([\w-]+)(?:\s+(L-\d{4}))?")
JS_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
MAX_FILE_BYTES = 1_000_000


class LintConfigError(Exception):
    """Raised when a TOML lint rule file is malformed or violates schema contracts."""


def _norm_id(value: str) -> str:
    return (value or "").strip().replace("-", "_")


def _strip_rel_prefix(path: str) -> str:
    s = (path or "").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _compile_glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\[")
                i += 1
            else:
                stuff = pattern[i + 1 : j]
                neg = stuff.startswith(("!", "^"))
                body = stuff[1:] if neg else stuff
                if not body:
                    raise ValueError(f"invalid glob pattern {pattern!r}: empty character class")
                body = body.replace("\\", r"\\").replace("[", r"\[").replace("--", r"\-\-")
                body = re.sub(r"([&~|])", r"\\\1", body)
                if body.startswith("]"):
                    body = r"\]" + body[1:]
                if not neg and body.startswith("^"):
                    body = r"\^" + body[1:]
                out.append(f"(?!/)[{'^' if neg else ''}{body}]")
                i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    try:
        return re.compile("".join(out) + r"\Z")
    except re.error as err:
        raise ValueError(f"invalid glob pattern {pattern!r}: {err}") from err


def glob_matches(path: str, pattern: str) -> bool:
    p = _strip_rel_prefix(path)
    pat = _strip_rel_prefix(pattern)
    if not pat:
        return False
    if pat.endswith("/"):
        return p.startswith(pat)
    return bool(_compile_glob_regex(pat).match(p))


def _line_exempt(line: str, rule_id: str) -> bool:
    for m in ALLOW_EXEMPT_RE.finditer(line or ""):
        if _norm_id(m.group(1)) == _norm_id(rule_id):
            return True
    return False


def _is_line_exempt(lines: list[str], lineno: int, rule_id: str) -> bool:
    if 1 <= lineno <= len(lines) and _line_exempt(lines[lineno - 1], rule_id):
        return True
    if lineno > 1 and lineno - 1 <= len(lines) and _line_exempt(lines[lineno - 2], rule_id):
        return True
    return False


@dataclass(frozen=True)
class LintRule:
    id: str
    kind: str
    message: str
    severity: str = "warn"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    when: tuple[dict[str, Any], ...] = ()
    target_include: tuple[str, ...] = ()
    target_exclude: tuple[str, ...] = ()
    target_patterns: tuple[re.Pattern[str], ...] = ()
    target_mode: str = "forbid"
    forbidden_modules: tuple[str, ...] = ()
    max_files: int | None = None
    max_added_lines: int | None = None
    fixtures: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def applies(self, rel_path: str) -> bool:
        if any(glob_matches(rel_path, ex) for ex in self.exclude):
            return False
        incs = self.target_include if self.kind == "cross_file" else self.include
        if self.kind == "cross_file" and self.target_exclude:
            if any(glob_matches(rel_path, ex) for ex in self.target_exclude):
                return False
        return any(glob_matches(rel_path, inc) for inc in incs) if incs else False


def _validate_rule_globs(globs: tuple[str, ...], *, src: Path, label: str) -> None:
    for g in globs:
        pat = _strip_rel_prefix(g)
        if not pat or pat.endswith("/"):
            continue
        try:
            _compile_glob_regex(pat)
        except ValueError as err:
            raise LintConfigError(f"{src}: `{label}` {err}") from err


def _compile_patterns(raw: Any, *, flags: int, src: Path, label: str) -> tuple[re.Pattern[str], ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) and x for x in raw):
        raise LintConfigError(f"{src}: `{label}` must be a non-empty list of regex strings")
    compiled: list[re.Pattern[str]] = []
    for pat in raw:
        try:
            compiled.append(re.compile(pat, flags))
        except re.error as err:
            raise LintConfigError(f"{src}: invalid regex {pat!r}: {err}") from err
    return tuple(compiled)


def load_rule(path: Path) -> LintRule:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise LintConfigError(f"{path}: {err}") from err

    rid = str(data.get("id") or "").strip()
    if not rid:
        raise LintConfigError(f"{path}: missing `id`")
    if _norm_id(rid) != _norm_id(path.stem):
        raise LintConfigError(f"{path}: `id` ({rid!r}) must match filename stem ({path.stem!r})")

    kind = str(data.get("kind") or "regex").strip().lower()
    if kind not in VALID_KINDS:
        raise LintConfigError(f"{path}: invalid kind {kind!r} (allowed: {', '.join(sorted(VALID_KINDS))})")

    message = str(data.get("message") or "").strip()
    if not message:
        raise LintConfigError(f"{path}: missing `message`")

    severity = str(data.get("severity") or "warn").strip().lower()
    if severity not in VALID_SEVERITIES:
        raise LintConfigError(f"{path}: invalid severity {severity!r}")

    case_sensitive = bool(data.get("case_sensitive", False))
    flags = re.M | (0 if case_sensitive else re.I)

    include = tuple(str(x) for x in (data.get("include") or []) if str(x).strip())
    exclude = tuple(str(x) for x in (data.get("exclude") or []) if str(x).strip())
    _validate_rule_globs(include, src=path, label="include")
    _validate_rule_globs(exclude, src=path, label="exclude")
    fixtures = tuple(f for f in (data.get("fixture") or []) if isinstance(f, dict))

    patterns: tuple[re.Pattern[str], ...] = ()
    when_clauses: tuple[dict[str, Any], ...] = ()
    target_include: tuple[str, ...] = ()
    target_exclude: tuple[str, ...] = ()
    target_patterns: tuple[re.Pattern[str], ...] = ()
    target_mode = "forbid"
    forbidden_modules: tuple[str, ...] = ()
    max_files: int | None = None
    max_added_lines: int | None = None

    if kind == "regex":
        if not include:
            raise LintConfigError(f"{path}: regex rule requires non-empty `include`")
        patterns = _compile_patterns(data.get("patterns"), flags=flags, src=path, label="patterns")
    elif kind == "cross_file":
        raw_when = data.get("when")
        if isinstance(raw_when, dict):
            raw_when = [raw_when]
        if not isinstance(raw_when, list) or not raw_when or not all(isinstance(w, dict) for w in raw_when):
            raise LintConfigError(f"{path}: cross_file rule requires non-empty `[[when]]` clause(s)")
        compiled_when: list[dict[str, Any]] = []
        for idx, clause in enumerate(raw_when):
            wpath = str(clause.get("path") or "").strip()
            if not wpath:
                raise LintConfigError(f"{path}: when[{idx}] requires `path`")
            has_env = bool(clause.get("env_key"))
            has_pats = bool(clause.get("patterns"))
            if not has_env and not has_pats:
                raise LintConfigError(f"{path}: when[{idx}] requires `env_key` or `patterns`")
            compiled_when.append({
                "path": wpath,
                "env_key": str(clause.get("env_key") or "").strip(),
                "env_in": [str(x).strip().lower() for x in (clause.get("env_in") or [])],
                "env_not_in": [str(x).strip().lower() for x in (clause.get("env_not_in") or [])],
                "patterns": _compile_patterns(clause["patterns"], flags=flags, src=path, label=f"when[{idx}].patterns")
                if has_pats
                else (),
            })
        when_clauses = tuple(compiled_when)
        target = data.get("target")
        if not isinstance(target, dict):
            raise LintConfigError(f"{path}: cross_file rule requires `[target]` table")
        target_include = tuple(str(x) for x in (target.get("include") or []) if str(x).strip())
        target_exclude = tuple(str(x) for x in (target.get("exclude") or []) if str(x).strip())
        _validate_rule_globs(target_include, src=path, label="target.include")
        _validate_rule_globs(target_exclude, src=path, label="target.exclude")
        if not target_include:
            raise LintConfigError(f"{path}: [target] requires non-empty `include`")
        target_mode = str(target.get("mode") or "forbid").strip().lower()
        if target_mode not in ("forbid", "require"):
            raise LintConfigError(f"{path}: invalid [target].mode {target_mode!r}")
        target_patterns = _compile_patterns(target.get("patterns"), flags=flags, src=path, label="target.patterns")
    elif kind == "import_boundary":
        if not include:
            raise LintConfigError(f"{path}: import_boundary rule requires non-empty `include`")
        raw_mods = data.get("forbidden_modules") or data.get("forbidden_prefixes") or []
        if not isinstance(raw_mods, list) or not raw_mods or not all(isinstance(x, str) and x.strip() for x in raw_mods):
            raise LintConfigError(f"{path}: import_boundary rule requires non-empty `forbidden_modules`")
        forbidden_modules = tuple(x.strip() for x in raw_mods)
    elif kind in DIFF_KINDS:
        mf = data.get("max_files")
        ml = data.get("max_added_lines")
        if mf is None and ml is None:
            raise LintConfigError(f"{path}: {kind} rule requires `max_files` or `max_added_lines`")
        if mf is not None:
            if not isinstance(mf, int) or mf <= 0:
                raise LintConfigError(f"{path}: `max_files` must be a positive integer")
            max_files = mf
        if ml is not None:
            if not isinstance(ml, int) or ml <= 0:
                raise LintConfigError(f"{path}: `max_added_lines` must be a positive integer")
            max_added_lines = ml

    return LintRule(
        id=rid,
        kind=kind,
        message=message,
        severity=severity,
        include=include,
        exclude=exclude,
        patterns=patterns,
        when=when_clauses,
        target_include=target_include,
        target_exclude=target_exclude,
        target_patterns=target_patterns,
        target_mode=target_mode,
        forbidden_modules=forbidden_modules,
        max_files=max_files,
        max_added_lines=max_added_lines,
        fixtures=fixtures,
    )


def load_rules(rules_dir: Path | str) -> list[LintRule]:
    root = Path(rules_dir)
    if not root.is_dir():
        return []
    return [load_rule(p) for p in sorted(root.glob("*.toml"))]


def _resolve_py_relative_import(rel_path: str, module: str | None, level: int) -> str:
    if level <= 0:
        return module or ""
    parts = list(PurePosixPath(rel_path).parent.parts)
    if parts == ["."]:
        parts = []
    climb = level - 1
    base = parts[:-climb] if climb and climb <= len(parts) else ([] if climb else parts)
    return ".".join([*base, module]) if module else ".".join(base)


def _import_matches_forbidden(imp: str, forbidden: str) -> bool:
    clean_imp = (imp or "").strip()
    forb = (forbidden or "").strip()
    if not clean_imp or not forb:
        return False
    if forb.endswith("/"):
        return clean_imp == forb.rstrip("/") or clean_imp.startswith(forb)
    if "/" in clean_imp and "/" not in forb:
        dotted = clean_imp.replace("/", ".")
        return dotted == forb or dotted.startswith(forb + ".")
    return clean_imp == forb or clean_imp.startswith(forb + ".") or clean_imp.startswith(forb + "/")


_JS_SIMPLE_ESCAPES = {
    "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v",
    "\\": "\\", "'": "'", '"': '"', "`": "`", "$": "$",
}
_JS_HEX_CHARS = frozenset("0123456789abcdefABCDEF")
_JS_LINE_BREAKS = frozenset(("\r", "\n", "\u2028", "\u2029"))


def _decode_js_escape(text: str, bs_idx: int, lineno: int) -> tuple[str, int, int]:
    """Decode JS string/template backslash escape at `bs_idx`; return `(decoded, next_idx, next_line)`."""
    n = len(text)
    if bs_idx + 1 >= n:
        return "", bs_idx + 1, lineno
    nxt = text[bs_idx + 1]
    if nxt == "\r":
        step = 3 if (bs_idx + 2 < n and text[bs_idx + 2] == "\n") else 2
        return "", bs_idx + step, lineno + 1
    if nxt in ("\n", "\u2028", "\u2029"):
        return "", bs_idx + 2, lineno + 1
    if nxt in _JS_SIMPLE_ESCAPES:
        return _JS_SIMPLE_ESCAPES[nxt], bs_idx + 2, lineno
    if nxt == "x":
        hx = text[bs_idx + 2 : bs_idx + 4]
        if len(hx) == 2 and all(c in _JS_HEX_CHARS for c in hx):
            return chr(int(hx, 16)), bs_idx + 4, lineno
        return "\\x", bs_idx + 2, lineno
    if nxt == "u":
        if bs_idx + 2 < n and text[bs_idx + 2] == "{":
            end = text.find("}", bs_idx + 3)
            hx = text[bs_idx + 3 : end] if end != -1 else ""
            if hx and all(c in _JS_HEX_CHARS for c in hx):
                cp = int(hx, 16)
                if 0xD800 <= cp <= 0xDFFF:
                    return "\ufffd", end + 1, lineno
                if cp <= 0x10FFFF:
                    return chr(cp), end + 1, lineno
            return "\\u", bs_idx + 2, lineno
        hx = text[bs_idx + 2 : bs_idx + 6]
        if len(hx) == 4 and all(c in _JS_HEX_CHARS for c in hx):
            cp = int(hx, 16)
            next_i = bs_idx + 6
            if 0xD800 <= cp <= 0xDBFF and text[next_i : next_i + 2] == "\\u":
                lo_hx = text[next_i + 2 : next_i + 6]
                if len(lo_hx) == 4 and all(c in _JS_HEX_CHARS for c in lo_hx):
                    lo = int(lo_hx, 16)
                    if 0xDC00 <= lo <= 0xDFFF:
                        return chr(0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)), next_i + 6, lineno
            if 0xD800 <= cp <= 0xDFFF:
                return "\ufffd", next_i, lineno
            return chr(cp), next_i, lineno
        return "\\u", bs_idx + 2, lineno
    if nxt in "01234567":
        j = bs_idx + 2
        if nxt in "0123":
            limit = min(n, bs_idx + 4)
            while j < limit and text[j] in "01234567":
                j += 1
        elif j < n and text[j] in "01234567":
            j += 1
        return chr(int(text[bs_idx + 1 : j], 8)), j, lineno
    return nxt, bs_idx + 2, lineno


def _scan_template_tail(text: str, start: int, lineno: int, *, first: bool) -> tuple[str, int, int, bool]:
    """Scan a JS template segment starting after `` ` `` or `}`; returns `(kind, next_idx, next_line, hit_interp)`."""
    buf: list[str] = []
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            dec, i, lineno = _decode_js_escape(text, i, lineno)
            if first and dec:
                buf.append(dec)
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "{":
            return "", i + 2, lineno, True
        if ch == "`":
            return ("".join(buf) if first else ""), i + 1, lineno, False
        if ch == "\r":
            lineno += 1
            i += 2 if (i + 1 < n and text[i + 1] == "\n") else 1
            if first:
                buf.append("\n")
            continue
        if ch in ("\n", "\u2028", "\u2029"):
            lineno += 1
        if first:
            buf.append(ch)
        i += 1
    return ("".join(buf) if first else ""), i, lineno, False


_REGEX_PREV_KW = frozenset({
    "return", "throw", "case", "else", "do", "yield", "await",
    "typeof", "void", "delete", "in", "default",
})
_BRACE_EXPR_KW = frozenset({
    "return", "throw", "yield", "await", "typeof", "void", "delete", "in",
})
_CTRL_KW = frozenset({"if", "while", "with"})
_REGEX_PREV_PUNCT = frozenset({"(", "[", "{", "}", ",", ";", "=", ":", "?"})
_EXPR_PREV_PUNCT = frozenset({"(", "[", ",", "=", ":", "?"})
_REGEX_PREV_OP = frozenset({
    "=", "+=", "-=", "*=", "/=", "%=", "**=", "&=", "|=", "^=", "<<=", ">>=", ">>>=",
    "&&=", "||=", "??=", "=>", "!", "~", "+", "-", "*", "%", "**", "&", "|", "^",
    "&&", "||", "??", "==", "===", "!=", "!==", ">", "<=", ">=", "<<", ">>", ">>>",
})
_MULTI_OPS = (
    ">>>=", "===", "!==", "**=", "<<=", ">>=", "&&=", "||=", "??=", "...",
    ">>>", "=>", "++", "--", "==", "!=", "<=", ">=", "&&", "||", "??", "**", "<<", ">>",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
)
_REGEX_FLAGS = frozenset("dgimsuvy")


def _sig_tokens_tail(tokens: list[tuple[str, str, int]], limit: int = 3) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for tok in reversed(tokens):
        if tok[0] != "NL":
            out.append(tok)
            if len(out) >= limit:
                break
    return out


def _is_jsx_close_gt(tokens: list[tuple[str, str, int]]) -> bool:
    """Return True when a trailing `>` token closes `<Tag />` or `</Tag>` JSX syntax."""
    sig = _sig_tokens_tail(tokens, 8)
    if len(sig) < 2 or sig[0][0] != "OP" or sig[0][1] != ">":
        return False
    if sig[1][0] == "OP" and sig[1][1] == "/":
        return True
    i = 1
    while i < len(sig):
        k, v, _ = sig[i]
        if k == "IDENT" or (k == "PUNCT" and v == "."):
            i += 1
            continue
        if k == "OP" and v == "/" and i > 1 and i + 1 < len(sig):
            return sig[i + 1][0] == "OP" and sig[i + 1][1] == "<"
        return False
    return False


def _js_allows_regex(tokens: list[tuple[str, str, int]], *, in_for_paren: bool = False) -> bool:
    sig = _sig_tokens_tail(tokens, 2)
    if not sig:
        return True
    kind, val, _ = sig[0]
    if kind in ("TPL", "TPL_MID", "CTRL_CLOSE", "LABEL_COLON"):
        return True
    if kind == "OP":
        if val == ">" and _is_jsx_close_gt(tokens):
            return False
        return val in _REGEX_PREV_OP
    if kind == "PUNCT":
        return val in _REGEX_PREV_PUNCT
    if kind == "IDENT":
        if len(sig) >= 2 and sig[1] == ("PUNCT", ".", sig[1][2]):
            return False
        if val == "of":
            return in_for_paren
        return val in _REGEX_PREV_KW
    return False


def _ctrl_paren_kind(tokens: list[tuple[str, str, int]]) -> str:
    """Return `'for'`, `'ctrl'`, or `''` for an opening `(` (`if`/`while`/`for`/`with`/`for await`)."""
    sig = _sig_tokens_tail(tokens, 3)
    if not sig or sig[0][0] != "IDENT":
        return ""
    if sig[0][1] == "for" and not (len(sig) >= 2 and sig[1][0] == "PUNCT" and sig[1][1] == "."):
        return "for"
    if sig[0][1] in _CTRL_KW and not (len(sig) >= 2 and sig[1][0] == "PUNCT" and sig[1][1] == "."):
        return "ctrl"
    if sig[0][1] == "await" and len(sig) >= 2 and sig[1][0] == "IDENT" and sig[1][1] == "for":
        if not (len(sig) >= 3 and sig[2][0] == "PUNCT" and sig[2][1] == "."):
            return "for"
    return ""


def _is_expr_prefix_tok(
    sig: list[tuple[str, str, int]],
    idx: int,
    brace_kinds: list[str],
    *,
    in_for_paren: bool = False,
) -> bool:
    pk, pv, _ = sig[idx]
    if pk in ("TPL", "TPL_MID"):
        return True
    if pk == "OP":
        return pv != "=>" and pv in _REGEX_PREV_OP
    if pk == "PUNCT":
        if brace_kinds and brace_kinds[-1] == "obj" and pv in ("{", ","):
            return False
        return pv in _EXPR_PREV_PUNCT
    if pk == "IDENT":
        if idx + 1 < len(sig) and sig[idx + 1] == ("PUNCT", ".", sig[idx + 1][2]):
            return False
        if pv == "of":
            return in_for_paren
        if pv == "default":
            is_exp = idx + 1 < len(sig) and sig[idx + 1][0] == "IDENT" and sig[idx + 1][1] == "export"
            return is_exp
        return pv in _BRACE_EXPR_KW
    return False


def _is_fn_class_expr(
    tokens: list[tuple[str, str, int]],
    kw: str,
    brace_kinds: list[str],
    *,
    in_for_paren: bool = False,
) -> bool:
    """Return True when `function`/`class` begins an expression rather than a statement declaration."""
    sig = _sig_tokens_tail(tokens, 4)
    if not sig:
        return False
    idx = 0
    if kw == "function" and sig[0][0] == "IDENT" and sig[0][1] == "async":
        if not (len(sig) >= 2 and sig[1] == ("PUNCT", ".", sig[1][2])):
            idx = 1
    if idx >= len(sig):
        return False
    pk, pv, _ = sig[idx]
    if pk == "IDENT" and pv == "default" and idx + 1 < len(sig) and sig[idx + 1][0] == "IDENT" and sig[idx + 1][1] == "export":
        return False
    return _is_expr_prefix_tok(sig, idx, brace_kinds, in_for_paren=in_for_paren)


def _brace_open_kind(
    tokens: list[tuple[str, str, int]],
    brace_kinds: list[str],
    expr_head_depths: list[tuple[int, int]],
    paren_len: int,
    *,
    in_for_paren: bool = False,
) -> str:
    """Classify `{` as `'fn_expr'`, `'obj'`, or `'block'` so closing `}` preserves division vs regex."""
    depth = (paren_len, len(brace_kinds))
    sig = _sig_tokens_tail(tokens, 2)
    if expr_head_depths and expr_head_depths[-1] == depth:
        is_expr_or_label = bool(
            sig and (sig[0][0] == "LABEL_COLON" or _is_expr_prefix_tok(sig, 0, [], in_for_paren=in_for_paren))
        )
        if not is_expr_or_label:
            expr_head_depths.pop()
            return "fn_expr"
    if not sig:
        return "block"
    pk, pv, _ = sig[0]
    if pk == "PUNCT" and pv in _EXPR_PREV_PUNCT:
        return "obj"
    if _is_expr_prefix_tok(sig, 0, [], in_for_paren=in_for_paren):
        return "obj"
    return "block"


def _prune_depths(
    ternary_depths: list[tuple[int, int]],
    expr_head_depths: list[tuple[int, int]],
    paren_len: int,
    brace_len: int,
    *,
    strict: bool,
) -> None:
    for stack in (ternary_depths, expr_head_depths):
        while stack:
            dp, db = stack[-1]
            drop = (dp >= paren_len and db >= brace_len) if not strict else (dp > paren_len or db > brace_len)
            if not drop:
                break
            stack.pop()


_JS_HORIZ_WS = frozenset(("\t", "\v", "\f", " ", "\u00a0", "\ufeff"))


def _is_js_ws(ch: str) -> bool:
    """Return True for ECMAScript WhiteSpace characters excluding line terminators."""
    return ch in _JS_HORIZ_WS or unicodedata.category(ch) == "Zs"


def _count_js_line_breaks(text: str, start: int, end: int) -> int:
    """Count ECMAScript line breaks (coalescing CRLF) in `text[start:end]`."""
    count = 0
    pos = start
    while pos < end:
        ch = text[pos]
        if ch in _JS_LINE_BREAKS:
            count += 1
            pos += 2 if (ch == "\r" and pos + 1 < end and text[pos + 1] == "\n") else 1
        else:
            pos += 1
    return count


def _try_scan_js_regex(text: str, start: int, lineno: int) -> int | None:
    """Speculatively scan single-line `/pattern/flags`; return next index or None if unterminated or invalid flags."""
    i = start + 1
    n = len(text)
    in_cls = False
    while i < n:
        rc = text[i]
        if rc == "\\" and i + 1 < n:
            if text[i + 1] in _JS_LINE_BREAKS:
                return None
            i += 2
            continue
        if rc in _JS_LINE_BREAKS:
            return None
        if rc == "[":
            in_cls = True
        elif rc == "]":
            in_cls = False
        elif rc == "/" and not in_cls:
            i += 1
            flag_start = i
            while i < n and (text[i].isalnum() or text[i] in ("_", "$")):
                i += 1
            flags = text[flag_start:i]
            if flags and (len(set(flags)) != len(flags) or any(ch not in _REGEX_FLAGS for ch in flags)):
                return None
            return i
        i += 1
    return None


_JSX_CLOSE_TAG_RE = re.compile(r"</(?:\s|\ufeff)*([A-Za-z_$][\w$.-]*)?(?:\s|\ufeff)*>")
_JSX_CLOSE_TAG_AT_RE = _JSX_CLOSE_TAG_RE


def _last_jsx_closes(text: str) -> dict[str, int]:
    """Map each JSX tag name (`""` for fragments) to its latest closing tag offset, or empty if none."""
    if "</" not in text:
        return {}
    out: dict[str, int] = {}
    for m in _JSX_CLOSE_TAG_RE.finditer(text):
        out[m.group(1) or ""] = m.end()
    return out


def _scan_jsx_attr_string(text: str, start: int, lineno: int) -> tuple[int, int] | None:
    """Scan `'...'` or `"..."` inside a JSX attribute; return `(next_idx, next_line)` or None if unclosed."""
    q = text[start]
    i = start + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _JS_LINE_BREAKS:
                lineno += 1
                i += 3 if (nxt == "\r" and i + 2 < n and text[i + 2] == "\n") else 2
                continue
            i += 2
            continue
        if c == q:
            return i + 1, lineno
        if c in _JS_LINE_BREAKS:
            lineno += 1
            i += 2 if (c == "\r" and i + 1 < n and text[i + 1] == "\n") else 1
            continue
        i += 1
    return None


def _scan_jsx_open_tag(
    text: str,
    lt_idx: int,
    lineno: int,
    jsx_closes: dict[str, int],
) -> tuple[str, bool, int, int, list[tuple[str, str, int]]] | None:
    """Scan `<Tag ...>` / `<Tag .../>` / `<>`; return `(tag, self_closing, next_i, next_line, prop_tokens)` or None."""
    n = len(text)
    p = lt_idx + 1
    if p >= n:
        return None
    if text[p] == ">":
        return "", False, p + 1, lineno, []
    if not (text[p].isalpha() or text[p] in ("_", "$")):
        return None
    q = p + 1
    while q < n and (text[q].isalnum() or text[q] in ("_", "$", ".", "-")):
        q += 1
    tag_name = text[p:q]
    p = q
    if p < n and not (text[p] in _JS_LINE_BREAKS or _is_js_ws(text[p]) or text[p] in (">", "/", "<")):
        return None

    prop_tokens: list[tuple[str, str, int]] = []
    angle = 0
    while p < n:
        ch = text[p]
        if ch in _JS_LINE_BREAKS:
            lineno += 1
            p += 2 if (ch == "\r" and p + 1 < n and text[p + 1] == "\n") else 1
            continue
        if _is_js_ws(ch):
            p += 1
            continue
        if ch == "<":
            angle += 1
            p += 1
            continue
        if ch == ">":
            if angle > 0:
                angle -= 1
                p += 1
                continue
            return tag_name, False, p + 1, lineno, prop_tokens
        if ch in ("'", '"'):
            s_res = _scan_jsx_attr_string(text, p, lineno)
            if s_res is None:
                return None
            p, lineno = s_res
            continue
        if ch == "{":
            attr_line = lineno
            inner_toks, p, lineno, closed = _tokenize_js_span(
                text, p + 1, lineno, stop_on_rbrace=True, jsx_closes=jsx_closes,
            )
            if not closed:
                return None
            prop_tokens.append(("PUNCT", "{", attr_line))
            prop_tokens.extend(inner_toks)
            prop_tokens.append(("PUNCT", "}", lineno))
            continue
        if angle > 0:
            if ch == ";":
                return None
            p += 1
            continue
        if ch == "/":
            if p + 1 < n and text[p + 1] == ">":
                return tag_name, True, p + 2, lineno, prop_tokens
            return None
        if ch.isalnum() or ch in ("_", "$", "-", "=", ".", ":"):
            p += 1
            continue
        return None
    return None


def _try_scan_jsx(
    text: str,
    start_lt: int,
    lineno: int,
    jsx_closes: dict[str, int],
) -> tuple[list[tuple[str, str, int]], int, int] | None:
    """Speculatively scan a JSX element or fragment so JSX child text apostrophes/quotes never start JS strings."""
    head = _scan_jsx_open_tag(text, start_lt, lineno, jsx_closes)
    if head is None:
        return None
    tag_name, self_closing, i, cur_line, toks_out = head
    if self_closing:
        return toks_out, i, cur_line
    if jsx_closes.get(tag_name, -1) < i:
        return None

    stack = [tag_name]
    n = len(text)
    while stack and i < n:
        ch = text[i]
        if ch in _JS_LINE_BREAKS:
            cur_line += 1
            i += 2 if (ch == "\r" and i + 1 < n and text[i + 1] == "\n") else 1
            continue
        if ch == "{":
            expr_line = cur_line
            inner_toks, next_i, next_line, closed = _tokenize_js_span(
                text, i + 1, cur_line, stop_on_rbrace=True, jsx_closes=jsx_closes,
            )
            if not closed:
                return None
            toks_out.append(("PUNCT", "{", expr_line))
            toks_out.extend(inner_toks)
            toks_out.append(("PUNCT", "}", next_line))
            i, cur_line = next_i, next_line
            continue
        if ch == "<" and i + 1 < n:
            if text.startswith("</", i):
                m = _JSX_CLOSE_TAG_AT_RE.match(text, i)
                if m and (m.group(1) or "") == stack[-1]:
                    cur_line += _count_js_line_breaks(text, i, m.end())
                    i = m.end()
                    stack.pop()
                    continue
            elif text[i + 1] == ">" or text[i + 1].isalpha() or text[i + 1] in ("_", "$"):
                child = _scan_jsx_open_tag(text, i, cur_line, jsx_closes)
                if child is not None:
                    ctag, cself, ci, cline, ctoks = child
                    if cself:
                        toks_out.extend(ctoks)
                        i, cur_line = ci, cline
                        continue
                    if jsx_closes.get(ctag, -1) >= ci:
                        toks_out.extend(ctoks)
                        stack.append(ctag)
                        i, cur_line = ci, cline
                        continue
        i += 1
    if stack:
        return None
    return toks_out, i, cur_line


def _tokenize_js_span(
    text: str,
    start: int = 0,
    lineno: int = 1,
    *,
    stop_on_rbrace: bool = False,
    jsx_closes: dict[str, int] | None = None,
) -> tuple[list[tuple[str, str, int]], int, int, bool]:
    """Tokenize JS/TS/JSX from `start`, optionally stopping on an unmatched `}` (for `{expr}` spans)."""
    tokens: list[tuple[str, str, int]] = []
    tpl_braces: list[int] = []
    paren_ctrl: list[str] = []
    brace_kinds: list[str] = []
    expr_head_depths: list[tuple[int, int]] = []
    ternary_depths: list[tuple[int, int]] = []
    closes = _last_jsx_closes(text) if jsx_closes is None else jsx_closes
    i = start
    n = len(text)
    if start == 0:
        if i < n and text[i] == "\ufeff":
            i += 1
        if text.startswith("#!", i):
            i += 2
            while i < n and text[i] not in _JS_LINE_BREAKS:
                i += 1
    while i < n:
        ch = text[i]
        if ch in _JS_LINE_BREAKS:
            if not tokens or tokens[-1][0] != "NL":
                tokens.append(("NL", "\n", lineno))
            lineno += 1
            i += 2 if (ch == "\r" and i + 1 < n and text[i + 1] == "\n") else 1
            continue
        if _is_js_ws(ch):
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                i += 2
                while i < n and text[i] not in _JS_LINE_BREAKS:
                    i += 1
                continue
            if nxt == "*":
                cmt_line = lineno
                had_nl = False
                i += 2
                while i < n:
                    if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                        i += 2
                        break
                    if text[i] in _JS_LINE_BREAKS:
                        had_nl = True
                        lineno += 1
                        i += 2 if (text[i] == "\r" and i + 1 < n and text[i + 1] == "\n") else 1
                    else:
                        i += 1
                if had_nl and (not tokens or tokens[-1][0] != "NL"):
                    tokens.append(("NL", "\n", cmt_line))
                continue
        in_for = bool(paren_ctrl and paren_ctrl[-1] == "for")
        if ch == "/" and _js_allows_regex(tokens, in_for_paren=in_for):
            end = _try_scan_js_regex(text, i, lineno)
            if end is not None:
                tokens.append(("REGEX", "/re/", lineno))
                i = end
                continue
        if ch == "<" and _js_allows_regex(tokens, in_for_paren=in_for):
            jsx = _try_scan_jsx(text, i, lineno, closes)
            if jsx is not None:
                jtoks, jend, jline = jsx
                tokens.extend(jtoks)
                tokens.append(("JSX_CLOSE", ">", jline))
                i, lineno = jend, jline
                continue
        if ch in ("'", '"'):
            q = ch
            start_line = lineno
            i += 1
            buf: list[str] = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    dec, i, lineno = _decode_js_escape(text, i, lineno)
                    if dec:
                        buf.append(dec)
                    continue
                if c == q:
                    i += 1
                    break
                if c in _JS_LINE_BREAKS:
                    lineno += 1
                    i += 2 if (c == "\r" and i + 1 < n and text[i + 1] == "\n") else 1
                    break
                buf.append(c)
                i += 1
            tokens.append(("STR", "".join(buf), start_line))
            continue
        if ch == "`":
            start_line = lineno
            val, i, lineno, interp = _scan_template_tail(text, i + 1, lineno, first=True)
            if interp:
                tokens.append(("TPL", "", start_line))
                tpl_braces.append(0)
            else:
                tokens.append(("STR", val, start_line))
            continue
        if ch == "{":
            if tpl_braces:
                tpl_braces[-1] += 1
            bkind = _brace_open_kind(tokens, brace_kinds, expr_head_depths, len(paren_ctrl), in_for_paren=in_for)
            brace_kinds.append(bkind)
            tokens.append(("PUNCT", "{", lineno))
            i += 1
            continue
        if ch == "}":
            if tpl_braces:
                if tpl_braces[-1] == 0:
                    tpl_braces.pop()
                    end_line = lineno
                    _, i, lineno, interp = _scan_template_tail(text, i + 1, lineno, first=False)
                    if interp:
                        tpl_braces.append(0)
                        tokens.append(("TPL_MID", "}${", end_line))
                    else:
                        tokens.append(("TPL_END", "`", end_line))
                    continue
                tpl_braces[-1] -= 1
            if stop_on_rbrace and not brace_kinds:
                return tokens, i + 1, lineno, True
            bkind = brace_kinds.pop() if brace_kinds else "block"
            _prune_depths(ternary_depths, expr_head_depths, len(paren_ctrl), len(brace_kinds), strict=True)
            tokens.append(("EXPR_RBRACE" if bkind != "block" else "PUNCT", "}", lineno))
            i += 1
            continue
        if ch == "(":
            paren_ctrl.append(_ctrl_paren_kind(tokens))
            tokens.append(("PUNCT", "(", lineno))
            i += 1
            continue
        if ch == ")":
            is_ctrl = paren_ctrl.pop() if paren_ctrl else ""
            _prune_depths(ternary_depths, expr_head_depths, len(paren_ctrl), len(brace_kinds), strict=True)
            tokens.append(("CTRL_CLOSE" if is_ctrl else "PUNCT", ")", lineno))
            i += 1
            continue
        if ch == "?":
            if i + 1 < n and text[i + 1] == ".":
                tokens.append(("PUNCT", "?", lineno))
            else:
                ternary_depths.append((len(paren_ctrl), len(brace_kinds)))
                tokens.append(("PUNCT", "?", lineno))
            i += 1
            continue
        if ch == ":":
            depth = (len(paren_ctrl), len(brace_kinds))
            if ternary_depths and ternary_depths[-1] == depth:
                ternary_depths.pop()
                tokens.append(("PUNCT", ":", lineno))
            elif brace_kinds and brace_kinds[-1] == "obj":
                tokens.append(("PUNCT", ":", lineno))
            else:
                tokens.append(("LABEL_COLON", ":", lineno))
            i += 1
            continue
        if ch == ";":
            _prune_depths(ternary_depths, expr_head_depths, len(paren_ctrl), len(brace_kinds), strict=False)
            tokens.append(("PUNCT", ";", lineno))
            i += 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            start_num = i
            start_line = lineno
            is_dec = True
            if ch == ".":
                i += 1
                while i < n and (text[i].isdigit() or text[i] == "_"):
                    i += 1
            elif ch == "0" and i + 1 < n and text[i + 1] in ("x", "X", "b", "B", "o", "O"):
                is_dec = False
                i += 2
                while i < n and text[i] in "0123456789abcdefABCDEF_":
                    i += 1
            else:
                i += 1
                while i < n and (text[i].isdigit() or text[i] == "_"):
                    i += 1
                if i < n and text[i] == ".":
                    i += 1
                    while i < n and (text[i].isdigit() or text[i] == "_"):
                        i += 1
            if is_dec and i < n and text[i] in ("e", "E"):
                exp = i + 1
                if exp < n and text[exp] in ("+", "-"):
                    exp += 1
                if exp < n and text[exp].isdigit():
                    i = exp
                    while i < n and (text[i].isdigit() or text[i] == "_"):
                        i += 1
            if i < n and text[i] == "n":
                i += 1
            tokens.append(("NUM", text[start_num:i], start_line))
            continue
        if ch.isalpha() or ch in ("_", "$"):
            start_id = i
            start_line = lineno
            i += 1
            while i < n and (text[i].isalnum() or text[i] in ("_", "$")):
                i += 1
            ident = text[start_id:i]
            if ident in ("function", "class") and _is_fn_class_expr(tokens, ident, brace_kinds, in_for_paren=in_for):
                expr_head_depths.append((len(paren_ctrl), len(brace_kinds)))
            tokens.append(("IDENT", ident, start_line))
            continue
        matched_op = ""
        for mop in _MULTI_OPS:
            if text.startswith(mop, i):
                matched_op = mop
                break
        if matched_op:
            tokens.append(("OP", matched_op, lineno))
            i += len(matched_op)
            continue
        if ch == "!":
            # Prefix logical negation allows a following regex literal; TS postfix `!` ends an operand so `/` divides.
            tok_kind = "OP" if _js_allows_regex(tokens, in_for_paren=in_for) else "POSTFIX_BANG"
            tokens.append((tok_kind, "!", lineno))
            i += 1
            continue
        if ch in ("[", "]", ".", "=", ","):
            tokens.append(("PUNCT", ch, lineno))
        else:
            tokens.append(("OP", ch, lineno))
        i += 1
    return tokens, i, lineno, not stop_on_rbrace


def _tokenize_js(text: str) -> list[tuple[str, str, int]]:
    """Lex JS/TS outside comments, strings, regexes, templates, and JSX text so literals never look like imports."""
    return _tokenize_js_span(text)[0]


def _extract_js_imports(text: str) -> list[tuple[int, str]]:
    """Extract static/dynamic JS/TS module specifiers without scanning string or comment contents."""
    toks = _tokenize_js(text)
    out: list[tuple[int, str]] = []
    n = len(toks)
    paren_depth = 0
    brace_depth = 0
    stmt_start = True
    prev_kind, prev_val = "", ""

    def _next_sig(idx: int) -> int:
        while idx < n and toks[idx][0] == "NL":
            idx += 1
        return idx

    i = 0
    while i < n:
        kind, val, line = toks[i]
        if kind == "NL":
            if paren_depth == 0 and brace_depth == 0 and prev_val != ".":
                stmt_start = True
            i += 1
            continue
        if kind == "PUNCT" and val == "(":
            paren_depth += 1
        elif (kind == "PUNCT" and val == ")") or kind == "CTRL_CLOSE":
            paren_depth -= 1
            if paren_depth < 0:
                paren_depth = 0
        elif (kind == "PUNCT" and val in ("{", "}")) or kind == "EXPR_RBRACE":
            brace_depth += 1 if kind == "PUNCT" and val == "{" else -1
            if brace_depth < 0:
                brace_depth = 0

        is_member = prev_kind == "PUNCT" and prev_val == "."
        if kind == "IDENT" and not is_member:
            if val in ("import", "require"):
                j = _next_sig(i + 1)
                if j < n and toks[j][0] == "PUNCT" and toks[j][1] == "(":
                    k = _next_sig(j + 1)
                    if k < n and toks[k][0] == "STR":
                        out.append((line, toks[k][1].strip()))
                elif val == "import" and stmt_start and j < n:
                    if toks[j][0] == "STR":
                        out.append((line, toks[j][1].strip()))
                    elif toks[j][0] != "PUNCT" or toks[j][1] != ".":
                        # Scan `import ... from 'mod'` allowing newlines inside `{ ... }`.
                        depth = 0
                        m = j
                        while m < n:
                            mk, mv, mline = toks[m]
                            if mk == "PUNCT" and mv == "{":
                                depth += 1
                            elif mk == "PUNCT" and mv == "}":
                                depth = max(0, depth - 1)
                            elif depth == 0 and mk == "PUNCT" and mv in (";", "="):
                                break
                            elif depth == 0 and mk == "IDENT" and mv == "from":
                                s_idx = _next_sig(m + 1)
                                if s_idx < n and toks[s_idx][0] == "STR":
                                    out.append((toks[s_idx][2], toks[s_idx][1].strip()))
                                break
                            elif depth == 0 and mk == "NL" and m > j and toks[m - 1][0] in ("STR", "IDENT") and toks[m - 1][1] not in ("import", "type", "from"):
                                nxt = _next_sig(m + 1)
                                if nxt < n and toks[nxt][0] == "IDENT" and toks[nxt][1] == "from":
                                    m = nxt
                                    continue
                                if m > j and toks[m - 1][1] == "}":
                                    m += 1
                                    continue
                                break
                            m += 1
            elif val == "export" and stmt_start:
                j = _next_sig(i + 1)
                if j < n and (
                    (toks[j][0] == "OP" and toks[j][1] == "*")
                    or (toks[j][0] == "PUNCT" and toks[j][1] == "{")
                    or (toks[j][0] == "IDENT" and toks[j][1] == "type")
                ):
                    depth = 0
                    m = j
                    while m < n:
                        mk, mv, _ = toks[m]
                        if mk == "PUNCT" and mv == "{":
                            depth += 1
                        elif mk == "PUNCT" and mv == "}":
                            depth = max(0, depth - 1)
                        elif depth == 0 and mk == "PUNCT" and mv in (";", "="):
                            break
                        elif depth == 0 and mk == "IDENT" and mv == "from":
                            s_idx = _next_sig(m + 1)
                            if s_idx < n and toks[s_idx][0] == "STR":
                                out.append((toks[s_idx][2], toks[s_idx][1].strip()))
                            break
                        m += 1

        if kind == "PUNCT" and val in (";", "{", "}") and paren_depth == 0:
            stmt_start = True
        else:
            stmt_start = False
        prev_kind, prev_val = kind, val
        i += 1
    return out


def _extract_python_imports(rel_path: str, text: str) -> tuple[list[tuple[int, str]], tuple[int, str] | None]:
    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError as err:
        return [], (err.lineno or 1, err.msg or "syntax error")
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                base_mod = _resolve_py_relative_import(rel_path, node.module, node.level)
                if base_mod:
                    out.append((node.lineno, base_mod))
                    for alias in node.names:
                        if alias.name and alias.name != "*":
                            out.append((node.lineno, f"{base_mod}.{alias.name}"))
            else:
                for alias in node.names:
                    resolved = _resolve_py_relative_import(rel_path, alias.name, node.level)
                    if resolved:
                        out.append((node.lineno, resolved))
    return out, None


def _when_satisfied(clause: dict[str, Any], files: dict[str, str]) -> bool:
    wpath = clause["path"]
    if wpath not in files:
        return False
    content = files[wpath]
    if clause["env_key"]:
        env = policy_lib.env_values(content)
        val = (env.get(clause["env_key"]) or "").strip().lower()
        if clause["env_in"] and val not in clause["env_in"]:
            return False
        if clause["env_not_in"] and (not val or val in clause["env_not_in"]):
            return False
    if clause["patterns"]:
        if not any(pat.search(content) for pat in clause["patterns"]):
            return False
    return True


def _finding(rule: LintRule, rel_path: str, match: str, *, line: int = 0) -> dict[str, Any]:
    return {
        "id": rule.id,
        "kind": rule.kind,
        "severity": rule.severity,
        "file": rel_path,
        "line": line,
        "match": (match or "")[:80],
        "detail": rule.message,
    }


def scan_files(rules: list[LintRule], files: dict[str, str]) -> list[dict[str, Any]]:
    """Evaluate all file-based rules against a deterministic `{repo_rel_path: content}` map."""
    findings: list[dict[str, Any]] = []
    norm_files = {_strip_rel_prefix(k): v for k, v in files.items()}
    for rule in rules:
        if rule.kind in DIFF_KINDS:
            continue
        if rule.kind == "cross_file":
            if not all(_when_satisfied(c, norm_files) for c in rule.when):
                continue
            for rel_path, text in sorted(norm_files.items()):
                if not rule.applies(rel_path):
                    continue
                if rule.target_mode == "forbid":
                    lines = text.splitlines()
                    reported = False
                    for pat in rule.target_patterns:
                        for m in pat.finditer(text):
                            lineno = text.count("\n", 0, m.start()) + 1
                            if _is_line_exempt(lines, lineno, rule.id):
                                continue
                            findings.append(_finding(rule, rel_path, m.group(0), line=lineno))
                            reported = True
                            break
                        if reported:
                            break
                elif rule.target_mode == "require":
                    if not all(pat.search(text) for pat in rule.target_patterns):
                        findings.append(_finding(rule, rel_path, f"missing required pattern in {rel_path}", line=1))
            continue

        for rel_path, text in sorted(norm_files.items()):
            if not rule.applies(rel_path):
                continue
            lines = text.splitlines()
            if rule.kind == "regex":
                for pat in rule.patterns:
                    for m in pat.finditer(text):
                        lineno = text.count("\n", 0, m.start()) + 1
                        if _is_line_exempt(lines, lineno, rule.id):
                            continue
                        findings.append(_finding(rule, rel_path, m.group(0), line=lineno))
                        break
            elif rule.kind == "import_boundary":
                ext = PurePosixPath(rel_path).suffix.lower()
                if ext == ".py":
                    imports, syn_err = _extract_python_imports(rel_path, text)
                    if syn_err is not None:
                        lineno, msg = syn_err
                        if not _is_line_exempt(lines, lineno, rule.id):
                            findings.append({
                                "id": rule.id,
                                "kind": rule.kind,
                                "severity": rule.severity,
                                "file": rel_path,
                                "line": lineno,
                                "match": "SyntaxError",
                                "detail": f"{rule.message} (Python SyntaxError: {msg})",
                            })
                        continue
                elif ext in JS_EXTS:
                    imports = _extract_js_imports(text)
                else:
                    imports = []
                reported_lines: set[int] = set()
                for lineno, imp in imports:
                    if lineno in reported_lines:
                        continue
                    if _is_line_exempt(lines, lineno, rule.id):
                        continue
                    if any(_import_matches_forbidden(imp, forb) for forb in rule.forbidden_modules):
                        findings.append(_finding(rule, rel_path, imp, line=lineno))
                        reported_lines.add(lineno)
    return findings


def scan_repo(rules: list[LintRule], repo_dir: Path | str | None) -> list[dict[str, Any]]:
    """Read-only scan of all relevant files inside `repo_dir` for `rules` (pruning worktrees/deps)."""
    if not repo_dir:
        return []
    root = Path(repo_dir)
    if not root.is_dir():
        return []

    wanted: dict[str, str] = {}
    need_paths: set[str] = set()
    for rule in rules:
        if rule.kind == "cross_file":
            for clause in rule.when:
                need_paths.add(_strip_rel_prefix(clause["path"]))

    for rel in sorted(need_paths):
        p = root / rel
        if p.is_file() and p.stat().st_size <= MAX_FILE_BYTES:
            wanted[rel] = p.read_text(encoding="utf-8", errors="replace")

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in DEFAULT_SKIP_DIRS]
        cur_dir = Path(dirpath)
        for fname in sorted(filenames):
            p = cur_dir / fname
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel not in wanted and any(r.applies(rel) for r in rules) and p.stat().st_size <= MAX_FILE_BYTES:
                try:
                    wanted[rel] = p.read_text(encoding="utf-8", errors="replace")
                except UnicodeDecodeError:
                    continue
    return scan_files(rules, wanted)


def evaluate_diff_policy(
    rules: list[LintRule],
    *,
    changed_files: list[str],
    added_lines: int | None = None,
    numstat: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate `diff_policy` / `diff_budget` rules against changed files and added-line counts."""
    findings: list[dict[str, Any]] = []
    norm_changed = [_strip_rel_prefix(p) for p in changed_files if p and p.strip()]
    for rule in rules:
        if rule.kind not in DIFF_KINDS:
            continue
        kept = [p for p in norm_changed if not any(glob_matches(p, ex) for ex in rule.exclude)]
        if rule.max_files is not None and len(kept) > rule.max_files:
            findings.append({
                "id": rule.id,
                "kind": rule.kind,
                "severity": rule.severity,
                "file": "",
                "line": 0,
                "match": f"files={len(kept)}>{rule.max_files}",
                "detail": f"{rule.message} (changed_files={len(kept)}, max_files={rule.max_files})",
            })
        if rule.max_added_lines is not None:
            total_added = sum(numstat.get(p, 0) for p in kept) if numstat is not None else added_lines
            if total_added is not None and total_added > rule.max_added_lines:
                findings.append({
                    "id": rule.id,
                    "kind": rule.kind,
                    "severity": rule.severity,
                    "file": "",
                    "line": 0,
                    "match": f"added_lines={total_added}>{rule.max_added_lines}",
                    "detail": f"{rule.message} (added_lines={total_added}, max_added_lines={rule.max_added_lines})",
                })
    return findings


def _default_fixture_path(rule: LintRule) -> str:
    if rule.include:
        sample = rule.include[0].replace("**/", "").replace("**", "x.py").replace("*", "x")
        return sample or "x.py"
    return "x.py"


def self_test_rules(rules: list[LintRule]) -> list[str]:
    """Verify every rule has both `pass` and `fail` fixtures and each behaves as declared."""
    errs: list[str] = []
    for rule in rules:
        expects = [str(f.get("expect") or "").strip().lower() for f in rule.fixtures]
        if "fail" not in expects or "pass" not in expects:
            errs.append(f"{rule.id}: 須至少各有一組 expect='fail' 與 expect='pass' 的 [[fixture]]")
            continue
        for idx, fix in enumerate(rule.fixtures, 1):
            exp = str(fix.get("expect") or "").strip().lower()
            if exp not in ("pass", "fail"):
                errs.append(f"{rule.id}: fixture[{idx}] invalid expect={exp!r}")
                continue
            if rule.kind in DIFF_KINDS:
                hits = evaluate_diff_policy(
                    [rule],
                    changed_files=[str(x) for x in (fix.get("changed_files") or [])],
                    added_lines=fix.get("added_lines"),
                )
            elif isinstance(fix.get("files"), dict):
                hits = scan_files([rule], {str(k): str(v) for k, v in fix["files"].items()})
            else:
                fpath = str(fix.get("path") or _default_fixture_path(rule))
                hits = scan_files([rule], {fpath: str(fix.get("text") or "")})
            if exp == "fail" and not hits:
                errs.append(f"{rule.id}: fail fixture[{idx}] did not trigger")
            elif exp == "pass" and hits:
                errs.append(f"{rule.id}: pass fixture[{idx}] unexpectedly triggered ({hits[0]['match']!r})")
    return errs


def self_test_dir(rules_dir: Path | str) -> list[str]:
    try:
        rules = load_rules(rules_dir)
    except LintConfigError as err:
        return [str(err)]
    return self_test_rules(rules)
