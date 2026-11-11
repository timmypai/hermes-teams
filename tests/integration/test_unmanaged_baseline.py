"""Operator-only compatibility comparison, runnable on unpatched AND patched base.

No candidate imports are needed for the unpatched run. The full native suite
also discovers this test against its guarded overlay. Never use a live DB.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_RUN_ID'):
    raise RuntimeError('native integration is forbidden in a managed Worker')

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_graph as graph, kanban_decompose as kd


class UnmanagedBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        expected = json.loads((root / 'patches/base.json').read_text())['source_sha256']
        overlay = os.environ.get('HERMES_TEAMS_TEST_OVERLAY')
        if overlay:
            expected.update(json.loads((root / 'patches/patched-sha256.json').read_text()))
        for module in (kb, kbc, graph, kd):
            source = Path(module.__file__).resolve()
            name = 'hermes_cli/' + source.name
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected[name]:
                raise RuntimeError('baseline comparison requires pinned native source: ' + name)
            if overlay and module is not kbc and not source.is_relative_to(Path(overlay).resolve()):
                raise RuntimeError('patched comparison did not load guarded overlay')

    def test_unmanaged_graph_retains_workspace_override_and_null_contract(self):
        with tempfile.TemporaryDirectory(dir='reports') as tmp:
            conn = kbc.connect(db_path=(Path(tmp) / 'kanban.db').resolve())
            self.addCleanup(conn.close)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
            self.assertTrue({'completion_contract', 'workspace_kind', 'workspace_path'} <= columns)
            root = kb.create_task(conn, title='Unmanaged baseline', assignee='orchestrator',
                                  triage=True, workspace_kind='scratch', completion_contract='local-only')
            self.assertEqual(kb.get_task(conn, root).completion_contract, 'local-only')
            path = str((Path(tmp) / 'legacy-workspace').resolve())
            proposal = {'title': 'Legacy child', 'body': 'Synthetic fixture',
                        'assignee': 'developer', 'parents': [],
                        'workspace_kind': 'dir', 'workspace_path': path}
            routing = kd._Routing('orchestrator', 'developer', True, [],
                                  {'orchestrator', 'developer'})
            cleaned, reason = kd._clean_children(root, [proposal], routing)
            self.assertEqual(reason, '')
            self.assertEqual(cleaned, [{'title': 'Legacy child', 'body': 'Synthetic fixture',
                                       'assignee': 'developer', 'parents': []}])
            ids = graph.decompose_triage_task(conn, root, root_assignee='orchestrator',
                                              children=[proposal])
            self.assertEqual(len(ids), 1)
            child = kb.get_task(conn, ids[0])
            self.assertEqual((child.workspace_kind, child.workspace_path, child.completion_contract),
                             ('dir', path, None))
            self.assertEqual(child.status, 'ready')
            self.assertEqual(kb.parent_ids(conn, root), ids)
            self.assertEqual(kb.get_task(conn, root).status, 'todo')
