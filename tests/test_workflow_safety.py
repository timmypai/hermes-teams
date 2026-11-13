"""Configuration-product safety checks; no controller imports or operational effects.

Run locally and in CI: timeout 600 python3 -m unittest discover -s tests
-p test_workflow_safety.py -v. Fixtures are disposable COPIES, outside source.
"""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT.parent / 'reports' / 'n3-v2'
FIXTURES = REPORTS / 'fixtures'
PINS = {
    'actions/checkout': '11bd71901bbe5b1630ceea73d27597364c9af683',
    'actions/setup-python': 'a26af69be951a213d495a4c3e4e4022e16d87065',
    'actions/upload-artifact': 'ea165f8d65b6e75b540449e92b4886f43607fa02',
}
COMMAND = 'timeout 600 python3 -m unittest discover -s tests -p test_workflow_safety.py -v'
APP_COMMAND = '''set -euo pipefail
mkdir -p ../reports/n3-v2
test -f apps/sevenbot-flow-probe/server.py
test -f tests/test_flow_probe.py
timeout 600 python3 -m unittest discover -s tests -p 'test_flow_probe*.py' -v > ../reports/n3-v2/application.log 2>&1
'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently accepting policy replacement."""


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        require(key not in result, 'duplicate YAML key')
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load(root):
    return {name: yaml.load((root / '.github/workflows' / (name + '.yml')).read_text(),
                            Loader=UniqueLoader)
            for name in ('ci', 'reusable-ci', 'release', 'deploy')}


def validate_guard(script):
    require(script.startswith('set -euo pipefail\n'), 'guard strict shell')
    require(script.rstrip().endswith('exit 78'), 'unbound must fail')
    require('$(' not in script and '`' not in script and '${{' not in script,
            'guard command interpolation')
    for line in script.splitlines():
        line = line.strip()
        allowed = line in ('set -euo pipefail', 'fi', 'exit 64', 'exit 78')
        allowed |= (line.startswith('if [[ ') and line.endswith(' ]]; then')
                    and line.count(';') == 1)
        allowed |= bool(re.fullmatch(r"printf '%s\\n' '[A-Za-z0-9_:, ./-]+' >&2", line))
        require(allowed, 'unexpected guard shell command: ' + line)


def validate(config):
    require(set(config) == {'ci', 'reusable-ci', 'release', 'deploy'}, 'mandatory workflows')
    for name, workflow in config.items():
        require(isinstance(workflow, dict), 'workflow mapping')
        require(workflow['permissions'] == {'contents': 'read'}, 'read-only permissions')
        if name != 'reusable-ci':
            require(workflow['concurrency']['cancel-in-progress'] is False, 'no cancellation')
        require('pull_request_target' not in workflow['on'], 'untrusted privileged event')
        text = json.dumps(workflow)
        for forbidden in ('secrets.', 'id-token', 'self-hosted', 'continue-on-error',
                          'kanban_', 'hermes ', 'worker', 'nextEpic', 'eval ', 'bash -c'):
            require(forbidden not in text, 'forbidden operational authority: ' + forbidden)
        for job in workflow['jobs'].values():
            if 'uses' in job:
                require(name == 'ci' and job == {
                    'uses': './.github/workflows/reusable-ci.yml',
                    'with': {'test-profile': 'python-stdlib-http-v1'}}, 'fixed common caller')
                continue
            require(0 < job['timeout-minutes'] <= 10, 'bounded job')
            require(job['runs-on'] == 'ubuntu-24.04', 'fixed hosted runner')
            require(job.get('permissions', {'contents': 'read'}) == {'contents': 'read'},
                    'job privilege boundary')
            for step in job['steps']:
                if 'uses' in step:
                    action, sha = step['uses'].split('@')
                    require(PINS.get(action) == sha and re.fullmatch('[0-9a-f]{40}', sha),
                            'resolved immutable action pin')
                    if action == 'actions/checkout':
                        require(step['with']['persist-credentials'] is False, 'checkout credentials')
                        require('ref' not in step['with'], 'event checkout only')
                if 'run' in step:
                    require('${{' not in step['run'], 'expressions must be passed as data')
    ci = config['ci']
    require(ci['on'] == {'push': {'branches': ['main', 'zero-core-v3/**']},
                         'pull_request': {'branches': ['main'],
                                          'types': ['opened', 'synchronize', 'reopened']}},
            'restricted CI triggers')
    require(set(ci['jobs']) == {'tests', 'ci'} and ci['jobs']['ci']['name'] == 'ci', 'required check ci')
    gate = ci['jobs']['ci']
    require(gate == {
        'name': 'ci', 'needs': ['tests'], 'if': '${{ always() }}',
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 1,
        'steps': [{'name': 'Require successful common CI', 'shell': 'bash',
                   'env': {'TEST_RESULT': '${{ needs.tests.result }}'},
                   'run': 'set -euo pipefail\ntest "$TEST_RESULT" = success\n'}]},
        'exact fail-closed required gate')
    common = config['reusable-ci']
    require(common['on'] == {'workflow_call': {'inputs': {'test-profile': {
        'description': 'Closed test contract; only python-stdlib-http-v1 is supported',
        'required': True, 'type': 'string'}}}}, 'closed common interface')
    require(set(common['jobs']) == {'tests'}, 'single common test job')
    require(set(common['jobs']['tests']) == {'name', 'runs-on', 'timeout-minutes', 'steps'},
            'common job cannot be skipped or given credentials')
    require('github.event_name' in ci['concurrency']['group'] and
            'github.event.pull_request.number || github.ref' in ci['concurrency']['group'],
            'event-separated concurrency')
    steps = common['jobs']['tests']['steps']
    require(all('if' not in s for s in steps if 'run' in s and s.get('id') != 'evidence'
                and s['name'] != 'Source receipt even on failure'), 'mandatory steps not conditional')
    require(steps[0] == {
        'name': 'Validate closed project profile', 'shell': 'bash',
        'env': {'TEST_PROFILE': '${{ inputs.test-profile }}'},
        'run': 'set -euo pipefail\ntest "$TEST_PROFILE" = python-stdlib-http-v1\n'},
        'closed profile validation before checkout')
    require(any(s.get('run') == APP_COMMAND for s in steps), 'mandatory real HTTP app command')
    require(any('timeout 600 python3 -m unittest discover -s tests -p test_shared_ci.py -v'
                in s.get('run', '') for s in steps), 'common regression entry')
    require(any(COMMAND in s.get('run', '') for s in steps), 'real unittest entry')
    require(any('timeout 600 .lint-cache/actionlint -color=false' in s.get('run', '')
                for s in steps), 'real actionlint entry')
    require(any('--require-hashes --only-binary=:all: --no-deps' in s.get('run', '')
                for s in steps), 'frozen dependencies')
    uploads = [s for s in steps if s.get('uses', '').startswith('actions/upload-artifact@')]
    require(len(uploads) == 1 and uploads[0]['if'] == '${{ always() }}' and
            uploads[0]['with']['retention-days'] == 7 and
            uploads[0]['with']['if-no-files-found'] == 'error' and
            uploads[0]['with']['path'] == '${{ steps.evidence.outputs.path }}',
            'bounded failure evidence')
    resolvers = [s for s in steps if s.get('id') == 'evidence']
    require(len(resolvers) == 1 and resolvers[0]['if'] == '${{ always() }}' and
            resolvers[0]['shell'] == 'bash' and
            steps.index(resolvers[0]) < steps.index(uploads[0]), 'evidence resolver before upload')
    for name in ('release', 'deploy'):
        w = config[name]
        require(set(w['on']) == {'workflow_dispatch'}, 'manual trusted preflight only')
        require(set(w['jobs']) == {'preflight'}, 'no privileged job while unbound')
        job = w['jobs']['preflight']
        require(job['if'] == "${{ github.repository == 'timmypai/hermes-teams' && github.ref == 'refs/heads/main' }}",
                'trusted repo/main')
        require('environment' not in job and len(job['steps']) == 1, 'no credential-bearing stage')
        guard = job['steps'][0]
        require(guard['id'] == 'guard' and guard['shell'] == 'bash' and 'uses' not in guard,
                'preflight before checkout/credentials')
        require(guard['env']['SOURCE_SHA'] == '${{ inputs.source_sha }}', 'source is data')
        require('UNTRUSTED_SOURCE' in guard['run'] and 'INVALID_INPUT' in guard['run'],
                'source/input rejection')
        require(name.upper() + '_BINDINGS_UNBOUND' in guard['run'], 'explicit unbound error')
        validate_guard(guard['run'])
    deploy = config['deploy']
    require(deploy['concurrency']['group'] == 'hermes-teams-unbound-service-unbound-environment',
            'identical deploy/rollback lock independent of mode/request')
    inputs = deploy['on']['workflow_dispatch']['inputs']
    require(inputs['mode']['options'] == ['deploy', 'rollback'], 'closed mode')
    require(inputs['environment']['options'] == ['unbound'], 'no invented target')
    require(set(inputs) == {'mode', 'environment', 'source_sha', 'release_digest',
                            'expected_current_digest', 'request_id', 'reason', 'knownhealthy_record'},
            'no arbitrary command/ref/host input')
    require(set(config['release']['on']['workflow_dispatch']['inputs']) ==
            {'source_sha', 'request_id', 'task_id'}, 'closed release inputs')


class WorkflowSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        cls.config = load(ROOT)

    def test_valid_delivered_configuration(self):
        validate(self.config)

    def mutated_copy_rejected(self, label, mutate):
        config = copy.deepcopy(self.config)
        mutate(config)
        # Exercise real YAML serialization/parsing and actual isolated file copies.
        with tempfile.TemporaryDirectory(prefix='mutation-', dir=FIXTURES) as directory:
            root = Path(directory)
            target = root / '.github/workflows'
            target.mkdir(parents=True)
            for name, workflow in config.items():
                (target / (name + '.yml')).write_text(yaml.safe_dump(workflow, sort_keys=False))
            with self.assertRaises((ValueError, KeyError, FileNotFoundError), msg=label):
                validate(load(root))

    def test_upload_path_resolves_real_evidence_and_missing_files_fail(self):
        steps = self.config['reusable-ci']['jobs']['tests']['steps']
        upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact@'))
        pattern = upload['with']['path']
        # Check raw segments BEFORE pathlib normalization: the hosted glob rejects
        # dot/dotdot even when an ordinary filesystem lookup would succeed.
        self.assertNotIn('.', pattern.split('/'), 'upload glob rejects dot segments')
        self.assertNotIn('..', pattern.split('/'), 'upload glob rejects dotdot segments')
        self.assertEqual(pattern, '${{ steps.evidence.outputs.path }}')
        resolver = next(s for s in steps if s.get('id') == 'evidence')
        self.assertEqual(resolver['if'], '${{ always() }}')
        self.assertLess(steps.index(resolver), steps.index(upload))
        with tempfile.TemporaryDirectory(prefix='upload-', dir=FIXTURES) as directory:
            root = Path(directory).resolve()
            checkout = root / 'checkout'
            checkout.mkdir()
            evidence = root / 'reports/n3-v2'
            evidence.mkdir(parents=True)
            output = root / 'step-output'
            script = root / 'resolve.sh'
            script.write_text(resolver['run'])
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'GITHUB_OUTPUT': str(output)}

            def resolve():
                return subprocess.run(['/bin/bash', '--noprofile', '--norc', str(script)],
                                      cwd=checkout, env=env, text=True, capture_output=True, timeout=10)

            # Empty/missing evidence must not yield an upload output (even if
            # if-no-files-found were accidentally changed in a later edit).
            missing = resolve()
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse(output.exists())
            (evidence / 'source.txt').write_text('exact-source-receipt\n')
            (evidence / 'unittest.log').write_text('FAILED deliberate control\n')
            (evidence / 'guard.json').write_text('{"exit":78}\n')
            present = resolve()
            self.assertEqual(present.returncode, 0, present.stderr)
            lines = output.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith('path='))
            resolved = lines[0].removeprefix('path=')
            self.assertTrue(Path(resolved).is_absolute())
            self.assertFalse({'.', '..'} & set(resolved.split('/')))
            self.assertEqual(Path(resolved), evidence)
            self.assertEqual((Path(resolved) / 'unittest.log').read_text(),
                             'FAILED deliberate control\n')
            self.assertEqual((Path(resolved) / 'guard.json').read_text(), '{"exit":78}\n')
            self.assertEqual(sorted(p.name for p in Path(resolved).iterdir()),
                             ['guard.json', 'source.txt', 'unittest.log'])
            # Do not follow a directory symlink into an unrelated upload source.
            output.unlink()
            evidence.rename(root / 'other-evidence')
            evidence.symlink_to(root / 'other-evidence', target_is_directory=True)
            linked = resolve()
            self.assertNotEqual(linked.returncode, 0)
            self.assertFalse(output.exists())

    def test_upload_dot_segments_and_retention_mutations_rejected(self):
        for path in ('../reports/n3-v2/', './reports/n3-v2/', '/tmp/../reports', ''):
            with self.subTest(path=path):
                self.mutated_copy_rejected('invalid upload path', lambda c:
                    c['reusable-ci']['jobs']['tests']['steps'][-1]['with'].update(path=path))
        self.mutated_copy_rejected('retention weakened', lambda c:
            c['reusable-ci']['jobs']['tests']['steps'][-1]['with'].update(**{'retention-days': 1}))
        self.mutated_copy_rejected('missing files ignored', lambda c:
            c['reusable-ci']['jobs']['tests']['steps'][-1]['with'].update(**{'if-no-files-found': 'ignore'}))

    def test_floating_action_rejected(self):
        self.mutated_copy_rejected('floating action', lambda c: c['reusable-ci']['jobs']['tests']['steps'][1].update(
            uses='actions/checkout@v4'))

    def test_writable_permissions_rejected(self):
        self.mutated_copy_rejected('write token', lambda c: c['ci'].update(permissions={'contents': 'write'}))

    def test_untrusted_privileged_event_rejected(self):
        self.mutated_copy_rejected('PR target', lambda c: c['release']['on'].update(pull_request_target={}))

    def test_command_and_ref_injection_rejected(self):
        self.mutated_copy_rejected('arbitrary command', lambda c: c['deploy']['jobs']['preflight']['steps'][0].update(
            run='eval "${{ inputs.command }}"\n'))
        self.mutated_copy_rejected('arbitrary ref', lambda c: c['reusable-ci']['jobs']['tests']['steps'][1]['with'].update(
            ref='${{ inputs.ref }}'))
        self.mutated_copy_rejected('extra target step', lambda c: c['deploy']['jobs']['preflight']['steps'].append(
            {'run': 'touch target-mutation'}))

    def test_mismatched_locks_rejected(self):
        self.mutated_copy_rejected('split mode locks', lambda c: c['deploy']['concurrency'].update(
            group='deploy-${{ inputs.mode }}'))

    def test_missing_mandatory_configuration_rejected(self):
        self.mutated_copy_rejected('missing workflow', lambda c: c.pop('release'))
        self.mutated_copy_rejected('missing guard', lambda c: c['deploy']['jobs']['preflight'].update(steps=[]))
        self.mutated_copy_rejected('missing timeout', lambda c: c['ci']['jobs']['ci'].pop('timeout-minutes'))

    def test_failure_masking_and_retention_rejected(self):
        self.mutated_copy_rejected('masked failure', lambda c: c['reusable-ci']['jobs']['tests']['steps'][4].update(
            **{'continue-on-error': True}))
        self.mutated_copy_rejected('missing reports', lambda c: c['reusable-ci']['jobs']['tests']['steps'][-1].pop('if'))

    def test_bad_yaml_and_duplicate_policy_rejected(self):
        with self.assertRaises(yaml.YAMLError):
            yaml.load('jobs: [', Loader=UniqueLoader)
        with self.assertRaises(ValueError):
            yaml.load('permissions: read\npermissions: write\n', Loader=UniqueLoader)

    def run_guard(self, name, label, expected, **overrides):
        script = self.config[name]['jobs']['preflight']['steps'][0]['run']
        with tempfile.TemporaryDirectory(prefix='guard-', dir=FIXTURES) as directory:
            root = Path(directory)
            # No inherited environment or credentials, network client, controller or target.
            env = {'PATH': '/usr/bin:/bin', 'HOME': directory,
                   'REPOSITORY': 'timmypai/hermes-teams', 'SOURCE_REF': 'refs/heads/main',
                   'WORKFLOW_REF': f'timmypai/hermes-teams/.github/workflows/{name}.yml@refs/heads/main',
                   'SOURCE_SHA': 'a' * 40, 'REQUEST_ID': 'local-negative', 'TASK_ID': 't_1234abcd',
                   'MODE': 'deploy', 'TARGET_ENVIRONMENT': 'unbound',
                   'RELEASE_DIGEST': 'sha256:' + 'b' * 64,
                   'EXPECTED_CURRENT_DIGEST': 'sha256:' + 'c' * 64,
                   'REASON': 'local-test', 'KNOWNHEALTHY_RECORD': 'record-test'}
            env.update(overrides)
            # Sentinel follows the EXACT extracted guard, proving exit propagation.
            executable = root / 'guard.sh'
            executable.write_text(script + "\nprintf '%s' mutated > mutation-sentinel\n")
            result = subprocess.run(['/bin/bash', '--noprofile', '--norc', str(executable)],
                                    cwd=root, env=env, text=True, capture_output=True, timeout=10)
            receipt = {'case': label, 'workflow': name, 'argv': ['/bin/bash', '--noprofile', '--norc', 'guard.sh'],
                       'cwd_kind': 'disposable-test-owned-fixture', 'exit': result.returncode,
                       'stdout': result.stdout, 'stderr': result.stderr,
                       'sentinel_absent': not (root / 'mutation-sentinel').exists()}
            (REPORTS / (label + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
            self.assertEqual(result.returncode, expected, receipt)
            self.assertTrue(receipt['sentinel_absent'], receipt)
            self.assertNotEqual(result.returncode, 0)
            return result

    def test_release_unbound_fails_before_publication(self):
        result = self.run_guard('release', 'release-unbound', 78)
        self.assertIn('RELEASE_BINDINGS_UNBOUND', result.stderr)

    def test_deploy_and_rollback_unbound_fail_before_target(self):
        for mode in ('deploy', 'rollback'):
            with self.subTest(mode=mode):
                result = self.run_guard('deploy', mode + '-unbound', 78, MODE=mode)
                self.assertIn('DEPLOY_BINDINGS_UNBOUND', result.stderr)

    def test_malformed_sha_digest_and_stale_data_rejected(self):
        self.run_guard('release', 'invalid-source-sha', 64, SOURCE_SHA='main')
        self.run_guard('deploy', 'invalid-digest', 64, RELEASE_DIGEST='latest')
        self.run_guard('deploy', 'invalid-current-digest', 64, EXPECTED_CURRENT_DIGEST='unknown')
        self.run_guard('deploy', 'unbound-current-version', 78,
                       EXPECTED_CURRENT_DIGEST='sha256:' + 'd' * 64)
        # Live current-version/provenance comparison is deliberately NOT implemented or mocked.

    def test_injection_shaped_inputs_remain_data(self):
        for name in ('release', 'deploy'):
            self.run_guard(name, name + '-injection', 64,
                           SOURCE_SHA='$(touch mutation-sentinel);main')
            self.run_guard(name, name + '-request-injection', 64,
                           REQUEST_ID='x; touch mutation-sentinel')
        self.run_guard('deploy', 'rollback-injection', 64, MODE='rollback',
                       REASON='$(touch mutation-sentinel)')
        self.run_guard('deploy', 'invalid-mode', 64, MODE='deploy;touch mutation-sentinel')

    def test_untrusted_repository_ref_and_workflow_rejected(self):
        for name in ('release', 'deploy'):
            self.run_guard(name, name + '-wrong-repo', 64, REPOSITORY='other/repo')
            self.run_guard(name, name + '-wrong-ref', 64, SOURCE_REF='refs/heads/untrusted')
            self.run_guard(name, name + '-wrong-workflow', 64, WORKFLOW_REF='untrusted')

    def test_native_procedure_document_consistency(self):
        readme = (ROOT / 'README.md').read_text()
        policy = (ROOT / 'docs/engineering/release-policy.md').read_text()
        operations = (ROOT / 'docs/engineering/operations-runbook.md').read_text()
        for token in ('explicit native planning', 'codex_app_server', 'sole existing Dispatcher'):
            self.assertIn(token, readme)
        for token in ('ci', '15368', 'independent', 'D-DEPLOY-01', 'NOT_RUN'):
            self.assertIn(token, policy)
        for token in ('request_changes', 'request_review', 'knownhealthy', 'Incident', 'NOT_RUN'):
            self.assertIn(token, operations)
        self.assertIn(COMMAND, operations)
        self.assertNotIn('SOURCE_ENTRYPOINT_UNBOUND', json.dumps(self.config))
