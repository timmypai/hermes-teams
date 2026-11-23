"""devflow EPIC acceptance spec, its plan rules, and `check acceptance` against GitHub run metadata.

GitHub (`gh api`, `gh run download`), the board and the Decisions API are in-memory fakes; the module
under test is bin/devflow with DEVFLOW_ROOT pointing at a temporary state directory.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, REPO, load_devflow

EPIC = "t_ep01"
H, M, D = "a" * 40, "m" * 40, "d" * 40
BODY = """\
## 目標
待辦清單

## 驗收
- AC-1 [ui] 使用者可新增待辦
  - S1 新增後重新整理仍存在
    - Given 已登入、清單為空
    - When 輸入「買牛奶」並按「新增」
    - Then 清單只有「買牛奶」
  - S2 [smoke] 首頁顯示清單
    - Given 已登入
    - When 開啟首頁
    - Then 看到標題「待辦」與「新增」按鈕
- AC-2 [cli] 匯出 CSV
  - S1 匯出
    - Given 資料庫有 2 筆訂單
    - When 執行 export --csv
    - Then 第一列是 id,amount
- AC-3 [manual] README 說明安裝步驟
"""
A1S1, A1S2, A2S1 = f"{EPIC}/AC-1.S1", f"{EPIC}/AC-1.S2", f"{EPIC}/AC-2.S1"
ALL = [A1S1, A1S2, A2S1]
PR_BODY = (f"Adds todos.\n\nCovers: {A1S1}, {A1S2}\nCovers: {A2S1}\n"
           f"Already-met: {A1S2} 首頁在這個變更前就已顯示標題與新增按鈕\n")


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-acceptance-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "state").mkdir()
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state" / "repos.json").write_text(json.dumps(
            {"b": {"slug": "b", "repo": REPO, "path": str(self.root / "repo"), "port": 1, "default_branch": "main"}}))
        self.df = load_devflow(self.root)


class Spec(Case):
    def test_types_scenarios_smoke_and_ids(self) -> None:
        spec = self.df.acceptance_spec(EPIC, BODY)
        self.assertEqual([(a["ac"], a["type"], a["text"]) for a in spec],
                         [("AC-1", "ui", "使用者可新增待辦"), ("AC-2", "cli", "匯出 CSV"), ("AC-3", "manual", "README 說明安裝步驟")])
        self.assertEqual([[(s["id"], s["smoke"], s["description"]) for s in a["scenarios"]] for a in spec],
                         [[(A1S1, False, "新增後重新整理仍存在"), (A1S2, True, "首頁顯示清單")], [(A2S1, False, "匯出")], []])
        s = spec[0]["scenarios"][0]
        self.assertEqual((s["given"], s["when"], s["then"]), ("已登入、清單為空", "輸入「買牛奶」並按「新增」", "清單只有「買牛奶」"))
        self.assertTrue(all(self.df.SCENARIO_ID.match(x["id"]) for a in spec for x in a["scenarios"]))
        self.assertEqual([self.df.spec_problems(a) for a in spec], [[], [], []])
        self.assertEqual(self.df.criteria(BODY), [(a["ac"], a["text"]) for a in spec])

    def test_texts_are_normalised_and_hashed(self) -> None:
        spec = self.df.acceptance_spec(EPIC, "- AC-1 [api]   GET  /api/todos\n  - S1   [smoke]  讀取\n"
                                             "    - Given   有 2 筆 \n    -  When GET   /api/todos\n    - Then 回 2 筆\t陣列\n")
        s = spec[0]["scenarios"][0]
        self.assertEqual((spec[0]["text"], s["description"], s["given"], s["when"], s["then"]),
                         ("GET /api/todos", "讀取", "有 2 筆", "GET /api/todos", "回 2 筆 陣列"))
        self.assertTrue(s["smoke"])
        self.assertEqual(s["spec_hash"], hashlib.sha256("有 2 筆\nGET /api/todos\n回 2 筆 陣列".encode()).hexdigest()[:12])

    def test_shape_problems(self) -> None:
        body = ("- AC-1 [ui] a\n  - Given 孤兒\n  - S1 x\n    - Given g\n    - When w\n    - When w2\n"
                "  - S1 again\n    - Given g\n    - When w\n    - Then t\n"
                "- AC-2 b\n- AC-3 [manual] c\n  - S1 y\n    - Given g\n    - When w\n    - Then t\n"
                "- AC-4 [api] d\n## 備註\n- S2 not a scenario\n")
        spec = self.df.acceptance_spec(EPIC, body)
        probs = {a["ac"]: self.df.spec_problems(a) for a in spec}
        self.assertEqual(len(probs["AC-1"]), 4, probs["AC-1"])
        self.assertTrue(any("Given 不在任何情境" in p for p in probs["AC-1"]))
        self.assertTrue(any("多個 When" in p for p in probs["AC-1"]))
        self.assertTrue(any(f"{EPIC}/AC-1.S1 重複" in p for p in probs["AC-1"]))
        self.assertTrue(any(f"{EPIC}/AC-1.S1 缺少 Then" in p for p in probs["AC-1"]))
        self.assertEqual(len(probs["AC-2"]), 2)  # no type tag, no scenario
        self.assertIn("沒有類型標記", probs["AC-2"][0])
        self.assertIn("[manual] 卻有情境", probs["AC-3"][0])
        self.assertIn("沒有任何情境", probs["AC-4"][0])
        self.assertEqual(spec[3]["scenarios"], [])


class PlanRules(Case):
    def findings(self, body: str) -> list[str]:
        cards = {EPIC: {"title": "[EPIC] e", "body": body, "status": "todo", "assignee": "orchestrator",
                        "workspace_kind": "dir", "parents": [], "children": []}}
        return [f for f in self.df.plan_rules("b", EPIC, cards) if "AC-" in f]

    def test_well_formed_spec_only_asks_the_advisor_about_manual(self) -> None:
        self.assertEqual(self.findings(BODY),
                         [f"{EPIC}: AC-3 標為 [manual]，無法自動驗證：請 advisor 確認確實無法寫成情境，並指定證據"])

    def test_spec_problems_are_findings(self) -> None:
        found = self.findings("- AC-1 x\n- AC-2 [api] y\n  - S1 z\n    - Given g\n    - When w\n"
                              "  - S1 z\n    - Given g\n    - When w\n    - Then t\n")
        for want in ("AC-1 沒有類型標記", "AC-1 沒有任何情境", f"{EPIC}/AC-2.S1 缺少 Then", f"{EPIC}/AC-2.S1 重複"):
            self.assertTrue(any(want in f for f in found), (want, found))


class FakeGitHub:
    def __init__(self, df) -> None:
        self.df = df
        self.runs: dict[str, list[dict]] = {}
        self.artifacts: dict[int, dict[str, dict]] = {}
        self.compare: dict[tuple[str, str], str] = {}
        self.calls: list[list[str]] = []
        self.tip = D  # newest commit of the default branch

    def run(self, cmd, gh=None, check=True, cwd=None, input=None):
        self.calls.append(cmd)
        out = None
        if cmd[:2] == ["gh", "api"]:
            path = cmd[2].split("?")[0]
            if path == f"repos/{REPO}":
                out = {"default_branch": "main"}
            elif path == f"repos/{REPO}/commits/main":
                out = {"sha": self.tip}
            elif m := re.fullmatch(rf"repos/{REPO}/actions/workflows/(.+)/runs", path):
                out = {"workflow_runs": self.runs.get(m[1], [])}
            elif m := re.fullmatch(rf"repos/{REPO}/actions/runs/(\d+)/artifacts", path):
                out = {"artifacts": [{"name": n, "expired": False} for n in self.artifacts.get(int(m[1]), {})]}
            elif m := re.fullmatch(rf"repos/{REPO}/compare/(\w+)\.\.\.(\w+)", path):
                out = {"status": self.compare.get((m[1], m[2]), "diverged")}
        elif cmd[:3] == ["gh", "run", "download"]:
            dest = Path(cmd[cmd.index("-D") + 1])
            for name, data in self.artifacts[int(cmd[3])][cmd[cmd.index("-n") + 1]].items():
                f = dest / name
                f.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(data, bytes):
                    f.write_bytes(data)
                else:
                    f.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if out is None:
            raise AssertionError(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")


def wf_run(rid: int, workflow: str, event: str, sha: str, branch: str, attempt: int = 1) -> dict:
    return {"id": rid, "run_number": rid, "run_attempt": attempt, "path": f".github/workflows/{workflow}",
            "event": event, "head_sha": sha, "head_branch": branch, "status": "completed", "conclusion": "success"}


class CheckAcceptance(Case):
    """One merged PR covering every scenario; deploy and integration on D, which contains its merge."""

    def setUp(self) -> None:
        super().setUp()
        self.gh = FakeGitHub(self.df)
        self.df.run = self.gh.run
        self.body = BODY
        self.df.plan_cards = lambda board, epic: {
            EPIC: {"title": "[EPIC] e", "body": self.body, "status": "todo"},
            "t_dev": {"title": "[DEV] d", "status": "done", "result": "README 已寫安裝步驟"}}
        self.pr = {"number": 5, "title": "feat: todos", "state": "MERGED", "headRefOid": H, "mergeCommit": {"oid": M},
                   "body": PR_BODY}
        self.df.pr_meta = lambda repo, n: dict(self.pr)
        self.decided: list[tuple[str, list[str], list[dict]]] = []
        self.answers: dict[str, float] = {}

        def decide(content, questions):
            data = next(p["data"] for p in content if p["type"] == "input_json")
            self.decided.append((data["ac"], [q["name"] for q in questions], content))
            return {q["name"]: {"name": q["name"], "probability": self.answers.get(q["name"], 0.95)} for q in questions}
        self.df.decide = decide
        self.spec = {s["id"]: s for a in self.df.acceptance_spec(EPIC, BODY) for s in a["scenarios"]}
        self.pr_run = wf_run(100, "ci.yml", "pull_request", H, "feat")
        self.deploy_run = wf_run(200, "deploy.yml", "push", D, "main")
        self.integ_run = wf_run(300, "ci.yml", "workflow_dispatch", D, "main")
        self.base = {A1S1: "red", A1S2: "green", A2S1: "red"}  # how each scenario ran on the base
        self.ids = {"pr": list(ALL), "deploy": [A1S2], "integration": list(ALL)}
        self.identity: dict[str, dict] = {}  # kind -> identity overrides
        self.gh.compare[(M, D)] = "ahead"

    def scenario(self, sid: str, how: str = "passed") -> dict:
        s = self.spec[sid]
        steps = [{"phase": ph, "title": f"{ph.capitalize()} {s[ph]}", "status": "passed", "error": ""} for ph in self.df.PHASES]
        if how == "red":
            steps[2].update(status="failed", error="expected 1 item, got 0")
            return {"id": sid, "runner": "playwright", "smoke": s["smoke"], "status": "failed", "steps": steps,
                    "failed_phase": "then", "evidence": []}
        safe = sid.replace("/", "__")
        return {"id": sid, "runner": "playwright", "smoke": s["smoke"], "status": "passed", "steps": steps,
                "failed_phase": None, "evidence": [f"shots/{safe}.png"]}

    def report(self, kind: str, sha: str, r: dict, scenarios: list[dict]) -> dict:
        ident = {"repo": REPO, "head_sha": sha, "run_id": str(r["id"]), "run_attempt": str(r["run_attempt"]),
                 "workflow_ref": "o/r/.github/workflows/ci.yml@refs/heads/x", "image_id": ""}
        return {"version": 1, "kind": kind, "identity": {**ident, **self.identity.get(kind, {})}, "health": "passed",
                "scenarios": scenarios, "problems": []}

    def evidence(self, kind: str, sha: str, r: dict, scenarios: list[dict]) -> dict:
        files = {"acceptance.json": self.report(kind, sha, r, scenarios),
                 "sources/ui/ac.ts": "\n".join(f"scenario('{x}', 'x', {{...}});" for x in (A1S1, A1S2)),
                 "sources/verify.sh": f"ac2_s1_given() {{ seed 2; }}\nscenario {A2S1} \"匯出\" ac2_s1\n"}
        for s in scenarios:
            files[f"shots/{s['id'].replace('/', '__')}.png"] = b"\x89PNG\r\n\x1a\n" + s["id"].encode()
        return files

    def materialize(self) -> None:
        """GitHub state from the fixture fields (tests change the fields, not the files)."""
        pr, dep, integ = self.pr_run, self.deploy_run, self.integ_run
        base_rep = self.report("base", H, pr, [self.scenario(x, "passed" if how == "green" else how)
                                               for x, how in self.base.items()])
        verdicts = {"red": "RED", "green": "GREEN"}
        self.gh.runs.setdefault("ci.yml", [pr, integ])
        self.gh.runs.setdefault("deploy.yml", [dep])
        self.gh.artifacts = {
            pr["id"]: {f"devflow-evidence-pr-{H}-1": self.evidence("pr", H, pr, [self.scenario(x) for x in self.ids["pr"]]),
                       f"devflow-evidence-base-{H}-1": {
                           "acceptance.json": base_rep,
                           "acceptance-base.json": {"identity": base_rep["identity"], "base_sha": "c" * 40, "results": [
                               {"id": x, "verdict": verdicts[how], "failed_phase": None, "error": "", "exempt": None}
                               for x, how in self.base.items()]}}},
            dep["id"]: {f"devflow-evidence-deploy-{dep['head_sha']}-1": self.evidence(
                "deploy", dep["head_sha"], dep, [self.scenario(x) for x in self.ids["deploy"]])},
            integ["id"]: {f"devflow-evidence-integration-{integ['head_sha']}-1": self.evidence(
                "integration", integ["head_sha"], integ, [self.scenario(x) for x in self.ids["integration"]])},
        }

    def check(self, review: bool = False, evidence: list[str] | None = None) -> tuple[int, dict]:
        self.materialize()
        buf = io.StringIO()
        code = 0
        try:
            with redirect_stdout(buf):
                self.df.cmd_check_acceptance(SimpleNamespace(board="b", epic=EPIC, pr=[5], review=review,
                                                             evidence=evidence or [], json=True))
        except SystemExit as e:
            code = int(e.code)
        return code, json.loads(buf.getvalue())

    def rows(self, rep: dict) -> dict[str, dict]:
        return {r["ac"]: r for r in rep["criteria"]}

    def assert_fails(self, acs: list[str], reason: str) -> dict:
        code, rep = self.check()
        rows = self.rows(rep)
        self.assertEqual(code, 1, rep)
        for ac in acs:
            self.assertEqual(rows[ac]["verdict"], "FAIL", rows[ac])
            self.assertTrue(any(reason in x for x in rows[ac]["reasons"]), (reason, rows[ac]["reasons"]))
        decided = [ac for ac, _, _ in self.decided]
        self.assertFalse(set(acs) & set(decided), decided)
        self.assertIn("AC-3", decided)  # [manual] is judged on its own evidence
        return rep

    # --- consistent evidence ------------------------------------------------------------------

    def test_consistent_evidence_passes_with_one_decision_per_ac(self) -> None:
        code, rep = self.check()
        self.assertEqual(code, 0, rep)
        self.assertEqual((rep["mode"], rep["deploy_sha"], rep["problems"]), ("final", D, []))
        rows = self.rows(rep)
        self.assertEqual({ac: r["verdict"] for ac, r in rows.items()}, {"AC-1": "PASS", "AC-2": "PASS", "AC-3": "PASS"})
        self.assertEqual(rows["AC-1"]["scenarios"], [
            {"id": A1S1, "head": "passed", "base": "RED", "deploy": None, "integration": "passed"},
            {"id": A1S2, "head": "passed", "base": "GREEN (Already-met)", "deploy": "passed", "integration": "passed"}])
        self.assertEqual(rows["AC-1"]["probabilities"], {"met": 0.95, "assertions_sufficient": 0.95})
        self.assertEqual([(ac, qs) for ac, qs, _ in self.decided],
                         [("AC-1", ["met", "assertions_sufficient"]), ("AC-2", ["met", "assertions_sufficient"]),
                          ("AC-3", ["met"])])
        ac1 = self.decided[0][2]
        text = json.dumps([p for p in ac1 if p["type"] != "input_image"], ensure_ascii=False)
        self.assertNotIn("AC-2", text)  # only this AC's evidence
        self.assertIn("scenario('t_ep01/AC-1.S1'", text)  # source excerpt
        self.assertEqual(sum(p["type"] == "input_image" for p in ac1), 2)
        self.assertEqual(sum(p["type"] == "input_image" for p in self.decided[1][2]), 1)

    def test_low_probability_fails(self) -> None:
        self.answers = {"assertions_sufficient": 0.4}
        code, rep = self.check()
        self.assertEqual((code, self.rows(rep)["AC-1"]["verdict"]), (1, "FAIL"))

    def test_review_checks_the_open_pr_without_deploy_or_decisions(self) -> None:
        self.pr.update(state="OPEN", mergeCommit=None)
        code, rep = self.check(review=True)
        self.assertEqual(code, 0, rep)
        self.assertEqual((rep["mode"], [r["ac"] for r in rep["criteria"]], self.decided), ("review", ["AC-1", "AC-2"], []))
        self.assertFalse([c for c in self.gh.calls if "deploy.yml" in " ".join(c) or "compare" in " ".join(c)])

    def test_open_pr_fails_the_final_check(self) -> None:
        self.pr.update(state="OPEN", mergeCommit=None)
        self.assert_fails(["AC-1", "AC-2"], "最終驗收需要 MERGED")

    def test_repo_without_deploy_uses_the_latest_successful_integration_run(self) -> None:
        (self.root / "repo" / ".devflow").mkdir(parents=True)
        (self.root / "repo" / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY=false\n")
        self.deploy_run = wf_run(200, "deploy.yml", "push", "f" * 40, "main")  # ignored
        code, rep = self.check()
        self.assertEqual((code, rep["deploy_sha"]), (0, D), rep)
        self.assertEqual(self.rows(rep)["AC-1"]["scenarios"][1]["deploy"], None)

    # --- the deploy that counts is the one of the default branch tip --------------------------

    def test_tip_deployed_by_workflow_dispatch_counts(self) -> None:
        self.deploy_run = wf_run(200, "deploy.yml", "workflow_dispatch", D, "main")
        code, rep = self.check()
        self.assertEqual((code, rep["deploy_sha"]), (0, D), rep)

    def test_failed_deploy_of_the_tip_hides_an_older_success(self) -> None:
        self.gh.tip = "e" * 40
        self.gh.runs["deploy.yml"] = [self.deploy_run, {**wf_run(201, "deploy.yml", "push", "e" * 40, "main"),
                                                        "conclusion": "failure"}]
        self.assert_fails(["AC-1", "AC-2"], "部署 run 201 是 failure")

    def test_running_deploy_of_the_tip_waits(self) -> None:
        self.gh.runs["deploy.yml"] = [{**self.deploy_run, "status": "in_progress", "conclusion": None}]
        self.assert_fails(["AC-1", "AC-2"], "還在 in_progress")

    def test_tip_never_deployed(self) -> None:
        self.gh.tip = "e" * 40
        self.assert_fails(["AC-1", "AC-2"], f"找不到 main 最新 commit {'e' * 12} 的 deploy.yml run")

    # --- deterministic failures: that AC FAILs, no Decisions call for it ----------------------

    def test_ci_run_for_another_head_sha(self) -> None:
        self.pr_run["head_sha"] = "b" * 40
        self.assert_fails(["AC-1", "AC-2"], "找不到 head")

    def test_ci_run_of_another_workflow_file(self) -> None:
        self.pr_run["path"] = ".github/workflows/other.yml"
        self.assert_fails(["AC-1", "AC-2"], "找不到 head")

    def test_artifact_of_an_older_run_attempt(self) -> None:
        self.pr_run["run_attempt"] = 2
        self.assert_fails(["AC-1", "AC-2"], f"沒有 artifact devflow-evidence-pr-{H}-2")

    def test_report_identity_differs_from_github(self) -> None:
        self.identity["pr"] = {"run_id": "999"}
        self.assert_fails(["AC-1", "AC-2"], "identity.run_id='999'")

    def test_epic_step_text_changed_after_the_run(self) -> None:
        self.body = BODY.replace("Then 清單只有「買牛奶」", "Then 清單只有一筆「買牛奶」")
        rep = self.assert_fails(["AC-1"], "then 文字")
        self.assertEqual(self.rows(rep)["AC-2"]["verdict"], "PASS")

    def test_pr_covering_only_another_epics_scenario(self) -> None:
        self.pr["body"] = "Covers: t_other/AC-1.S1\n"
        rep = self.assert_fails(["AC-1", "AC-2"], "沒有 PR 以 Covers: 覆蓋")
        self.assertTrue(any(f"沒有 {EPIC} 的情境" in p for p in rep["problems"]))

    def test_base_green_without_already_met(self) -> None:
        self.base[A2S1] = "green"
        rep = self.assert_fails(["AC-2"], f"沒有 `Already-met: {A2S1}")
        self.assertEqual(self.rows(rep)["AC-1"]["verdict"], "PASS")

    def test_short_already_met_reason_does_not_exempt(self) -> None:
        self.pr["body"] = PR_BODY.replace("首頁在這個變更前就已顯示標題與新增按鈕", "早就有")
        self.assert_fails(["AC-1"], "Already-met")

    def test_merge_commit_not_in_the_integrated_sha(self) -> None:
        self.gh.compare[(M, D)] = "diverged"
        self.assert_fails(["AC-1", "AC-2"], "祖先")

    def test_integration_run_on_another_sha_than_deployed(self) -> None:
        self.integ_run["head_sha"] = "e" * 40
        self.assert_fails(["AC-1", "AC-2"], "workflow_dispatch 整合 run")

    def test_missing_integration_run(self) -> None:
        self.gh.runs["ci.yml"] = [self.pr_run]
        self.assert_fails(["AC-1", "AC-2"], "workflow_dispatch 整合 run")

    def test_smoke_scenario_missing_in_deploy_report(self) -> None:
        self.ids["deploy"] = []
        rep = self.assert_fails(["AC-1"], "deploy 報告沒有這個情境")
        self.assertEqual(self.rows(rep)["AC-2"]["verdict"], "PASS")

    def test_scenario_failed_on_the_integrated_sha(self) -> None:
        self.ids["integration"] = [A1S1, A1S2]
        self.assert_fails(["AC-2"], "integration 報告沒有這個情境")

    # --- [manual] ----------------------------------------------------------------------------

    def test_manual_ac_is_judged_by_decisions_with_handoffs_and_evidence(self) -> None:
        shot = self.root / "readme.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nreadme")
        code, rep = self.check(evidence=[str(shot)])
        self.assertEqual(code, 0, rep)
        self.assertEqual(self.rows(rep)["AC-3"]["scenarios"], [])
        _, qs, content = next(d for d in self.decided if d[0] == "AC-3")
        self.assertEqual(qs, ["met"])
        data = next(p["data"] for p in content if p["type"] == "input_json")
        self.assertEqual(data["handoffs"][0]["result"], "README 已寫安裝步驟")
        self.assertEqual(sum(p["type"] == "input_image" for p in content), 1)

    def test_manual_ac_fails_when_decisions_says_not_met(self) -> None:
        self.answers = {"met": 0.2}
        code, rep = self.check()
        self.assertEqual((code, self.rows(rep)["AC-3"]["verdict"]), (1, "FAIL"))

    def test_unavailable_decisions_is_unjudged_not_a_failure(self) -> None:
        def down(content, questions):
            raise self.df.DecisionsError("HTTP 503")
        self.df.decide = down
        code, rep = self.check()
        self.assertEqual(code, 3, rep)
        self.assertEqual({r["verdict"] for r in rep["criteria"]}, {"UNJUDGED"})

    def test_unjudged_next_to_an_evidence_failure_is_still_a_failure(self) -> None:
        def down(content, questions):
            raise self.df.DecisionsError("HTTP 503")
        self.df.decide = down
        self.pr.update(state="OPEN", mergeCommit=None)
        code, rep = self.check()
        self.assertEqual((code, self.rows(rep)["AC-1"]["verdict"], self.rows(rep)["AC-3"]["verdict"]), (1, "FAIL", "UNJUDGED"))


class SourceExcerpts(Case):
    def write(self, name: str, text: str) -> Path:
        p = self.root / "ev" / "sources" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return self.root / "ev"

    def test_data_driven_spec_sends_assertions_far_below_the_id_table(self) -> None:
        # ids in a spec table; the shared Then assertion sits 80 lines below (seen in a real run: the
        # judge got only the table and refused to rate assertions_sufficient)
        table = "\n".join(f"  {{ id: '{x}' }}," for x in (A1S1, A1S2))
        root = self.write("ui/ac.spec.ts", f"for (const spec of [\n{table}\n]) {{\n" + "  // step\n" * 80
                          + "  expect(text).toBe('requests: 1');\n}\n")
        text = "".join(p["text"] for p in self.df.source_excerpts(root, [A1S1]))
        self.assertIn("expect(text).toBe('requests: 1')", text)

    def test_file_over_budget_falls_back_to_lines_around_the_id(self) -> None:
        root = self.write("verify.sh", "filler\n" * 200 + f"scenario {A2S1} x\n" + "tail\n" * 200)
        text = self.df.source_excerpts(root, [A2S1], budget=1000)[0]["text"]
        self.assertIn(A2S1, text)
        self.assertLess(len(text), 1000)

    def test_files_without_the_ids_are_left_out(self) -> None:
        root = self.write("verify.sh", f"scenario {A2S1} x\n")
        self.assertEqual(self.df.source_excerpts(root, [A1S1]), [])


class CheckAc(Case):
    """D0: draft AC lines before the EPIC exists (no scenarios yet)."""

    DRAFT = "- AC-1 [ui] 在待辦頁輸入「買牛奶」按新增，清單出現「買牛奶」\n- AC-2 [api] GET /api/todos 回 200 與 JSON 陣列\n"

    def setUp(self) -> None:
        super().setUp()
        self.asked: list[str] = []
        self.answers: dict[str, float] = {}

        def decide(content, questions):
            self.asked = [q["name"] for q in questions]
            return {q["name"]: {"probability": self.answers.get(q["name"], 0.1)} for q in questions}
        self.df.decide = decide

    def test_clear_draft_passes_without_scenarios(self) -> None:
        rep = self.df.check_ac(self.DRAFT, "我要能新增待辦")
        self.assertEqual((rep["findings"], rep["note"]), ([], ""))
        self.assertEqual(self.asked, ["vague_1", "vague_2", "uncovered"])

    def test_shape_problems_skip_the_model(self) -> None:
        rep = self.df.check_ac("- AC-1 很好用\n", "")
        self.assertTrue(any("沒有類型標記" in f for f in rep["findings"]), rep)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.df.check_ac("要做一個待辦清單", "")["findings"], ["沒有任何 `AC-n` 驗收條件"])

    def test_vague_and_uncovered_become_questions_for_the_user(self) -> None:
        self.answers = {"vague_2": 0.9, "uncovered": 0.7}
        found = self.df.check_ac(self.DRAFT, "新增待辦，而且要能匯出 CSV")["findings"]
        self.assertTrue(any(f.startswith("AC-2 太模糊") for f in found), found)
        self.assertTrue(any("沒有任何 AC 覆蓋" in f for f in found), found)

    def test_unavailable_decisions_is_a_note_not_a_finding(self) -> None:
        def down(content, questions):
            raise self.df.DecisionsError("timed out")
        self.df.decide = down
        rep = self.df.check_ac(self.DRAFT, "")
        self.assertEqual(rep["findings"], [])
        self.assertIn("timed out", rep["note"])


class PlanUnjudged(Case):
    def test_unavailable_decisions_hands_every_question_to_a_ship_gating_advisor(self) -> None:
        def down(content, questions):
            raise self.df.DecisionsError("HTTP 500")
        self.df.decide = down
        cards = {EPIC: {"title": "[EPIC] e", "body": BODY, "status": "todo", "assignee": "orchestrator",
                        "workspace_kind": "dir", "parents": [], "children": []}}
        opened = []
        self.df.plan_rules = lambda board, epic, cards: []
        self.df.open_advisor_card = lambda board, epic, cards, findings, risky: opened.append((findings, risky)) or "t_adv"
        rep = self.df.check_plan("b", EPIC, advise=True, cards=cards)
        self.assertEqual(len(opened), 1)
        findings, risky = opened[0]
        self.assertTrue(risky)
        self.assertEqual(len(findings), len(self.df.PLAN_QUESTIONS))
        self.assertIn("HTTP 500", rep["note"])
        # Same plan, another failure: the stable finding text is already advised, so no second [ADV].
        self.df.check_plan("b", EPIC, advise=True, cards=cards, advised=frozenset(findings))
        self.assertEqual(len(opened), 1)

    def test_beyond_said_is_asked_only_when_the_epic_keeps_the_users_words(self) -> None:
        asked = []
        self.df.decide = lambda content, qs: asked.append([q["name"] for q in qs]) or {
            q["name"]: {"probability": 0.9 if q["name"] == "beyond_said" else 0.0} for q in qs}
        self.df.plan_rules = lambda board, epic, cards: []
        card = {"title": "[EPIC] e", "status": "todo", "assignee": "orchestrator", "workspace_kind": "dir",
                "parents": [], "children": []}
        plain = self.df.check_plan("b", EPIC, advise=False, cards={EPIC: {**card, "body": BODY}})
        said = self.df.check_plan("b", EPIC, advise=False, cards={EPIC: {**card, "body": f"{BODY}\n## 使用者原話\n部署並登入"}})
        self.assertEqual(["beyond_said" in a for a in asked], [False, True])
        self.assertEqual([any("beyond_said" in f for f in r["findings"]) for r in (plain, said)], [False, True])

    def test_deterministic_schema_findings_do_not_open_an_advisor_only_manual_and_risks_do(self) -> None:
        """#2: deterministic schema errors (body length, missing goal_mode, assignee) stay in findings/EPIC
        comments and never open an Opus [ADV]; [manual] ACs and Decisions risks still open one."""
        self.df.decide = lambda content, qs: {q["name"]: {"probability": 0.0} for q in qs}
        long = self.df.body_problem("SHIP", "[SHIP] s", "x" * 1600)
        schema_other = "t_dev: [DEV] 沒有 goal_mode"
        manual = f"{EPIC}: AC-3 標為 [manual]，無法自動驗證：請 advisor 確認確實無法寫成情境，並指定證據"
        self.df.plan_rules = lambda board, epic, cards: [f"t_ship: {long}", schema_other]
        self.df.readiness_rules = lambda board, cards: []
        opened = []
        self.df.open_advisor_card = lambda board, epic, cards, findings, risky: opened.append(findings) or "t_adv"
        cards = {EPIC: {"title": "[EPIC] e", "body": BODY, "status": "todo", "assignee": "orchestrator", "parents": []}}
        rep = self.df.check_plan("b", EPIC, advise=True, cards=cards)
        self.assertEqual((rep["findings"], opened), ([f"t_ship: {long}", schema_other], []))
        # A [manual] AC finding (advisory) DOES open an [ADV], without dragging the schema errors in.
        self.df.plan_rules = lambda board, epic, cards: [f"t_ship: {long}", schema_other, manual]
        self.df.check_plan("b", EPIC, advise=True, cards=cards)
        self.assertEqual(opened, [[manual]])


class PlanShipGate(Case):
    """An advisor's ok covers the [SHIP] cards it gated, not ones the plan gains later."""

    def setUp(self) -> None:
        super().setUp()
        self.df.plan_rules = lambda board, epic, cards: []
        self.df.readiness_rules = lambda board, cards: []
        self.df.decide = lambda content, qs: {q["name"]: {"name": q["name"], "probability": 0.9 if q["name"] == "irreversible"
                                                          else 0.0} for q in qs}
        self.created: list[str] = []   # idempotency keys
        self.links: list[tuple[str, str]] = []

        def run(cmd, gh=None, check=True, cwd=None, input=None):
            if "create" in cmd:
                self.created.append(cmd[cmd.index("--idempotency-key") + 1])
                return SimpleNamespace(returncode=0, stdout=f"Created t_{len(self.created):08x}\n", stderr="")
            if "link" in cmd:
                self.links.append((cmd[-2], cmd[-1]))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(cmd)
        self.df.run = run

    def plan(self, **ships: str) -> dict[str, dict]:
        cards = {EPIC: {"title": "[EPIC] e", "body": BODY, "status": "todo", "assignee": "orchestrator", "parents": []}}
        for t, status in ships.items():
            cards[t] = {"title": f"[SHIP] {t}", "body": f"EPIC: {EPIC}", "status": status, "assignee": "devops", "parents": []}
        return cards

    def test_a_new_ship_gets_its_own_advisor_and_link(self) -> None:
        rep = self.df.check_plan("b", EPIC, advise=True, cards=self.plan(t_a="todo"))
        self.assertEqual((rep["advisor_card"], self.links), ("t_00000001", [("t_00000001", "t_a")]))
        advised = set(rep["advised"])
        # Advisor said ok; the plan now adds t_b with the same finding text: ask again and gate it.
        rep = self.df.check_plan("b", EPIC, advise=True, cards=self.plan(t_a="todo", t_b="todo"), advised=frozenset(advised))
        self.assertEqual(rep.get("advisor_card"), "t_00000002")
        self.assertNotEqual(self.created[0], self.created[1])
        self.assertIn(("t_00000002", "t_b"), self.links)
        advised |= set(rep["advised"])
        # t_a shipped: nothing new to gate, no third [ADV].
        rep = self.df.check_plan("b", EPIC, advise=True, cards=self.plan(t_a="done", t_b="todo"), advised=frozenset(advised))
        self.assertNotIn("advisor_card", rep)
        self.assertEqual(len(self.created), 2)

    def test_already_linked_ship_is_not_linked_again(self) -> None:
        cards = self.plan(t_a="todo")
        cards["t_a"]["parents"] = ["t_00000001"]
        self.df.check_plan("b", EPIC, advise=True, cards=cards)
        self.assertEqual(self.links, [])

    def test_scope_risks_rearm_for_new_work_cards_and_gate_pending_ships(self) -> None:
        """Items 2 & 3: beyond_said/scope_creep must re-arm when new cards join (not permanently muted after
        first firing) and must gate pending [SHIP] cards; BEYOND_SAID inspects cards too."""
        self.assertIn("cards", self.df.BEYOND_SAID[1])
        self.df.decide = lambda content, qs: {
            q["name"]: {"name": q["name"], "probability": 0.9 if q["name"] == "beyond_said" else 0.0} for q in qs}
        epic_said = {"title": "[EPIC] e", "body": f"{BODY}\n## 使用者原話\n只部署並登入", "status": "todo",
                     "assignee": "orchestrator", "parents": ["t_s1"]}
        cards1 = {
            EPIC: epic_said,
            "t_d1": {"title": "[DEV] d1", "body": f"EPIC: {EPIC}", "status": "ready", "assignee": "developer", "parents": []},
            "t_s1": {"title": "[SHIP] s1", "body": f"EPIC: {EPIC}", "status": "todo", "assignee": "devops", "parents": ["t_d1"]},
        }
        rep1 = self.df.check_plan("b", EPIC, advise=True, cards=cards1)
        self.assertEqual(rep1.get("advisor_card"), "t_00000001")
        self.assertIn(("t_00000001", "t_s1"), self.links)   # beyond_said gates pending [SHIP]
        advised = set(rep1["advised"])
        # Same cards: no duplicate [ADV].
        rep2 = self.df.check_plan("b", EPIC, advise=True, cards=cards1, advised=frozenset(advised))
        self.assertNotIn("advisor_card", rep2)
        # Adding a brand-new work card t_d2 re-arms beyond_said with a distinct idempotency key.
        cards2 = {**cards1, "t_d2": {"title": "[OPS] d2", "body": f"EPIC: {EPIC}", "status": "ready", "assignee": "devops", "parents": []}}
        rep3 = self.df.check_plan("b", EPIC, advise=True, cards=cards2, advised=frozenset(advised))
        self.assertEqual(rep3.get("advisor_card"), "t_00000002")
        self.assertNotEqual(self.created[0], self.created[1])
        self.assertIn(("t_00000002", "t_s1"), self.links)

    def test_actionable_rules_exclude_body_length_and_manual_advisory(self) -> None:
        """Item 1: check_plan separates actionable structural rules from body-length and [manual] reminders."""
        self.df.decide = lambda content, qs: {q["name"]: {"name": q["name"], "probability": 0.0} for q in qs}
        long_msg = f"t_s1: {self.df.body_problem('SHIP', '[SHIP] s1', 'x' * 1600)}"
        manual_msg = f"{EPIC}: AC-3 標為 [manual]，無法自動驗證：請 advisor 確認確實無法寫成情境，並指定證據"
        bad_rule = "t_d1: [DEV] assignee 應為 developer，現在是 devops"
        self.df.plan_rules = lambda board, epic, cards: [long_msg, manual_msg, bad_rule]
        cards = {
            EPIC: {"title": "[EPIC] e", "body": BODY, "status": "todo", "assignee": "orchestrator", "parents": ["t_s1"]},
            "t_s1": {"title": "[SHIP] s1", "body": "x" * 1600, "status": "todo", "assignee": "devops", "parents": []},
        }
        rep = self.df.check_plan("b", EPIC, advise=False, cards=cards)
        self.assertEqual(rep["actionable_rules"], [bad_rule])

    def test_epic_acceptance_spec_hydrates_scenarios_from_repo_scenarios_json_so_epic_card_stays_human_readable(self) -> None:
        """Human-readable EPIC cards list only plain-language AC lines while automated Given/When/Then
        scenarios live in `<repo>/.devflow/scenarios.json`."""
        repo_dir = self.root / "repo" / ".devflow"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / "scenarios.json").write_text(json.dumps({
            f"{EPIC}/AC-1.S1": {
                "description": "登入成功",
                "smoke": True,
                "given": "服務已啟動",
                "when": "輸入正確密碼按登入",
                "then": "進入管理頁面",
            }
        }, ensure_ascii=False))
        human_epic = "## 目標\n部署服務並驗證管理登入。\n\n## 驗收\n- AC-1 [ui] 可開啟管理後台並以密碼登入成功\n- AC-2 [manual] 不影響主機上其他服務\n"
        spec = self.df.epic_acceptance_spec("b", EPIC, human_epic)
        self.assertEqual([self.df.spec_problems(a) for a in spec], [[], []])
        self.assertEqual(len(spec[0]["scenarios"]), 1)
        self.assertEqual(spec[0]["scenarios"][0]["id"], f"{EPIC}/AC-1.S1")
        self.assertTrue(spec[0]["scenarios"][0]["smoke"])
        self.assertEqual(spec[0]["scenarios"][0]["then"], "進入管理頁面")

    def test_body_problem_enforces_600_char_human_readable_limit_excluding_fixed_done_section(self) -> None:
        """Cards are for humans to read and judge (<= 600 chars, excluding fixed `## 本卡完成` boilerplate)."""
        self.assertEqual(self.df.CARD_CHARS, 600)
        short_dev = f"## 目標\n{'白話目標。' * 80}\n\n{self.df.DONE_HEADING}\n{self.df.DONE_TEXT}\n\n## 驗收\n覆蓋：AC-1\nEPIC: {EPIC}"
        self.assertIsNone(self.df.body_problem("DEV", "[DEV] d", short_dev))
        bloated_ship = "## 目標\n" + ("技術細節 " * 130)
        self.assertIsNotNone(self.df.body_problem("SHIP", "[SHIP] s", bloated_ship))


class PrExemption(Case):
    def test_base_exemptions_are_judged(self) -> None:
        asked = []
        self.df.decide = lambda content, qs: asked.extend(q["name"] for q in qs) or {q["name"]: {"probability": 0} for q in qs}
        files = [{"filename": "app.py", "status": "modified", "patch": "+x"}]
        for body in (f"Already-met: {A1S2} 首頁在這個變更前就已存在", f"Base-unverifiable: {A1S1} base 沒有 /api/todos 端點"):
            asked.clear()
            self.df.judge({"title": "feat: x", "body": body}, files)
            self.assertIn("weak_exemption", asked)


if __name__ == "__main__":
    unittest.main()
