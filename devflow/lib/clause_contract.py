"""Deterministic Then-clause contract validation, Python AST assertion linting, and runner reachability."""
from __future__ import annotations

import ast
import fnmatch
import re
import shlex
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

VALID_CLAUSE_KINDS = frozenset({"changes", "preserves", "rejects", "manual"})
VALID_REJECT_MODES = frozenset({"new", "preserves"})

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CLAIM_SPLIT_RE = re.compile(r"[;；]+")
_EXIT_CODE_NAMES = frozenset({"returncode", "exit_code", "status_code"})

_NEGATIVE_ASSERT_CALLS = frozenset({
    "assertRaises", "assertRaisesRegex", "assertWarns", "assertWarnsRegex",
    "assertNotIn", "assertFalse", "assertIsNone", "assertNotEqual",
    "assertNotRegex", "assert_fails", "raises",
})
_NEGATIVE_SUBSTRINGS = ("not", "raises", "reject", "refus", "deny", "invalid", "fail", "hidden", "disabled")
_TAUTOLOGY_FALSE_CALLS = frozenset({"assertFalse"})
_TAUTOLOGY_TRUE_CALLS = frozenset({"assertTrue"})
_PAIR_COMPARE_CALLS = frozenset({"assertEqual", "assertNotEqual", "assertIs", "assertIsNot", "assert_equal"})
_UNARY_ASSERT_CALLS = frozenset({"assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone"})
_BINARY_ASSERT_CALLS = frozenset({
    "assertEqual", "assertNotEqual", "assertIs", "assertIsNot", "assertIn", "assertNotIn",
    "assertLess", "assertLessEqual", "assertGreater", "assertGreaterEqual",
    "assertRegex", "assertNotRegex", "assertAlmostEqual", "assertNotAlmostEqual",
    "assertCountEqual", "assert_equal",
})
_PYTEST_VALUE_FLAGS = frozenset({
    "-k", "-m", "-c", "-o", "-p", "-n", "-W", "--rootdir", "--confcutdir", "--maxfail",
    "--junitxml", "--junit-xml", "--ignore", "--ignore-glob", "--deselect", "--tb",
    "--log-level", "--basetemp", "--capture",
})


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _is_safe_rel_path(rel_path: str) -> bool:
    if not rel_path or "\x00" in rel_path or rel_path.startswith(("/", "\\")):
        return False
    if "\\" in rel_path:
        return False
    p = PurePosixPath(rel_path)
    return bool(p.parts) and ".." not in p.parts and not p.is_absolute()


def parse_then_clauses(raw_scenario: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Parse scenario `then` / `then_clauses` into `(then_text, valid_clauses, errors)`.

    Supports both legacy string `then` + optional `then_clauses`, and structured list `then`.
    """
    errs: list[str] = []
    raw_then = raw_scenario.get("then")
    raw_clauses = raw_scenario.get("then_clauses")
    clause_input: list[Any] | None = None

    if isinstance(raw_then, list):
        if raw_clauses is not None:
            errs.append("情境同時使用結構化 then 陣列與 then_clauses，請只保留一種宣告格式")
        clause_input = raw_then
        if not raw_then:
            errs.append("結構化 then 陣列不可為空")
    elif raw_then is None or isinstance(raw_then, str):
        if raw_clauses is not None:
            if not isinstance(raw_clauses, list):
                errs.append("then_clauses 必須為物件陣列")
            else:
                clause_input = raw_clauses
                if not raw_clauses:
                    errs.append("then_clauses 不可為空陣列")
    else:
        errs.append("then 格式必須為字串或子句物件陣列")
        return "", [], errs

    valid_clauses: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    derived_texts: list[str] = []

    if clause_input is not None:
        for idx, item in enumerate(clause_input, 1):
            if not isinstance(item, dict):
                errs.append(f"then_clauses[{idx}] 必須為 JSON 物件")
                continue
            cid = str(item.get("id") or "").strip() if isinstance(item.get("id"), str) else ""
            if not cid:
                errs.append(f"then_clauses[{idx}] 缺少有效的字串 id")
            elif cid in seen_ids:
                errs.append(f"then_clauses 子句 id 重複：{cid}")
            else:
                seen_ids.add(cid)

            raw_text = item.get("text")
            ctext = _norm(raw_text) if isinstance(raw_text, str) else ""
            if not ctext:
                errs.append(f"子句 {cid or f'#{idx}'} 缺少有效的 text")
            else:
                derived_texts.append(ctext)

            raw_kind = item.get("kind")
            kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
            if kind not in VALID_CLAUSE_KINDS:
                errs.append(
                    f"子句 {cid or f'#{idx}'} 的 kind={raw_kind!r} 無效"
                    f"（允許：{', '.join(sorted(VALID_CLAUSE_KINDS))}）"
                )

            tests_norm: list[str] = []
            if "tests" in item and item["tests"] is not None:
                raw_tests = item["tests"]
                if not isinstance(raw_tests, list) or not all(isinstance(t, str) and t.strip() for t in raw_tests):
                    errs.append(f"子句 {cid or f'#{idx}'} 的 tests 必須為非空字串陣列")
                else:
                    tests_norm = [t.strip() for t in raw_tests]

            receipt_norm: dict[str, str] | None = None
            receipt_err = False
            if "receipt" in item and item["receipt"] is not None:
                raw_rc = item["receipt"]
                if not isinstance(raw_rc, dict):
                    errs.append(f"子句 {cid or f'#{idx}'} 的 receipt 必須為 JSON 物件")
                    receipt_err = True
                else:
                    rfile = str(raw_rc.get("file") or "").strip() if isinstance(raw_rc.get("file"), str) else ""
                    rkey = str(raw_rc.get("key") or "").strip() if isinstance(raw_rc.get("key"), str) else ""
                    if not _is_safe_rel_path(rfile):
                        errs.append(f"子句 {cid or f'#{idx}'} 的 receipt.file={rfile!r} 無效或含路徑穿越")
                        receipt_err = True
                    if not rkey:
                        errs.append(f"子句 {cid or f'#{idx}'} 的 receipt.key 不可為空")
                        receipt_err = True
                    if not receipt_err:
                        receipt_norm = {"file": rfile, "key": rkey}

            mode_norm: str | None = None
            if "mode" in item and item["mode"] is not None:
                raw_mode = item["mode"]
                m_val = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
                if kind and kind != "rejects":
                    errs.append(f"子句 {cid or f'#{idx}'} 為 kind={kind}，不可設定 mode")
                elif m_val not in VALID_REJECT_MODES:
                    errs.append(
                        f"子句 {cid or f'#{idx}'} 的 mode={raw_mode!r} 無效"
                        f"（允許：{', '.join(sorted(VALID_REJECT_MODES))}）"
                    )
                else:
                    mode_norm = m_val

            label = cid or f"#{idx}"
            if kind == "changes" and not tests_norm:
                errs.append(f"子句 {label}（kind=changes）必須綁定至少一個具名 test ID")
            elif kind == "preserves" and not receipt_err and receipt_norm is None and not tests_norm:
                errs.append(f"子句 {label}（kind=preserves）必須綁定 receipt 或具名 test ID")
            elif kind == "rejects" and not tests_norm:
                errs.append(f"子句 {label}（kind=rejects）必須綁定至少一個具名 test ID")

            if cid and ctext and kind in VALID_CLAUSE_KINDS:
                clause_out: dict[str, Any] = {
                    "id": cid,
                    "text": ctext,
                    "kind": kind,
                    "tests": tests_norm,
                }
                if receipt_norm is not None:
                    clause_out["receipt"] = receipt_norm
                if kind == "rejects":
                    clause_out["mode"] = mode_norm or "new"
                valid_clauses.append(clause_out)

    if isinstance(raw_then, list):
        then_text = "；".join(derived_texts)
    else:
        then_text = _norm(raw_then)
    return then_text, valid_clauses, errs


def split_then_claims(then_text: str) -> list[str]:
    """Split a `Then` expectation string on `;`/`；` into distinct verifiable claims."""
    claims: list[str] = []
    for part in _CLAIM_SPLIT_RE.split(then_text or ""):
        cleaned = _norm(part).rstrip(".。")
        if cleaned:
            claims.append(cleaned)
    return claims


def _norm_cov(text: str) -> str:
    """Normalize punctuation to single spaces while preserving word boundaries for coverage matching."""
    cleaned = re.sub(r"[,，;；.。:：()（）\[\]、`'\"“”‘’]+", " ", (text or "").lower())
    return " ".join(cleaned.split())


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    prefix = r"(?<![a-z0-9_])" if phrase and phrase[0].isascii() and phrase[0].isalnum() else ""
    suffix = r"(?![a-z0-9_])" if phrase and phrase[-1].isascii() and phrase[-1].isalnum() else ""
    return re.compile(prefix + re.escape(phrase) + suffix)


def check_clause_coverage(sid: str, then_text: str, clauses: list[dict[str, Any]]) -> list[str]:
    """Verify every claim fragment in `then_text` is contiguously covered by declared `clauses`.

    Rejects uncovered short tails, ASCII word-prefix inversions (`invalid` vs `valid`),
    negation prefixes (`不`), and cross-clause word recombinations.
    """
    if not clauses:
        return []
    claims = split_then_claims(then_text)
    if not claims:
        return []
    clause_norms = sorted(
        (_norm_cov(c["text"]) for c in clauses if c.get("text")),
        key=len,
        reverse=True,
    )
    clause_norms = [c for c in clause_norms if c]
    if not clause_norms:
        return [f"情境 {sid} 的 then_clauses 缺少有效文字說明"]
    clause_pats = [(c, _phrase_pattern(c)) for c in clause_norms]
    errs: list[str] = []
    for claim in claims:
        subparts = [_norm_cov(p) for p in re.split(r"[,，]+", claim) if _norm_cov(p)]
        if not subparts:
            continue
        claim_uncovered = False
        for part in subparts:
            if any(_phrase_pattern(part).search(c) and len(part) >= 4 for c in clause_norms):
                continue
            rem = part
            for c, pat in clause_pats:
                if pat.search(rem):
                    rem = pat.sub("\x00", rem)
            leftovers = [seg.strip() for seg in rem.split("\x00") if seg.strip()]
            for seg in leftovers:
                if len(seg) < 4 or not any(_phrase_pattern(seg).search(c) for c in clause_norms):
                    claim_uncovered = True
                    break
            if claim_uncovered:
                break
        if claim_uncovered:
            errs.append(f"情境 {sid} 的 Then 主張「{claim}」未被 then_clauses 覆蓋")
    return errs


def resolve_test_id(repo_root: Path, test_id: str) -> tuple[Path | None, str | None, str | None, str | None]:
    """Resolve `path/to/file.py::[Class::]func` or dotted unittest ID inside `repo_root`.

    Returns `(resolved_file, class_name, func_name, error_or_none)`.
    """
    base = repo_root.resolve()
    tid = str(test_id or "").strip()
    if not tid or "\x00" in tid:
        return None, None, None, f"test ID {tid!r} 無效"

    if "::" in tid:
        parts = tid.split("::")
        if len(parts) not in (2, 3) or any(not p.strip() for p in parts):
            return None, None, None, f"test ID {tid!r} 格式無效（預期 file.py::[Class::]func）"
        rel = parts[0].strip()
        if not _is_safe_rel_path(rel):
            return None, None, None, f"test ID {tid!r} 路徑無效或含路徑穿越"
        cls_name = parts[1].strip() if len(parts) == 3 else None
        fn_name = parts[-1].strip()
        if cls_name and not _IDENT_RE.fullmatch(cls_name):
            return None, None, None, f"test ID {tid!r} 類別名稱無效：{cls_name!r}"
        if not _IDENT_RE.fullmatch(fn_name):
            return None, None, None, f"test ID {tid!r} 函式名稱無效：{fn_name!r}"
        try:
            cand = (base / rel).resolve()
        except ValueError:
            return None, None, None, f"test ID {tid!r} 路徑無效"
        if not cand.is_relative_to(base) or not cand.is_file():
            return None, None, None, f"找不到測試檔 {rel!r}（來自 {tid!r}）"
        return cand, cls_name, fn_name, None

    segs = tid.split(".")
    if len(segs) < 2 or any(not _IDENT_RE.fullmatch(s) for s in segs):
        return None, None, None, f"test ID {tid!r} 格式無效"
    for mod_end in range(len(segs) - 1, 0, -1):
        rem = segs[mod_end:]
        if len(rem) not in (1, 2):
            continue
        mod_rel = Path(*segs[:mod_end]).with_suffix(".py")
        for prefix in (base, base / "tests"):
            if not prefix.is_dir():
                continue
            try:
                cand = (prefix / mod_rel).resolve()
            except ValueError:
                continue
            if cand.is_relative_to(base) and cand.is_file():
                cls_name = rem[0] if len(rem) == 2 else None
                fn_name = rem[-1]
                return cand, cls_name, fn_name, None
    return None, None, None, f"找不到 test ID {tid!r} 對應的測試檔案"


def _iter_func_stmts_and_exprs(fn_node: ast.AST) -> list[ast.AST]:
    """Collect AST nodes inside `fn_node` body without inspecting nested/deferred defs, classes, or lambdas."""
    out: list[ast.AST] = []
    skipped = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    stack: list[ast.AST] = [st for st in getattr(fn_node, "body", ()) if not isinstance(st, skipped)]
    while stack:
        node = stack.pop()
        out.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, skipped):
                continue
            stack.append(child)
    return out


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _const_eval(expr: ast.AST) -> tuple[bool, Any]:
    """Deterministically evaluate pure-constant expressions to detect tautologies."""
    if isinstance(expr, ast.Constant):
        return True, expr.value
    if isinstance(expr, ast.UnaryOp):
        ok, val = _const_eval(expr.operand)
        if not ok:
            return False, None
        if isinstance(expr.op, ast.Not):
            return True, not val
        if isinstance(expr.op, ast.USub) and isinstance(val, (int, float)) and type(val) is not bool:
            return True, -val
        if isinstance(expr.op, ast.UAdd) and isinstance(val, (int, float)) and type(val) is not bool:
            return True, +val
        return False, None
    if isinstance(expr, ast.BoolOp):
        vals: list[Any] = []
        for v in expr.values:
            ok, val = _const_eval(v)
            if not ok:
                return False, None
            vals.append(val)
        return True, (all(vals) if isinstance(expr.op, ast.And) else any(vals))
    if isinstance(expr, ast.Compare):
        ok_l, left = _const_eval(expr.left)
        if not ok_l:
            return False, None
        cur = left
        for op, comp in zip(expr.ops, expr.comparators):
            ok_r, right = _const_eval(comp)
            if not ok_r:
                return False, None
            if isinstance(op, ast.Eq):
                res = cur == right
            elif isinstance(op, ast.NotEq):
                res = cur != right
            elif isinstance(op, ast.Is):
                res = cur is right
            elif isinstance(op, ast.IsNot):
                res = cur is not right
            elif isinstance(op, ast.Lt):
                res = cur < right
            elif isinstance(op, ast.LtE):
                res = cur <= right
            elif isinstance(op, ast.Gt):
                res = cur > right
            elif isinstance(op, ast.GtE):
                res = cur >= right
            else:
                return False, None
            if not res:
                return True, False
            cur = right
        return True, True
    return False, None


def _is_exit_code_or_const(expr: ast.AST) -> tuple[bool, bool]:
    """Return `(only_exit_code_or_const, saw_exit_code)` for an assertion expression."""
    if _const_eval(expr)[0]:
        return True, False
    if isinstance(expr, ast.Name):
        if expr.id in _EXIT_CODE_NAMES:
            return True, True
        return expr.id in ("self", "cls", "True", "False", "None"), False
    if isinstance(expr, ast.Attribute):
        if expr.attr in _EXIT_CODE_NAMES:
            return True, True
        return False, False
    if isinstance(expr, ast.Compare):
        parts = [_is_exit_code_or_const(node) for node in (expr.left, *expr.comparators)]
        return all(ok for ok, _ in parts), any(seen for _, seen in parts)
    if isinstance(expr, ast.UnaryOp):
        return _is_exit_code_or_const(expr.operand)
    if isinstance(expr, ast.BoolOp):
        parts = [_is_exit_code_or_const(v) for v in expr.values]
        return all(ok for ok, _ in parts), any(seen for _, seen in parts)
    if isinstance(expr, ast.BinOp):
        lo, ls = _is_exit_code_or_const(expr.left)
        ro, rs = _is_exit_code_or_const(expr.right)
        return lo and ro, ls or rs
    return False, False


def _is_tautology_assert(stmt: ast.Assert) -> bool:
    return _const_eval(stmt.test)[0]


def _asserted_call_value_args(node: ast.Call, cname: str) -> list[ast.AST]:
    """Return value expressions asserted by `node`, excluding trailing message arguments (`msg=`)."""
    kw_vals = {
        kw.arg: kw.value
        for kw in node.keywords
        if kw.arg and kw.arg != "msg"
    }
    if cname in _UNARY_ASSERT_CALLS:
        if node.args:
            return [node.args[0]]
        return [kw_vals["expr"]] if "expr" in kw_vals else []
    if cname in _BINARY_ASSERT_CALLS:
        vals = list(node.args[:2])
        for k in ("first", "second", "member", "container", "a", "b", "text", "expected_regex"):
            if len(vals) < 2 and k in kw_vals:
                vals.append(kw_vals[k])
        return vals
    return list(node.args) + list(kw_vals.values())


def _is_tautology_call(node: ast.Call, cname: str) -> bool:
    vals = _asserted_call_value_args(node, cname)
    if cname in _UNARY_ASSERT_CALLS and vals:
        return _const_eval(vals[0])[0]
    if cname in _PAIR_COMPARE_CALLS and len(vals) >= 2:
        return _const_eval(vals[0])[0] and _const_eval(vals[1])[0]
    return False


def _is_empty_or_falsy_const(node: ast.AST) -> bool:
    ok, val = _const_eval(node)
    if ok:
        return val in (False, None, 0, "")
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return not (node.elts if hasattr(node, "elts") else node.keys)
    return False


def _expr_is_negative(expr: ast.AST) -> bool:
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return True
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Compare):
            if any(isinstance(op, (ast.NotEq, ast.NotIn, ast.IsNot)) for op in sub.ops):
                return True
            if any(isinstance(op, (ast.Eq, ast.Is)) for op in sub.ops):
                all_terms = [sub.left, *sub.comparators]
                if any(_is_empty_or_falsy_const(t) for t in all_terms):
                    return True
    return False


def _call_is_negative(node: ast.Call, cname: str) -> bool:
    if cname in _NEGATIVE_ASSERT_CALLS:
        return True
    low = cname.lower()
    if any(tok in low for tok in _NEGATIVE_SUBSTRINGS):
        return True
    vals = _asserted_call_value_args(node, cname)
    if cname in _PAIR_COMPARE_CALLS and len(vals) >= 2:
        if _is_empty_or_falsy_const(vals[0]) or _is_empty_or_falsy_const(vals[1]):
            return True
    return False


def audit_test_ast(file_path: Path, class_name: str | None, func_name: str, *,
                   require_negative: bool = False) -> list[str]:
    """Statically verify a Python test function exists, is non-empty, has non-tautological assertions,
    is not exit-code-only, and (if `require_negative`) asserts at least one rejection/negative check."""
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError) as err:
        return [f"測試檔 {file_path.name} 無法解析（{err}）"]

    target_fn: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    if class_name:
        cls_node: ast.ClassDef | None = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                cls_node = node
                break
        if cls_node is None:
            return [f"{file_path.name} 找不到測試類別 {class_name}"]
        for item in cls_node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == func_name:
                target_fn = item
                break
        if target_fn is None:
            return [f"{file_path.name} 的 {class_name} 找不到測試方法 {func_name}"]
    else:
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == func_name:
                target_fn = item
                break
        if target_fn is None:
            return [f"{file_path.name} 找不到測試函式 {func_name}"]

    non_doc_stmts = [
        st for st in target_fn.body
        if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str))
    ]
    if not non_doc_stmts or all(isinstance(st, ast.Pass) for st in non_doc_stmts):
        return [f"{file_path.name}::{func_name} 為空測試或只有 pass/docstring"]

    nodes = _iter_func_stmts_and_exprs(target_fn)
    real_assert_exprs: list[ast.AST] = []
    has_negative = False
    tautology_count = 0

    for node in nodes:
        if isinstance(node, ast.Assert):
            if _is_tautology_assert(node):
                tautology_count += 1
                continue
            real_assert_exprs.append(node.test)
            if _expr_is_negative(node.test):
                has_negative = True
        elif isinstance(node, ast.Call):
            cname = _call_name(node)
            is_assert_call = (
                cname.startswith("assert")
                or cname in ("raises", "expect")
            )
            if not is_assert_call:
                continue
            if _is_tautology_call(node, cname):
                tautology_count += 1
                continue
            val_args = _asserted_call_value_args(node, cname)
            real_assert_exprs.extend(val_args or [node])
            if _call_is_negative(node, cname):
                has_negative = True

    if not real_assert_exprs:
        if tautology_count:
            return [f"{file_path.name}::{func_name} 只包含恆真斷言（如 assert True）"]
        return [f"{file_path.name}::{func_name} 缺少實質斷言（assert / assert* / pytest.raises / expect）"]

    if all(_is_exit_code_or_const(expr)[0] for expr in real_assert_exprs) and any(
        _is_exit_code_or_const(expr)[1] for expr in real_assert_exprs
    ):
        return [f"{file_path.name}::{func_name} 只檢查 exit code / returncode，未驗證輸出或狀態"]

    if require_negative and not has_negative:
        return [f"{file_path.name}::{func_name} 為 rejects 子句測試，但缺少否定／異常拒收斷言"]

    return []


def _collect_runner_texts(repo_root: Path) -> list[str]:
    texts: list[str] = []
    for folder in (repo_root / ".github" / "workflows", repo_root / ".devflow"):
        if not folder.is_dir():
            continue
        for p in sorted(folder.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".yml", ".yaml", ".sh", ".py"):
                if p.name == "scenarios.json":
                    continue
                with suppress(OSError, ValueError):
                    texts.append(p.read_text(encoding="utf-8", errors="replace"))
    for root_file in ("pyproject.toml", "Makefile"):
        rf = repo_root / root_file
        if rf.is_file():
            with suppress(OSError, ValueError):
                texts.append(rf.read_text(encoding="utf-8", errors="replace"))
    return texts


def _split_shell_segments(runner_text: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for raw_line in runner_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip simple YAML step prefixes (`- run:` / `run:`) before shell tokenization.
        stripped = re.sub(r"^(?:-\s*)?run:\s*", "", stripped)
        for chunk in re.split(r"(?:&&|\|\||;|\|)", stripped):
            chunk = chunk.strip()
            if not chunk or chunk.startswith("#"):
                continue
            try:
                toks = shlex.split(chunk, comments=True)
            except ValueError:
                toks = chunk.split()
            while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", toks[0]):
                toks = toks[1:]
            if toks:
                segments.append(toks)
    return segments


def _is_python_bin(tok: str) -> bool:
    name = PurePosixPath(tok).name
    return name == "python" or name.startswith("python3")


def _unittest_discover_match(toks: list[str], rel_posix: str, filename: str) -> bool:
    if not filename.endswith(".py"):
        return False
    # Expect `python [-flags] -m unittest discover ...` or `unittest discover ...`
    idx = 0
    if _is_python_bin(toks[0]):
        idx = 1
        while idx < len(toks) and toks[idx] != "-m":
            if toks[idx] in ("-c", "-W"):
                idx += 2
            else:
                idx += 1
        if idx + 1 >= len(toks) or toks[idx] != "-m" or toks[idx + 1] != "unittest":
            return False
        idx += 2
    elif toks[0] == "unittest":
        idx = 1
    else:
        return False
    if idx >= len(toks) or toks[idx] != "discover":
        return False
    args = toks[idx + 1:]
    start_dir = ""
    pattern = "test*.py"
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-s", "--start-directory") and i + 1 < len(args):
            start_dir = args[i + 1].strip("./")
            i += 2
            continue
        if arg.startswith("--start-directory="):
            start_dir = arg.split("=", 1)[1].strip("./")
        elif arg.startswith("-s") and len(arg) > 2:
            start_dir = arg[2:].strip("./")
        elif arg in ("-p", "--pattern") and i + 1 < len(args):
            pattern = args[i + 1]
            i += 2
            continue
        elif arg.startswith("--pattern="):
            pattern = arg.split("=", 1)[1]
        elif arg.startswith("-p") and len(arg) > 2:
            pattern = arg[2:]
        i += 1
    if not fnmatch.fnmatchcase(filename, pattern):
        return False
    parent_dir = PurePosixPath(rel_posix).parent.as_posix()
    if not start_dir or start_dir == ".":
        return parent_dir == "." or parent_dir.startswith("tests")
    return parent_dir == start_dir or parent_dir.startswith(start_dir + "/")


def _pytest_invocation_match(toks: list[str], rel_posix: str, filename: str) -> bool:
    if not (filename.startswith("test_") and filename.endswith(".py")) and not filename.endswith("_test.py"):
        return False
    idx = -1
    if PurePosixPath(toks[0]).name == "pytest":
        idx = 1
    elif _is_python_bin(toks[0]):
        j = 1
        while j < len(toks) and toks[j] != "-m":
            j += 2 if toks[j] in ("-c", "-W") else 1
        if j + 1 < len(toks) and toks[j] == "-m" and toks[j + 1] == "pytest":
            idx = j + 2
    if idx < 0:
        return False
    args = toks[idx:]
    pos_targets: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _PYTEST_VALUE_FLAGS and i + 1 < len(args):
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        pos_targets.append(arg.split("::", 1)[0].strip("./"))
        i += 1
    if not pos_targets:
        return True
    for tgt in pos_targets:
        if not tgt or tgt == ".":
            return True
        if rel_posix == tgt or rel_posix.startswith(tgt.rstrip("/") + "/"):
            return True
    return False


def _runner_discovers_path(runner_text: str, rel_posix: str, filename: str) -> bool:
    if not filename.endswith(".py"):
        return False
    non_comment_lines = [ln for ln in runner_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    active_text = "\n".join(non_comment_lines)
    if re.search(rf"(?<![A-Za-z0-9_./-]){re.escape(rel_posix)}(?![A-Za-z0-9_.-])", active_text):
        return True
    if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(filename)}(?![A-Za-z0-9_.-])", active_text):
        return True
    for toks in _split_shell_segments(runner_text):
        if _unittest_discover_match(toks, rel_posix, filename):
            return True
        if _pytest_invocation_match(toks, rel_posix, filename):
            return True
    return False


def check_runner_reachability(repo_root: Path, test_path: Path, class_name: str | None, func_name: str, *,
                              require_runner: bool = False) -> list[str]:
    """Verify `test_path` is referenced or discovered by non-comment CI/devflow runner entrypoints."""
    base = repo_root.resolve()
    try:
        rel_posix = test_path.resolve().relative_to(base).as_posix()
    except ValueError:
        return [f"測試檔 {test_path} 不在 repo 根目錄 {repo_root} 內"]
    runner_texts = _collect_runner_texts(repo_root)
    if not runner_texts:
        return ["找不到任何 CI 或 .devflow runner 執行定義，無法驗證可達性"] if require_runner else []
    filename = test_path.name
    for txt in runner_texts:
        active_text = "\n".join(ln for ln in txt.splitlines() if ln.strip() and not ln.strip().startswith("#"))
        if _runner_discovers_path(txt, rel_posix, filename):
            return []
        if class_name and re.search(rf"\b{re.escape(class_name)}\b", active_text):
            return []
        if re.search(rf"\b{re.escape(func_name)}\b", active_text):
            return []
    return [f"測試 {rel_posix}::{func_name} 未被任何 CI workflow 或 .devflow runner 執行／探索到"]


def validate_scenario_clauses(
    sid: str,
    then_text: str,
    clauses: list[dict[str, Any]],
    *,
    repo_root: Path | None = None,
    require_clauses: bool = False,
    require_runner: bool = False,
) -> list[str]:
    """Run all deterministic clause-contract, AST assertion lint, and runner-reachability checks."""
    errs: list[str] = []
    if not clauses:
        if require_clauses:
            return [f"情境 {sid} 必須在 .devflow/scenarios.json 宣告結構化 Then 子句與具名測試／收據綁定"]
        return []

    errs.extend(check_clause_coverage(sid, then_text, clauses))

    if repo_root is None or not repo_root.is_dir():
        return errs

    audited: dict[tuple[Path, str | None, str, bool], list[str]] = {}
    reachable_cache: dict[tuple[Path, str | None, str], list[str]] = {}
    for clause in clauses:
        require_neg = clause.get("kind") == "rejects"
        for tid in clause.get("tests") or []:
            fpath, cls_name, fn_name, res_err = resolve_test_id(repo_root, tid)
            if res_err or fpath is None or fn_name is None:
                errs.append(f"情境 {sid} 子句 {clause['id']}：{res_err}")
                continue
            ast_key = (fpath, cls_name, fn_name, require_neg)
            if ast_key not in audited:
                audited[ast_key] = audit_test_ast(fpath, cls_name, fn_name, require_negative=require_neg)
            for msg in audited[ast_key]:
                errs.append(f"情境 {sid} 子句 {clause['id']}：{msg}")
            reach_key = (fpath, cls_name, fn_name)
            if reach_key not in reachable_cache:
                reachable_cache[reach_key] = check_runner_reachability(
                    repo_root, fpath, cls_name, fn_name, require_runner=require_runner
                )
            for msg in reachable_cache[reach_key]:
                errs.append(f"情境 {sid} 子句 {clause['id']}：{msg}")
    return errs
