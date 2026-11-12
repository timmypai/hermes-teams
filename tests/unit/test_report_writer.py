"""Behavior tests using real wrapper subprocesses and workspace-local fixtures."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ReportWriterTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get('WP06_REPORT_TEST_EVIDENCE')
        parent = Path(evidence) if evidence else ROOT / 'reports'
        parent.mkdir(parents=True, exist_ok=True)
        if evidence:
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + '-', dir=parent))
        else:
            temporary = tempfile.TemporaryDirectory(dir=parent)
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)
        self.root = self.root.resolve()
        for directory in ('scripts/ci', 'tests/unit', 'src', 'reports/out'):
            (self.root / directory).mkdir(parents=True)
        for name in ('run.py', 'check.sh', 'test.sh'):
            shutil.copyfile(ROOT / 'scripts/ci' / name, self.root / 'scripts/ci' / name)
        self.fixture(True)
        self.sequence = 0

    def fixture(self, passes):
        (self.root / 'tests/unit/test_fixture.py').write_text(
            'import unittest\nclass Fixture(unittest.TestCase):\n'
            '    def test_real(self):\n'
            '        print("WP06_FIXTURE_REACHED")\n'
            f'        self.assertTrue({passes!r}, "WP06_INTENTIONAL_FAILURE")\n')

    def invoke(self, command, report='reports/out'):
        argv = ['sh', str(self.root / 'scripts/ci' / (command + '.sh')),
                '--report-dir', report]
        run = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=30)
        result = self.root / report / 'result.json'
        # Never follow rejected symlinks for evidence collection.
        raw = result.read_text() if result.is_file() and not result.is_symlink() else None
        record = {'argv': argv, 'cwd': str(self.root), 'exit': run.returncode,
                  'stdout': run.stdout, 'stderr': run.stderr, 'report_bytes': raw,
                  'runner_sha256': hashlib.sha256((self.root / 'scripts/ci/run.py').read_bytes()).hexdigest()}
        self.sequence += 1
        (self.root / f'execution-{self.sequence}.json').write_text(json.dumps(record, indent=2) + '\n')
        return run, json.loads(raw) if raw else None

    def hardlink(self, command):
        sentinel = self.root / 'sentinel.txt'  # Outside reports, inside own fixture.
        sentinel.write_bytes(b'WP06_SENTINEL_UNCHANGED\n')
        target = self.root / 'reports/out/result.json'
        target.hardlink_to(sentinel)
        run, report = self.invoke(command)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(report['exit'], 0)
        self.assertIn('WP06_FIXTURE_REACHED' if command == 'test' else 'syntax OK', run.stdout)
        self.assertEqual(sentinel.read_bytes(), b'WP06_SENTINEL_UNCHANGED\n')
        self.assertNotEqual(target.stat().st_ino, sentinel.stat().st_ino)

    def test_check_preserves_hardlink_sentinel(self):
        self.hardlink('check')

    def test_test_preserves_hardlink_sentinel(self):
        self.hardlink('test')

    def test_repeated_reports_and_real_command_failures(self):
        for command in ('check', 'test'):
            with self.subTest(command=command):
                self.fixture(True)
                for _ in range(2):
                    run, report = self.invoke(command)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(report['exit'], run.returncode)
                if command == 'test':
                    self.fixture(False)
                    marker = 'WP06_INTENTIONAL_FAILURE'
                else:
                    (self.root / 'src/broken.py').write_text('def broken(\n')
                    marker = 'broken.py'
                run, report = self.invoke(command)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(report['exit'], run.returncode)
                self.assertIn(marker, run.stdout)
                if command == 'check':
                    (self.root / 'src/broken.py').unlink()

    def test_report_replacement_failure_is_nonzero(self):
        target = self.root / 'reports/out/result.json'
        target.mkdir()
        (target / 'keep').write_text('unchanged')
        for command in ('check', 'test'):
            with self.subTest(command=command):
                run, report = self.invoke(command)
                self.assertNotEqual(run.returncode, 0)
                self.assertIsNone(report)
                self.assertEqual((target / 'keep').read_text(), 'unchanged')
                self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ['result.json'])

    def test_report_directory_creation_failure_is_nonzero(self):
        (self.root / 'reports/file').write_text('unchanged')
        for command in ('check', 'test'):
            with self.subTest(command=command):
                run, _ = self.invoke(command, 'reports/file/child')
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual((self.root / 'reports/file').read_text(), 'unchanged')

    def test_rejects_traversal_root_and_symlink_targets(self):
        sentinel = self.root / 'sentinel.txt'
        sentinel.write_text('unchanged')
        (self.root / 'outside').mkdir()
        (self.root / 'reports/link').symlink_to(self.root / 'outside', target_is_directory=True)
        (self.root / 'reports/out/result.json').symlink_to(sentinel)
        for command in ('check', 'test'):
            for path in ('reports', 'reports/../../escape', 'reports/link', 'reports/out'):
                with self.subTest(command=command, path=path):
                    run, _ = self.invoke(command, path)
                    self.assertEqual(run.returncode, 2, run.stderr)
                    self.assertEqual(sentinel.read_text(), 'unchanged')
        self.assertEqual(list((self.root / 'outside').iterdir()), [])

    def test_rejects_reports_root_symlink(self):
        shutil.rmtree(self.root / 'reports')
        (self.root / 'outside').mkdir()
        (self.root / 'reports').symlink_to(self.root / 'outside', target_is_directory=True)
        for command in ('check', 'test'):
            with self.subTest(command=command):
                run, _ = self.invoke(command)
                self.assertEqual(run.returncode, 2, run.stderr)
        self.assertEqual(list((self.root / 'outside').iterdir()), [])
