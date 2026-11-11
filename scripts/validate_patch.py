#!/usr/bin/env python3
"""Dry-run by default; --apply-to emits a new overlay, NEVER edits the base."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from hermes_teams.patching import validate, stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--apply-to', type=Path)
    args = parser.parse_args()
    baseline = json.loads((ROOT / 'patches/base.json').read_text())
    edits = json.loads((ROOT / 'patches/edits.json').read_text())
    try:
        candidates = validate(args.base, baseline['source_sha256'], edits)
        diff = ''.join(''.join(difflib.unified_diff(
            (args.base / name).read_text().splitlines(True), data.decode().splitlines(True),
            fromfile='a/' + name, tofile='b/' + name)) for name, data in candidates.items())
        if diff != (ROOT / 'patches/contracts.patch').read_text():
            raise ValueError('reviewable patch does not match guarded edits')
        candidate_hashes = {name: hashlib.sha256(data).hexdigest()
                            for name, data in candidates.items()}
        if candidate_hashes != json.loads((ROOT / 'patches/patched-sha256.json').read_text()):
            raise ValueError('patched source hashes do not match frozen receipt')
        if args.apply_to:
            stage(args.base, baseline['source_sha256'], edits, args.apply_to)
        print(json.dumps({'result': 'STAGED' if args.apply_to else 'DRY_RUN_VALID',
            'upstream_commit': baseline['upstream_commit'],
            'candidate_sha256': candidate_hashes,
            'input_unchanged': True}, indent=2))
        return 0
    except (ValueError, OSError, KeyError, SyntaxError) as exc:
        print('REFUSED: ' + str(exc), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
