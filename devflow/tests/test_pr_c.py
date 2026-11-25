"""Regression tests for PR-C:
1. Pinned `mattpocock/skills` commit SHA in `roles.json` + fail-closed `vendor.sync_pinned_git_repo`.
2. `DBUS_SESSION_BUS_ADDRESS=disabled:` in `roles.json` `common_env`.
3. Read-only `devflow stats [slug] [--json]` telemetry CLI across healthy, missing, and corrupt boards.
"""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from test_judgment import KIT, REPO, load_devflow

if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import stats as stats_lib, vendor as vendor_lib  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return p.stdout.strip()


def _create_bare_skill_repo(root: Path) -> tuple[Path, str, str]:
    """Create a local bare git repo with two commits (c1 and c2) containing skills/*/*/SKILL.md."""
    src = root / "upstream-src"
    bare = root / "upstream.git"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Tester")

    s1 = src / "skills" / "engineering" / "tdd" / "SKILL.md"
    s1.parent.mkdir(parents=True)
    s1.write_text("# tdd v1\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "v1")
    c1 = _git(src, "rev-parse", "HEAD")

    s1.write_text("# tdd v2\n", encoding="utf-8")
    s2 = src / "skills" / "productivity" / "grilling" / "SKILL.md"
    s2.parent.mkdir(parents=True)
    s2.write_text("# grilling v2\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "v2")
    c2 = _git(src, "rev-parse", "HEAD")

    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare, c1, c2


class PinnedMattpocockSkills(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-prc-vendor-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_roles_json_pins_valid_sha_and_disables_dbus_session_leak(self) -> None:
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        mp = roles.get("mattpocock_skills") or {}
        self.assertEqual(mp.get("repo"), "https://github.com/mattpocock/skills")
        sha = vendor_lib.validate_pinned_sha(mp.get("sha"))
        self.assertEqual(len(sha), 40)
        self.assertEqual((roles.get("common_env") or {}).get("DBUS_SESSION_BUS_ADDRESS"), "disabled:")

    def test_sync_pinned_git_repo_checks_out_exact_sha_cleans_tampering_and_fails_closed(self) -> None:
        bare, c1, c2 = _create_bare_skill_repo(self.root)
        vendor_dir = self.root / "vendor" / "mattpocock-skills"

        # 1. Fresh checkout pinned specifically to older commit c1 (NOT HEAD c2!)
        logs: list[str] = []
        actual = vendor_lib.sync_pinned_git_repo(vendor_dir, str(bare), c1, log_fn=logs.append)
        self.assertEqual(actual, c1)
        self.assertEqual(_git(vendor_dir, "rev-parse", "HEAD"), c1)
        self.assertIn("# tdd v1", (vendor_dir / "skills" / "engineering" / "tdd" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(sorted(vendor_lib.discover_mattpocock_skills(vendor_dir)), ["tdd"])
        self.assertTrue(any(c1[:12] in m for m in logs))

        # 2. Tamper tracked file + inject untracked file -> re-sync MUST restore exact clean c1
        (vendor_dir / "skills" / "engineering" / "tdd" / "SKILL.md").write_text("# tampered\n", encoding="utf-8")
        (vendor_dir / "skills" / "engineering" / "rogue.txt").write_text("rogue", encoding="utf-8")
        actual2 = vendor_lib.sync_pinned_git_repo(vendor_dir, str(bare), c1)
        self.assertEqual(actual2, c1)
        self.assertEqual((vendor_dir / "skills" / "engineering" / "tdd" / "SKILL.md").read_text(encoding="utf-8"), "# tdd v1\n")
        self.assertFalse((vendor_dir / "skills" / "engineering" / "rogue.txt").exists())

        # 2b. Already-pinned clean-status checkout with ignored injected SKILL.md (via .git/info/exclude)
        #     AND an untracked nested git repo MUST both be purged and never discovered.
        exclude = vendor_dir / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("skills/engineering/rogue/\n", encoding="utf-8")
        ignored_skill = vendor_dir / "skills" / "engineering" / "rogue" / "SKILL.md"
        ignored_skill.parent.mkdir(parents=True, exist_ok=True)
        ignored_skill.write_text("# rogue ignored skill\n", encoding="utf-8")
        # Even before sync, tracked-only discovery must ignore untracked/ignored SKILL.md
        self.assertEqual(sorted(vendor_lib.discover_mattpocock_skills(vendor_dir)), ["tdd"])
        # Plain git status is clean, yet sync_pinned_git_repo MUST still purge the ignored skill
        self.assertEqual(_git(vendor_dir, "status", "--porcelain"), "")
        vendor_lib.sync_pinned_git_repo(vendor_dir, str(bare), c1)
        self.assertFalse(ignored_skill.exists())

        nested_repo = vendor_dir / "skills" / "engineering" / "nested-rogue"
        nested_repo.mkdir(parents=True, exist_ok=True)
        _git(nested_repo, "init", "-q", "-b", "main")
        (nested_repo / "SKILL.md").write_text("# nested git rogue skill\n", encoding="utf-8")
        vendor_lib.sync_pinned_git_repo(vendor_dir, str(bare), c1)
        self.assertFalse(nested_repo.exists())
        self.assertEqual(sorted(vendor_lib.discover_mattpocock_skills(vendor_dir)), ["tdd"])

        # 2c. Tracked SKILL.md tampered under --assume-unchanged AND --skip-worktree (where git status
        #     reports completely clean) MUST be restored and verified against commit c1's exact tree.
        tdd_rel = "skills/engineering/tdd/SKILL.md"
        tdd_path = vendor_dir / tdd_rel
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(index_flag=flag):
                _git(vendor_dir, "update-index", flag, tdd_rel)
                tdd_path.write_text(f"# injected under {flag}\n", encoding="utf-8")
                self.assertEqual(_git(vendor_dir, "status", "--porcelain", "--ignored=matching"), "")
                vendor_lib.sync_pinned_git_repo(vendor_dir, str(bare), c1)
                self.assertEqual(tdd_path.read_text(encoding="utf-8"), "# tdd v1\n")

        # 3. Advance pin from c1 -> c2 fetches and checks out exact c2
        actual3 = vendor_lib.sync_pinned_git_repo(vendor_dir, str(bare), c2)
        self.assertEqual(actual3, c2)
        self.assertEqual(sorted(vendor_lib.discover_mattpocock_skills(vendor_dir)), ["grilling", "tdd"])

        # 4. Invalid/non-40-hex SHA fails closed
        for bad_sha in ("", "main", "HEAD", c1[:12], "z" * 40):
            with self.assertRaises(ValueError):
                vendor_lib.sync_pinned_git_repo(vendor_dir, str(bare), bad_sha)

        # 5. Nonexistent 40-hex SHA fails closed without changing HEAD
        with self.assertRaises(RuntimeError):
            vendor_lib.sync_pinned_git_repo(vendor_dir, str(bare), "0" * 40)
        self.assertEqual(_git(vendor_dir, "rev-parse", "HEAD"), c2)

        # 6. Non-empty non-git directory fails closed without deleting files
        stray = self.root / "stray-dir"
        stray.mkdir()
        (stray / "keep.txt").write_text("important", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            vendor_lib.sync_pinned_git_repo(stray, str(bare), c1)
        self.assertTrue((stray / "keep.txt").is_file())


class ReadOnlyStatsTelemetry(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devflow-prc-stats-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        (self.root / "state").mkdir()
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.repo_a.mkdir()
        self.repo_b.mkdir()
        (self.root / "state" / "repos.json").write_text(json.dumps({
            "alpha": {"slug": "alpha", "repo": REPO, "path": str(self.repo_a), "port": 18100, "default_branch": "main"},
            "beta": {"slug": "beta", "repo": "owner/beta", "path": str(self.repo_b), "port": 18101, "default_branch": "main"},
        }))
        self.df = load_devflow(self.root)
        self.df.BOARDS = self.root / "data" / "kanban" / "boards"

    def _seed_alpha(self) -> Path:
        db = self.df.BOARDS / "alpha" / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as c, c:
            c.executescript("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT,
                    created_by TEXT, result TEXT, created_at INTEGER, completed_at INTEGER
                );
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER
                );
            """)
            c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)", [
                ("t_ep1", "[EPIC] Feature A", "", "done", "orchestrator", "default", None, 100, 500),
                ("t_ep2", "[EPIC] Feature B", "", "todo", "orchestrator", "default", None, 600, None),
                ("t_dev1", "[DEV] Clean first pass", "", "done", "developer", "orchestrator", None, 110, 200),
                ("t_dev2", "[DEV] Needed rework", "", "done", "developer", "orchestrator", None, 210, 350),
                ("t_dev3", "[DEV] Tripped open card", "", "ready", "developer", "devops", None, 610, None),
                ("t_sh1", "[SHIP] Ship A", "", "done", "devops", "orchestrator", None, 360, 490),
                ("t_adj1", "[ADJ] Fix A", "", "done", "orchestrator", "devflow-tick",
                 json.dumps({"summary": "fixed", "metadata": {"root_cause": "missing_scope"}}), 250, 290),
                ("t_adj2", "[ADJ] Fix B", "", "done", "orchestrator", "devflow-tick",
                 json.dumps({"root_cause": "missing_scope"}), 650, 680),
            ])
            c.executemany("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)", [
                ("t_dev2", "changes_requested", "{}", 240),
                ("t_dev3", "changes_requested", "{}", 620),
                ("t_dev3", "changes_requested", "{}", 630),
            ])
        return db

    def test_stats_cli_reports_accurate_metrics_and_stays_read_only(self) -> None:
        db = self._seed_alpha()
        before_bytes = db.read_bytes()

        (self.root / "state" / "pr-judgments.json").write_text(json.dumps({
            f"{REPO}#1": {"findings": [], "needs_human_approval": False},
            f"{REPO}#2": {"findings": ["gate file"], "needs_human_approval": True},
        }), encoding="utf-8")
        (self.root / "state" / "human-approvals.json").write_text(json.dumps({
            "_pending": {
                "req123456789": {"repo": REPO, "pr": 2, "head_sha": "a" * 40},
            }
        }), encoding="utf-8")
        (self.root / "state" / "plan-risk.json").write_text(json.dumps({
            "alpha/t_ep2": {"board": "alpha", "epic": "t_ep2", "rhash": "abc123def456", "status": "pending"},
        }), encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_stats(SimpleNamespace(slug="alpha", json=True))
        rep = json.loads(buf.getvalue())
        self.assertEqual(len(rep["boards"]), 1)
        b = rep["boards"][0]
        self.assertTrue(b["db_available"])
        self.assertEqual(b["epics"], {"total": 2, "done": 1, "open": 1, "archived": 0})
        self.assertEqual(b["cards"]["avg_work_cards_per_epic"], 2.0)  # 4 work cards / 2 epics
        self.assertEqual(b["review"]["pr_cards_completed"], 2)
        self.assertEqual(b["review"]["first_pass_count"], 1)
        self.assertEqual(b["review"]["first_pass_rate"], 0.5)
        self.assertEqual(b["review"]["total_changes_requested"], 3)
        self.assertEqual(b["review"]["currently_tripped_cards"], 1)
        self.assertEqual(b["adjustments"]["total"], 2)
        self.assertEqual(b["adjustments"]["top_root_causes"], [{"root_cause": "missing_scope", "count": 2}])
        self.assertEqual(b["shipping"]["post_hoc_patch_total"], 1)
        self.assertEqual(b["gates"], {
            "pr_judgments_total": 2,
            "pr_judgments_with_findings": 1,
            "pr_judgments_needs_human_approval": 1,
            "pending_human_approvals": 1,
            "pending_plan_risk_gates": 1,
        })
        # Strictly read-only: DB untouched byte-for-byte
        self.assertEqual(db.read_bytes(), before_bytes)

        # Text output across all boards (beta has no DB -> graceful [no db])
        buf_txt = io.StringIO()
        with redirect_stdout(buf_txt):
            self.df.cmd_stats(SimpleNamespace(slug=None, json=False))
        txt = buf_txt.getvalue()
        self.assertIn("alpha", txt)
        self.assertIn("first_pass=50%", txt)
        self.assertIn("missing_scope(2)", txt)
        self.assertIn("beta (owner/beta) [no db]", txt)
        self.assertIn("TOTAL (2 boards)", txt)

    def test_stats_handles_corrupt_db_and_corrupt_state_json_gracefully(self) -> None:
        self._seed_alpha()
        bad_db = self.df.BOARDS / "beta" / "kanban.db"
        bad_db.parent.mkdir(parents=True, exist_ok=True)
        bad_db.write_bytes(b"not a valid sqlite database")
        (self.root / "state" / "pr-judgments.json").write_text("{broken json", encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.df.cmd_stats(SimpleNamespace(slug=None, json=True))
        rep = json.loads(buf.getvalue())
        by_slug = {b["slug"]: b for b in rep["boards"]}
        self.assertTrue(by_slug["alpha"]["db_available"])
        self.assertFalse(by_slug["beta"]["db_available"])
        self.assertIn("sqlite_error", by_slug["beta"].get("error", ""))
        self.assertTrue(any("pr-judgments.json" in w for w in rep["warnings"]))

        # Unknown slug exits 1 cleanly
        with self.assertRaises(SystemExit) as cm:
            self.df.cmd_stats(SimpleNamespace(slug="nonexistent-board", json=True))
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
