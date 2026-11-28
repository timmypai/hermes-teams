"""Card scope contract parsing, body hygiene checks, and PR goal-diff alignment."""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

ALLOWED_SCOPE_RE = re.compile(r"^\s*Allowed-Scope[：:]\s*(.+)$", re.M | re.I)
OUT_OF_SCOPE_RE = re.compile(r"^\s*Out-of-Scope[：:]\s*(.+)$", re.M | re.I)
NON_GOALS_RE = re.compile(r"^\s*Non-Goals[：:]\s*(.+)$", re.M | re.I)
VERIFY_CMD_RE = re.compile(r"^\s*Verify[：:]\s*(.+)$", re.M | re.I)
MODE_RE = re.compile(r"^\s*Mode[：:][ \t]*(.*?)[ \t]*$", re.M | re.I)
ALLOW_WRITE_RE = re.compile(r"^\s*Allow-Write[：:][ \t]*(.*?)[ \t]*$", re.M | re.I)
KANBAN_TASK_RE = re.compile(r"^\s*Kanban[：:]\s*(t_[0-9A-Za-z]+)\b", re.M | re.I)
EPIC_SECTION_RE = re.compile(r"^##\s*(目標|驗收|使用者原話|使用者決定|不能碰)[ \t]*\n(.*?)(?=^## |\Z)", re.M | re.S)
_PATH_TOKEN_RE = re.compile(r"`([^`\s]+)`|(?<![A-Za-z0-9_])((?:\.[A-Za-z0-9_-]+|[A-Za-z0-9_.-]+)/(?:[A-Za-z0-9_.*?/-]+))")

# Deterministic leaks that must never appear in human-facing card bodies (Item #8).
LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("commit SHA", re.compile(r"\b[0-9a-f]{40}\b")),
    ("CI run URL/ID", re.compile(r"/actions/runs/\d+\b|\brun_id\s*[=:]\s*\d+\b", re.I)),
    ("內部容器/核心旗標", re.compile(r"(?:--no-sandbox|\bCAP_SYS_[A-Z_]+\b|\bseccomp\b|\b(?:uid|gid)=\d+\b)", re.I)),
)


def split_globs(raw: str) -> list[str]:
    return [tok.strip().strip("`") for tok in re.split(r"[,，\s]+", raw or "") if tok.strip().strip("`")]


def parse_card_contract(body: str | None) -> dict:
    """Extract structured scope, non-goals & verification fields from a card body."""
    text = body or ""
    allowed = [g for m in ALLOWED_SCOPE_RE.finditer(text) for g in split_globs(m.group(1))]
    out_of_scope = [g for m in OUT_OF_SCOPE_RE.finditer(text) for g in split_globs(m.group(1))]
    non_goals = [m.group(1).strip() for m in NON_GOALS_RE.finditer(text) if m.group(1).strip()]
    verify = [m.group(1).strip() for m in VERIFY_CMD_RE.finditer(text) if m.group(1).strip()]
    return {
        "allowed_scope": list(dict.fromkeys(allowed)),
        "out_of_scope": list(dict.fromkeys(out_of_scope)),
        "non_goals": non_goals,
        "verify": verify,
    }


def _strip_inline_comment(raw: str | None) -> str:
    """Strip trailing `# ...` inline comments from a header directive value."""
    return (raw or "").split("#", 1)[0].strip()


def parse_epic_mode(epic_body: str | None, valid_modes: set[str] | frozenset[str] | None = None) -> tuple[str, str | None]:
    """Return `(resolved_mode, invalid_or_conflict_raw)` from an EPIC's `Mode:` line (defaults to `deliver`).

    Parses every declaration line (supporting inline `# ...` comments) and fails closed on
    invalid syntax or conflicting duplicates.
    """
    matches = list(MODE_RE.finditer(epic_body or ""))
    if not matches:
        return "deliver", None
    allowed = valid_modes or frozenset({"deliver", "investigate", "review"})
    modes: list[str] = []
    invalids: list[str] = []
    for m in matches:
        raw = _strip_inline_comment(m.group(1)).lower()
        if not raw:
            invalids.append("<empty>")
        elif raw not in allowed:
            invalids.append(raw)
        else:
            modes.append(raw)
    if invalids:
        return "deliver", ", ".join(dict.fromkeys(invalids))
    unique_modes = list(dict.fromkeys(modes))
    if len(unique_modes) > 1:
        return "deliver", " vs ".join(unique_modes)
    return unique_modes[0], None


def parse_epic_allow_write(epic_body: str | None) -> tuple[bool | None, str | None]:
    """Return `(allow_write_override, invalid_or_conflict_raw)` from an EPIC's `Allow-Write:` lines.

    Parses every declaration line (supporting inline `# ...` comments) and fails closed to `False`
    whenever any declaration is invalid or contradictory duplicates are present.
    """
    matches = list(ALLOW_WRITE_RE.finditer(epic_body or ""))
    if not matches:
        return None, None
    values: list[bool] = []
    invalids: list[str] = []
    for m in matches:
        raw = _strip_inline_comment(m.group(1)).lower()
        if raw in ("true", "yes", "on", "1"):
            values.append(True)
        elif raw in ("false", "no", "off", "0"):
            values.append(False)
        else:
            invalids.append(raw or "<empty>")
    if invalids:
        return False, ", ".join(dict.fromkeys(invalids))
    if len(set(values)) > 1:
        return False, "conflicting declarations (true vs false)"
    return values[0], None


_ROOT_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def extract_do_not_touch_paths(do_not_touch: str | None) -> list[str]:
    """Extract explicit repo-relative path/glob patterns from EPIC `## 不能碰` for deterministic
    `Out-of-Scope` inheritance (ignores URLs and plain prose)."""
    out: list[str] = []
    text = re.sub(r"https?://\S+", " ", do_not_touch or "")
    for m in _PATH_TOKEN_RE.finditer(text):
        tok = (m.group(1) or m.group(2) or "").strip().strip(",;:()[]\"'").rstrip(".")
        if not tok or "://" in tok or tok.startswith(("http", "/", "-")) or tok in (".", "..") or tok.isdigit():
            continue
        if "/" in tok or "*" in tok:
            out.append(tok)
        elif m.group(1) and _ROOT_FILENAME_RE.fullmatch(tok):
            out.append(tok)
    return list(dict.fromkeys(out))


def parse_epic_sections(body: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in EPIC_SECTION_RE.finditer(body or ""):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def match_glob(path: str, pattern: str) -> bool:
    """Match a POSIX repo-relative file path against a glob or directory prefix."""
    p = path.lstrip("./")
    pat = pattern.lstrip("./")
    if not pat:
        return False
    if pat.endswith("/"):
        return p.startswith(pat)
    if "**" in pat:
        # PurePosixPath.match handles leading wildcards; also support dir/**/*.py via fnmatch fallback
        prefix, _, suffix = pat.partition("**")
        prefix = prefix.rstrip("/")
        suffix = suffix.lstrip("/")
        if prefix and not (p == prefix or p.startswith(prefix + "/")):
            return False
        if not suffix:
            return True
        return fnmatch.fnmatch(p, f"*{suffix}") or PurePosixPath(p).match(pat)
    return fnmatch.fnmatch(p, pat) or p == pat or p.startswith(pat.rstrip("/") + "/")


def touched_paths(files: list[dict]) -> list[str]:
    """Return every repo-relative path touched by `files`, preserving exact `filename` and `previous_filename`."""
    paths: list[str] = []
    for f in files:
        if prev := f.get("previous_filename"):
            paths.append(prev)
        if name := f.get("filename"):
            paths.append(name)
    return sorted(set(paths))


_BROAD_WILDCARD_RE = re.compile(r"^(?:\*+(?:/\*+)*|\*\*+(?:/\*+(?:\.\*)?)?)$")


def is_overly_broad_scope(pattern: str) -> bool:
    """True when an `Allowed-Scope` pattern matches the entire repo (e.g. `*`, `**`, `**/*`, `.`)."""
    p = (pattern or "").strip().strip("`").lstrip("./").rstrip("/")
    if p in ("", ".", "*", "**", "**/*", "**/*.*"):
        return True
    return bool(_BROAD_WILDCARD_RE.fullmatch(p))


def allowed_scope_problems(allowed_scope: list[str] | None, *, required: bool = True) -> list[str]:
    """Validate declared `Allowed-Scope:` entries on a work card."""
    items = [p.strip() for p in (allowed_scope or []) if p and p.strip()]
    if not items:
        if required:
            return ["缺少 `Allowed-Scope:` 宣告（請列出本卡預期改動的目錄或檔案 glob，避免範圍外擴）"]
        return []
    broad = [p for p in items if is_overly_broad_scope(p)]
    if broad:
        return [
            f"`Allowed-Scope:` 過寬（{', '.join(broad)}），不得使用全專案萬用模式；請縮小到實際要改動的目錄或檔案"
        ]
    return []


def check_files_against_scope(
    filenames: list[str],
    *,
    allowed_scope: list[str] | None = None,
    out_of_scope: list[str] | None = None,
    test_paths: list[str] | None = None,
    max_diff_files: int | None = None,
    file_count: int | None = None,
) -> list[str]:
    """Deterministic diff scope checks: Out-of-Scope forbidden, Allowed-Scope bounded, file count capped.
    Declared `test_paths` (`DEVFLOW_TEST_PATHS`) are exempted alongside `allowed_scope` but NEVER
    override `out_of_scope`."""
    findings: list[str] = []
    files = sorted(set(filenames))
    if out_of_scope:
        violated = [f for f in files if any(match_glob(f, pat) for pat in out_of_scope)]
        if violated:
            findings.append(
                f"改動觸及卡片禁止範圍（Out-of-Scope）：{', '.join(violated[:10])}"
                + (f" 等共 {len(violated)} 檔" if len(violated) > 10 else "")
            )
    if allowed_scope:
        extra_tests = [p.strip() for p in (test_paths or []) if p and p.strip()]
        # Allow declared DEVFLOW_TEST_PATHS and standard lesson/scenario paths alongside Allowed-Scope
        # (Out-of-Scope above still strictly forbids matching paths).
        outside = [
            f for f in files
            if not any(match_glob(f, pat) for pat in allowed_scope)
            and not any(match_glob(f, pat) for pat in extra_tests)
            and not f.startswith((".devflow/lessons/", ".devflow/scenarios.json"))
        ]
        if outside:
            findings.append(
                f"改動超出卡片宣告範圍（Allowed-Scope={', '.join(allowed_scope)}）：{', '.join(outside[:10])}"
                + (f" 等共 {len(outside)} 檔" if len(outside) > 10 else "")
            )
    effective_count = len(files) if file_count is None else int(file_count)
    if max_diff_files and max_diff_files > 0 and effective_count > max_diff_files:
        findings.append(
            f"單一 PR 改動 {effective_count} 個檔案，超過上限 {max_diff_files} 檔（請拆小或確認是否夾帶無關重構）"
        )
    return findings


def body_hygiene_leaks(text: str) -> list[str]:
    """Return labels of technical leaks found in human-facing card body text."""
    return [label for label, pat in LEAK_PATTERNS if pat.search(text or "")]


_CARD_SECTION_HEAD_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
_CLAUSE_SPLIT_RE = re.compile(r"[\n。；;]+")


def parse_card_sections(body: str | None) -> list[tuple[str, str]]:
    """Split a card body into ordered `(heading, content)` pairs (`""` for leading preamble)."""
    text = body or ""
    matches = list(_CARD_SECTION_HEAD_RE.finditer(text))
    if not matches:
        stripped = text.strip()
        return [("", stripped)] if stripped else []
    sections: list[tuple[str, str]] = []
    preamble = text[:matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(1).strip(), text[m.end():end].strip()))
    return sections


def strip_excluded_card_fields(text: str, excluded_fields: list[str] | tuple[str, ...]) -> str:
    """Drop single-line structured directive fields (`Field: ...`) exempt from handoff checks."""
    fields = [re.escape(str(f).strip()) for f in (excluded_fields or ()) if str(f).strip()]
    if not fields:
        return text or ""
    field_re = re.compile(rf"^\s*(?:{'|'.join(fields)})[：:]", re.I)
    return "\n".join(line for line in (text or "").splitlines() if not field_re.match(line))


def _compile_patterns(raw_patterns: list[str] | tuple[str, ...], *, flags: int = 0) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pat in raw_patterns or ():
        if not isinstance(pat, str) or not pat.strip():
            continue
        try:
            compiled.append(re.compile(pat, flags))
        except re.error:
            continue
    return compiled


def post_handoff_problems(kind: str | None, body: str | None, handoff_rules: dict | None) -> list[str]:
    """Detect post-handoff lifecycle criteria (review/approval, merge/deploy, live acceptance)
    leaked into author-stage card sections before handoff, driven declaratively by policy rules."""
    if not isinstance(handoff_rules, dict):
        return []
    target_cards = {str(k).strip().upper() for k in (handoff_rules.get("cards") or ()) if str(k).strip()}
    if not kind or kind.strip().upper() not in target_cards:
        return []
    excluded_sections = {str(s).strip() for s in (handoff_rules.get("excluded_sections") or ()) if str(s).strip()}
    if done_heading := str(handoff_rules.get("done_heading") or "").strip():
        excluded_sections.add(done_heading.lstrip("#").strip())
    excluded_fields = list(handoff_rules.get("excluded_fields") or ())
    negations = _compile_patterns(handoff_rules.get("negation_prefixes") or (), flags=re.I)

    compiled_rules: list[tuple[str, str, list[re.Pattern[str]]]] = []
    for item in handoff_rules.get("post_handoff_clauses") or ():
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "downstream").strip()
        label = str(item.get("label") or item.get("id") or "下游階段條件").strip()
        pats = _compile_patterns(item.get("patterns") or (), flags=re.I)
        if pats:
            compiled_rules.append((stage, label, pats))
    if not compiled_rules:
        return []

    findings: list[str] = []
    seen_hits: set[tuple[str, str, str]] = set()
    for heading, raw_section in parse_card_sections(body):
        if heading in excluded_sections:
            continue
        cleaned = strip_excluded_card_fields(raw_section, excluded_fields)
        if not cleaned.strip():
            continue
        sec_label = f"`## {heading}`" if heading else "卡片前言"
        for clause in _CLAUSE_SPLIT_RE.split(cleaned):
            clause_str = clause.strip()
            if not clause_str:
                continue
            for stage, label, pats in compiled_rules:
                for pat in pats:
                    m = pat.search(clause_str)
                    if not m:
                        continue
                    prefix = clause_str[max(0, m.start() - 28):m.start()]
                    if any(neg.search(prefix) for neg in negations):
                        continue
                    snippet = m.group(0).strip()
                    sig = (sec_label, stage, label)
                    if sig in seen_hits:
                        break
                    seen_hits.add(sig)
                    findings.append(
                        f"{sec_label} 不可要求{label}（「{snippet}」屬於送審後 `{stage}` 階段）："
                        "goal_mode 會在送審前卡死；請只寫送審前可驗證交付，或移入 `Non-Goals` / repo 政策 / `[SHIP]` / EPIC"
                    )
                    break
    return findings


def extract_kanban_task_id(pr_body: str | None) -> str | None:
    m = KANBAN_TASK_RE.search(pr_body or "")
    return m.group(1) if m else None


def build_goal_context(
    *,
    epic_id: str | None,
    epic_title: str | None,
    epic_body: str | None,
    card_id: str | None = None,
    card_title: str | None = None,
    card_body: str | None = None,
) -> dict | None:
    """Assemble structured goal context passed into `check_pr` / `judge()` for scope-diff verification."""
    if not epic_body and not card_body:
        return None
    epic_secs = parse_epic_sections(epic_body)
    epic_mode, _ = parse_epic_mode(epic_body)
    contract = parse_card_contract(card_body)
    raw_do_not_touch = epic_secs.get("不能碰") or ""
    do_not_touch = raw_do_not_touch[:1000]
    merged_oos = list(dict.fromkeys([*contract["out_of_scope"], *extract_do_not_touch_paths(raw_do_not_touch)]))
    ctx = {
        "epic_id": epic_id,
        "epic_title": epic_title or "",
        "epic_mode": epic_mode,
        "user_said": (epic_secs.get("使用者原話") or "")[:2000],
        "user_decisions": (epic_secs.get("使用者決定") or "")[:1500],
        "epic_goal": (epic_secs.get("目標") or "")[:1000],
        "epic_ac": (epic_secs.get("驗收") or "")[:2500],
        "do_not_touch": do_not_touch,
        "card_id": card_id,
        "card_title": card_title or "",
        "card_body": (card_body or "")[:1500],
        "allowed_scope": contract["allowed_scope"],
        "out_of_scope": merged_oos,
        "non_goals": contract["non_goals"],
    }
    return ctx
