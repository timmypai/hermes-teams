#!/usr/bin/env python3
"""devflow repo checks: rule self-test, lesson ledger, rule scan. Stdlib only (Python >= 3.11).

Run from the repo root (`.devflow/check.sh`). Exit 0 = pass, 1 = findings, 2 = broken config.
  check.py                   self-test every rule, validate lessons, scan tracked files
  check.py --write-baseline  record current violations as accepted legacy (adoption only)

Rules: .devflow/rules/<id>.toml
  id, message, include = [globs], exclude = [globs], patterns = [regex, ...]
  [[fixture]] path, text, expect = "fail" | "pass"   (at least one of each; proves the rule fires)
Exemption: `devflow: allow <rule-id> L-NNNN` on the offending line or the line above;
  L-NNNN must be an existing lesson. Baseline (.devflow/rules/baseline.json) hides legacy
  violations by (rule, path, line text) so only new ones fail.
Lessons: .devflow/lessons/L-NNNN.toml (see validate_lesson).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

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


class ConfigError(Exception):
    pass


def glob_regex(glob: str) -> re.Pattern:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif glob.startswith("**", i):
            out.append(".*"); i += 2
        elif glob[i] == "*":
            out.append("[^/]*"); i += 1
        elif glob[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(glob[i])); i += 1
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True)
class Rule:
    id: str
    message: str
    include: tuple[re.Pattern, ...]
    exclude: tuple[re.Pattern, ...]
    patterns: tuple[re.Pattern, ...]
    fixtures: tuple[dict, ...]

    def applies(self, path: str) -> bool:
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
            if not d.get("patterns") or not d.get("include"):
                raise ConfigError(f"{f}: needs include and patterns")
            rules.append(Rule(
                id=rid, message=d["message"],
                include=tuple(glob_regex(g) for g in d["include"]),
                exclude=tuple(glob_regex(g) for g in [*d.get("exclude", []), *ALWAYS_EXCLUDE]),
                patterns=tuple(re.compile(p) for p in d["patterns"]),
                fixtures=tuple(d.get("fixture", []))))
            seen.add(rid)
        except (KeyError, tomllib.TOMLDecodeError, re.error) as e:
            raise ConfigError(f"{f}: {e}") from e
    return rules


def scan_text(rules: list[Rule], path: str, text: str, lessons: set[str]) -> list[Violation]:
    out: list[Violation] = []
    applicable = [r for r in rules if r.applies(path)]
    if not applicable:
        return out
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        for r in applicable:
            if not r.hits(line):
                continue
            allowed = [m for src in (line, lines[n - 2] if n > 1 else "")
                       for m in ALLOW.finditer(src) if m.group(1) == r.id]
            if not allowed:
                out.append(Violation(r.id, path, n, line, r.message))
            elif not any(m.group(2) in lessons for m in allowed):
                out.append(Violation(r.id, path, n, line,
                                     f"exemption cites missing lesson {allowed[0].group(2)}"))
    return out


def self_test(rules: list[Rule]) -> list[str]:
    errors = []
    for r in rules:
        expects = Counter(fx.get("expect") for fx in r.fixtures)
        if not expects["fail"] or not expects["pass"]:
            errors.append(f"rule {r.id}: needs at least one fixture with expect=fail and one with expect=pass")
        for i, fx in enumerate(r.fixtures, 1):
            if fx.get("expect") not in ("fail", "pass") or "path" not in fx or "text" not in fx:
                errors.append(f"rule {r.id} fixture {i}: needs path, text, expect=fail|pass")
                continue
            cited = {m.group(2) for m in ALLOW.finditer(fx["text"])}
            fired = bool(scan_text([r], fx["path"], fx["text"], cited))
            if fired != (fx["expect"] == "fail"):
                verb = "did not fire on" if fx["expect"] == "fail" else "fired on"
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
