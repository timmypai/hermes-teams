"""Portable authority-boundary regressions; no native imports or databases."""
import unittest

from hermes_teams import policy as subject
from test_policy import policy, REPO


OVERRIDES = (
    {'workspace_kind': 'dir', 'workspace_path': '/unapproved/other-repository'},
    {'workspace_kind': 'worktree'},
    {'workspace_path': '/unapproved/other-repository'},
    {'workspace_path': None},
    {'branch_name': 'unapproved-branch'},
    {'model_override': 'unapproved-model'},
    {'provider_override': 'unapproved-provider'},
    {'reasoning_effort': 'high'},
    {'skills': ['unapproved-skill']},
    {'max_runtime_seconds': 999999},
    {'execution_config': {'workspace_path': '/unapproved/other-repository'}},
)


class WorkspaceTests(unittest.TestCase):
    def test_cleaner_outputs_only_permitted_child_fields(self):
        original = {'title': ' Implement ', 'body': ' details ',
                    'assignee': 'developer', 'parents': []}
        expected = {'title': 'Implement', 'body': 'details', 'parents': [],
                    'assignee': 'developer', 'completion_contract': REPO,
                    'repository': REPO, 'project_id': 'hermes-teams',
                    'work_kind': 'implementation'}
        for override in OVERRIDES:
            proposal = {**original, **override}
            with self.subTest(override=override):
                self.assertEqual(subject.clean_children(
                    policy(), 'root', [proposal], {'developer'}), [expected])
                self.assertEqual(proposal, {**original, **override})

    def test_direct_managed_graph_rejects_execution_configuration(self):
        normalized = subject.normalize(policy(), 'child:root:0',
                                       {'title': 'Implement', 'parents': []})
        # This is the pure validation boundary called by the native transaction
        # BEFORE normalization/insertion. Normalized-looking input is untrusted.
        for override in OVERRIDES:
            with self.subTest(override=override), self.assertRaisesRegex(
                    ValueError, 'unsupported managed child field'):
                subject.validate_edges([normalized, {**normalized, **override}])

    def test_direct_managed_graph_allows_content_and_trusted_metadata(self):
        first = subject.normalize(policy(), 'child:root:0',
                                  {'title': 'First', 'body': 'Details', 'parents': []})
        second = {**first, 'title': 'Second', 'parents': [0]}
        self.assertIsNone(subject.validate_edges([first, second]))
        self.assertEqual((first['project_id'], first['completion_contract']),
                         ('hermes-teams', REPO))
