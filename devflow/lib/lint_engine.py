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
import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from lib import policy as policy_lib

VALID_KINDS = frozenset({"regex", "cross_file", "import_boundary", "diff_policy"})
VALID_SEVERITIES = frozenset({"warn", "error"})
ALLOW_EXEMPT_RE = re.compile(r"devflow:\s*allow\s+([\w-]+)(?:\s+(L-\d{4}))?")
JS_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
JS_IMPORT_RE = re.compile(
    r"""(?:^|[;\n])\s*(?:import\s+(?:[^'"]+\s+from\s+)?|export\s+[^'"]*\s+from\s+|(?:const|let|var)?[^=\n]*=?\s*require\s*\(\s*)['"]([^'"]+)['"]""",
    re.M,
)
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


def glob_matches(path: str, pattern: str) -> bool:
    p = _strip_rel_prefix(path)
    pat = _strip_rel_prefix(pattern)
    if not pat:
        return False
    if pat.endswith("/"):
        return p.startswith(pat)
    if "**" in pat:
        prefix, _, suffix = pat.partition("**")
        prefix = prefix.rstrip("/")
        suffix = suffix.lstrip("/")
        if prefix and not (p == prefix or p.startswith(prefix + "/")):
            return False
        if not suffix:
            return True
        return fnmatch.fnmatch(p, f"*{suffix}") or PurePosixPath(p).match(pat)
    return fnmatch.fnmatch(p, pat) or PurePosixPath(p).match(pat)


def _line_exempt(line: str, rule_id: str) -> bool:
    for m in ALLOW_EXEMPT_RE.finditer(line or ""):
        if _norm_id(m.group(1)) == _norm_id(rule_id):
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
    elif kind == "diff_policy":
        mf = data.get("max_files")
        ml = data.get("max_added_lines")
        if mf is None and ml is None:
            raise LintConfigError(f"{path}: diff_policy rule requires `max_files` or `max_added_lines`")
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


def _extract_python_imports(rel_path: str, text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError:
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                resolved = _resolve_py_relative_import(rel_path, node.module, node.level)
                if resolved:
                    out.append((node.lineno, resolved))
            else:
                for alias in node.names:
                    resolved = _resolve_py_relative_import(rel_path, alias.name, node.level)
                    if resolved:
                        out.append((node.lineno, resolved))
    return out


def _extract_js_imports(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for m in JS_IMPORT_RE.finditer(text):
        lineno = text.count("\n", 0, m.start()) + 1
        out.append((lineno, m.group(1).strip()))
    return out


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
        if rule.kind == "diff_policy":
            continue
        if rule.kind == "cross_file":
            if not all(_when_satisfied(c, norm_files) for c in rule.when):
                continue
            for rel_path, text in sorted(norm_files.items()):
                if not rule.applies(rel_path):
                    continue
                if rule.target_mode == "forbid":
                    lines = text.splitlines()
                    for pat in rule.target_patterns:
                        if m := pat.search(text):
                            lineno = text.count("\n", 0, m.start()) + 1
                            if 1 <= lineno <= len(lines) and _line_exempt(lines[lineno - 1], rule.id):
                                continue
                            findings.append(_finding(rule, rel_path, m.group(0), line=lineno))
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
                        if 1 <= lineno <= len(lines) and _line_exempt(lines[lineno - 1], rule.id):
                            continue
                        findings.append(_finding(rule, rel_path, m.group(0), line=lineno))
                        break
            elif rule.kind == "import_boundary":
                ext = PurePosixPath(rel_path).suffix.lower()
                imports = _extract_python_imports(rel_path, text) if ext == ".py" else (
                    _extract_js_imports(text) if ext in JS_EXTS else []
                )
                for lineno, imp in imports:
                    if 1 <= lineno <= len(lines) and _line_exempt(lines[lineno - 1], rule.id):
                        continue
                    if any(_import_matches_forbidden(imp, forb) for forb in rule.forbidden_modules):
                        findings.append(_finding(rule, rel_path, imp, line=lineno))
    return findings


def scan_repo(rules: list[LintRule], repo_dir: Path | str | None) -> list[dict[str, Any]]:
    """Read-only scan of all relevant files inside `repo_dir` for `rules`."""
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

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        if any(r.applies(rel) for r in rules) and p.stat().st_size <= MAX_FILE_BYTES:
            if rel not in wanted:
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
) -> list[dict[str, Any]]:
    """Evaluate `diff_policy` rules against a PR's changed file list & optional added-line count."""
    findings: list[dict[str, Any]] = []
    norm_changed = [_strip_rel_prefix(p) for p in changed_files if p and p.strip()]
    for rule in rules:
        if rule.kind != "diff_policy":
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
        if rule.max_added_lines is not None and added_lines is not None and added_lines > rule.max_added_lines:
            findings.append({
                "id": rule.id,
                "kind": rule.kind,
                "severity": rule.severity,
                "file": "",
                "line": 0,
                "match": f"added_lines={added_lines}>{rule.max_added_lines}",
                "detail": f"{rule.message} (added_lines={added_lines}, max_added_lines={rule.max_added_lines})",
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
            if rule.kind == "diff_policy":
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
