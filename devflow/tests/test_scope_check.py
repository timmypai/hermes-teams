"""Declarative artifact-path policy exemptions & `devflow scope check` CLI tests."""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, load_devflow

if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import policy as policy_lib  # noqa: E402
from lib import preflight as preflight_lib  # noqa: E402
from lib import scope as scope_lib  # noqa: E402


class DeclarativeArtifactScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        self.policy = policy_lib.load_policy(KIT, self.roles)

    def test_repo_artifact_paths_uses_policy_defaults_and_env_overrides(self) -> None:
        defaults = policy_lib.repo_artifact_paths({}, self.policy)
        self.assertIn(".devflow/lessons/**", defaults)
        self.assertIn(".devflow/scenarios.json", defaults)
        self.assertEqual(policy_lib.repo_artifact_paths({}), [])
        for off_val in ("none", "off", "false", "0"):
            with self.subTest(off_val=off_val):
                self.assertEqual(
                    policy_lib.repo_artifact_paths({"DEVFLOW_ARTIFACT_PATHS": off_val}, self.policy),
                    [],
                )
        custom = policy_lib.repo_artifact_paths(
            {"DEVFLOW_ARTIFACT_PATHS": "docs/adr/**, .devflow/lessons/**"},
            self.policy,
        )
        self.assertEqual(custom, ["docs/adr/**", ".devflow/lessons/**"])

    def test_classify_and_check_files_remove_hardcoded_lesson_exemption(self) -> None:
        files = [
            "teams_transcription/b_browser_tx.py",
            ".devflow/lessons/L-0022.toml",
            ".devflow/scenarios.json",
        ]
        # Without explicit exempt_paths, lessons/scenarios fail instead of being silently hardcoded
        without_exempt = scope_lib.check_files_against_scope(
            files,
            allowed_scope=["teams_transcription/b_browser_tx.py"],
        )
        self.assertEqual(len(without_exempt), 1, without_exempt)
        self.assertIn(".devflow/lessons/L-0022.toml", without_exempt[0])

        # With declarative artifact defaults, both pass and classify cleanly
        exempt = policy_lib.repo_artifact_paths({}, self.policy)
        self.assertEqual(
            scope_lib.check_files_against_scope(
                files,
                allowed_scope=["teams_transcription/b_browser_tx.py"],
                exempt_paths=exempt,
            ),
            [],
        )
        cls = scope_lib.classify_paths_against_scope(
            [*files, "tests/test_tx.py"],
            allowed_scope=["teams_transcription/b_browser_tx.py"],
            test_paths=policy_lib.repo_test_paths({}, self.policy),
            exempt_paths=exempt,
        )
        self.assertEqual(cls["allowed"], ["teams_transcription/b_browser_tx.py"])
        self.assertEqual(cls["exempt_test"], ["tests/test_tx.py"])
        self.assertEqual(
            cls["exempt_artifact"],
            [".devflow/lessons/L-0022.toml", ".devflow/scenarios.json"],
        )
        self.assertEqual(cls["outside"], [])
        self.assertEqual(cls["out_of_scope"], [])

        # Out-of-Scope / EPIC cannot-touch ALWAYS overrides exempt_paths and test_paths
        oos_cls = scope_lib.classify_paths_against_scope(
            [".devflow/lessons/L-0022.toml", "tests/secret/test_x.py"],
            allowed_scope=["teams_transcription/b_browser_tx.py"],
            out_of_scope=[".devflow/lessons/**", "tests/secret/**"],
            test_paths=["tests/**"],
            exempt_paths=exempt,
        )
        self.assertEqual(
            oos_cls["out_of_scope"],
            [".devflow/lessons/L-0022.toml", "tests/secret/test_x.py"],
        )
        oos_errs = scope_lib.check_files_against_scope(
            [".devflow/lessons/L-0022.toml"],
            allowed_scope=["teams_transcription/b_browser_tx.py"],
            out_of_scope=[".devflow/lessons/**"],
            exempt_paths=exempt,
        )
        self.assertTrue(oos_errs and "Out-of-Scope" in oos_errs[0], oos_errs)


class ScopeCheckCliAndPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-sc-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state").mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".devflow").mkdir()
        (self.repo / ".devflow" / "config.env").write_text("", encoding="utf-8")
        (self.root / "state" / "repos.json").write_text(
            json.dumps({"app": {"repo": "o/r", "path": str(self.repo), "contract": False}}),
            encoding="utf-8",
        )
        self.db = self.root / "data" / "kanban" / "boards" / "app" / "kanban.db"
        self.db.parent.mkdir(parents=True)
        self._old_env = os.environ.get("DEVFLOW_HERMES_DATA")
        os.environ["DEVFLOW_HERMES_DATA"] = str(self.root / "data")
        self.addCleanup(self._restore_env)
        self.df = load_devflow(self.root)
        self.epic_body = "## 目標\ng\n## 驗收\n- AC-1 [lib] ok\n## 不能碰\n- `infra/**`\n"
        self.card_body = (
            f"## 目標\n修復診斷\n"
            f"{self.df.DONE_HEADING}\n{self.df.DONE_TEXT}\n"
            "## 驗收\n覆蓋：AC-1\n"
            "Allowed-Scope: teams_transcription/b_browser_tx.py\n"
            "Out-of-Scope: teams_transcription/secrets/**\n"
            "EPIC: t_ep1\n"
        )
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT)")
            c.execute("INSERT INTO tasks VALUES (?, ?)", ("t_ep1", self.epic_body))
        self.df.kanban = lambda b, cmd, tid, *a: json.dumps(
            {
                "task": {
                    "id": tid,
                    "title": "[DEV] 修復診斷" if tid == "t_dev1" else "[EPIC] e",
                    "body": self.card_body if tid == "t_dev1" else self.epic_body,
                    "status": "running",
                }
            }
        )

    def _restore_env(self) -> None:
        if self._old_env is None:
            os.environ.pop("DEVFLOW_HERMES_DATA", None)
        else:
            os.environ["DEVFLOW_HERMES_DATA"] = self._old_env

    def test_scope_check_cli_allows_exempt_paths_inside_worker_and_rejects_outside_or_oos(self) -> None:
        old_task = os.environ.get("HERMES_KANBAN_TASK")
        os.environ["HERMES_KANBAN_TASK"] = "t_dev1"
        try:
            # 1. Allowed + test_paths + artifact_paths passes with exit 0
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.df.cmd_scope_check(
                    SimpleNamespace(
                        slug="app",
                        task="t_dev1",
                        dir=None,
                        paths=[
                            "teams_transcription/b_browser_tx.py",
                            "tests/test_x.py",
                            ".devflow/lessons/L-0022.toml",
                        ],
                    )
                )
            ok_res = json.loads(buf.getvalue())
            self.assertTrue(ok_res["ok"], ok_res)
            self.assertEqual(ok_res["exempt_artifact"], [".devflow/lessons/L-0022.toml"])
            self.assertEqual(ok_res["exempt_test"], ["tests/test_x.py"])

            # 2. Genuinely outside path fails with exit 1 and names `outside`
            buf = io.StringIO()
            with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
                self.df.cmd_scope_check(
                    SimpleNamespace(
                        slug="app",
                        task="t_dev1",
                        dir=None,
                        paths=[".devflow/lessons/L-0022.toml", "teams_transcription/daemon.py"],
                    )
                )
            self.assertEqual(cm.exception.code, 1)
            bad_res = json.loads(buf.getvalue())
            self.assertFalse(bad_res["ok"])
            self.assertEqual(bad_res["outside"], ["teams_transcription/daemon.py"])

            # 3. Forbidden by EPIC ## 不能碰 (`infra/**`) fails with exit 1 and names `out_of_scope`
            buf = io.StringIO()
            with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
                self.df.cmd_scope_check(
                    SimpleNamespace(
                        slug="app",
                        task="t_dev1",
                        dir=None,
                        paths=["infra/deploy.sh"],
                    )
                )
            self.assertEqual(cm.exception.code, 1)
            oos_res = json.loads(buf.getvalue())
            self.assertFalse(oos_res["ok"])
            self.assertEqual(oos_res["out_of_scope"], ["infra/deploy.sh"])

            # 4. Repo opting out via DEVFLOW_ARTIFACT_PATHS=none rejects `.devflow/lessons/L-0022.toml`
            (self.repo / ".devflow" / "config.env").write_text("DEVFLOW_ARTIFACT_PATHS=none\n", encoding="utf-8")
            buf = io.StringIO()
            with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
                self.df.cmd_scope_check(
                    SimpleNamespace(
                        slug="app",
                        task="t_dev1",
                        dir=None,
                        paths=[".devflow/lessons/L-0022.toml"],
                    )
                )
            self.assertEqual(cm.exception.code, 1)
            opt_res = json.loads(buf.getvalue())
            self.assertFalse(opt_res["ok"])
            self.assertEqual(opt_res["outside"], [".devflow/lessons/L-0022.toml"])
        finally:
            if old_task is None:
                os.environ.pop("HERMES_KANBAN_TASK", None)
            else:
                os.environ["HERMES_KANBAN_TASK"] = old_task

    def test_preflight_honors_default_artifact_paths_and_opt_out(self) -> None:
        wt = self.root / "wt"
        wt.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=wt, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=wt, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=wt, check=True)
        (wt / "teams_transcription").mkdir()
        (wt / "teams_transcription" / "b_browser_tx.py").write_text("x = 1\n", encoding="utf-8")
        (wt / ".devflow" / "lessons").mkdir(parents=True)
        (wt / ".devflow" / "config.env").write_text("", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=wt, check=True)
        (wt / ".devflow" / "lessons" / "L-0022.toml").write_text(
            'id = "L-0022"\ntitle = "t"\nevent = "e"\nroot_cause = "r"\ncategory = "diagnostics"\n'
            '[enforcement]\nkind = "rule"\nref = "teams_transcription/b_browser_tx.py"\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat: add lesson (t_dev1)"], cwd=wt, check=True)

        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        pol = policy_lib.load_policy(self.root, roles)
        ok_res = preflight_lib.run_preflight(
            wt,
            base_ref="HEAD~1",
            policy=pol,
            card_body=self.card_body,
            epic_id="t_ep1",
            epic_body=self.epic_body,
            pr_title="feat: add lesson",
            pr_body="Covers: t_ep1/AC-1.S1\nKanban: t_dev1",
            run_tests=False,
        )
        self.assertEqual(ok_res["checks"].get("scope"), "ok", ok_res)

        (wt / ".devflow" / "config.env").write_text("DEVFLOW_ARTIFACT_PATHS=none\n", encoding="utf-8")
        subprocess.run(["git", "add", ".devflow/config.env"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "--amend", "--no-edit"], cwd=wt, check=True)
        no_res = preflight_lib.run_preflight(
            wt,
            base_ref="HEAD~1",
            policy=pol,
            card_body=self.card_body,
            epic_id="t_ep1",
            epic_body=self.epic_body,
            pr_title="feat: add lesson",
            pr_body="Covers: t_ep1/AC-1.S1\nKanban: t_dev1",
            run_tests=False,
        )
        self.assertEqual(no_res["checks"].get("scope"), "fail", no_res)
        self.assertTrue(any(".devflow/lessons/L-0022.toml" in f for f in no_res["findings"]), no_res)

    def test_check_card_reports_effective_exempt_paths(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_check_card(
                SimpleNamespace(
                    title="[DEV] 診斷修正",
                    body=self.card_body,
                    body_file=None,
                    assignee="developer",
                    workspace="worktree",
                    board="app",
                    parent=[],
                )
            )
        res = json.loads(buf.getvalue())
        self.assertTrue(res["ok"], res)
        self.assertIn("exempt_paths", res)
        self.assertIn(".devflow/lessons/**", res["exempt_paths"]["artifact_paths"])
        self.assertIn("tests/**", res["exempt_paths"]["test_paths"])

    def test_judgment_fingerprint_and_resolve_inputs_bind_artifact_paths(self) -> None:
        pr = {"fingerprint": "abc123def456"}
        fp_default = self.df.judgment_fingerprint(pr, repo_cfg={})
        fp_none = self.df.judgment_fingerprint(pr, repo_cfg={"DEVFLOW_ARTIFACT_PATHS": "none"})
        fp_custom = self.df.judgment_fingerprint(
            pr, repo_cfg={"DEVFLOW_ARTIFACT_PATHS": ".devflow/lessons/**"}
        )
        self.assertEqual(len({fp_default, fp_none, fp_custom}), 3)

        ctx = {"epic_id": "t_ep1", "allowed_scope": ["teams_transcription/b_browser_tx.py"]}
        fp_ctx_default = self.df.judgment_fingerprint(pr, goal_context=ctx, repo_cfg={})
        fp_ctx_none = self.df.judgment_fingerprint(
            pr, goal_context=ctx, repo_cfg={"DEVFLOW_ARTIFACT_PATHS": "none"}
        )
        fp_ctx_custom = self.df.judgment_fingerprint(
            pr, goal_context=ctx, repo_cfg={"DEVFLOW_ARTIFACT_PATHS": ".devflow/lessons/**"}
        )
        self.assertEqual(len({fp_ctx_default, fp_ctx_none, fp_ctx_custom}), 3)

        # resolve_pr_judgment_inputs must reflect config.env DEVFLOW_ARTIFACT_PATHS changes immediately
        pr_full = {
            "number": 25,
            "title": "feat: add lesson",
            "body": "Kanban: t_dev1",
            "headRefOid": "a" * 40,
            "fingerprint": "deadbeef1234",
        }
        _, _, eff_default = self.df.resolve_pr_judgment_inputs("app", "o/r", 25, pr_full)
        (self.repo / ".devflow" / "config.env").write_text("DEVFLOW_ARTIFACT_PATHS=none\n", encoding="utf-8")
        _, cfg_none, eff_none = self.df.resolve_pr_judgment_inputs("app", "o/r", 25, pr_full)
        self.assertEqual(cfg_none.get("DEVFLOW_ARTIFACT_PATHS"), "none")
        self.assertNotEqual(eff_default, eff_none)


if __name__ == "__main__":
    unittest.main()
