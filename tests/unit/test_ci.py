import subprocess
import unittest

class ScriptTests(unittest.TestCase):
    def test_check_script_checks_source_and_bounds_reports(self):
        run = subprocess.run(['sh', 'scripts/ci/check.sh', '--report-dir', 'reports/check-test'],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for path in ('../escape', 'src', 'reports/../../escape'):
            bad = subprocess.run(['sh', 'scripts/ci/check.sh', '--report-dir', path],
                                 capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)

    def test_test_script_rejects_unknown_suite(self):
        run = subprocess.run(['sh', 'scripts/ci/test.sh', '--suite', 'invented'],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn('invalid choice', run.stderr)

    def test_report_file_symlink_cannot_escape_reports(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(dir='reports') as report, tempfile.TemporaryDirectory(dir='.') as outside:
            target = Path(outside).resolve() / 'sentinel.json'
            target.write_text('unchanged')
            (Path(report) / 'result.json').symlink_to(target)
            run = subprocess.run(['sh', 'scripts/ci/check.sh', '--report-dir', report],
                                 capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(target.read_text(), 'unchanged')
