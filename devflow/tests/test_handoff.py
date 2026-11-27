"""Structured handoff schemas, ARCH target_files inheritance, and atomic Allowed-Scope merging."""
from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import handoff as handoff_lib  # noqa: E402
from lib import policy as policy_lib  # noqa: E402
from lib import scope as scope_lib  # noqa: E402
from test_judgment import REPO, load_devflow  # noqa: E402


class HandoffValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        self.policy = policy_lib.load_policy(KIT, self.roles)

    def test_valid_handoffs_pass_all_pipeline_steps(self) -> None:
        cases = {
            "repro": {"root_cause": "timeout on /v1/foo", "evidence": ["logs/1.txt"]},
            "design": {"decisions": ["split queue"], "target_files": ["app/q.py", "app/worker.py"]},
            "implement": {"pr_url": "https://github.com/o/r/pull/1", "head_sha": "a" * 40, "tests": "12 passed"},
            "implement_local": {
                "branch": "wt/t_dev1", "head_sha": "c" * 40, "tests": ["pytest -> pass"], "changed_files": ["app/q.py"],
            },
            "ship": {"merged_sha": "b" * 40},
        }
        for step, meta in cases.items():
            self.assertEqual(handoff_lib.validate_handoff(step, meta, self.policy), [], step)

    def test_implement_local_schema_and_step_override(self) -> None:
        local_meta = {
            "branch": "wt/t_dev1",
            "head_sha": "c" * 40,
            "tests": ["check.sh=pass"],
            "changed_files": ["app/a.py"],
        }
        self.assertEqual(handoff_lib.validate_handoff("implement_local", local_meta, self.policy), [])
        self.assertEqual(handoff_lib.validate_card_handoff("DEV", local_meta, self.policy, step="implement_local"), [])
        self.assertEqual(handoff_lib.validate_card_handoff("OPS", local_meta, self.policy, step="implement_local"), [])
        # Default DEV completion still requires PR metadata
        default_errs = handoff_lib.validate_card_handoff("DEV", local_meta, self.policy)
        self.assertTrue(any("pr_url" in e for e in default_errs), default_errs)
        # Missing tests or changed_files fails implement_local
        no_tests = handoff_lib.validate_handoff("implement_local", {**local_meta, "tests": []}, self.policy)
        self.assertTrue(any("tests" in e for e in no_tests), no_tests)
        no_files = handoff_lib.validate_handoff("implement_local", {**local_meta, "changed_files": []}, self.policy)
        self.assertTrue(any("changed_files" in e for e in no_files), no_files)
        # Other card kinds reject implement_local
        self.assertTrue(handoff_lib.validate_card_handoff("RES", local_meta, self.policy, step="implement_local"))
        self.assertTrue(handoff_lib.validate_card_handoff("SHIP", local_meta, self.policy, step="implement_local"))

    def test_missing_or_empty_required_fields_and_broad_target_files_fail(self) -> None:
        errs = handoff_lib.validate_handoff("design", {"decisions": ["x"], "target_files": []}, self.policy)
        self.assertTrue(any("target_files" in e for e in errs), errs)
        errs2 = handoff_lib.validate_handoff("repro", {"root_cause": ""}, self.policy)
        self.assertTrue(any("root_cause" in e for e in errs2) and any("evidence" in e for e in errs2), errs2)
        errs3 = handoff_lib.validate_handoff("design", {"decisions": ["x"], "target_files": ["**/*"]}, self.policy)
        self.assertTrue(any("萬用" in e or "過寬" in e for e in errs3), errs3)
        self.assertTrue(handoff_lib.validate_handoff("unknown", {}, self.policy))
        self.assertTrue(handoff_lib.validate_handoff("implement", None, self.policy))

    def test_repro_matches_research_skill_and_ship_supports_conditional_required_when(self) -> None:
        repro_meta = {
            "root_cause": "worker skipped lock before writing cursor",
            "evidence": ["https://github.com/o/r/issues/1", "bin/worker.py:42"],
            "recommendation": "acquire per-task lock",
            "findings": ["cursor updated out of order"],
            "sources": ["https://github.com/o/r/issues/1"],
            "risks": ["slightly higher lock contention"],
            "unverified": [],
        }
        self.assertEqual(handoff_lib.validate_card_handoff("RES", repro_meta, self.policy), [])

        # Conditional ship schema: shipped=false requires `reason` (NOT merged_sha); shipped PR requires `merged_sha`
        self.assertEqual(
            handoff_lib.validate_card_handoff("SHIP", {"shipped": False, "reason": "advisor advise revise"}, self.policy),
            [],
        )
        errs_false = handoff_lib.validate_card_handoff("SHIP", {"shipped": False}, self.policy)
        self.assertTrue(any("`reason`" in e for e in errs_false), errs_false)
        self.assertFalse(any("merged_sha" in e for e in errs_false), errs_false)
        self.assertEqual(handoff_lib.validate_card_handoff("SHIP", {"merged_sha": "d" * 40}, self.policy), [])
        errs_empty = handoff_lib.validate_card_handoff("SHIP", {}, self.policy)
        self.assertTrue(any("`merged_sha`" in e for e in errs_empty), errs_empty)


class ArchTargetFileInheritance(unittest.TestCase):
    def test_extracts_target_files_and_flags_missing_downstream_allowed_scope_replay_7cebef22_26a85b2b(self) -> None:
        """Replay t_7cebef22 [ARCH] -> t_26a85b2b [DEV]: missing ARCH target file is caught at planning."""
        arch_meta = {
            "decisions": ["wire state + store"],
            "target_files": [
                "pkg/calendar_client.py",
                "pkg/assistant_store.py",
                "pkg/daemon.py",
            ],
        }
        files = handoff_lib.extract_arch_target_files(arch_meta)
        self.assertEqual(files, ["pkg/calendar_client.py", "pkg/assistant_store.py", "pkg/daemon.py"])

        # [DEV] forgot pkg/daemon.py
        missing = handoff_lib.missing_arch_scope_files(files, ["pkg/calendar_client.py", "pkg/assistant_store.py"])
        self.assertEqual(missing, ["pkg/daemon.py"])

        # Covered via dir glob or exact list
        self.assertEqual(handoff_lib.missing_arch_scope_files(files, ["pkg/**"]), [])

    def test_extracts_legacy_file_plan_dicts_and_strings(self) -> None:
        meta = {
            "file_plan": [
                {"path": "app/a.py", "note": "x"},
                "app/b.py",
            ]
        }
        self.assertEqual(handoff_lib.extract_arch_target_files(meta), ["app/a.py", "app/b.py"])

    def test_test_paths_are_exempted_when_explicitly_provided(self) -> None:
        files = ["app/core.py", "tests/test_core.py"]
        self.assertEqual(handoff_lib.missing_arch_scope_files(files, ["app/**"]), ["tests/test_core.py"])
        self.assertEqual(handoff_lib.missing_arch_scope_files(files, ["app/**"], test_paths=["tests/**"]), [])


class AtomicScopeMerge(unittest.TestCase):
    def test_merge_appends_deduped_globs_in_place(self) -> None:
        body = (
            "## 目標\n修 bug\n\n"
            "## 本卡完成\n完成。\n\n"
            "## 驗收\n覆蓋：AC-1\n"
            "Allowed-Scope: app/a.py\n"
            "Out-of-Scope: secrets/**, docker/seccomp.json\n"
            "EPIC: t_ep1\n"
        )
        new_body, merged = handoff_lib.merge_allowed_scope_into_body(
            body, ["app/b.py", "app/a.py", "app/c/**"]
        )
        self.assertEqual(merged, ["app/a.py", "app/b.py", "app/c/**"])
        self.assertIn("Allowed-Scope: app/a.py, app/b.py, app/c/**\n", new_body)
        self.assertIn("EPIC: t_ep1", new_body)

    def test_merge_adds_allowed_scope_line_when_missing(self) -> None:
        body = "## 目標\nx\n\n## 驗收\n覆蓋：AC-1\nEPIC: t_ep1\n"
        new_body, merged = handoff_lib.merge_allowed_scope_into_body(body, ["app/x.py"])
        self.assertEqual(merged, ["app/x.py"])
        self.assertIn("Allowed-Scope: app/x.py\nEPIC: t_ep1", new_body)

    def test_merge_preserves_leading_dot_on_dotfiles(self) -> None:
        body = "## 目標\nx\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: ./.devflow/verify.sh\nEPIC: t_ep1\n"
        new_body, merged = handoff_lib.merge_allowed_scope_into_body(body, [".github/workflows/ci.yml"])
        self.assertEqual(merged, [".devflow/verify.sh", ".github/workflows/ci.yml"])
        self.assertIn("Allowed-Scope: .devflow/verify.sh, .github/workflows/ci.yml\n", new_body)

    def test_merge_rejects_broad_wildcards_out_of_scope_and_epic_do_not_touch(self) -> None:
        body = (
            "## 目標\nx\n\n## 驗收\n覆蓋：AC-1\n"
            "Allowed-Scope: app/a.py\n"
            "Out-of-Scope: secrets/**, infra/prod.yml\n"
            "EPIC: t_ep1\n"
        )
        epic_body = "## 目標\nx\n## 不能碰\n- `deploy/**` 與 `auth/keys.json` 與 `**/*.env` 與 `**/seccomp*.json` 與 `*.yml` 與 `app/secret*.py`\n"
        for bad in (
            ["*"], ["**/*"], ["."], ["secrets/api.key"], ["infra/**"], ["deploy/compose.yml"], ["auth/keys.json"],
            ["config"], ["config.v1"], ["config/"], ["config/**"], ["docker/**"], ["deploy/**"], ["app/secret_cfg.py"],
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                handoff_lib.merge_allowed_scope_into_body(body, bad, epic_body=epic_body)
        # Disjoint extension-constrained wildcard passes despite **/*.env / *.yml
        _, ok_py = handoff_lib.merge_allowed_scope_into_body(body, ["src/**/*.py"], epic_body=epic_body)
        self.assertIn("src/**/*.py", ok_py)
        # Disjoint concrete file / bare & dotted directories pass against non-leading-wildcard prohibitions
        _, ok_dir = handoff_lib.merge_allowed_scope_into_body(
            body, ["app/other.py", "config", "config.v1"], epic_body="## 目標\nx\n## 不能碰\n- `deploy/**` 與 `auth/keys.json` 與 `app/secret*.py`\n",
        )
        self.assertEqual(ok_dir, ["app/a.py", "app/other.py", "config", "config.v1"])
        with self.assertRaises(ValueError):
            handoff_lib.merge_allowed_scope_into_body(body, [])

    def test_merge_matches_scope_match_glob_crossing_slash_semantics(self) -> None:
        base = "## 目標\nx\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: tests/**\nOut-of-Scope: src/private/**\nEPIC: t_ep1\n"
        # `src/*.py` and bare/dotted directory prefixes overlap `scope.match_glob` and MUST be rejected.
        env_body = "## 目標\nx\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nOut-of-Scope: **/*.env\nEPIC: t_ep1\n"
        for oos_body, epic_body, bad_add in (
            (base, None, ["src/*.py"]),
            (base.replace("src/private/**", "src/private/"), None, ["src/*.py"]),
            (base.replace("src/private/**", "src/private"), None, ["src/*.py"]),
            ("## 目標\nx\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: tests/**\nEPIC: t_ep1\n", "## 目標\nx\n## 不能碰\n- `src/private/**`\n", ["src/*.py"]),
            (base.replace("src/private/**", "src/api/**"), None, ["src/a*.py"]),
            (env_body, None, ["config"]),
            (env_body, None, ["config.v1"]),
            (env_body, None, ["config/"]),
            ("## 目標\nx\n\n## 驗收\n覆蓋：AC-1\nAllowed-Scope: app/**\nEPIC: t_ep1\n", "## 目標\nx\n## 不能碰\n- `**/*.env`\n", ["config.v1"]),
            (base.replace("src/private/**", "src*/private.py"), None, ["src/a*"]),
            (base.replace("src/private/**", "src/?x.py"), None, ["src/**src/a?.py"]),
            (base.replace("src/private/**", "app/[z].py"), None, ["app/[!z-a].py"]),
            (base.replace("src/private/**", "app/[! -~].py"), None, ["app/[! -~]*.py"]),
        ):
            with self.assertRaises(ValueError, msg=f"{oos_body} / {epic_body} + {bad_add}"):
                handoff_lib.merge_allowed_scope_into_body(oos_body, bad_add, epic_body=epic_body)

        # Genuinely disjoint prefixes/extensions or concrete dirs vs non-intersecting prefixes pass
        _, ok1 = handoff_lib.merge_allowed_scope_into_body(base, ["tests/unit/*.py"], epic_body=None)
        self.assertIn("tests/unit/*.py", ok1)
        py_only_oos = base.replace("src/private/**", "src/private/*.py")
        _, ok2 = handoff_lib.merge_allowed_scope_into_body(py_only_oos, ["src/private/*.ts"], epic_body=None)
        self.assertIn("src/private/*.ts", ok2)
        _, ok_ext = handoff_lib.merge_allowed_scope_into_body(
            py_only_oos.replace("src/private/*.py", "src/*.py"), ["src/private/*.ts"], epic_body=None,
        )
        self.assertIn("src/private/*.ts", ok_ext)
        _, ok3 = handoff_lib.merge_allowed_scope_into_body(base, ["src/public*.py"], epic_body=None)
        self.assertIn("src/public*.py", ok3)
        _, ok4 = handoff_lib.merge_allowed_scope_into_body(env_body, ["tests/**/*.py"], epic_body=None)
        self.assertIn("tests/**/*.py", ok4)
        _, ok5 = handoff_lib.merge_allowed_scope_into_body(base, ["config", "config.v1"], epic_body=None)
        self.assertIn("config.v1", ok5)

    def test_globs_overlap_precision_and_zero_false_negatives_property(self) -> None:
        # Tightened precision on genuinely disjoint shapes without violating scope.match_glob soundness
        for a, b in (
            ("app/[a-z]/*.py", "app/secret/x.py"),
            ("src/foo.py", "src/foobar.py"),
            ("src/*_test.py", "src/*_prod.py"),
            ("src/*_test.py", "src/*.ts"),
            ("src/**src/foo.py", "src/*bar.py"),
            ("app/[z-a].py", "app/[z].py"),
        ):
            self.assertFalse(handoff_lib._globs_overlap(a, b), (a, b))
            self.assertFalse(handoff_lib._globs_overlap(b, a), (b, a))
        # Wildcardless `src/secret.py` matches descendants `src/secret.py/...` under scope.match_glob -> sound True
        self.assertTrue(handoff_lib._globs_overlap("src/*_test.py", "src/secret.py"))
        # Slash-spanning, prefix/suffix `**`, descending/empty class & Unicode complement (PR #41 regressions)
        for a, b in (
            ("src/a*", "src*/private.py"),
            ("src/**src/a?.py", "src/?x.py"),
            ("src/**src", "src"),
            (".env", "env"),
            (".devflow/**", "devflow/a.py"),
            ("app/[!z-a].py", "app/[z].py"),
            ("app/[! -~].py", "app/[! -~]*.py"),
        ):
            self.assertTrue(handoff_lib._globs_overlap(a, b), (a, b))
            self.assertTrue(handoff_lib._globs_overlap(b, a), (b, a))

        # Character-class interval parser parity against Python stdlib `fnmatch.fnmatchcase`
        sample_chars = [chr(c) for c in range(32, 127)] + ["中", "é", "\n"]
        for cls_pat in ("[z-a]", "[!z-a]", "[z-ax]", "[!z-az-a]", "[a-z-0]", "[!--]", "[!]]", "[! -~]", "[z-a-b]", "[^a]", "[!^a]", "[]a]"):
            matcher, next_i = handoff_lib._parse_fnmatch_class(cls_pat, 0)
            self.assertEqual(next_i, len(cls_pat), cls_pat)
            for ch in sample_chars:
                got = True if matcher[0] == "any" else any(lo <= ord(ch) <= hi for lo, hi in matcher[1])
                self.assertEqual(got, fnmatch.fnmatchcase(ch, cls_pat), (cls_pat, ch, matcher))

        # Unconditional 0-false-negative property check across 57 globs x 56 paths
        globs = [
            "app/a.py", "app/b.py", "src/foo.py", "src/foobar.py", "src/secret.py",
            "src/*_test.py", "src/*_prod.py", "src/*.py", "src/*.ts", "src/*.rs",
            "src/private", "src/private/", "src/private/**", "src/private/*.py", "src/private/*.ts",
            "src/a*", "src*/private.py", "src/*/private.py",
            "src/**src/a?.py", "src/?x.py", "src/**src", "src/**a.py", "src/**/a.py",
            "src/**src/foo.py", "src/*bar.py",
            "app/[a-z]/*.py", "app/[a-z]*.py", "app/[a-z]", "app/secret/x.py", "app/secret/**", "app/public*.py",
            "app/[!z-a].py", "app/[z].py", "app/[z-a].py", "app/[z-ax].py", "app/[! -~].py", "app/[! -~]*.py",
            "**/*.env", "src/**/*.py", "tests/**/*.py", "config", "config.v1", "config/",
            "a/**/foo.py", "a/*/foo.py", "a/*/b/*.py", "docs/[!x]/*.md", "ops/?/cfg.yml",
            "a[/-]b", "a/**/b/**/c.py", "a/**b/c", "a/**//b",
            ".env", "env", ".devflow/**", "devflow/a.py",
            "*/*.py", "app/*/", "src/api/**", "src/a*.py", "seccomp*.json",
        ]
        paths = [
            "app/a.py", "app/b.py", "app/x/m.py", "app/1/m.py", "app/secret/x.py", "app/secret/x.py/a.py",
            "app/secret/a.py", "app/public_api.py", "app/a", "app/a/b.py",
            "app/z.py", "app/x.py", "app/中.py",
            "src/foo.py", "src/foobar.py", "src/a_test.py", "src/a_prod.py", "src/secret.py",
            "src/secret.py/a_test.py", "src/private", "src/private/a.py", "src/private/sub/b.ts",
            "src/app/private.py", "src/x/private.py",
            "src/ax.py", "src/src/ax.py", "src", "src/a.py", "src/x/a.py", "src/bar.py/src/foo.py",
            "src/api/auth.py", "src/lib.rs", "config", "config/a.env", "config.v1", "config.v1/a.env",
            "a/foo.py", "a/b/foo.py", "a/b/c/foo.py", "a/x/b/y.py", "a/bar_foo.py",
            "a/b", "a-b", "a/b/c.py", "a/x/b/y/c.py", "a/b/c", "a/xb/c", "a/x/b",
            ".env", "env", ".devflow/a.py", "devflow/a.py",
            "docs/y/a.md", "docs/x/a.md", "docs/x/y/a.md", "ops/z/cfg.yml", "ops/a/b/cfg.yml",
            "tests/unit/a.py", "src/nested/mod.py", "a.env", "x/y.py",
        ]
        for p in paths:
            matching = [g for g in globs if scope_lib.match_glob(p, g)]
            for i, g1 in enumerate(matching):
                for g2 in matching[i:]:
                    self.assertTrue(handoff_lib._globs_overlap(g1, g2), (p, g1, g2))
                    self.assertTrue(handoff_lib._globs_overlap(g2, g1), (p, g2, g1))


class BinDevflowHandoffIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-handoff-int-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.hermes = self.root / "hermes"
        self.board_dir = self.hermes / "kanban" / "boards" / "b"
        self.board_dir.mkdir(parents=True)
        repo_dir = self.root / "repo"
        (repo_dir / ".devflow").mkdir(parents=True)
        (repo_dir / ".devflow" / "config.env").write_text("DEVFLOW_DEPLOY=true\n", encoding="utf-8")
        (self.root / "state").mkdir(parents=True)
        (self.root / "state" / "repos.json").write_text(
            json.dumps({"b": {"slug": "b", "repo": REPO, "path": str(repo_dir), "default_branch": "main",
                              "port": 18100, "contract": True}}),
            encoding="utf-8",
        )
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        old_data = os.environ.get("DEVFLOW_HERMES_DATA")
        os.environ["DEVFLOW_HERMES_DATA"] = str(self.hermes)
        self.addCleanup(
            lambda: os.environ.__setitem__("DEVFLOW_HERMES_DATA", old_data)
            if old_data is not None else os.environ.pop("DEVFLOW_HERMES_DATA", None)
        )
        self.df = load_devflow(self.root)
        patcher = mock.patch.object(self.df.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _dev_body(self, scope: str) -> str:
        return (
            f"## 目標\n修流程\n\n## 本卡完成\n{self.df.DONE_TEXT}\n\n"
            f"## 驗收\n覆蓋：AC-1\nAllowed-Scope: {scope}\n"
            "Out-of-Scope: secrets/**\nEPIC: t_ep1\n"
        )

    def test_plan_rules_and_check_card_catch_missing_arch_target_files(self) -> None:
        epic_body = (
            "Mode: deliver\n## 目標\ng\n## 不能碰\n- `deploy/**`\n## 驗收\n- AC-1 [lib] x\n"
            "  - S1 [smoke]\n    - Given a\n    - When b\n    - Then c\n"
        )
        cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo", "assignee": "orchestrator",
                      "workspace_kind": "dir", "parents": ["t_ship1"], "children": [], "goal_mode": False,
                      "body": epic_body},
            "t_arch1": {"id": "t_arch1", "title": "[ARCH] 設計", "status": "done", "assignee": "architect",
                        "workspace_kind": "dir", "parents": [], "children": ["t_dev1"], "goal_mode": False,
                        "body": "EPIC: t_ep1\n",
                        "runs": [{"status": "done", "metadata": {
                            "decisions": ["wire all 3"],
                            "target_files": ["app/store.py", "app/daemon.py", "app/cli.py"],
                        }}]},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] 實作", "status": "todo", "assignee": "developer",
                       "workspace_kind": "worktree", "parents": ["t_arch1"], "children": ["t_ship1"],
                       "goal_mode": True, "completion_contract": REPO,
                       "body": self._dev_body("app/store.py, app/daemon.py")},
            "t_ship1": {"id": "t_ship1", "title": "[SHIP] 出貨", "status": "todo", "assignee": "devops",
                        "workspace_kind": "dir", "parents": ["t_dev1"], "children": ["t_ep1"],
                        "goal_mode": False, "body": "EPIC: t_ep1\n"},
        }
        found = self.df.plan_rules("b", "t_ep1", cards)
        self.assertTrue(any("app/cli.py" in f and "t_arch1" in f for f in found), found)

        # Adding app/cli.py makes plan_rules pass
        cards["t_dev1"]["body"] = self._dev_body("app/store.py, app/daemon.py, app/cli.py")
        self.assertEqual(self.df.plan_rules("b", "t_ep1", cards), [])

        # check card --parent checks the same contract before creating a card
        self.df.kanban = lambda b, cmd, tid, *a: json.dumps({"task": cards[tid], "runs": cards[tid]["runs"]})
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            self.df.cmd_check_card(types.SimpleNamespace(
                board="b", title="[DEV] 實作", body=self._dev_body("app/store.py"),
                body_file=None, assignee="developer", workspace="worktree", parent=["t_arch1"],
            ))
        with contextlib.redirect_stdout(io.StringIO()):
            self.df.cmd_check_card(types.SimpleNamespace(
                board="b", title="[DEV] 實作", body=self._dev_body("app/**"),
                body_file=None, assignee="developer", workspace="worktree", parent=["t_arch1"],
            ))

    def test_empty_or_invalid_arch_handoff_never_silently_bypasses_scope_check(self) -> None:
        epic_body = (
            "Mode: deliver\n## 目標\ng\n## 驗收\n- AC-1 [lib] x\n"
            "  - S1 [smoke]\n    - Given a\n    - When b\n    - Then c\n"
        )
        base_cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo", "assignee": "orchestrator",
                      "workspace_kind": "dir", "parents": ["t_ship1"], "children": [], "goal_mode": False,
                      "body": epic_body},
            "t_arch1": {"id": "t_arch1", "title": "[ARCH] 設計", "status": "done", "assignee": "architect",
                        "workspace_kind": "dir", "parents": [], "children": ["t_dev1"], "goal_mode": False,
                        "body": "EPIC: t_ep1\n", "runs": []},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] 實作", "status": "todo", "assignee": "developer",
                       "workspace_kind": "worktree", "parents": ["t_arch1"], "children": ["t_ship1"],
                       "goal_mode": True, "completion_contract": REPO,
                       "body": self._dev_body("app/store.py")},
            "t_ship1": {"id": "t_ship1", "title": "[SHIP] 出貨", "status": "todo", "assignee": "devops",
                        "workspace_kind": "dir", "parents": ["t_dev1"], "children": ["t_ep1"],
                        "goal_mode": False, "body": "EPIC: t_ep1\n"},
        }
        for bad_runs in (
            [],
            [{"status": "done", "metadata": {}}],
            [{"status": "done", "metadata": {"decisions": ["split"]}}],
            [{"status": "done", "metadata": {"decisions": ["split"], "target_files": ["**/*"]}}],
        ):
            cards = json.loads(json.dumps(base_cards))
            cards["t_arch1"]["runs"] = bad_runs
            found = self.df.plan_rules("b", "t_ep1", cards)
            self.assertTrue(any("t_arch1" in f and "結構化交接" in f for f in found), (bad_runs, found))

            self.df.kanban = lambda b, cmd, tid, *a: json.dumps({"task": cards[tid], "runs": cards[tid]["runs"]})
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                self.df.cmd_check_card(types.SimpleNamespace(
                    board="b", title="[DEV] 實作", body=self._dev_body("app/store.py"),
                    body_file=None, assignee="developer", workspace="worktree", parent=["t_arch1"],
                ))

        # Supported legacy file_plan + decisions passes handoff check and enforces Allowed-Scope
        legacy_cards = json.loads(json.dumps(base_cards))
        legacy_cards["t_arch1"]["runs"] = [
            {"status": "done", "metadata": {"decisions": ["keep"], "file_plan": ["app/store.py", "app/cli.py"]}}
        ]
        found_leg = self.df.plan_rules("b", "t_ep1", legacy_cards)
        self.assertFalse(any("結構化交接" in f for f in found_leg), found_leg)
        self.assertTrue(any("app/cli.py" in f for f in found_leg), found_leg)
        legacy_cards["t_dev1"]["body"] = self._dev_body("app/store.py, app/cli.py")
        self.assertEqual(self.df.plan_rules("b", "t_ep1", legacy_cards), [])

    def test_answer_add_scope_and_cmd_scope_add_atomic_and_fail_closed(self) -> None:
        db = self.board_dir / "kanban.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT, assignee TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
        """)
        epic_body = "## 目標\nx\n## 不能碰\n- `infra/**`\n"
        dev_body = self._dev_body("app/a.py")
        conn.execute("INSERT INTO tasks VALUES ('t_ep1', '[EPIC] e', ?, 'todo', NULL, 'orchestrator')", (epic_body,))
        conn.execute("INSERT INTO tasks VALUES ('t_dev1', '[DEV] d', ?, 'blocked', 'needs_input', 'developer')", (dev_body,))
        conn.execute("INSERT INTO task_events VALUES (5, 't_dev1', 'blocked', '{}')")
        conn.commit()
        conn.close()

        state = {"t_dev1": {"status": "blocked", "assignee": "developer", "body": dev_body}}
        ops_log: list[tuple[str, str]] = []

        def fake_kanban(board, cmd, tid, *args):
            ops_log.append((cmd, " ".join(args)))
            if cmd == "show":
                if tid == "t_ep1":
                    return json.dumps({"task": {"id": "t_ep1", "body": epic_body}, "events": []})
                return json.dumps({"task": dict(state["t_dev1"]), "events": []})
            if cmd == "edit":
                state["t_dev1"]["body"] = args[args.index("--body") + 1]
                return ""
            if cmd == "assign":
                state["t_dev1"]["assignee"] = None if args[0] == "none" else args[0]
                return ""
            if cmd == "unblock":
                state["t_dev1"]["status"] = "ready"
                return ""
            if cmd == "comment":
                return ""
            return ""

        self.df.kanban = fake_kanban
        # Adding Out-of-Scope or EPIC ## 不能碰 path via answer --add-scope fails closed with zero writes
        with self.assertRaises(SystemExit):
            self.df.answer("b", "t_dev1", 5, "核准", "App", add_scope=["infra/deploy.yml"])
        self.assertEqual(state["t_dev1"]["status"], "blocked")
        self.assertEqual([c for c, _ in ops_log if c in ("edit", "unblock", "assign")], [])

        # cmd_scope_add rejects wildcard without writing
        with self.assertRaises(SystemExit):
            self.df.cmd_scope_add(types.SimpleNamespace(slug="b", task="t_dev1", add=["**/*"], note=""))
        self.assertEqual([c for c, _ in ops_log if c == "edit"], [])

        # Valid cmd_scope_add writes atomically
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.df.cmd_scope_add(types.SimpleNamespace(slug="b", task="t_dev1", add=["app/b.py"], note="ok"))
        self.assertIn("app/b.py", state["t_dev1"]["body"])

        # Valid answer --add-scope edits BEFORE unblock/resume
        ops_log.clear()
        self.df.answer("b", "t_dev1", 5, "核准新增", "App", add_scope=["app/c.py"])
        self.assertEqual(state["t_dev1"]["status"], "ready")
        self.assertIn("Allowed-Scope: app/a.py, app/b.py, app/c.py", state["t_dev1"]["body"])
        edit_idx = next(i for i, (c, _) in enumerate(ops_log) if c == "edit")
        unblock_idx = next(i for i, (c, _) in enumerate(ops_log) if c == "unblock")
        self.assertLess(edit_idx, unblock_idx)

        # Non-protected .devflow/verify.sh produces NO protected_warning; .github/workflows/ci.yml & .devflow/** DO preserve leading dot and warn
        buf_v = io.StringIO()
        with contextlib.redirect_stdout(buf_v):
            self.df.cmd_scope_add(types.SimpleNamespace(slug="b", task="t_dev1", add=[".devflow/verify.sh"], note="verify"))
        out_v = json.loads(buf_v.getvalue())
        self.assertNotIn("protected_warning", out_v)

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            self.df.cmd_scope_add(types.SimpleNamespace(slug="b", task="t_dev1", add=[".github/workflows/ci.yml", ".devflow/**"], note="ci"))
        out2 = json.loads(buf2.getvalue())
        self.assertEqual(out2["protected_warning"], [".github/workflows/ci.yml", ".devflow/**"])

    def test_ancestor_arch_chain_step_handoffs_and_cmd_check_handoff(self) -> None:
        epic_body = (
            "Mode: deliver\n## 目標\ng\n## 驗收\n- AC-1 [lib] x\n"
            "  - S1 [smoke]\n    - Given a\n    - When b\n    - Then c\n"
        )
        cards = {
            "t_ep1": {"id": "t_ep1", "title": "[EPIC] e", "status": "todo", "assignee": "orchestrator",
                      "workspace_kind": "dir", "parents": ["t_ship1"], "children": [], "goal_mode": False,
                      "body": epic_body},
            "t_arch1": {"id": "t_arch1", "title": "[ARCH] 設計", "status": "done", "assignee": "architect",
                        "workspace_kind": "dir", "parents": [], "children": ["t_res1"], "goal_mode": False,
                        "body": "EPIC: t_ep1\n",
                        "runs": [{"status": "done", "metadata": {
                            "decisions": ["split a/b"],
                            "target_files": ["app/a.py", "app/b.py"],
                        }}]},
            "t_res1": {"id": "t_res1", "title": "[RES] 追查", "status": "done", "assignee": "researcher",
                       "workspace_kind": "scratch", "parents": ["t_arch1"], "children": ["t_dev1"], "goal_mode": False,
                       "body": "EPIC: t_ep1\n",
                       "runs": [{"status": "done", "metadata": {"root_cause": "race", "evidence": ["log1"]}}]},
            "t_dev1": {"id": "t_dev1", "title": "[DEV] slice1", "status": "todo", "assignee": "developer",
                       "workspace_kind": "worktree", "parents": ["t_res1"], "children": ["t_ship1"],
                       "goal_mode": True, "completion_contract": REPO,
                       "body": self._dev_body("app/a.py")},
            "t_ship1": {"id": "t_ship1", "title": "[SHIP] 出貨1", "status": "done", "assignee": "devops",
                        "workspace_kind": "dir", "parents": ["t_dev1"], "children": ["t_dev2", "t_ep1"],
                        "goal_mode": False, "body": "EPIC: t_ep1\n", "runs": []},
        }
        # [ARCH] -> [RES] -> [DEV] catches missing app/b.py through ancestor traversal
        found = self.df.plan_rules("b", "t_ep1", cards)
        self.assertTrue(any("app/b.py" in f and "t_arch1" in f for f in found), found)

        # check card --parent t_res1 also resolves ancestor [ARCH] t_arch1
        self.df.kanban = lambda b, cmd, tid, *a: json.dumps({"task": cards[tid], "runs": cards[tid].get("runs", [])})
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            self.df.cmd_check_card(types.SimpleNamespace(
                board="b", title="[DEV] 實作", body=self._dev_body("app/a.py"),
                body_file=None, assignee="developer", workspace="worktree", parent=["t_res1"],
            ))

        # Slice-2 [DEV] downstream of [SHIP] must NOT falsely inherit slice-1 [ARCH] target_files
        cards["t_dev1"]["body"] = self._dev_body("app/a.py, app/b.py")
        cards["t_dev1"]["status"] = "done"
        cards["t_dev1"]["runs"] = [{"status": "done", "metadata": {"pr_url": "https://g/1", "head_sha": "a" * 40, "tests": "ok"}}]
        cards["t_dev2"] = {"id": "t_dev2", "title": "[DEV] slice2", "status": "todo", "assignee": "developer",
                           "workspace_kind": "worktree", "parents": ["t_ship1"], "children": ["t_ship2"],
                           "goal_mode": True, "completion_contract": REPO,
                           "body": self._dev_body("other/x.py")}
        cards["t_ship2"] = {"id": "t_ship2", "title": "[SHIP] 出貨2", "status": "todo", "assignee": "devops",
                            "workspace_kind": "dir", "parents": ["t_dev2"], "children": ["t_ep1"],
                            "goal_mode": False, "body": "EPIC: t_ep1\n"}
        cards["t_ep1"]["parents"] = ["t_ship2"]
        self.assertEqual(self.df.plan_rules("b", "t_ep1", cards), [])

        # Empty, null, list, and non-object JSON string completed handoffs fail closed across RES/DEV/SHIP in plan_rules
        for bad_meta in ({}, [], None, "[]", "null", "{bad", '"ok"', ""):
            cards["t_dev1"]["status"] = "todo"
            cards["t_ship1"]["status"] = "todo"
            cards["t_res1"]["runs"] = [{"status": "done", "metadata": bad_meta}]
            bad_found = self.df.plan_rules("b", "t_ep1", cards)
            self.assertTrue(any("t_res1" in f and "交接 metadata" in f for f in bad_found), (bad_meta, bad_found))
            # Also verify empty/non-dict completed metadata on DEV feeding open SHIP, and SHIP feeding open DEV2
            cards["t_res1"]["runs"] = [{"status": "done", "metadata": {"root_cause": "race", "evidence": ["log1"]}}]
            cards["t_dev1"]["body"] = self._dev_body("app/a.py, app/b.py")
            cards["t_dev1"]["status"] = "done"
            cards["t_dev1"]["runs"] = [{"status": "done", "metadata": bad_meta}]
            dev_bad_found = self.df.plan_rules("b", "t_ep1", cards)
            self.assertTrue(any("t_dev1" in f and "交接 metadata" in f for f in dev_bad_found), (bad_meta, dev_bad_found))

            cards["t_dev1"]["runs"] = [{"status": "done", "metadata": {"pr_url": "https://g/1", "head_sha": "a" * 40, "tests": "ok"}}]
            cards["t_ship1"]["status"] = "done"
            cards["t_ship1"]["runs"] = [{"status": "done", "metadata": bad_meta}]
            ship_bad_found = self.df.plan_rules("b", "t_ep1", cards)
            self.assertTrue(any("t_ship1" in f and "交接 metadata" in f for f in ship_bad_found), (bad_meta, ship_bad_found))
        cards["t_ship1"]["runs"] = []

        # Verify SQLite task_runs reader preserves [], {}, null, '[]' identically
        db = self.board_dir / "kanban.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT, assignee TEXT);
            CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, status TEXT, metadata TEXT);
        """)
        for raw_json in ("{}", "[]", "null", '"str"', "{bad"):
            conn.execute("DELETE FROM task_runs")
            conn.execute("INSERT INTO task_runs (task_id, status, metadata) VALUES ('t_res1', 'done', ?)", (raw_json,))
            conn.commit()
            cards_sql = json.loads(json.dumps(cards))
            cards_sql["t_res1"]["runs"] = []
            cards_sql["t_dev1"]["status"] = "todo"
            cards_sql["t_dev1"]["runs"] = []
            sql_found = self.df.plan_rules("b", "t_ep1", cards_sql)
            self.assertTrue(any("t_res1" in f and "交接 metadata" in f for f in sql_found), (raw_json, sql_found))
        conn.close()

        # CLI `cmd_check_handoff` enforces schema strictly via --metadata or card runs
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.df.cmd_check_handoff(types.SimpleNamespace(
                board="b", task="t_dev1", metadata='{"pr_url":"https://g/1","head_sha":"' + "a" * 40 + '","tests":"5 ok"}',
                metadata_file=None,
            ))
        self.assertTrue(json.loads(buf.getvalue())["ok"])
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            cards["t_res1"]["runs"] = [{"status": "done", "metadata": {"root_cause": "partial"}}]
            self.df.cmd_check_handoff(types.SimpleNamespace(
                board="b", task="t_res1", metadata=None, metadata_file=None,
            ))


if __name__ == "__main__":
    unittest.main()
