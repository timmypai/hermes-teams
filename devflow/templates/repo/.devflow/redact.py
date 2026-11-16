#!/usr/bin/env python3
"""遮罩診斷檔中的秘密（盡力而為；根本做法仍是應用程式不要記錄秘密）。

用法：redact.py <env 檔> <檔案或目錄>...
- env 檔（部署秘密來源 deploy/.env）中長度 >= 8 的值逐字遮罩。
- 常見憑證樣式以 regex 遮罩：Bearer/Basic、JWT、URL 帳密、私鑰、key=value 型的密碼/token/cookie。
- 每行截斷到 4000 字元。
任何檔案處理失敗即以非 0 結束，呼叫端應刪除未遮罩的輸出（fail closed）。
"""
import re
import sys
from pathlib import Path

MASK = "<REDACTED>"
MIN_LITERAL = 8
MAX_LINE = 4000

PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S), MASK),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"), MASK),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + MASK + "@"),
    (re.compile(
        r"""(?ix)
        ((?:authorization|set-cookie|cookie|x-api-key|api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret
           |secret|password|passwd|pwd|access[_-]?token|refresh[_-]?token|token|session[_-]?id|private[_-]?key)
         ["']?\s*[:=]\s*["']?)
        [^\s"',;&}]+"""), r"\1" + MASK),
]


def env_literals(path: Path) -> list[str]:
    if not path.is_file():
        return []
    values = set()
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if len(value) >= MIN_LITERAL:
            values.add(value)
    return sorted(values, key=len, reverse=True)


def redact(text: str, literals: list[str]) -> str:
    for value in literals:
        text = text.replace(value, MASK)
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return "\n".join(line if len(line) <= MAX_LINE else line[:MAX_LINE] + " …[truncated]"
                     for line in text.split("\n"))


def files(targets: list[str]):
    for target in map(Path, targets):
        if target.is_dir():
            yield from (p for p in sorted(target.rglob("*")) if p.is_file())
        elif target.is_file():
            yield target


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    literals = env_literals(Path(sys.argv[1]))
    for path in files(sys.argv[2:]):
        path.write_text(redact(path.read_text(errors="replace"), literals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
