import unittest
from test_policy import policy, REPO
from hermes_teams import policy as subject

class BoundaryTests(unittest.TestCase):
    def test_managed_cleaner_preserves_normalized_contract_and_dependencies(self):
        trusted = policy()
        children = subject.clean_children(trusted, 'root', [
            {'title': ' Implement ', 'body': ' details ', 'parents': [], 'assignee': 'developer'}], {'developer'})
        self.assertEqual(children, [{'title': 'Implement', 'body': 'details', 'parents': [],
            'assignee': 'developer', 'completion_contract': REPO, 'repository': REPO,
            'project_id': 'hermes-teams', 'work_kind': 'implementation'}])

    def test_managed_cleaner_rejects_malformed_or_cyclic_edges(self):
        trusted = policy()
        trusted['grants']['child:root:1'] = trusted['grants']['child:root:0']
        for edges in ([False], [0], [2], 'bad', None):
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                subject.clean_children(trusted, 'root', [{'title': 'x', 'parents': edges}], {'developer'})
        with self.assertRaises(ValueError):
            subject.clean_children(trusted, 'root', [
                {'title': 'a', 'parents': [1]}, {'title': 'b', 'parents': [0]}], {'developer'})

    def test_existing_pr_contract_cannot_be_downgraded_even_by_non_pr_grant(self):
        trusted = policy()
        trusted['grants']['single:root'] = {'work_kind': 'research', 'assignee': 'researcher'}
        with self.assertRaises(ValueError):
            subject.prepare(trusted, 'single:root', {}, previous=REPO)
        trusted['grants']['single:root'] = {'work_kind': 'implementation', 'assignee': 'developer'}
        exact = 'https://github.com/timmypai/hermes-teams/pull/12'
        self.assertEqual(subject.prepare(trusted, 'single:root', {}, previous=exact)['completion_contract'], exact)
        with self.assertRaises(ValueError):
            subject.prepare(trusted, 'single:root', {}, previous='another/repo')

    def test_policy_binding_is_explicit_and_unmanaged_database_is_unchanged(self):
        bindings = {'/example/managed.db': policy()}
        self.assertIsNone(subject.select_policy(bindings, '/example/other.db'))
        selected = subject.select_policy(bindings, '/example/managed.db')
        self.assertEqual(selected, policy())
        selected['grants'].clear()
        self.assertTrue(bindings['/example/managed.db']['grants'])
        for bad in (None, {'relative.db': policy()}, {'/example/managed.db': {}},
                    {'/example/../managed.db': policy()}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                subject.select_policy(bad, '/example/managed.db')

    def test_cleaner_rejects_unknown_specialist_and_contradictions(self):
        for proposal, roster in [({'title': 'x'}, {'researcher'}),
                                  ({'title': 'x', 'completion_contract': 'local-only'}, {'developer'}),
                                  ({'title': 'x', 'repository': 'elsewhere/repo'}, {'developer'}),
                                  ({'title': 'x', 'work_kind': 'research'}, {'developer'})]:
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                subject.clean_children(policy(), 'root', [proposal], roster)

    def test_trusted_ci_and_docker_changes_require_pr_contract(self):
        for kind in ('ci', 'docker'):
            trusted = policy()
            trusted['grants']['child:root:0'] = {'work_kind': kind, 'assignee': 'devops'}
            self.assertEqual(subject.normalize(trusted, 'child:root:0', {})['completion_contract'], REPO)
            with self.assertRaises(ValueError):
                subject.normalize(trusted, 'child:root:0', {'completion_contract': 'local-only'})
