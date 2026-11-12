"""Exact-source patch validation. No application at import time."""
import ast
import hashlib
from pathlib import Path


def validate(root, hashes, edits):
    root = Path(root).resolve()
    sources = {}
    for name, digest in hashes.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or str(relative) != name:
            raise ValueError('unsafe source path')
        source = root / relative
        if any(part.is_symlink() for part in [source, *source.parents] if part != root.parent):
            raise ValueError('symlink source path')
        if not source.resolve().is_relative_to(root):
            raise ValueError('source outside base')
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('wrong source base: ' + name)
        sources[name] = data
    result = {}
    for name, replacements in edits.items():
        text = sources[name].decode('utf-8')
        for old, new in replacements:
            if not old or text.count(old) != 1:
                raise ValueError('patch anchor not unique: ' + name)
            text = text.replace(old, new, 1)
        ast.parse(text, filename=name)
        result[name] = text.encode('utf-8')
    return result


def stage(root, hashes, edits, output):
    """Apply into a NEW source overlay. Never edit the input installation.

    All hashes/anchors/syntax are checked before writing. The output is not an
    executable installation; a rebuild owner must integrate it deliberately.
    """
    import json
    root = Path(root).resolve()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or output.resolve() != output:
        raise ValueError('output must be a new canonical directory')
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError('output must be separate from source base')
    candidates = validate(root, hashes, edits)
    output.mkdir()  # exclusive: an existing destination is never overwritten
    for name, data in candidates.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    receipt = {name: hashlib.sha256(data).hexdigest() for name, data in candidates.items()}
    (output / 'overlay-sha256.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt
