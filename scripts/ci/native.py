#!/usr/bin/env python3
"""Fixed bounded native test entry point for approved unbound validation only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_RUN_ID'):
        raise SystemExit('REFUSED: native tests forbidden in a managed Worker')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    args = parser.parse_args()
    base = args.base.resolve()
    reports = ROOT / 'reports'
    if reports.is_symlink():
        raise SystemExit('REFUSED: reports symlink')
    reports.mkdir(exist_ok=True)
    overlay = reports / 'native-overlay'
    # New destination required. Never overwrite stale outputs or alter the base.
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_patch.py'),
        '--base', str(base), '--apply-to', str(overlay)], timeout=60, check=False)
    if result.returncode:
        return result.returncode
    # Overlay package: only the three patched modules override the immutable
    # base. All other native modules/dependencies come from the verified image.
    (overlay / 'hermes_cli/__init__.py').write_text(
        '__path__.append(' + repr(str(base / 'hermes_cli')) + ')\n')
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join([str(overlay), str(ROOT / 'src'), str(base)])
    env['HERMES_TEAMS_TEST_OVERLAY'] = str(overlay)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/run.py'), 'test',
        '--suite', 'integration', '--report-dir', 'reports/ci-native'],
        cwd=ROOT, env=env, timeout=180, check=False)
    return result.returncode

if __name__ == '__main__':
    sys.exit(main())
