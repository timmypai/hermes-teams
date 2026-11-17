"""devflow deploy readiness: `repo doctor` / `check plan` read the shared deploy channel back.

GitHub is an in-memory fake behind `run`; the host watcher's request directory is real (mtime =
request age). The module under test is bin/devflow with DEVFLOW_ROOT pointing at a temp directory.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, REPO, load_devflow

CONFIG = "DEVFLOW_APP=b\nDEVFLOW_PORT=18100\nDEVFLOW_SERVICE=app\nDEVFLOW_DEPLOY=true\n"
CONTRACT = [".devflow/config.env", ".devflow/test.sh", ".devflow/verify.sh", ".devflow/deploy.sh",
            ".github/workflows/ci.yml", ".github/workflows/deploy.yml", "deploy/compose.yml", "Dockerfile"]


class FakeGitHub:
    def __init__(self) -> None:
        self.files = {p: "" for p in CONTRACT} | {".devflow/config.env": CONFIG}
        self.required = ["devflow-ci", "devflow-judgment"]
        self.runners = [{"name": "vm-o__r", "status": "online", "labels": [{"name": "self-hosted"}, {"name": "devflow"}]}]
        self.private = False
        self.policy = "all_external_contributors"
        self.rulesets_error = ""   # stderr of a refused `repos/o/r/rulesets` read; "" = readable

    def run(self, cmd, gh=None, check=True, cwd=None, input=None):
        ok = lambda out: SimpleNamespace(returncode=0, stdout=out, stderr="")  # noqa: E731
        url = cmd[2] if cmd[:2] == ["gh", "api"] else ""
        if url.startswith(f"repos/{REPO}/contents/"):
            path = url.removeprefix(f"repos/{REPO}/contents/").split("?")[0]
            if path not in self.files:
                return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404")
            return ok(base64.b64encode(self.files[path].encode()).decode())
        if url == f"repos/{REPO}/git/trees/main?recursive=1":
            return ok(json.dumps({"tree": [{"path": p} for p in self.files]}))
        if url == f"repos/{REPO}/rules/branches/main":
            return ok(json.dumps([{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": c} for c in self.required]}}]))
        if url == f"repos/{REPO}/actions/runners":
            return ok(json.dumps({"runners": self.runners}))
        if url == f"repos/{REPO}/actions/permissions/fork-pr-contributor-approval":
            return ok(json.dumps({"approval_policy": self.policy}))
        if url == f"repos/{REPO}/rulesets":
            return SimpleNamespace(returncode=1 if self.rulesets_error else 0, stdout="" if self.rulesets_error else "[]",
                                   stderr=self.rulesets_error)
        if cmd[:3] == ["gh", "repo", "view"]:
            return ok(json.dumps({"isPrivate": self.private}))
        raise AssertionError(cmd)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-ready-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "state").mkdir()
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state" / "repos.json").write_text(json.dumps(
            {"b": {"slug": "b", "repo": REPO, "path": str(self.root / "repo"), "port": 18100, "default_branch": "main"}}))
        self.df = load_devflow(self.root)
        self.gh = FakeGitHub()
        self.df.run = self.gh.run

    def failing(self) -> dict[str, str]:
        return {i["id"]: i["detail"] for i in self.df.deploy_readiness("b") if not i["ok"]}

    def request(self, minutes_ago: float) -> None:
        req = self.root / "runner" / "requests" / "o__r.json"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text("{}")
        t = time.time() - minutes_ago * 60
        os.utime(req, (t, t))


class Readiness(Case):
    def test_a_ready_repo_passes_every_item(self) -> None:
        items = self.df.deploy_readiness("b")
        self.assertEqual([(i["id"], i["ok"]) for i in items],
                         [(x, True) for x in ("contract-files", "port", "ci-required", "runner-online",
                                              "runner-request", "fork-approval")])

    def test_unprocessed_runner_request_fails_once_stale(self) -> None:
        # 2026-11-17: the host watcher never consumed the request; nothing reported it.
        self.gh.runners = []
        self.request(minutes_ago=2)
        self.assertEqual(list(self.failing()), ["runner-online"])
        self.request(minutes_ago=12)
        failing = self.failing()
        self.assertEqual(list(failing), ["runner-online", "runner-request"])
        self.assertIn("12 分鐘", failing["runner-request"])

    def test_offline_runner_and_settings_read_back(self) -> None:
        self.gh.runners[0]["status"] = "offline"
        self.gh.required = []
        self.gh.policy = "first_time_contributors"
        self.gh.files[".devflow/config.env"] = CONFIG.replace("18100", "18101")
        self.assertEqual(sorted(self.failing()), ["ci-required", "fork-approval", "port", "runner-online"])

    def test_unsupported_plan_exemption_is_read_live_not_from_the_stored_note(self) -> None:
        # repos.json still says "unsupported" from when the repo was private; it is public now.
        state = self.root / "state" / "repos.json"
        repos = json.loads(state.read_text())
        repos["b"]["contract_note"] = self.df.RULES_UNSUPPORTED
        state.write_text(json.dumps(repos))
        self.gh.required = []
        self.assertEqual(list(self.failing()), ["ci-required"])
        self.gh.rulesets_error = "Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)"
        self.assertEqual(self.failing(), {})

    def test_missing_contract_files_are_listed(self) -> None:
        del self.gh.files["Dockerfile"], self.gh.files[".github/workflows/deploy.yml"]
        self.assertIn("Dockerfile, .github/workflows/deploy.yml", self.failing()["contract-files"])

    def test_repo_without_deploy_or_service_needs_no_runner_or_image(self) -> None:
        self.gh.files = {p: "" for p in CONTRACT[:3] + [".github/workflows/ci.yml"]}
        self.gh.files[".devflow/config.env"] = "DEVFLOW_PORT=18100\nDEVFLOW_SERVICE=\nDEVFLOW_DEPLOY=false\n"
        self.gh.runners = []
        self.assertEqual([(i["id"], i["ok"]) for i in self.df.deploy_readiness("b")],
                         [("contract-files", True), ("port", True), ("ci-required", True)])


class PlanRule(Case):
    """A pending [SHIP] on a repo missing contract files or with the wrong port needs an [OPS] PR ahead
    of it naming the items; channel items (runner, fork approval) are `ship merge`'s question instead."""

    def plan(self, ops_body: str | None) -> dict[str, dict]:
        card = lambda title, body, parents: {"title": title, "body": body, "status": "todo", "parents": parents}  # noqa: E731
        cards = {"t_ep": card("[EPIC] e", "", ["t_ship"]), "t_dev": card("[DEV] d", "", []),
                 "t_ship": card("[SHIP] s", "", ["t_dev"])}
        if ops_body is not None:
            cards["t_ops"] = card("[OPS] o", ops_body, [])
            cards["t_dev"]["parents"] = ["t_ops"]
        return cards

    def setUp(self) -> None:
        super().setUp()
        self.gh.runners = []
        self.request(minutes_ago=30)
        del self.gh.files["Dockerfile"]
        self.gh.files[".devflow/config.env"] = CONFIG.replace("18100", "18101")

    def test_no_fixing_ops_card_is_a_finding(self) -> None:
        found = self.df.readiness_rules("b", self.plan(None))
        self.assertEqual(len(found), 1)
        self.assertIn("t_ship: repo 契約未就緒（contract-files, port；", found[0])

    def test_ops_ancestor_must_name_every_failing_item(self) -> None:
        self.assertEqual(self.df.readiness_rules("b", self.plan("補 contract-files、修 port")), [])
        # "report" is not "port"; a missing id keeps the finding.
        self.assertEqual(len(self.df.readiness_rules("b", self.plan("contract-files report"))), 1)

    def test_channel_only_failures_or_no_pending_ship_add_nothing(self) -> None:
        cards = self.plan(None)
        cards["t_ship"]["status"] = "done"
        self.assertEqual(self.df.readiness_rules("b", cards), [])
        self.gh.files["Dockerfile"] = ""
        self.gh.files[".devflow/config.env"] = CONFIG
        self.assertEqual(sorted(self.failing()), ["runner-online", "runner-request"])
        self.assertEqual(self.df.readiness_rules("b", self.plan(None)), [])


if __name__ == "__main__":
    unittest.main()
