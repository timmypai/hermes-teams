"""Bounded stdlib CI entry points. Logs are private, ignored local evidence."""
import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['check', 'test'])
    parser.add_argument('--suite', choices=['unit', 'integration'], default='unit')
    parser.add_argument('--report-dir', default='reports/check')
    args = parser.parse_args()
    path = (ROOT / args.report_dir).resolve()
    reports = ROOT / 'reports'
    if reports.is_symlink() or path == reports or not path.is_relative_to(reports):
        parser.error('report directory must be a child of repository reports/')
    path.mkdir(parents=True, exist_ok=True)
    if (path / 'result.json').is_symlink():
        parser.error('report file must not be a symlink')
    if args.command == 'test':
        command = [sys.executable, '-m', 'unittest', 'discover', '-s',
                   'tests/' + args.suite, '-v']
        # Import the package without installs; native integration retains all
        # inherited runtime authorization and fencing unchanged.
        import os
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
        try:
            run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                 text=True, timeout=120, check=False)
            status, output = run.returncode, run.stdout + run.stderr
        except subprocess.TimeoutExpired:
            status, output = 124, 'test timeout after 120 seconds'
        (path / 'result.json').write_text(json.dumps(
            {'command': command, 'exit': status, 'output': output}, indent=2) + '\n')
        print(output)
        return status
    output = []
    status = 0
    try:
        for base in ('src', 'tests', 'scripts'):
            for source in sorted((ROOT / base).rglob('*.py')):
                ast.parse(source.read_text(encoding='utf-8'), filename=str(source.relative_to(ROOT)))
                output.append('syntax OK: ' + str(source.relative_to(ROOT)))
        for source in sorted((ROOT / 'scripts').rglob('*.sh')):
            run = subprocess.run(['sh', '-n', str(source)], capture_output=True, text=True,
                                 timeout=30, check=False)
            output.append(run.stdout + run.stderr)
            if run.returncode:
                status = run.returncode
                break
    except (OSError, SyntaxError, subprocess.TimeoutExpired) as exc:
        output.append(str(exc))
        status = 1
    result = {'command': args.command, 'exit': status, 'output': output}
    (path / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print('\n'.join(output))
    return status


if __name__ == '__main__':
    sys.exit(main())
