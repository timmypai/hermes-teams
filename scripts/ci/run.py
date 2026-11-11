"""Bounded stdlib CI entry points. Logs are private, ignored local evidence."""
import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def write_report(path, result):
    """Replace the directory entry, never truncate an existing aliased inode.

    The report directory and its ancestors must remain trusted/stable during
    this call. This is not a sandbox against concurrent directory mutation.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=path, prefix='.result-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(result, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        if (path / 'result.json').is_symlink():
            raise OSError('report file must not be a symlink')
        os.replace(temporary, path / 'result.json')
    except OSError as exc:
        print('report write failed: ' + str(exc), file=sys.stderr)
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


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
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
        try:
            run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                 text=True, timeout=120, check=False)
            status, output = run.returncode, run.stdout + run.stderr
        except subprocess.TimeoutExpired:
            status, output = 124, 'test timeout after 120 seconds'
        written = write_report(path, {'command': command, 'exit': status, 'output': output})
        print(output)
        return status if written else (status or 1)
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
    written = write_report(path, result)
    print('\n'.join(output))
    return status if written else (status or 1)


if __name__ == '__main__':
    sys.exit(main())
