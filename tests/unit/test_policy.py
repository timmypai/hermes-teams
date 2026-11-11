import unittest
from hermes_teams.policy import normalize

REPO = 'timmypai/hermes-teams'

def policy():
    return {'version': 1, 'board': 'seven-bot-v1', 'project': 'hermes-teams',
            'repository': REPO, 'grants': {'child:root:0': {'work_kind': 'implementation', 'assignee': 'developer'}}}

class PolicyTests(unittest.TestCase):
    def test_implementation_requires_authorized_repository(self):
        result = normalize(policy(), 'child:root:0', {'assignee': 'developer'})
        self.assertEqual(result.get('completion_contract'), REPO)
        self.assertEqual(result['project_id'], 'hermes-teams')

    def test_proposals_cannot_authorize_or_downgrade(self):
        for proposal in ({'completion_contract': 'local-only'}, {'repository': 'other/repo'},
                         {'completion_contract': 'other/repo'}, {'work_kind': 'research'},
                         {'work_kind': 'unknown'}, {'assignee': 'researcher'},
                         {'project_id': 'other'}):
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                normalize(policy(), 'child:root:0', proposal)

    def test_malformed_policy_fails_closed(self):
        cases = [None, {}, {**policy(), 'board': 'other'}, {**policy(), 'project': 'other'},
                 {**policy(), 'repository': 'other/repo'}, {**policy(), 'version': True},
                 {**policy(), 'extra': True}, {**policy(), 'grants': {}},
                 {**policy(), 'grants': {'child:root:0': {'work_kind': 'nonsense', 'assignee': 'developer'}}},
                 {**policy(), 'grants': {'child:root:0': {'work_kind': 'implementation', 'assignee': 'researcher'}}}]
        for value in cases:
            with self.subTest(policy=value), self.assertRaises(ValueError):
                normalize(value, 'child:root:0', {})

    def test_non_pr_classification_is_explicit_not_inherited(self):
        for kind, role in [('research', 'researcher'), ('acceptance', 'reviewer'),
                           ('deploy-existing-image', 'devops')]:
            trusted = policy()
            trusted['grants']['child:root:0'] = {'work_kind': kind, 'assignee': role}
            result = normalize(trusted, 'child:root:0', {})
            self.assertEqual(result['completion_contract'], 'local-only')
            with self.assertRaises(ValueError):
                normalize(trusted, 'child:root:0', {'completion_contract': REPO})

    def test_malformed_grant_operation_keys_are_rejected(self):
        for key in ('create:', 'single:', 'child:root:-1', 'child:root:true', 'anything', 'child::0'):
            trusted = policy()
            trusted['grants'] = {key: {'work_kind': 'implementation', 'assignee': 'developer'}}
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize(trusted, key, {})
