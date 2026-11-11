"""Deterministic normalization of proposals under explicitly trusted policy."""

ROLES = {
    'implementation': {'developer'}, 'ci': {'developer', 'devops'},
    'docker': {'developer', 'devops'}, 'research': {'researcher', 'architect'},
    'acceptance': {'orchestrator', 'reviewer'}, 'deploy-existing-image': {'devops'},
}


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {'version', 'board', 'project', 'repository', 'grants'}:
        raise ValueError('malformed policy')
    if (type(policy['version']) is not int or policy['version'] != 1
            or policy['board'] != 'seven-bot-v1' or policy['project'] != 'hermes-teams'
            or policy['repository'] != 'timmypai/hermes-teams'):
        raise ValueError('unsupported policy scope/version/repository')
    if not isinstance(policy['grants'], dict):
        raise ValueError('malformed grants')
    import re
    for key, grant in policy['grants'].items():
        if (not isinstance(key, str) or not re.fullmatch(r'(child:[^:\s]+:(0|[1-9][0-9]*)|single:[^:\s]+|create:[^\s]+)', key)
                or not isinstance(grant, dict) or set(grant) != {'work_kind', 'assignee'}
                or not isinstance(grant['work_kind'], str)
                or not isinstance(grant['assignee'], str)
                or grant['work_kind'] not in ROLES
                or grant['assignee'] not in ROLES[grant['work_kind']]):
            raise ValueError('invalid trusted classification or role')
    return policy


def normalize(policy, key, proposal):
    validate_policy(policy)
    if key not in policy['grants']:
        raise ValueError('missing trusted operation grant')
    if not isinstance(proposal, dict):
        raise ValueError('proposal must be an object')
    grant = policy['grants'][key]
    contract = policy['repository'] if grant['work_kind'] in {'implementation', 'ci', 'docker'} else 'local-only'
    expected = {'completion_contract': contract,
                'repository': policy['repository'], 'project_id': policy['project'],
                'assignee': grant['assignee'], 'work_kind': grant['work_kind']}
    for field, value in expected.items():
        if field in proposal and proposal[field] != value:
            raise ValueError(f'proposal contradicts trusted {field}')
    # Model output supplies task content, never execution configuration.
    content = {field: proposal[field] for field in ('title', 'body', 'parents')
               if field in proposal}
    return {**content, **expected}


def clean_children(policy, task_id, entries, valid_names):
    """Managed cleaner; do not launder contradictory model fields by dropping them."""
    children = []
    for index, entry in enumerate(entries):
        child = normalize(policy, f'child:{task_id}:{index}', entry)
        title = child.get('title')
        if not isinstance(title, str) or not title.strip():
            raise ValueError('child title required')
        if child['assignee'] not in valid_names:
            raise ValueError('trusted specialist is not installed')
        child['title'] = title.strip()[:200]
        body = child.get('body', '')
        child['body'] = body.strip() if isinstance(body, str) else ''
        child.setdefault('parents', [])
        children.append(child)
    validate_edges(children)
    return children


def validate_child_fields(child):
    """Managed graph input is not trusted merely because it looks normalized."""
    allowed = {'title', 'body', 'assignee', 'parents', 'completion_contract',
               'repository', 'project_id', 'work_kind'}
    if not isinstance(child, dict) or set(child) - allowed:
        raise ValueError('unsupported managed child field')


def validate_edges(children):
    for index, child in enumerate(children):
        validate_child_fields(child)
        edges = child.get('parents', [])
        if not isinstance(edges, list) or any(
                type(p) is not int or p < 0 or p >= len(children) or p == index for p in edges):
            raise ValueError('invalid dependency indices')
    remaining = set(range(len(children)))
    while remaining:
        ready = {i for i in remaining if not (set(children[i].get('parents', [])) & remaining)}
        if not ready:
            raise ValueError('cyclic dependency')
        remaining -= ready


def prepare(policy, key, proposal, *, previous=None):
    """A managed mutation cannot loosen a previously declared PR contract."""
    import re
    result = normalize(policy, key, proposal)
    if previous not in (None, 'local-only'):
        repo = policy['repository']
        if result['completion_contract'] != repo:
            raise ValueError('cannot downgrade declared PR contract')
        if previous != repo and not re.fullmatch(
                r'https://github\.com/' + re.escape(repo) + r'/pull/[1-9][0-9]*', previous):
            raise ValueError('existing contract is outside authorized repository')
        result['completion_contract'] = previous
    return result


def select_policy(bindings, database_path):
    """Select by actual connection identity, not caller/model board parameters."""
    from copy import deepcopy
    from pathlib import Path
    if not isinstance(bindings, dict):
        raise ValueError('malformed site bindings')
    for path, policy in bindings.items():
        if not isinstance(path, str) or not Path(path).is_absolute() or str(Path(path).resolve()) != path:
            raise ValueError('site binding must use canonical absolute DB path')
        validate_policy(policy)
    return deepcopy(bindings.get(database_path))
