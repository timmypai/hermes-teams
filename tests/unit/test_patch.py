import hashlib
import tempfile
import unittest
from pathlib import Path
from hermes_teams import patching

class PatchTests(unittest.TestCase):
    def test_validator_refuses_stale_patched_hash_receipt_before_staging(self):
        import difflib
        import json
        import shutil
        import subprocess
        import sys
        with tempfile.TemporaryDirectory(dir='reports') as tmp:
            root = Path(tmp).resolve()
            base, candidate = root / 'base', root / 'candidate'
            base.mkdir()
            for directory in ('scripts', 'src/hermes_teams', 'patches'):
                (candidate / directory).mkdir(parents=True)
            for name in ('scripts/validate_patch.py', 'src/hermes_teams/__init__.py',
                         'src/hermes_teams/patching.py'):
                shutil.copyfile(name, candidate / name)
            before, after = 'value = 1\n', 'value = 2\n'
            (base / 'module.py').write_text(before)
            (candidate / 'patches/base.json').write_text(json.dumps({
                'upstream_commit': 'fixture',
                'source_sha256': {'module.py': hashlib.sha256(before.encode()).hexdigest()}}))
            (candidate / 'patches/edits.json').write_text(json.dumps(
                {'module.py': [[before, after]]}))
            (candidate / 'patches/contracts.patch').write_text(''.join(difflib.unified_diff(
                [before], [after], fromfile='a/module.py', tofile='b/module.py')))
            receipt = candidate / 'patches/patched-sha256.json'
            receipt.write_text(json.dumps({'module.py': '0' * 64}))
            output = root / 'overlay'
            command = [sys.executable, str(candidate / 'scripts/validate_patch.py'),
                       '--base', str(base), '--apply-to', str(output)]
            refused = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
            self.assertFalse(output.exists())
            self.assertEqual((base / 'module.py').read_text(), before)
            receipt.write_text(json.dumps({'module.py': hashlib.sha256(after.encode()).hexdigest()}))
            accepted = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            self.assertEqual((output / 'module.py').read_text(), after)

    def test_dry_run_checks_every_source_before_returning_candidate(self):
        with tempfile.TemporaryDirectory(dir='reports') as tmp:
            root = Path(tmp)
            (root / 'module.py').write_text('answer = 1\n')
            hashes = {'module.py': hashlib.sha256(b'answer = 1\n').hexdigest()}
            edits = {'module.py': [['answer = 1', 'answer = 2']]}
            candidate = patching.validate(root, hashes, edits)
            self.assertEqual(candidate, {'module.py': b'answer = 2\n'})
            self.assertEqual((root / 'module.py').read_text(), 'answer = 1\n')
            (root / 'module.py').write_text('wrong base\n')
            with self.assertRaises(ValueError):
                patching.validate(root, hashes, edits)

    def test_rejects_paths_and_symlinks_outside_exact_source_tree(self):
        with tempfile.TemporaryDirectory(dir='reports') as tmp:
            root = Path(tmp)
            (root / 'source.py').write_text('x = 1\n')
            (root / 'link.py').symlink_to('source.py')
            digest = hashlib.sha256(b'x = 1\n').hexdigest()
            for name in ('link.py', '../escape.py', str((root / 'source.py').resolve())):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    patching.validate(root, {name: digest}, {})

    def test_apply_creates_new_overlay_only_after_full_validation(self):
        with tempfile.TemporaryDirectory(dir='reports') as tmp:
            base = Path(tmp) / 'base'
            base.mkdir()
            (base / 'a.py').write_text('value = 1\n')
            (base / 'b.py').write_text('unchanged = True\n')
            hashes = {n: hashlib.sha256((base/n).read_bytes()).hexdigest() for n in ('a.py', 'b.py')}
            edits = {'a.py': [['value = 1', 'value = 2']]}
            target = Path(tmp) / 'overlay'
            patching.stage(base, hashes, edits, target)
            self.assertEqual((target / 'a.py').read_text(), 'value = 2\n')
            self.assertEqual((base / 'a.py').read_text(), 'value = 1\n')
            with self.assertRaises(ValueError):
                patching.stage(base, hashes, edits, target)
            (base / 'b.py').write_text('wrong base\n')
            bad_target = Path(tmp) / 'bad-overlay'
            with self.assertRaises(ValueError):
                patching.stage(base, hashes, edits, bad_target)
            self.assertFalse(bad_target.exists())

    def test_missing_nonunique_or_invalid_syntax_anchor_is_refused(self):
        with tempfile.TemporaryDirectory(dir='reports') as tmp:
            root = Path(tmp)
            (root / 'a.py').write_text('x = 1\ny = 1\n')
            hashes = {'a.py': hashlib.sha256((root / 'a.py').read_bytes()).hexdigest()}
            for changes in ([['missing', 'x']], [['1', '2']], [['x = 1', 'def bad(']]):
                with self.subTest(changes=changes), self.assertRaises((ValueError, SyntaxError)):
                    patching.validate(root, hashes, {'a.py': changes})
