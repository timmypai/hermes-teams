"""Operator/hosted CI only: actual isolated native SQLite, never live boards.

Connection redirection is test isolation, not a simulated SQL implementation.
No GitHub/model/deployment evidence is produced by these tests.
"""
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Refuse BEFORE importing native DB modules or opening any database.
if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_RUN_ID'):
    raise RuntimeError('native integration is forbidden in a managed Worker')

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_decompose as kd, projects_db as pdb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_teams import site_policy

REPO = 'timmypai/hermes-teams'

class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import hashlib
        import json
        import hermes_cli.kanban_db_graph as graph
        repo = Path(__file__).resolve().parents[2]
        expected = json.loads((repo / 'patches/patched-sha256.json').read_text())
        overlay_value = os.environ.get('HERMES_TEAMS_TEST_OVERLAY')
        if not overlay_value:
            raise RuntimeError('run scripts/ci/native.py to validate and stage the exact overlay')
        overlay = Path(overlay_value).resolve()
        for module in (kb, kd, graph):
            file = Path(module.__file__).resolve()
            if not file.is_relative_to(overlay):
                raise RuntimeError('native module did not load from guarded overlay')
            name = str(file.relative_to(overlay))
            if hashlib.sha256(file.read_bytes()).hexdigest() != expected[name]:
                raise RuntimeError('native module is not the frozen patch candidate')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='reports')
        self.addCleanup(self.tmp.cleanup)
        self.path = (Path(self.tmp.name) / 'kanban.db').resolve()
        # Native connect runs current SCHEMA_SQL AND additive migrations on this
        # fresh path. Do not replace it with a partial hand-written SQL schema.
        self.conn = kbc.connect(db_path=self.path)
        self.addCleanup(self.conn.close)
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(tasks)')}
        self.assertTrue({'completion_contract', 'project_id', 'workspace_kind',
                         'workspace_path', 'branch_name', 'current_run_id'} <= columns)
        self.projects = pdb.connect(db_path=Path(self.tmp.name) / 'projects.db')
        self.addCleanup(self.projects.close)
        self.project_path = (Path(self.tmp.name) / 'approved-repository').resolve()
        self.project_path.mkdir()
        self.project_id = pdb.create_project(self.projects, name='Hermes Teams', slug='hermes-teams',
                                             primary_path=str(self.project_path))
        @contextlib.contextmanager
        def project_connection():
            yield self.projects
        project_patch = patch.object(pdb, 'connect_closing', project_connection)
        project_patch.start()
        self.addCleanup(project_patch.stop)
        self.routing = kd._Routing('orchestrator', 'developer', True, [],
            {'orchestrator', 'developer', 'reviewer', 'researcher', 'architect', 'devops'})
        self.policy = {'version': 1, 'board': 'seven-bot-v1', 'project': 'hermes-teams',
                       'repository': REPO, 'grants': {}}
        self.bindings = patch.object(site_policy, 'BINDINGS', {})
        self.bindings.start()
        self.addCleanup(self.bindings.stop)

    def root(self, contract='local-only'):
        return kb.create_task(self.conn, title='Synthetic root', assignee='orchestrator',
            triage=True, workspace_kind='scratch', completion_contract=contract)

    def bind(self, root, count=2, kind='implementation', role='developer'):
        grant = {'work_kind': kind, 'assignee': role}
        self.policy['grants'].update({f'child:{root}:{i}': grant for i in range(count)})
        self.policy['grants'][f'single:{root}'] = grant
        site_policy.BINDINGS[str(self.path)] = self.policy

    def child(self, **fields):
        return {'title': 'Implement fixture', 'body': 'Synthetic fixture',
                'assignee': 'developer', 'parents': [], **fields}

    def count(self):
        return self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]

    def test_cleaner_preserves_managed_contract_project(self):
        self.bind('root')
        children, reason = kd._clean_children('root', [self.child()], self.routing, policy=self.policy)
        self.assertEqual(reason, '')
        self.assertEqual(children[0]['completion_contract'], REPO)
        self.assertEqual(children[0]['project_id'], 'hermes-teams')

    def test_fanout_persists_contract_before_ready_with_native_dependencies(self):
        root = self.root()
        self.bind(root)
        ids = decompose_triage_task(self.conn, root, root_assignee='orchestrator',
            children=[self.child(), self.child(parents=[0])])
        first, second = [kb.get_task(self.conn, cid) for cid in ids]
        self.assertEqual((first.status, second.status), ('ready', 'todo'))
        self.assertEqual(first.completion_contract, REPO)
        self.assertEqual(first.project_id, self.project_id)
        self.assertEqual(kb.parent_ids(self.conn, ids[1]), [ids[0]])
        self.assertEqual(set(kb.parent_ids(self.conn, root)), set(ids))
        self.assertEqual(kb.get_task(self.conn, root).status, 'todo')

    def test_single_routes_preassigned_orchestrator_and_persists_contract(self):
        root = self.root()
        self.bind(root)
        @contextlib.contextmanager
        def fixture_connection():
            yield self.conn
        with patch.object(kd.kbc, 'connect_closing', fixture_connection):
            result = kd._apply_single(kb.get_task(self.conn, root), self.child(), self.routing, 'fixture')
        self.assertTrue(result.ok, result.reason)
        task = kb.get_task(self.conn, root)
        self.assertEqual((task.assignee, task.completion_contract, task.project_id, task.status),
                         ('developer', REPO, self.project_id, 'ready'))

    def test_invalid_late_child_rolls_back_every_child_event_and_root(self):
        root = self.root()
        self.bind(root)
        before = self.count()
        events = self.conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
        with self.assertRaises(ValueError):
            decompose_triage_task(self.conn, root, root_assignee='orchestrator',
                children=[self.child(), self.child(completion_contract='local-only')])
        self.assertEqual(self.count(), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0], events)
        self.assertEqual(kb.get_task(self.conn, root).status, 'triage')

    def test_cycle_has_no_partial_graph(self):
        root = self.root()
        self.bind(root)
        with self.assertRaises(ValueError):
            decompose_triage_task(self.conn, root, root_assignee='orchestrator',
                children=[self.child(parents=[1]), self.child(parents=[0])])
        self.assertEqual(self.count(), 1)
        self.assertEqual(kb.get_task(self.conn, root).status, 'triage')

    def test_already_decomposed_marker_survives_retriage(self):
        root = self.root()
        self.bind(root)
        ids = decompose_triage_task(self.conn, root, root_assignee='orchestrator', children=[self.child()])
        with kb.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (root,))
        self.assertIsNone(decompose_triage_task(self.conn, root, root_assignee='orchestrator', children=[self.child()]))
        self.assertEqual(self.count(), 2)
        self.assertEqual(len(ids), 1)

    def test_managed_create_rejects_missing_grant_and_downgrade(self):
        self.bind('fixture')
        with self.assertRaises(ValueError):
            kb.create_task(self.conn, title='no grant', assignee='developer', completion_contract=REPO)
        self.policy['grants']['create:fixture'] = {'work_kind': 'implementation', 'assignee': 'developer'}
        for contract in ('local-only', 'other/repo'):
            with self.subTest(contract=contract), self.assertRaises(ValueError):
                kb.create_task(self.conn, title='bad', assignee='developer',
                    idempotency_key='fixture', completion_contract=contract, project_id='hermes-teams')
        self.assertEqual(self.count(), 0)

    def test_single_update_cannot_downgrade_existing_pr_contract(self):
        root = self.root(REPO)
        self.bind(root, kind='research', role='researcher')
        with self.assertRaises(ValueError):
            kb.specify_triage_task(self.conn, root, title='research', assignee='researcher')
        self.assertEqual(kb.get_task(self.conn, root).completion_contract, REPO)
        self.assertEqual(kb.get_task(self.conn, root).status, 'triage')

    def test_unmanaged_legacy_and_idempotency_unchanged(self):
        first = kb.create_task(self.conn, title='legacy', idempotency_key='legacy', assignee='developer')
        second = kb.create_task(self.conn, title='ignored duplicate', idempotency_key='legacy')
        self.assertEqual(first, second)
        self.assertEqual(kb.get_task(self.conn, first).completion_contract, 'local-only')
        root = self.root()
        children, reason = kd._clean_children(root, [self.child(completion_contract=REPO)], self.routing)
        self.assertEqual(reason, '')
        self.assertNotIn('completion_contract', children[0])
        ids = decompose_triage_task(self.conn, root, root_assignee='orchestrator', children=children)
        self.assertTrue(kb.complete_task(self.conn, ids[0], result='Synthetic local child complete', fire_lifecycle_hook=False))
        self.assertEqual(kb.get_task(self.conn, root).status, 'ready')

    def test_managed_direct_create_uses_native_project_and_idempotency(self):
        self.bind('fixture')
        self.policy['grants']['create:approved'] = {'work_kind': 'implementation', 'assignee': 'developer'}
        first = kb.create_task(self.conn, title='approved', idempotency_key='approved', assignee='developer')
        second = kb.create_task(self.conn, title='duplicate', idempotency_key='approved', assignee='developer')
        self.assertEqual(first, second)
        task = kb.get_task(self.conn, first)
        self.assertEqual((task.completion_contract, task.project_id), (REPO, self.project_id))

    def test_missing_native_project_fails_closed(self):
        self.bind('fixture')
        self.policy['grants']['create:approved'] = {'work_kind': 'implementation', 'assignee': 'developer'}
        pdb.delete_project(self.projects, self.project_id)
        with self.assertRaises(ValueError):
            kb.create_task(self.conn, title='approved', idempotency_key='approved')
        self.assertEqual(self.count(), 0)

    def test_insert_failure_rolls_back_native_transaction(self):
        root = self.root()
        self.bind(root)
        # Deliberately injected constraint in THIS disposable database only.
        with kb.write_txn(self.conn):
            self.conn.execute("CREATE TRIGGER reject_fixture BEFORE INSERT ON tasks "
                "WHEN NEW.title = 'reject fixture' BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            decompose_triage_task(self.conn, root, root_assignee='orchestrator',
                children=[self.child(), self.child(title='reject fixture')])
        self.assertEqual(self.count(), 1)
        self.assertEqual(kb.get_task(self.conn, root).status, 'triage')

    def test_explicit_non_pr_classifications_do_not_inherit_repository_contract(self):
        for kind, role in [('research', 'researcher'), ('acceptance', 'reviewer'),
                           ('deploy-existing-image', 'devops')]:
            site_policy.BINDINGS.clear()
            root = self.root()
            self.bind(root, kind=kind, role=role)
            ids = decompose_triage_task(self.conn, root, root_assignee='orchestrator',
                children=[self.child(assignee=role)])
            self.assertEqual(kb.get_task(self.conn, ids[0]).completion_contract, 'local-only')

    def test_unmanaged_single_keeps_legacy_preassigned_owner(self):
        root = self.root()
        @contextlib.contextmanager
        def fixture_connection():
            yield self.conn
        with patch.object(kd.kbc, 'connect_closing', fixture_connection):
            result = kd._apply_single(kb.get_task(self.conn, root), self.child(), self.routing, 'fixture')
        self.assertTrue(result.ok)
        task = kb.get_task(self.conn, root)
        self.assertEqual((task.assignee, task.completion_contract), ('orchestrator', 'local-only'))

    def test_f1_cleaner_override_cannot_redirect_approved_project_workspace(self):
        root = self.root()
        self.bind(root)
        children, reason = kd._clean_children(root, [self.child(
            workspace_kind='dir', workspace_path='/unapproved/other-repository',
            model_override='unapproved-model', branch_name='unapproved-branch')],
            self.routing, policy=self.policy)
        self.assertEqual(reason, '')
        self.assertEqual(set(children[0]), {'title', 'body', 'assignee', 'parents',
            'completion_contract', 'repository', 'project_id', 'work_kind'})
        ids = decompose_triage_task(self.conn, root, root_assignee='orchestrator', children=children)
        task = kb.get_task(self.conn, ids[0])
        self.assertEqual((task.workspace_kind, task.workspace_path),
                         ('worktree', str(self.project_path / '.worktrees' / task.id)))
        self.assertEqual((task.project_id, task.completion_contract, task.status),
                         (self.project_id, REPO, 'ready'))
        self.assertIsNone(task.model_override)
        self.assertEqual(task.branch_name, pdb.branch_name_for(
            pdb.get_project(self.projects, self.project_id), task.id, title=task.title))

    def test_f1_direct_graph_override_rejection_is_atomic(self):
        from hermes_teams.policy import normalize
        root = self.root()
        self.bind(root)
        normalized = normalize(self.policy, f'child:{root}:1', self.child())
        overrides = (
            {'workspace_kind': 'dir', 'workspace_path': '/unapproved/other-repository'},
            {'workspace_kind': 'worktree'}, {'workspace_path': '/unapproved/other-repository'},
            {'workspace_path': None}, {'branch_name': 'unapproved'},
            {'model_override': 'unapproved'}, {'provider_override': 'unapproved'},
            {'skills': ['unapproved']}, {'max_runtime_seconds': 999999},
        )
        before = self.graph_snapshot()
        for override in overrides:
            with self.subTest(override=override), self.assertRaisesRegex(
                    ValueError, 'unsupported managed child field'):
                decompose_triage_task(self.conn, root, root_assignee='orchestrator',
                    children=[self.child(), {**normalized, **override}])
            self.assertEqual(self.graph_snapshot(), before)
            self.assertEqual(kb.get_task(self.conn, root).status, 'triage')

    def graph_snapshot(self):
        return tuple(tuple(tuple(row) for row in self.conn.execute(query)) for query in (
            'SELECT * FROM tasks ORDER BY id',
            'SELECT * FROM task_events ORDER BY id',
            'SELECT * FROM task_links ORDER BY parent_id, child_id'))

    def test_f1_private_insert_cannot_bypass_managed_field_guard(self):
        from hermes_cli.kanban_db_graph import _insert_decomposed_child
        from hermes_teams.policy import normalize
        root = self.root()
        self.bind(root)
        row = self.conn.execute('SELECT * FROM tasks WHERE id=?', (root,)).fetchone()
        normalized = normalize(self.policy, f'child:{root}:0', self.child())
        before = self.graph_snapshot()
        with self.assertRaisesRegex(ValueError, 'unsupported managed child field'):
            with kb.write_txn(self.conn):
                _insert_decomposed_child(self.conn, root, row,
                    {**normalized, 'workspace_kind': 'dir',
                     'workspace_path': '/unapproved/other-repository'}, 'fixture', 1)
        self.assertEqual(self.graph_snapshot(), before)

    def test_f1_trusted_root_worktree_derives_distinct_child_workspaces(self):
        root = kb.create_task(self.conn, title='Trusted project root', assignee='orchestrator',
            triage=True, workspace_kind='scratch', project_id=self.project_id)
        root_task = kb.get_task(self.conn, root)
        self.bind(root)
        ids = decompose_triage_task(self.conn, root, root_assignee='orchestrator',
            children=[self.child(), self.child()])
        for cid in ids:
            task = kb.get_task(self.conn, cid)
            self.assertEqual(task.workspace_kind, 'worktree')
            self.assertEqual(task.workspace_path, str(self.project_path / '.worktrees' / cid))
            self.assertNotEqual(task.workspace_path, root_task.workspace_path)
            self.assertEqual((task.project_id, task.completion_contract), (self.project_id, REPO))

    def test_f1_unmanaged_workspace_override_legacy_control(self):
        root = self.root()
        legacy_path = str((Path(self.tmp.name) / 'legacy-workspace').resolve())
        proposal = self.child(workspace_kind='dir', workspace_path=legacy_path)
        cleaned, reason = kd._clean_children(root, [proposal], self.routing)
        self.assertEqual(reason, '')
        self.assertEqual(set(cleaned[0]), {'title', 'body', 'assignee', 'parents'})
        ids = decompose_triage_task(self.conn, root, root_assignee='orchestrator', children=[proposal])
        task = kb.get_task(self.conn, ids[0])
        # Unpatched graph INSERT omits completion_contract; the additive column
        # is nullable with no default. Unlike create_task, this path stores NULL.
        # Preserve that unmanaged behavior rather than imposing managed policy.
        self.assertEqual((task.workspace_kind, task.workspace_path, task.completion_contract),
                         ('dir', legacy_path, None))
