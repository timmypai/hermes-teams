"""devflow repo bind/doctor: one root-profile project <-> one board <-> one repo.

Real projects.db (Hermes schema subset), real board.json and real local git remotes; only the three
Hermes write helpers are replaced by fakes that perform the same writes, so every run is offline.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from test_judgment import KIT, load_devflow

SLUG, REPO = "app", "Owner/App"


class Binding(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copy(KIT / "roles.json", self.root / "roles.json")
        data = self.root / "data"
        self.board = data / "kanban" / "boards" / SLUG
        self.clone = self.board / "repo"
        self.git(self.clone, f"https://github.com/{REPO}.git")
        (self.root / "state").mkdir()
        (self.root / "state" / "repos.json").write_text(json.dumps(
            {SLUG: {"slug": SLUG, "repo": REPO, "path": str(self.clone), "port": 1, "default_branch": "main"}}))
        (self.board / "board.json").write_text(json.dumps({"slug": SLUG, "project_id": None}))
        self.db = data / "projects.db"
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("CREATE TABLE projects (id TEXT, slug TEXT, name TEXT, board_slug TEXT, primary_path TEXT, "
                      "archived INTEGER DEFAULT 0)")
            c.execute("CREATE TABLE project_folders (project_id TEXT, path TEXT)")
        os.environ["DEVFLOW_HERMES_DATA"] = str(data)
        self.addCleanup(os.environ.pop, "DEVFLOW_HERMES_DATA")
        self.df = load_devflow(self.root)
        self.writes: list[str] = []
        self.df.create_project = self.fake_create
        self.df.bind_board = self.fake_bind
        self.df.set_board_project = self.fake_set

    def git(self, path: Path, url: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)

    def project(self, pid, *, board=None, primary=None, folders=(), archived=0):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)", (pid, pid, pid, board, primary, archived))
            c.executemany("INSERT INTO project_folders VALUES (?, ?)", [(pid, str(f)) for f in folders])

    def fake_create(self, slug, repo, path):
        self.writes.append("create")
        self.project("p_new", board=slug, primary=path, folders=[path])

    def fake_bind(self, project_slug, slug):
        self.writes.append("bind-board")
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE projects SET board_slug = ? WHERE slug = ?", (slug, project_slug))

    def fake_set(self, slug, pid):
        self.writes.append("set-board-project")
        meta = json.loads((self.board / "board.json").read_text())
        (self.board / "board.json").write_text(json.dumps({**meta, "project_id": pid}))

    def board_pid(self):
        return json.loads((self.board / "board.json").read_text())["project_id"]

    def stops(self) -> str:
        with self.assertRaises(SystemExit):
            self.df.bind_project(SLUG)
        self.assertEqual([], self.writes, "a stopped binding must not write")
        return self.board_pid()

    def test_no_project_creates_and_links_both_ends_then_rerun_writes_nothing(self):
        self.assertEqual("p_new", self.df.bind_project(SLUG)["id"])
        self.assertEqual(["create", "set-board-project"], self.writes)
        self.assertEqual("p_new", self.board_pid())
        self.writes.clear()
        self.assertEqual([], self.df.bind_project(SLUG)["actions"])
        self.assertEqual([], self.writes)

    def test_existing_project_with_same_remote_elsewhere_stops_instead_of_moving_workdir(self):
        laptop = self.root / "laptop"
        self.git(laptop, f"git@github.com:{REPO.lower()}.git")
        self.project("p_old", primary=str(laptop), folders=[laptop])
        self.assertIsNone(self.stops())

    def test_existing_project_at_clone_is_reused_and_half_binding_completed(self):
        self.project("p_old", primary=str(self.clone), folders=[self.clone])
        self.assertEqual("p_old", self.df.bind_project(SLUG)["id"])
        self.assertEqual(["bind-board", "set-board-project"], self.writes)

    def test_project_already_pointed_at_by_another_board_is_not_shared(self):
        # Half binding elsewhere: board B has project_id=P while P.board_slug is still empty.
        self.project("p_old", primary=str(self.clone), folders=[self.clone])
        other = self.board.parent / "other"
        other.mkdir()
        (other / "board.json").write_text(json.dumps({"slug": "other", "project_id": "p_old"}))
        self.assertIsNone(self.stops())

    def test_archived_board_pointing_at_project_does_not_block(self):
        self.project("p_old", primary=str(self.clone), folders=[self.clone])
        old = self.board.parent / "old"
        old.mkdir()
        (old / "board.json").write_text(json.dumps({"slug": "old", "project_id": "p_old", "archived": True}))
        self.assertEqual("p_old", self.df.bind_project(SLUG)["id"])

    def test_project_bound_to_another_board_is_not_stolen(self):
        self.project("p_old", board="other", primary=str(self.clone), folders=[self.clone])
        self.assertIsNone(self.stops())

    def test_two_candidate_projects_stop(self):
        self.project("p_a", primary=str(self.clone), folders=[self.clone])
        self.project("p_b", board=SLUG)
        self.assertIsNone(self.stops())

    def test_board_pointing_at_archived_project_stops(self):
        self.project("p_gone", board=SLUG, primary=str(self.clone), folders=[self.clone], archived=1)
        self.fake_set(SLUG, "p_gone")
        self.writes.clear()
        self.assertEqual("p_gone", self.stops())

    def test_doctor_reports_without_writing(self):
        with self.assertRaises(SystemExit) as exit_:
            self.df.cmd_repo_doctor(type("A", (), {"slug": SLUG})())
        self.assertEqual(1, exit_.exception.code)
        self.assertEqual([], self.writes)
        self.df.bind_project(SLUG)
        self.writes.clear()
        self.df.cmd_repo_doctor(type("A", (), {"slug": SLUG})())
        self.assertEqual([], self.writes)


if __name__ == "__main__":
    unittest.main()
