#!/usr/bin/env python3
"""devflow repo checks: rule self-test, lesson ledger, rule scan. Stdlib only (Python >= 3.11).

Run from the repo root (`.devflow/check.sh`). Exit 0 = pass, 1 = findings, 2 = broken config.
  check.py                   self-test every rule, validate lessons, scan tracked files
  check.py --write-baseline  record current violations as accepted legacy (adoption only)
  check.py --pr BASE_SHA     PR gate (CI, env PR_TITLE / PR_BODY): a fix/revert PR names a lesson
                             (`Lesson: L-NNNN`) or says why not (`No-lesson: <reason>`); a cited lesson
                             that already existed must be updated (recurrence); a lesson added here must
                             prove itself on the pre-fix code: its rule fires on the base version of a
                             changed file, or its test (overlaid on BASE) makes .devflow/test.sh fail.
                             `No-proof: <reason>` in the PR body skips the proof (reviewer judges it).

Rules: .devflow/rules/<id>.toml
  kind = "regex" (default) | "import_boundary" | "diff_budget"
  id, message, include = [globs], exclude = [globs], patterns = [regex, ...]
  import_boundary: forbidden_modules = [prefix, ...]
  diff_budget: max_files = N, max_added_lines = N (checked during `--pr`)
  [[fixture]] path, text, expect = "fail" | "pass"   (diff_budget: changed_files, added_lines)
Exemption: `devflow: allow <rule-id> L-NNNN` on the offending line or the line above;
  L-NNNN must be an existing lesson. Baseline (.devflow/rules/baseline.json) hides legacy
  violations by (rule, path, line text) so only new ones fail.
Lessons: .devflow/lessons/L-NNNN.toml (see validate_lesson).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

RULES_DIR = Path(".devflow/rules")
LESSONS_DIR = Path(".devflow/lessons")
BASELINE = RULES_DIR / "baseline.json"
ALWAYS_EXCLUDE = (".devflow/rules/**", ".devflow/lessons/**", ".devflow/check.py",
                  "**/node_modules/**", "**/vendor/**", "**/.venv/**", "**/dist/**", "**/build/**")
ALLOW = re.compile(r"devflow:\s*allow\s+([a-z0-9-]+)\s+(L-\d{4})\b")
LESSON_ID = re.compile(r"^L-\d{4}$")
CATEGORIES = {"code-pattern", "architecture", "verification", "process", "kit"}
KINDS = {"rule", "test", "journey", "template", "cli", "skill-only"}
MAX_BYTES = 1_000_000
FIX_TITLE = re.compile(r"^\s*(fix|revert)(\([^)]*\))?!?:", re.I)
LESSON_LINE = re.compile(r"^\s*Lesson:\s*(L-\d{4})\s*$", re.M)
NO_LESSON = re.compile(r"^\s*No-lesson:\s*\S.{9,}$", re.M)
NO_PROOF = re.compile(r"^\s*No-proof:\s*\S.{9,}$", re.M)
PROVE_TIMEOUT = int(os.environ.get("DEVFLOW_PROVE_TIMEOUT", "1800"))
JS_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})


class ConfigError(Exception):
    pass


def glob_regex(glob: str) -> re.Pattern:
    out, i, n = [], 0, len(glob)
    while i < n:
        if glob.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif glob.startswith("**", i):
            out.append(".*"); i += 2
        elif glob[i] == "*":
            out.append("[^/]*"); i += 1
        elif glob[i] == "?":
            out.append("[^/]"); i += 1
        elif glob[i] == "[":
            j = i + 1
            if j < n and glob[j] in ("!", "^"):
                j += 1
            if j < n and glob[j] == "]":
                j += 1
            while j < n and glob[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\["); i += 1
            else:
                stuff = glob[i + 1 : j]
                neg = stuff.startswith(("!", "^"))
                body = stuff[1:] if neg else stuff
                if not body:
                    raise ConfigError(f"invalid glob pattern {glob!r}: empty character class")
                body = body.replace("\\", r"\\").replace("[", r"\[").replace("--", r"\-\-")
                body = re.sub(r"([&~|])", r"\\\1", body)
                if body.startswith("]"):
                    body = r"\]" + body[1:]
                if not neg and body.startswith("^"):
                    body = r"\^" + body[1:]
                out.append(f"(?!/)[{'^' if neg else ''}{body}]")
                i = j + 1
        else:
            out.append(re.escape(glob[i])); i += 1
    try:
        return re.compile("".join(out) + r"\Z")
    except re.error as err:
        raise ConfigError(f"invalid glob pattern {glob!r}: {err}") from err


@dataclass(frozen=True)
class Rule:
    id: str
    message: str
    kind: str = "regex"
    include: tuple[re.Pattern, ...] = ()
    exclude: tuple[re.Pattern, ...] = ()
    patterns: tuple[re.Pattern, ...] = ()
    forbidden_modules: tuple[str, ...] = ()
    max_files: int | None = None
    max_added_lines: int | None = None
    fixtures: tuple[dict, ...] = ()

    def applies(self, path: str) -> bool:
        if self.kind in ("diff_budget", "diff_policy"):
            return False
        return (any(g.match(path) for g in self.include)
                and not any(g.match(path) for g in self.exclude))

    def hits(self, line: str) -> bool:
        return any(p.search(line) for p in self.patterns)


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    text: str
    message: str

    def key(self) -> tuple[str, str, str]:
        return self.rule, self.path, self.text.strip()


def load_rules() -> list[Rule]:
    rules, seen = [], set()
    for f in sorted(RULES_DIR.glob("*.toml")):
        try:
            d = tomllib.loads(f.read_text())
            rid = d["id"]
            if rid != f.stem:
                raise ConfigError(f"{f}: id {rid!r} must equal file name")
            if rid in seen:
                raise ConfigError(f"{f}: duplicate rule id {rid}")
            kind = str(d.get("kind") or "regex").strip().lower()
            if kind not in ("regex", "import_boundary", "diff_budget", "diff_policy"):
                raise ConfigError(f"{f}: invalid kind {kind!r}")
            exclude = tuple(glob_regex(g) for g in [*d.get("exclude", []), *ALWAYS_EXCLUDE])
            if kind == "regex":
                if not d.get("patterns") or not d.get("include"):
                    raise ConfigError(f"{f}: needs include and patterns")
                rules.append(Rule(
                    id=rid, message=d["message"], kind=kind,
                    include=tuple(glob_regex(g) for g in d["include"]),
                    exclude=exclude,
                    patterns=tuple(re.compile(p) for p in d["patterns"]),
                    fixtures=tuple(d.get("fixture", []))))
            elif kind == "import_boundary":
                mods = d.get("forbidden_modules")
                if not mods or not d.get("include") or not isinstance(mods, list):
                    raise ConfigError(f"{f}: needs include and forbidden_modules")
                rules.append(Rule(
                    id=rid, message=d["message"], kind=kind,
                    include=tuple(glob_regex(g) for g in d["include"]),
                    exclude=exclude,
                    forbidden_modules=tuple(str(m).strip() for m in mods if str(m).strip()),
                    fixtures=tuple(d.get("fixture", []))))
            else:
                mf, ml = d.get("max_files"), d.get("max_added_lines")
                if mf is None and ml is None:
                    raise ConfigError(f"{f}: diff_budget needs max_files or max_added_lines")
                if mf is not None and (not isinstance(mf, int) or mf <= 0):
                    raise ConfigError(f"{f}: max_files must be positive integer")
                if ml is not None and (not isinstance(ml, int) or ml <= 0):
                    raise ConfigError(f"{f}: max_added_lines must be positive integer")
                rules.append(Rule(
                    id=rid, message=d["message"], kind=kind,
                    exclude=exclude, max_files=mf, max_added_lines=ml,
                    fixtures=tuple(d.get("fixture", []))))
            seen.add(rid)
        except ConfigError as e:
            raise ConfigError(f"{f}: {e}") from e
        except (KeyError, tomllib.TOMLDecodeError, re.error) as e:
            raise ConfigError(f"{f}: {e}") from e
    return rules


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
    clean_imp, forb = (imp or "").strip(), (forbidden or "").strip()
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
    buf: list[str] = []
    i, n = start, len(text)
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
            return idx + 1 < len(sig) and sig[idx + 1][0] == "IDENT" and sig[idx + 1][1] == "export"
        return pv in _BRACE_EXPR_KW
    return False


def _is_fn_class_expr(
    tokens: list[tuple[str, str, int]],
    kw: str,
    brace_kinds: list[str],
    *,
    in_for_paren: bool = False,
) -> bool:
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
            drop = (dp > paren_len or db > brace_len) if strict else (dp >= paren_len and db >= brace_len)
            if not drop:
                break
            stack.pop()


def _try_scan_js_regex(text: str, start: int, lineno: int) -> int | None:
    i, n, in_cls = start + 1, len(text), False
    while i < n:
        rc = text[i]
        if rc == "\\" and i + 1 < n:
            if text[i + 1] in ("\n", "\r"):
                return None
            i += 2
            continue
        if rc in ("\n", "\r"):
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


_JSX_CLOSE_TAG_RE = re.compile(r"</\s*([A-Za-z_$][\w$.-]*)?\s*>")
_JSX_CLOSE_TAG_AT_RE = re.compile(r"</([ \t\r\n]*)([A-Za-z_$][\w$.-]*)?([ \t\r\n]*)>")


def _last_jsx_closes(text: str) -> dict[str, int]:
    if "</" not in text:
        return {}
    return {m.group(1) or "": m.end() for m in _JSX_CLOSE_TAG_RE.finditer(text)}


def _scan_jsx_attr_string(text: str, start: int, lineno: int) -> tuple[int, int] | None:
    q, i, n = text[start], start + 1, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                lineno += 1
            i += 2
            continue
        if c == q:
            return i + 1, lineno
        if c == "\n":
            lineno += 1
        i += 1
    return None


def _scan_jsx_open_tag(
    text: str,
    lt_idx: int,
    lineno: int,
    jsx_closes: dict[str, int],
) -> tuple[str, bool, int, int, list[tuple[str, str, int]]] | None:
    n, p = len(text), lt_idx + 1
    if p >= n:
        return None
    if text[p] == ">":
        return "", False, p + 1, lineno, []
    if not (text[p].isalpha() or text[p] in ("_", "$")):
        return None
    q = p + 1
    while q < n and (text[q].isalnum() or text[q] in ("_", "$", ".", "-")):
        q += 1
    tag_name, p = text[p:q], q
    if p < n and text[p] not in (" ", "\t", "\r", "\n", ">", "/", "<"):
        return None
    prop_tokens: list[tuple[str, str, int]] = []
    angle = 0
    while p < n:
        ch = text[p]
        if ch in (" ", "\t", "\r"):
            p += 1
            continue
        if ch == "\n":
            lineno += 1; p += 1; continue
        if ch == "<":
            angle += 1; p += 1; continue
        if ch == ">":
            if angle > 0:
                angle -= 1; p += 1; continue
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
    head = _scan_jsx_open_tag(text, start_lt, lineno, jsx_closes)
    if head is None:
        return None
    tag_name, self_closing, i, cur_line, toks_out = head
    if self_closing:
        return toks_out, i, cur_line
    if jsx_closes.get(tag_name, -1) < i:
        return None
    stack, n = [tag_name], len(text)
    while stack and i < n:
        ch = text[i]
        if ch == "\n":
            cur_line += 1; i += 1; continue
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
                if m and (m.group(2) or "") == stack[-1]:
                    cur_line += text.count("\n", i, m.end())
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
    tokens: list[tuple[str, str, int]] = []
    tpl_braces: list[int] = []
    paren_ctrl: list[str] = []
    brace_kinds: list[str] = []
    expr_head_depths: list[tuple[int, int]] = []
    ternary_depths: list[tuple[int, int]] = []
    closes = _last_jsx_closes(text) if jsx_closes is None else jsx_closes
    i, n = start, len(text)
    while i < n:
        ch = text[i]
        if ch in (" ", "\t", "\r"):
            i += 1
            continue
        if ch in _JS_LINE_BREAKS:
            if not tokens or tokens[-1][0] != "NL":
                tokens.append(("NL", "\n", lineno))
            lineno += 1
            i += 2 if (ch == "\r" and i + 1 < n and text[i + 1] == "\n") else 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                i += 2
                while i < n and text[i] not in _JS_LINE_BREAKS:
                    i += 1
                continue
            if text[i + 1] == "*":
                cmt_line, had_nl = lineno, False
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
            q, start_line, buf = ch, lineno, []
            i += 1
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
            if not (i + 1 < n and text[i + 1] == "."):
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
            start_num, start_line, is_dec = i, lineno, True
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
            start_id, start_line = i, lineno
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
            tokens.append(("OP" if _js_allows_regex(tokens, in_for_paren=in_for) else "POSTFIX_BANG", "!", lineno))
            i += 1
            continue
        tokens.append(("PUNCT" if ch in ("[", "]", ".", "=", ",") else "OP", ch, lineno))
        i += 1
    return tokens, i, lineno, not stop_on_rbrace


def _tokenize_js(text: str) -> list[tuple[str, str, int]]:
    return _tokenize_js_span(text)[0]


def _extract_js_imports(text: str) -> list[tuple[int, str]]:
    toks = _tokenize_js(text)
    out: list[tuple[int, str]] = []
    n, paren_depth, brace_depth = len(toks), 0, 0
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
            paren_depth = max(0, paren_depth - 1)
        elif (kind == "PUNCT" and val in ("{", "}")) or kind == "EXPR_RBRACE":
            brace_depth = max(0, brace_depth + (1 if kind == "PUNCT" and val == "{" else -1))

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
                        depth, m = 0, j
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
                            elif depth == 0 and mk == "NL" and m > j and toks[m - 1][0] in ("STR", "IDENT") and toks[m - 1][1] not in ("import", "type", "from"):
                                nxt = _next_sig(m + 1)
                                if nxt < n and toks[nxt][0] == "IDENT" and toks[nxt][1] == "from":
                                    m = nxt
                                    continue
                                if toks[m - 1][1] == "}":
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
                    depth, m = 0, j
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

        stmt_start = bool(kind == "PUNCT" and val in (";", "{", "}") and paren_depth == 0)
        prev_kind, prev_val = kind, val
        i += 1
    return out


def _extract_python_imports(path: str, text: str) -> tuple[list[tuple[int, str]], tuple[int, str] | None]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as err:
        return [], (err.lineno or 1, err.msg or "syntax error")
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                if base_mod := _resolve_py_relative_import(path, node.module, node.level):
                    out.append((node.lineno, base_mod))
                    for alias in node.names:
                        if alias.name and alias.name != "*":
                            out.append((node.lineno, f"{base_mod}.{alias.name}"))
            else:
                for alias in node.names:
                    if mod := _resolve_py_relative_import(path, alias.name, node.level):
                        out.append((node.lineno, mod))
    return out, None


def _extract_imports(path: str, text: str) -> list[tuple[int, str]]:
    ext = PurePosixPath(path).suffix.lower()
    if ext == ".py":
        imports, _ = _extract_python_imports(path, text)
        return imports
    if ext in JS_EXTS:
        return _extract_js_imports(text)
    return []


def _record_line_violation(r: Rule, path: str, n: int, lines: list[str], lessons: set[str], out: list[Violation]) -> None:
    line = lines[n - 1] if 1 <= n <= len(lines) else ""
    allowed = [m for src in (line, lines[n - 2] if n > 1 else "")
               for m in ALLOW.finditer(src) if m.group(1) == r.id]
    if not allowed:
        out.append(Violation(r.id, path, n, line, r.message))
    elif not any(m.group(2) in lessons for m in allowed):
        out.append(Violation(r.id, path, n, line,
                             f"exemption cites missing lesson {allowed[0].group(2)}"))


def scan_text(rules: list[Rule], path: str, text: str, lessons: set[str]) -> list[Violation]:
    out: list[Violation] = []
    applicable = [r for r in rules if r.applies(path)]
    if not applicable:
        return out
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        for r in applicable:
            if r.kind == "regex" and r.hits(line):
                _record_line_violation(r, path, n, lines, lessons, out)
    ext = PurePosixPath(path).suffix.lower()
    for r in applicable:
        if r.kind != "import_boundary":
            continue
        if ext == ".py":
            imports, syn_err = _extract_python_imports(path, text)
            if syn_err is not None:
                lineno, msg = syn_err
                line = lines[lineno - 1] if 1 <= lineno <= len(lines) else ""
                out.append(Violation(r.id, path, lineno, line,
                                     f"Python SyntaxError prevents import_boundary check: {msg}"))
                continue
        else:
            imports = _extract_imports(path, text)
        reported_lines: set[int] = set()
        for lineno, imp in imports:
            if lineno in reported_lines:
                continue
            if any(_import_matches_forbidden(imp, forb) for forb in r.forbidden_modules):
                _record_line_violation(r, path, lineno, lines, lessons, out)
                reported_lines.add(lineno)
    return out


def evaluate_diff_budget(rule: Rule, changed_files: list[str], *, added_lines: int | None = None,
                         numstat: dict[str, int] | None = None) -> list[str]:
    if rule.kind not in ("diff_budget", "diff_policy"):
        return []
    kept = [p for p in changed_files if not any(ex.match(p) for ex in rule.exclude)]
    errs: list[str] = []
    if rule.max_files is not None and len(kept) > rule.max_files:
        errs.append(f"{rule.id}: {rule.message} (changed_files={len(kept)} > max_files={rule.max_files})")
    if rule.max_added_lines is not None:
        total_added = sum(numstat.get(p, 0) for p in kept) if numstat is not None else added_lines
        if total_added is not None and total_added > rule.max_added_lines:
            errs.append(f"{rule.id}: {rule.message} (added_lines={total_added} > max_added_lines={rule.max_added_lines})")
    return errs


def self_test(rules: list[Rule]) -> list[str]:
    errors = []
    for r in rules:
        expects = Counter(fx.get("expect") for fx in r.fixtures)
        if not expects["fail"] or not expects["pass"]:
            errors.append(f"rule {r.id}: needs at least one fixture with expect=fail and one with expect=pass")
        for i, fx in enumerate(r.fixtures, 1):
            exp = fx.get("expect")
            if r.kind in ("diff_budget", "diff_policy"):
                if exp not in ("fail", "pass") or ("changed_files" not in fx and "added_lines" not in fx):
                    errors.append(f"rule {r.id} fixture {i}: needs changed_files/added_lines, expect=fail|pass")
                    continue
                fired = bool(evaluate_diff_budget(
                    r, [str(x) for x in fx.get("changed_files", [])], added_lines=fx.get("added_lines")
                ))
                if fired != (exp == "fail"):
                    verb = "did not trigger on" if exp == "fail" else "triggered on"
                    errors.append(f"rule {r.id} fixture {i}: {verb} diff_budget fixture")
                continue
            if exp not in ("fail", "pass") or "path" not in fx or "text" not in fx:
                errors.append(f"rule {r.id} fixture {i}: needs path, text, expect=fail|pass")
                continue
            cited = {m.group(2) for m in ALLOW.finditer(fx["text"])}
            fired = bool(scan_text([r], fx["path"], fx["text"], cited))
            if fired != (exp == "fail"):
                verb = "did not fire on" if exp == "fail" else "fired on"
                errors.append(f"rule {r.id} fixture {i}: {verb} {fx['path']}: {fx['text']!r}")
    return errors


def validate_lesson(f: Path, rule_ids: set[str]) -> list[str]:
    try:
        d = tomllib.loads(f.read_text())
    except tomllib.TOMLDecodeError as e:
        return [f"{f}: {e}"]
    errs = []
    lid = d.get("id", "")
    if not LESSON_ID.match(lid) or lid != f.stem:
        errs.append(f"{f}: id must be L-NNNN and equal the file name")
    for key in ("title", "event", "root_cause"):
        if not str(d.get(key, "")).strip():
            errs.append(f"{f}: {key} is required")
    if d.get("category") not in CATEGORIES:
        errs.append(f"{f}: category must be one of {sorted(CATEGORIES)}")
    enf = d.get("enforcement") or {}
    kind, ref = enf.get("kind"), str(enf.get("ref", "")).strip()
    if kind not in KINDS:
        return [*errs, f"{f}: enforcement.kind must be one of {sorted(KINDS)}"]
    if not ref:
        errs.append(f"{f}: enforcement.ref is required")
    elif kind == "rule":
        if ref not in rule_ids:
            errs.append(f"{f}: enforcement.ref names no rule in {RULES_DIR}: {ref}")
    elif kind == "skill-only":
        if not str(enf.get("reason", "")).strip():
            errs.append(f"{f}: skill-only needs enforcement.reason (why no automated check)")
    else:
        path, _, needle = ref.partition("::")
        target = Path(path)
        if not target.is_file():
            errs.append(f"{f}: enforcement.ref file not found: {path}")
        elif needle and needle not in target.read_text(errors="replace"):
            errs.append(f"{f}: {needle!r} not found in {path}")
    return errs


def tracked_files() -> list[str]:
    p = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True)
    if p.returncode != 0:
        raise ConfigError("not a git checkout: " + p.stderr.strip())
    return [x for x in p.stdout.split("\0") if x]


def scan_repo(rules: list[Rule], lessons: set[str]) -> list[Violation]:
    out = []
    for path in tracked_files():
        if not any(r.applies(path) for r in rules):
            continue
        f = Path(path)
        if not f.is_file() or f.stat().st_size > MAX_BYTES:
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        out.extend(scan_text(rules, path, text, lessons))
    return out


def load_baseline() -> Counter:
    if not BASELINE.exists():
        return Counter()
    d = json.loads(BASELINE.read_text())
    return Counter((e["rule"], e["path"], e["text"]) for e in d["entries"])


def write_baseline(found: list[Violation]) -> None:
    entries = [{"rule": r, "path": p, "text": t} for r, p, t in sorted(v.key() for v in found)]
    BASELINE.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=1) + "\n")
    print(f"baseline: {len(entries)} legacy violation(s) recorded in {BASELINE}")


def git(*args: str) -> str:
    p = subprocess.run(["git", *args], capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise ConfigError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def pr_changes(base: str) -> dict[str, str]:
    """{path: A|M|D} between BASE and the checked-out tree (renames split into D + A)."""
    out = git("diff", "--no-renames", "--name-status", "-z", base, "HEAD").split("\0")
    return {out[i + 1]: out[i][0] for i in range(0, len(out) - 1, 2)}


def pr_numstat(base: str) -> dict[str, int]:
    """{path: added_lines} between BASE and HEAD (`-` binary files omitted)."""
    out = git("diff", "--no-renames", "--numstat", "-z", base, "HEAD").split("\0")
    stats: dict[str, int] = {}
    for entry in out:
        if not entry or "\t" not in entry:
            continue
        parts = entry.split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit():
            stats[parts[2]] = int(parts[0])
    return stats


def prove_rule(rule: Rule, base: str, changes: dict[str, str]) -> tuple[str, str]:
    """The rule must fire on the pre-fix (BASE) version of a file this PR changes. Returns (error, proof)."""
    for path, status in changes.items():
        if status == "A" or not rule.applies(path):
            continue
        if scan_text([rule], path, git("show", f"{base}:{path}"), set()):
            return "", f"rule {rule.id} fires on the pre-fix (BASE) version of {path}"
    return (f"rule {rule.id} does not fire on the pre-fix version of any file this PR changes; "
            f"make its patterns match the error this PR fixes"), ""


def run_test_file(cwd: Path, path: str, name: str) -> tuple[list[str], str]:
    """Run .devflow/test.sh limited to one test file; return the outcomes (failure / error / skipped /
    passed) of the JUnit test cases named NAME (parametrized `NAME[...]` included), or an error."""
    junit = cwd / ".devflow-prove-junit.xml"
    junit.unlink(missing_ok=True)
    env = {**os.environ, "DEVFLOW_TEST_ONLY": path, "DEVFLOW_JUNIT": str(junit)}
    try:
        subprocess.run(["bash", ".devflow/test.sh"], cwd=cwd, env=env, capture_output=True, timeout=PROVE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], f".devflow/test.sh exceeded {PROVE_TIMEOUT}s"
    if not junit.is_file():
        return [], (".devflow/test.sh wrote no JUnit results to $DEVFLOW_JUNIT (the template does this for "
                    "pytest; add it for this stack, or use `No-proof:`)")
    try:
        cases = list(ElementTree.parse(junit).getroot().iter("testcase"))
    except ElementTree.ParseError as e:
        return [], f"unreadable JUnit results: {e}"
    finally:
        junit.unlink(missing_ok=True)
    outcomes = []
    for c in cases:
        n = c.get("name", "")
        if n == name or n.startswith(name + "["):
            kinds = {child.tag for child in c} & {"failure", "error", "skipped"}
            outcomes.append("error" if "error" in kinds else next(iter(kinds), "passed"))
    return outcomes, ""


def tally(outcomes: list[str]) -> str:
    return ", ".join(f"{k} x{n}" for k, n in sorted(Counter(outcomes).items())) or "not run"


def prove_test(ref: str, base: str) -> tuple[str, str]:
    """Overlay the PR's test file and test.sh on BASE, run only that file: the named test must fail
    by assertion there (not error, skip or vanish through an import/collection error) and pass here.
    Returns (error, proof)."""
    path, _, name = ref.partition("::")
    if not name:
        return f"cannot prove {ref}: name the test (`path::test_name`)", ""
    tmp = Path(tempfile.mkdtemp(prefix="devflow-prove-"))
    tree = tmp / "base"
    try:
        git("worktree", "add", "--detach", "--quiet", str(tree), base)
        for f in (path, ".devflow/test.sh"):
            (tree / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, tree / f)
        before, err = run_test_file(tree, path, name)
        if err:
            return f"cannot prove {ref} on BASE: {err}", ""
        if "failure" not in before:
            seen = ", ".join(before) or "not run (import or collection error?)"
            return (f"test {ref} on the pre-fix code (BASE): {seen}; proof needs an assertion failure, "
                    f"so it does not show this test catches the error this PR fixes"), ""
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tree)], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    after, err = run_test_file(Path.cwd(), path, name)
    if err:
        return f"cannot prove {ref} on this PR: {err}", ""
    if not after or set(after) != {"passed"}:
        return f"test {ref} on this PR: {', '.join(after) or 'not run'}; it must pass after the fix", ""
    return "", f"test {ref}: BASE {tally(before)}; this PR {tally(after)}"


def pr_gate(base: str, title: str, body: str, rules: list[Rule]) -> tuple[list[str], list[str]]:
    """Returns (problems, proofs): proofs say what each new lesson was checked against, so a
    passing gate log shows the evidence instead of only a count."""
    changes = pr_changes(base)
    errs, proofs = [], []
    budget_rules = [r for r in rules if r.kind in ("diff_budget", "diff_policy")]
    if budget_rules:
        stats = pr_numstat(base)
        for br in budget_rules:
            errs.extend(evaluate_diff_budget(br, list(changes), numstat=stats))
    cited = LESSON_LINE.findall(body)
    if FIX_TITLE.match(title) and not cited and not NO_LESSON.search(body):
        errs.append("fix/revert PR: add `Lesson: L-NNNN` (a lesson added or updated in this PR) or "
                    "`No-lesson: <why this error cannot recur>` to the PR body")
    for lid in cited:
        path = f"{LESSONS_DIR}/{lid}.toml"
        if not Path(path).is_file():
            errs.append(f"PR body cites {lid} but {path} does not exist")
        elif path not in changes:
            errs.append(f"{lid} already existed and this PR leaves it unchanged: the same root cause came back, "
                        f"so its enforcement did not stop it; strengthen it (e.g. skill-only -> rule/test) and "
                        f"record this event in {path}")
    if NO_PROOF.search(body):
        return errs, ["No-proof in the PR body: new lessons were not proved; the reviewer judges the reason"]
    by_id = {r.id: r for r in rules}
    for path, status in sorted(changes.items()):
        if status != "A" or not re.fullmatch(rf"{LESSONS_DIR}/L-\d{{4}}\.toml", path):
            continue
        try:
            enf = tomllib.loads(Path(path).read_text()).get("enforcement") or {}
        except tomllib.TOMLDecodeError:
            continue  # reported by the normal check
        kind, ref = enf.get("kind"), str(enf.get("ref", "")).strip()
        if kind == "rule" and ref in by_id:
            err, proof = prove_rule(by_id[ref], base, changes)
        elif kind == "test" and ref and Path(ref.partition("::")[0]).is_file():
            err, proof = prove_test(ref, base)
        else:
            err, proof = "", f"{kind} lesson: not proved automatically; the reviewer judges it"
        if err:
            errs.append(f"{path}: {err}")
        else:
            proofs.append(f"{path}: {proof}")
    return errs, proofs


def main(argv: list[str]) -> int:
    try:
        rules = load_rules()
        rule_ids = {r.id for r in rules}
        lesson_files = sorted(LESSONS_DIR.glob("*.toml"))
        lessons = {f.stem for f in lesson_files}
        found = scan_repo(rules, lessons)
    except ConfigError as e:
        print(f"CONFIG {e}")
        return 2
    if argv == ["--write-baseline"]:
        write_baseline([v for v in found if not v.message.startswith("exemption cites")])
        return 0
    if len(argv) == 2 and argv[0] == "--pr":
        try:
            errs, proofs = pr_gate(argv[1], os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""), rules)
        except ConfigError as e:
            print(f"CONFIG {e}")
            return 2
        print(f"devflow pr gate: {len(errs)} problem(s)")
        for p in proofs:
            print(f"PROOF {p}")
        for e in errs:
            print(f"PR    {e}")
        return 1 if errs else 0
    if argv:
        print(__doc__)
        return 2

    problems = [f"RULE  {e}" for e in self_test(rules)]
    problems += [f"LESSON {e}" for f in lesson_files for e in validate_lesson(f, rule_ids)]
    legacy = load_baseline()
    new = []
    for v in found:
        if legacy[v.key()] > 0:
            legacy[v.key()] -= 1
        else:
            new.append(v)
    problems += [f"FAIL  {v.path}:{v.line} [{v.rule}] {v.message}\n      {v.text.strip()}" for v in new]

    stale = sum(legacy.values())
    print(f"devflow check: {len(rules)} rule(s), {len(lesson_files)} lesson(s), "
          f"{len(found) - len(new)} baselined, {len(new)} new violation(s)")
    if stale:
        print(f"note: {stale} baseline entr(y/ies) no longer occur; rerun --write-baseline to shrink it")
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
