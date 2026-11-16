#!/usr/bin/env python3
"""devflow acceptance scenarios: event log, report and CI gates. Stdlib only (Python >= 3.11).

Every EPIC acceptance criterion is proven by scenarios `<EPIC>/AC-<n>.S<m>` with three phases
(Given / When / Then). The runners (.devflow/verify.sh shell helper, .devflow/ui/ac.ts Playwright helper)
append one JSON line per phase to scenario-events.jsonl; this script turns events plus the Playwright
JSON report into the report, and gates on it. Run from the repo root.
Exit 0 = pass, 1 = gate failed, 2 = usage / unreadable input.

  acceptance.py event --id ID --runner shell --phase given|when|then --title T
                      --status passed|failed|not-run [--smoke] [--error TEXT]
      append one event line to $DEVFLOW_EVENTS (default $DEVFLOW_EVIDENCE/scenario-events.jsonl)
  acceptance.py build --kind K [--health passed|failed|none] [--playwright-exit N] [--problem TEXT ...]
      $DEVFLOW_EVIDENCE/scenario-events.jsonl + playwright-report.json -> acceptance.json,
      acceptance-matrix.md and sources/ (the scenario code that ran)
  acceptance.py gate
      fail unless health did not fail, every scenario passed, no problems, and (PR_BODY set) every
      `Covers:` id is in the report
  acceptance.py covers
      print the scenario ids of the PR_BODY `Covers:` lines, space separated
  acceptance.py classify-base --base-sha SHA
      on the base run's report: every covered id must be RED (Given passed, When or Then failed),
      GREEN with `Already-met: <id> <reason>`, or INVALID with `Base-unverifiable: <id> <reason>`;
      writes acceptance-base.json

Environment: DEVFLOW_EVIDENCE (default .devflow/evidence), PR_BODY, identity from DEVFLOW_HEAD_SHA
(else GITHUB_SHA), GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_WORKFLOW_REF, DEVFLOW_IMAGE.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ID_RE = re.compile(r"^(t_[0-9A-Za-z]+)/(AC-\d+)\.(S\d+)$")
SPEC_ID = re.compile(r"^(t_[0-9A-Za-z]+/AC-\d+\.S\d+)(?:\s|$)")
PHASES = ("given", "when", "then")
RUNNERS = ("playwright", "shell")
EVENT_STATUS = ("passed", "failed", "not-run")
KINDS = ("pr", "base", "integration", "push", "deploy", "repro", "local")
HEALTH = ("passed", "failed", "none")
COVERS = re.compile(r"^[ \t]*Covers:[ \t]*(.*?)[ \t]*$", re.M)
EXEMPT = re.compile(r"^[ \t]*(Already-met|Base-unverifiable):[ \t]*(\S*)[ \t]*(.*?)[ \t]*$", re.M)
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MIN_REASON = 10
MAX_ERROR = 2000


def evidence_dir() -> Path:
    return Path(os.environ.get("DEVFLOW_EVIDENCE") or ".devflow/evidence")


def clean(text: str, limit: int = 0) -> str:
    """Valid UTF-8 without terminal colour codes (argv may carry undecodable bytes), optionally cut."""
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raw = text.encode("utf-8", "replace")
    text = ANSI.sub("", raw.decode("utf-8", "replace"))
    return text[:limit] if limit else text


def safe_id(sid: str) -> str:
    return sid.replace("/", "__")


def sort_key(sid: str) -> tuple[str, int, int]:
    m = ID_RE.match(sid)
    assert m
    return m.group(1), int(m.group(2)[3:]), int(m.group(3)[1:])


def pr_body() -> str:
    return os.environ.get("PR_BODY", "").replace("\r\n", "\n").replace("\r", "\n")


def parse_covers(body: str) -> tuple[list[str], list[str]]:
    """(scenario ids in `Covers:` lines, errors). Ids are separated by commas and/or spaces."""
    ids: list[str] = []
    errs: list[str] = []
    for line in COVERS.findall(body):
        tokens = [t for t in re.split(r"[,\s]+", line) if t]
        if not tokens:
            errs.append("empty `Covers:` line")
        for tok in tokens:
            if not ID_RE.match(tok):
                errs.append(f"`Covers: {tok}` is not a scenario id like t_ab12/AC-1.S1")
            elif tok not in ids:
                ids.append(tok)
    return ids, errs


def parse_exemptions(body: str) -> tuple[dict[str, str], list[str]]:
    """({id: Already-met|Base-unverifiable}, errors). A reason of at least 10 characters is required."""
    found: dict[str, str] = {}
    errs: list[str] = []
    for kind, sid, reason in EXEMPT.findall(body):
        if not ID_RE.match(sid):
            errs.append(f"`{kind}: {sid}` does not name a scenario id like t_ab12/AC-1.S1")
        elif len(reason) < MIN_REASON:
            errs.append(f"`{kind}: {sid}` needs a reason of at least {MIN_REASON} characters")
        elif found.get(sid, kind) != kind:
            errs.append(f"{sid}: both Already-met and Base-unverifiable")
        else:
            found[sid] = kind
    return found, errs


# --- event ---

def cmd_event(a: argparse.Namespace) -> int:
    if not ID_RE.match(a.id):
        print(f"acceptance event: '{a.id}' is not a scenario id like t_ab12/AC-1.S1", file=sys.stderr)
        return 2
    path = Path(os.environ.get("DEVFLOW_EVENTS") or evidence_dir() / "scenario-events.jsonl")
    ev = {"id": a.id, "runner": a.runner, "smoke": a.smoke, "phase": a.phase, "title": clean(a.title),
          "status": a.status, "error": clean(a.error, MAX_ERROR)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return 0


# --- build ---

def read_events(path: Path, problems: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not path.is_file():
        return out
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{path.name} line {n}: not JSON")
            continue
        if not isinstance(ev, dict) or not ID_RE.match(str(ev.get("id", ""))):
            problems.append(f"{path.name} line {n}: no valid scenario id")
            continue
        out.setdefault(ev["id"], []).append(ev)
    return out


def run_outcome(test: dict) -> tuple[str, str]:
    """(passed|failed|skipped, error) of one Playwright test (retries are 0: the last result counts)."""
    results = test.get("results") or []
    last = results[-1] if results else {}
    if test.get("status") == "skipped" or last.get("status") == "skipped":
        return "skipped", ""
    if test.get("status") == "expected" and last.get("status") == "passed":
        return "passed", ""
    errors = [last.get("error"), *(last.get("errors") or [])]
    msg = next((str(e["message"]) for e in errors if isinstance(e, dict) and e.get("message")), "")
    return "failed", clean(msg or f"playwright status {last.get('status') or test.get('status')}", MAX_ERROR)


def read_playwright(path: Path, problems: list[str]) -> dict[str, list[dict]] | None:
    """{scenario id: [run]} from the Playwright JSON report; None when there is no report."""
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        problems.append(f"playwright-report.json unreadable: {e}")
        return {}
    for err in report.get("errors") or []:
        msg = err.get("message", "") if isinstance(err, dict) else err
        problems.append("playwright: " + clean(str(msg), 500))
    runs: dict[str, list[dict]] = {}
    stack = list(report.get("suites") or [])
    while stack:
        suite = stack.pop()
        stack.extend(suite.get("suites") or [])
        for spec in suite.get("specs") or []:
            title = str(spec.get("title", ""))
            m = SPEC_ID.match(title)
            tags = {str(t).lstrip("@") for t in spec.get("tags") or []}
            for test in spec.get("tests") or []:
                if not m:
                    problems.append(f"playwright test without a scenario id (define it with scenario() from "
                                    f".devflow/ui/ac.ts): {spec.get('file', '?')}: {title}")
                    continue
                outcome, error = run_outcome(test)
                runs.setdefault(m.group(1), []).append(
                    {"outcome": outcome, "error": error, "smoke": "@smoke" in title.split() or "smoke" in tags})
    return runs


def assemble(sid: str, evs: list[dict], runs: list[dict], ev_dir: Path) -> dict:
    """One report scenario from its phase events and (Playwright) test outcomes."""
    why: list[str] = []
    runners = {e.get("runner") for e in evs}
    if runners - set(RUNNERS):
        why.append(f"unknown runner {sorted(map(str, runners - set(RUNNERS)))}")
    if len(runners) > 1 or (runs and runners - {"playwright"}):
        why.append("defined by more than one runner")
    if len(runs) > 1:
        why.append(f"ran {len(runs)} times (one test per scenario id; duplicate id or several Playwright projects?)")
    seq = [e.get("phase") for e in evs]
    if seq != list(PHASES[: len(seq)]):
        why.append(f"phases recorded as {seq}; want given, when, then, once each and in that order")
    if any(e.get("status") not in EVENT_STATUS for e in evs):
        why.append("unknown phase status")
    steps = []
    for p in PHASES:
        e = next((e for e in evs if e.get("phase") == p), None)
        steps.append({"phase": p, "title": str(e.get("title", "")) if e else "",
                      "status": e.get("status") if e and e.get("status") in EVENT_STATUS else "not-run",
                      "error": clean(str(e.get("error", "")), MAX_ERROR) if e else ""})
    pattern = "".join({"passed": "p", "failed": "f"}.get(s["status"], "n") for s in steps)
    if not why and not re.fullmatch(r"p*f?n*", pattern):
        why.append(f"step results {[s['status'] for s in steps]} are inconsistent (a phase ran after a failure)")
    outcome = runs[0]["outcome"] if len(runs) == 1 else None
    run_error = runs[0]["error"] if len(runs) == 1 else ""

    failed_phase = None
    if outcome == "skipped":
        status, error = "skipped", "skipped in Playwright: a scenario may not be skipped"
    elif why:
        status, error = "structure", "; ".join(why)
    elif "f" in pattern:
        failed_phase = PHASES[pattern.index("f")]
        status, error = "failed", steps[pattern.index("f")]["error"]
    elif pattern == "ppp" and outcome in (None, "passed"):
        status, error = "passed", ""
    elif not evs and outcome == "passed":
        status, error = "structure", "test passed without recording Given/When/Then (use scenario() from .devflow/ui/ac.ts)"
    elif evs and pattern == "nnn":
        status, error = "structure", steps[0]["error"] or "no phase ran"
    elif outcome == "passed":
        status, error = "structure", f"test passed but only {seq} were recorded"
    else:  # the test failed (or the run stopped) outside every phase: timeout, fixture, hook, crash
        failed_phase = "outside"
        status, error = "failed", run_error or f"stopped after {seq or 'nothing'} without a phase failure"

    safe = safe_id(sid)
    candidates = [f"shots/{safe}.png", *(f"logs/{safe}.{p}.txt" for p in PHASES)]
    return {"id": sid, "runner": "playwright" if runs else str(next(iter(runners), "shell")),
            "smoke": any(e.get("smoke") is True for e in evs) or any(r["smoke"] for r in runs),
            "status": status, "steps": steps, "failed_phase": failed_phase,
            "evidence": [c for c in candidates if (ev_dir / c).is_file()], "error": clean(error, MAX_ERROR)}


def image_id() -> str:
    image = os.environ.get("DEVFLOW_IMAGE", "")
    if not image or not shutil.which("docker"):
        return ""
    p = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def identity() -> dict:
    env = os.environ.get
    return {"repo": env("GITHUB_REPOSITORY") or "", "head_sha": env("DEVFLOW_HEAD_SHA") or env("GITHUB_SHA") or "",
            "run_id": env("GITHUB_RUN_ID") or "", "run_attempt": env("GITHUB_RUN_ATTEMPT") or "",
            "workflow_ref": env("GITHUB_WORKFLOW_REF") or "", "image_id": image_id()}


def matrix(report: dict) -> str:
    def cell(v: object) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")

    ident = report["identity"]
    lines = [f"# devflow acceptance: {report['kind']} {ident['head_sha'][:12]}", "",
             f"health: {report['health']}", "",
             "| id | status | failed phase | steps | evidence |", "|---|---|---|---|---|"]
    for s in report["scenarios"]:
        steps = "<br>".join(f"{st['phase'].capitalize()} {cell(st['title'])}: {st['status']}" for st in s["steps"])
        evidence = "<br>".join(f"[{cell(e)}]({e})" for e in s["evidence"])
        smoke = " @smoke" if s["smoke"] else ""
        lines.append(f"| {s['id']}{smoke} | {s['status']} | {s['failed_phase'] or ''} | {steps} | {evidence} |")
    if report["problems"]:
        lines += ["", "## problems", "", *(f"- {cell(p)}" for p in report["problems"])]
    return "\n".join(lines) + "\n"


def copy_sources(ev_dir: Path) -> None:
    """The scenario code that produced this report: .devflow/verify.sh and .devflow/ui/**/*.ts."""
    dest = ev_dir / "sources"
    shutil.rmtree(dest, ignore_errors=True)
    root = Path(".devflow")
    files = [root / "verify.sh"]
    if (root / "ui").is_dir():
        files += sorted(p for p in (root / "ui").rglob("*.ts") if "node_modules" not in p.parts)
    for f in files:
        if f.is_file():
            target = dest / f.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)


def cmd_build(a: argparse.Namespace) -> int:
    ev_dir = evidence_dir()
    ev_dir.mkdir(parents=True, exist_ok=True)
    problems = [clean(p, 500) for p in a.problem]
    events = read_events(ev_dir / "scenario-events.jsonl", problems)
    runs = read_playwright(ev_dir / "playwright-report.json", problems)
    if a.playwright_exit is not None and runs is None:
        problems.append(f"playwright exited {a.playwright_exit} without writing playwright-report.json")
    ids = sorted(set(events) | set(runs or {}), key=sort_key)
    scenarios = [assemble(sid, events.get(sid, []), (runs or {}).get(sid, []), ev_dir) for sid in ids]
    if a.playwright_exit not in (None, 0) and runs is not None:
        if not any(s["runner"] == "playwright" and s["status"] != "passed" for s in scenarios) \
                and not any(p.startswith("playwright") for p in problems):
            problems.append(f"playwright exited {a.playwright_exit} but reported no failing scenario "
                            f"(see the Playwright log)")
    report = {"version": 1, "kind": a.kind, "identity": identity(), "health": a.health,
              "scenarios": scenarios, "problems": problems}
    (ev_dir / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ev_dir / "acceptance-matrix.md").write_text(matrix(report), encoding="utf-8")
    copy_sources(ev_dir)
    counts: dict[str, int] = {}
    for s in scenarios:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    tally = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "0 scenarios"
    print(f"devflow acceptance ({a.kind}): health {a.health}; {tally}; {len(problems)} problem(s) "
          f"-> {ev_dir / 'acceptance.json'}")
    return 0


# --- gates ---

def load_report(ev_dir: Path) -> dict | None:
    path = ev_dir / "acceptance.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"acceptance: cannot read {path}: {e}", file=sys.stderr)
        return None
    return report if isinstance(report, dict) else None


def describe(s: dict) -> str:
    where = f" in {s['failed_phase']}" if s.get("failed_phase") else ""
    error = str(s.get("error") or "").strip().replace("\n", " ")
    return f"{s['id']}: {s['status']}{where}" + (f": {error[:300]}" if error else "")


def cmd_gate(_: argparse.Namespace) -> int:
    report = load_report(evidence_dir())
    if report is None:
        return 2
    scenarios = report.get("scenarios") or []
    fails = []
    if report.get("health") == "failed":
        fails.append("health check failed: the service is not reachable, so no scenario ran against it")
    fails += [describe(s) for s in scenarios if s.get("status") != "passed"]
    fails += [f"problem: {p}" for p in report.get("problems") or []]
    # deploy（只跑 smoke）與 repro（只跑指定情境）本來就是篩過的子集：不比對 PR 的 `Covers:`。
    body = "" if report.get("kind") in ("deploy", "repro") else pr_body()
    covered: list[str] = []
    if body.strip():
        covered, errs = parse_covers(body)
        fails += [f"PR body: {e}" for e in errs]
        present = {s.get("id") for s in scenarios}
        fails += [f"{sid}: listed in `Covers:` but absent from the report (not defined, wrong id, or filtered out)"
                  for sid in covered if sid not in present]
    print(f"devflow acceptance gate ({report.get('kind')}): {len(scenarios)} scenario(s), "
          f"{len(covered)} covered by the PR, health {report.get('health')}: {len(fails)} problem(s)")
    for f in fails:
        print(f"FAIL  {f}")
    return 1 if fails else 0


def cmd_covers(_: argparse.Namespace) -> int:
    ids, errs = parse_covers(pr_body())
    for e in errs:
        print(f"PR body: {e}", file=sys.stderr)
    if errs:
        return 1
    print(" ".join(ids))
    return 0


def cmd_classify_base(a: argparse.Namespace) -> int:
    if not SHA_RE.match(a.base_sha):
        print(f"classify-base: --base-sha must be a commit sha, got '{a.base_sha}'", file=sys.stderr)
        return 2
    ev_dir = evidence_dir()
    report = load_report(ev_dir)
    if report is None:
        return 2
    body = pr_body()
    covered, errs = parse_covers(body)
    exempt, exempt_errs = parse_exemptions(body)
    fails = [f"PR body: {e}" for e in errs + exempt_errs]
    by_id = {s.get("id"): s for s in report.get("scenarios") or []}
    health_failed = report.get("health") == "failed"
    results = []
    for sid in covered:
        s = by_id.get(sid)
        failed_phase = s.get("failed_phase") if s else None
        if health_failed:
            verdict, error = "INVALID", "health check failed on base (service did not come up)"
        elif s is None:
            verdict, error = "INVALID", "absent from the base report (scenario not defined or not run on base)"
        elif s.get("status") == "passed":
            verdict, error = "GREEN", ""
        elif s.get("status") == "failed" and failed_phase in ("when", "then"):
            verdict, error = "RED", str(s.get("error") or "")
        else:
            verdict, error = "INVALID", describe(s)
        declared = exempt.get(sid)
        applies = declared if (verdict, declared) in (("GREEN", "Already-met"), ("INVALID", "Base-unverifiable")) else None
        results.append({"id": sid, "verdict": verdict, "failed_phase": failed_phase,
                        "error": clean(error, MAX_ERROR), "exempt": applies})
        if verdict == "GREEN" and not applies:
            fails.append(f"{sid}: GREEN on base: it passes before this change, so it does not prove the change; "
                         f"make Then assert the new behaviour, or add `Already-met: {sid} <reason>`")
        elif verdict == "INVALID" and not applies:
            fails.append(f"{sid}: INVALID on base ({error or 'no reason'}); the scenario must fail in When or Then "
                         f"with Given passing; fix it, or add `Base-unverifiable: {sid} <reason>`")
        elif declared and not applies:
            print(f"note  {sid}: `{declared}` ignored, verdict is {verdict}")
    out = {"identity": report.get("identity") or {}, "base_sha": a.base_sha, "results": results}
    (ev_dir / "acceptance-base.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"devflow base check ({a.base_sha[:12]}): {len(results)} covered scenario(s), "
          f"health {report.get('health')}: {len(fails)} problem(s)")
    for r in results:
        print(f"{r['verdict']:<7} {r['id']}" + (f" ({r['exempt']})" if r["exempt"] else ""))
    for f in fails:
        print(f"FAIL  {f}")
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="acceptance.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("event", help="append one scenario phase event")
    ev.add_argument("--id", required=True)
    ev.add_argument("--runner", required=True, choices=RUNNERS)
    ev.add_argument("--phase", required=True, choices=PHASES)
    ev.add_argument("--title", required=True)
    ev.add_argument("--status", required=True, choices=EVENT_STATUS)
    ev.add_argument("--smoke", action="store_true")
    ev.add_argument("--error", default="")
    ev.set_defaults(fn=cmd_event)
    b = sub.add_parser("build", help="build acceptance.json from events and the Playwright report")
    b.add_argument("--kind", required=True, choices=KINDS)
    b.add_argument("--health", default="none", choices=HEALTH)
    b.add_argument("--playwright-exit", type=int)
    b.add_argument("--problem", action="append", default=[])
    b.set_defaults(fn=cmd_build)
    sub.add_parser("gate", help="fail unless every scenario passed").set_defaults(fn=cmd_gate)
    sub.add_parser("covers", help="print the PR_BODY Covers: ids").set_defaults(fn=cmd_covers)
    c = sub.add_parser("classify-base", help="red/green check of the base run")
    c.add_argument("--base-sha", required=True)
    c.set_defaults(fn=cmd_classify_base)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
