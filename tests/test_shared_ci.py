"""Exercise actual workflow shell and closed call graph; no hosted-CI simulation."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_workflow_safety import ROOT, REPORTS, FIXTURES, load, validate


class SharedCI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load(ROOT)
        cls.steps = cls.config['reusable-ci']['jobs']['tests']['steps']
        FIXTURES.mkdir(parents=True, exist_ok=True)

    def shell(self, script, cwd, **values):
        env = {'PATH': os.environ['PATH'], 'HOME': str(cwd),
               'PYTHONPYCACHEPREFIX': str(ROOT / '.cache/pycache')}
        env.update(values)
        return subprocess.run(['/bin/bash', '--noprofile', '--norc', '-e', '-c', script],
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=60)

    def test_actual_application_entry_and_failure_propagation(self):
        script = next(s['run'] for s in self.steps
                      if s['name'] == 'Real application HTTP and CLI tests')
        with tempfile.TemporaryDirectory(prefix='common-app-', dir=FIXTURES) as directory:
            root = Path(directory)
            checkout = root / 'checkout'
            (checkout / 'tests').mkdir(parents=True)
            (checkout / 'apps/sevenbot-flow-probe').mkdir(parents=True)
            shutil.copy2(ROOT / 'apps/sevenbot-flow-probe/server.py',
                         checkout / 'apps/sevenbot-flow-probe/server.py')
            shutil.copy2(ROOT / 'tests/test_flow_probe.py', checkout / 'tests/test_flow_probe.py')
            good = self.shell(script, checkout)
            log = root / 'reports/n3-v2/application.log'
            (REPORTS / 'shared-app-positive.log').write_text(log.read_text() + good.stdout + good.stderr)
            (REPORTS / 'shared-app-positive.exit').write_text(str(good.returncode) + '\n')
            self.assertEqual(good.returncode, 0, good.stderr + log.read_text())
            self.assertIn('Ran 11 tests', log.read_text())
            self.assertIn('test_real_version_identity', log.read_text())
            self.assertIn('test_foreground_cli_http_and_sigterm', log.read_text())
            # Add a real failing control in a disposable copy, never alter mandatory tests.
            (checkout / 'tests/test_flow_probe_negative.py').write_text(
                'import unittest\nclass Negative(unittest.TestCase):\n'
                '    def test_deliberate_failure(self): self.fail("negative control")\n')
            bad = self.shell(script, checkout)
            (REPORTS / 'shared-app-negative.log').write_text(log.read_text() + bad.stdout + bad.stderr)
            (REPORTS / 'shared-app-negative.exit').write_text(str(bad.returncode) + '\n')
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('negative control', log.read_text())
            self.assertIn('FAILED (failures=1)', log.read_text())
            (checkout / 'tests/test_flow_probe.py').unlink()
            self.assertNotEqual(self.shell(script, checkout).returncode, 0)

    def test_required_gate_all_dependency_results(self):
        gate = self.config['ci']['jobs']['ci']
        self.assertEqual(gate['if'], '${{ always() }}')
        self.assertEqual(gate['needs'], ['tests'])
        for result in ('success', 'failure', 'cancelled', 'skipped', '', 'success; touch sentinel'):
            with self.subTest(result=result):
                actual = self.shell(gate['steps'][0]['run'], ROOT, TEST_RESULT=result)
                self.assertEqual(actual.returncode == 0, result == 'success')

    def test_closed_profile_shell_rejects_paths_and_injection(self):
        script = self.steps[0]['run']
        for value in ('python-stdlib-http-v1', '../tests', '/etc/passwd', 'main',
                      'python-stdlib-http-v1\n', '$(touch sentinel)', 'other/repo@main'):
            with self.subTest(value=value):
                result = self.shell(script, ROOT, TEST_PROFILE=value)
                self.assertEqual(result.returncode == 0, value == 'python-stdlib-http-v1')

    def test_graph_mutations_cannot_bypass_required_tests(self):
        def reject(mutate):
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.assertRaises((ValueError, KeyError)):
                validate(config)
        reject(lambda c: c['ci']['jobs']['ci'].pop('needs'))
        reject(lambda c: c['ci']['jobs']['ci'].update(**{'if': '${{ success() }}'}))
        reject(lambda c: c['ci']['jobs']['ci']['steps'][0].update(run='true\n'))
        reject(lambda c: c['ci']['jobs']['tests'].update(**{'if': '${{ false }}'}))
        reject(lambda c: c['reusable-ci']['jobs']['tests']['steps'].pop(6))
        reject(lambda c: c['reusable-ci']['jobs']['tests']['steps'][6].update(
            **{'if': '${{ false }}'}))

    def test_no_untrusted_helper_repo_ref_or_privileged_runner(self):
        for reference in ('./../private/reusable-ci.yml',
                          'other/repo/.github/workflows/reusable-ci.yml@main',
                          'timmypai/hermes-teams/.github/workflows/reusable-ci.yml@main',
                          './.github/workflows/reusable-ci.yml@wrong-version'):
            config = copy.deepcopy(self.config)
            config['ci']['jobs']['tests']['uses'] = reference
            with self.assertRaises(ValueError):
                validate(config)
        for runner in ('self-hosted', ['self-hosted', 'oracle'], 'ubuntu-latest'):
            config = copy.deepcopy(self.config)
            config['reusable-ci']['jobs']['tests']['runs-on'] = runner
            with self.assertRaises(ValueError):
                validate(config)

    def test_all_embedded_shell_has_valid_syntax(self):
        for workflow in self.config.values():
            for job in workflow['jobs'].values():
                for step in job.get('steps', []):
                    if 'run' in step:
                        result = subprocess.run(['/bin/bash', '-n'], input=step['run'],
                                                text=True, capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
