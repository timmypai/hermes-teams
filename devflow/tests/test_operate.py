"""`devflow operate`: the download never deletes or overwrites files that were already there."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_judgment import load_devflow  # noqa: E402

KIT = Path(__file__).resolve().parents[1]


class Operate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "root"
        (root / "state").mkdir(parents=True)
        (root / "roles.json").write_text((KIT / "roles.json").read_text())
        (root / "state" / "repos.json").write_text(json.dumps(
            {"demo": {"slug": "demo", "repo": "o/demo", "default_branch": "main", "path": "/x", "port": 18199}}))
        self.df = load_devflow(root)
        sleep = self.df.time.sleep                          # the real time module: restore after the test
        self.df.time.sleep = lambda s: None
        self.addCleanup(setattr, self.df.time, "sleep", sleep)
        self.calls: list[list[str]] = []
        self.req = ""

        def run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[1:3] == ["workflow", "run"]:
                self.req = next(x for x in cmd if x.startswith("request=")).split("=", 1)[1]
            if cmd[1:3] == ["run", "download"]:
                d = Path(cmd[cmd.index("-D") + 1])
                d.mkdir(parents=True, exist_ok=True)
                (d / "result.json").write_text('{"status": 200}')
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        run_info = {"id": 77, "status": "completed", "conclusion": "success", "run_attempt": 1,
                    "html_url": "https://github.com/o/demo/actions/runs/77", "head_sha": "abc"}
        self.df.run = run
        self.df.gh_json = lambda args, **kw: run_info
        self.df.workflow_runs = lambda *a, **kw: [{**run_info, "display_title": f"operate look {self.req}"}]

    def operate(self, dir=None):
        self.df.cmd_operate(argparse.Namespace(slug="demo", op="look", arg=[], args=None, dir=dir, timeout=60))

    def test_a_nonempty_dir_is_refused_before_dispatch_and_kept(self):
        keep = self.tmp / "checkout"
        keep.mkdir()
        (keep / "app.py").write_text("print('mine')")
        with self.assertRaises(SystemExit):
            self.operate(dir=str(keep))
        self.assertEqual((keep / "app.py").read_text(), "print('mine')")
        self.assertEqual(self.calls, [])                    # nothing dispatched to the live service

    def test_an_empty_dir_receives_the_download(self):
        out = self.tmp / "out"
        out.mkdir()
        self.operate(dir=str(out))
        self.assertEqual(json.loads((out / "result.json").read_text()), {"status": 200})

    def test_default_downloads_go_to_a_fresh_dir_each_time(self):
        self.operate()
        self.operate()
        dirs = [Path(c[c.index("-D") + 1]) for c in self.calls if c[1:3] == ["run", "download"]]
        self.assertEqual(len(set(dirs)), 2)
        self.assertTrue(all(d.parent == Path("/tmp/devflow-operate") and (d / "result.json").exists() for d in dirs))


if __name__ == "__main__":
    unittest.main()
