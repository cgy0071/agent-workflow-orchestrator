"""Authenticated project extensions outside the fixed MCP core catalog.

CLI and opt-in HTTP/WS use this same boundary. The domain owns payload
validation; the service owns credentials, scopes, epochs and durable replay.
"""
import json
import sqlite3

import awo_service as service
import collaboration_runtime as collaboration
import merge_queue as merge
import project_knowledge as knowledge
import service_access as access
from service_api import APIError, _error_code, _fail

WRITE_ROLES = {name: ('worker', 'reviewer', 'controller') for name in knowledge.COMMANDS}
WRITE_ROLES.update({name: ('reviewer', 'controller') for name in
                    ('knowledge_review', 'knowledge_promote', 'doc_review')})
WRITE_ROLES.update({name: ('controller',) for name in
                    ('knowledge_source_approve', 'knowledge_source_revoke', 'doc_publish')})
MERGE_COMMANDS = frozenset(('merge.policy', 'merge.propose', 'merge.materialize',
                            'merge.enqueue', 'merge.begin', 'merge.review',
                            'merge.cancel', 'merge.recover', 'merge.finish', 'artifact.begin'))
MERGE_QUERIES = frozenset(('merge_queue', 'merge_intents'))
COMMANDS = knowledge.COMMANDS | MERGE_COMMANDS | collaboration.COMMANDS
QUERIES = knowledge.QUERIES | MERGE_QUERIES | collaboration.QUERIES
WRITE_ROLES.update({name: ('controller',) for name in MERGE_COMMANDS | collaboration.COMMANDS})
WRITE_ROLES['merge.review'] = ('controller', 'reviewer')


def create_extension_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS v2_project_content_root (
      project_id TEXT PRIMARY KEY REFERENCES v2_project(project_id),
      root_path TEXT NOT NULL, root_sha256 TEXT NOT NULL, revision INTEGER NOT NULL,
      approved_by TEXT NOT NULL, created_at TEXT NOT NULL)''')


def bind_root(con, project_id, actor, payload):
    if set(payload) != {'project_root', 'expected_revision'} or type(payload['expected_revision']) is not int:
        _fail('invalid_arguments', 'project.bind_root needs project_root and expected_revision=0')
    root = access.canonical_root(payload['project_root'])
    if payload['expected_revision'] != 0 or con.execute(
            'SELECT 1 FROM v2_project_content_root WHERE project_id=?', (project_id,)).fetchone():
        _fail('revision_conflict', 'Content root is immutable once bound; replay the original command')
    digest = access.root_hash(root)
    con.execute('INSERT INTO v2_project_content_root VALUES (?,?,?,?,?,?)',
                (project_id, root, digest, 1, actor, service._now()))
    service._event(con, project_id, 'project', project_id, 'project.content_root_bound', actor,
                   'v2', None, {'root_sha256': digest, 'revision': 1})
    return {'status': 'ROOT_BOUND', 'project_id': project_id, 'project_root': root,
            'root_sha256': digest, 'revision': 1}


def tool_specs():
    specs = []
    for name in sorted(COMMANDS | QUERIES):
        write = name in COMMANDS
        properties = {'payload': {'type': 'object', 'additionalProperties': True}}
        if write:
            properties.update(command_id={'type': 'string', 'minLength': 8, 'maxLength': 128,
                                          'pattern': '^[A-Za-z0-9._:-]{8,128}$'},
                              expected_epoch={'type': 'integer', 'minimum': 0})
        specs.append({'name': name, 'description': 'Project extension: ' + name,
                      'inputSchema': {'type': 'object', 'properties': properties,
                                      'required': list(properties), 'additionalProperties': False},
                      'annotations': {'readOnlyHint': not write}})
    return specs


def _authorize(con, principal, tool, payload):
    if tool not in knowledge.COMMANDS | knowledge.QUERIES:
        return _authorize_domain(con, principal, tool, payload)
    role = principal['role']
    allowed = WRITE_ROLES.get(tool, ('worker', 'reviewer', 'controller', 'observer'))
    if role not in allowed:
        _fail('forbidden_role', 'This credential cannot perform the project extension')
    if principal.get('task_id') or principal.get('assignment_id'):
        _fail('forbidden_role', 'A task/run credential does not grant project knowledge access')
    root = con.execute('SELECT * FROM v2_project_content_root WHERE project_id=?',
                       (principal['project_id'],)).fetchone()
    if not root or access.root_hash(principal['project_root']) != root['root_sha256']:
        _fail('project_mismatch', 'Controller must explicitly bind the project content root first')
    if access.root_hash(root['root_path']) != root['root_sha256']:
        _fail('project_mismatch', 'Bound project content root changed')
    entity = payload.get('knowledge_id') or payload.get('doc_id')
    scoped = principal.get('knowledge_id')
    if scoped and (entity != scoped or tool in ('knowledge_list', 'doc_list')):
        _fail('forbidden_role', 'Credential authorizes one knowledge/document entity')
    # Re-check current visibility before returning a saved result. A narrowed
    # credential or revoked personal-reader grant must not recover old content.
    if entity:
        name = 'doc_get' if tool.startswith('doc_') else 'knowledge_get'
        key = 'doc_id' if name == 'doc_get' else 'knowledge_id'
        knowledge.query(con, principal['project_id'], principal, name, {key: entity})


def _identity(payload, name):
    value = payload.get(name)
    if name in payload and (not isinstance(value, str) or not value or len(value) > 1024):
        _fail('invalid_arguments', name + ' must be a nonempty identifier')
    return value


def _policy_root(con, principal, policy_id):
    policy = merge._policy(con, principal['project_id'], policy_id)
    return access.root_hash(policy['repo_root']) == access.root_hash(principal['project_root'])


def _authorize_domain(con, principal, tool, payload):
    # Scope checks run even on a replay. Never replace the authenticated Agent
    # identity with a fabricated reviewer identity or broaden its credential.
    role = principal['role']
    if principal.get('knowledge_id') or role not in WRITE_ROLES.get(
            tool, ('controller', 'reviewer', 'worker', 'observer')):
        _fail('forbidden_role', 'Credential does not authorize this domain route')
    if tool in COMMANDS:
        if role == 'controller' and (principal.get('task_id') or principal.get('assignment_id')):
            _fail('forbidden_role', 'Domain control requires a project controller credential')
        if role == 'reviewer' and (tool != 'merge.review' or not principal.get('task_id')):
            _fail('forbidden_role', 'Reviewer requires a Task scope and merge.review')
    task_id, assignment_id = (_identity(payload, key) for key in ('task_id', 'assignment_id'))
    policy_id = _identity(payload, 'policy_id')
    if tool in MERGE_COMMANDS | MERGE_QUERIES:
        merge._principal(con, principal['project_id'], principal, (role,))
        if 'merge_request_id' in payload:
            row, _ = merge._mr(con, principal['project_id'], _identity(payload, 'merge_request_id'))
            if task_id and task_id != row['task_id']:
                _fail('forbidden_role', 'Merge request and Task scope differ')
            task_id, assignment_id, policy_id = row['task_id'], row['assignment_id'], row['policy_id']
        if 'intent_id' in payload:
            row, bundle = merge._intent(con, principal['project_id'], _identity(payload, 'intent_id'))
            task_id, policy_id = row['task_id'], bundle['policy_id']
        if tool == 'merge.policy':
            if access.root_hash(payload.get('repo_root')) != access.root_hash(principal['project_root']):
                _fail('project_mismatch', 'Merge repository must equal the credential project root')
        elif policy_id and not _policy_root(con, principal, policy_id):
            _fail('project_mismatch', 'Merge policy belongs to a different credential root')
    else:
        collaboration._principal(con, principal['project_id'], principal)
        if 'ticket_id' in payload:
            row, _ = collaboration._ticket(con, principal['project_id'], _identity(payload, 'ticket_id'))
            task_id = row['task_id']
        # These projections expose the complete Task policy and history, not
        # an individual Assignment. Do not silently widen an Assignment token.
        if tool in collaboration.QUERIES and principal.get('assignment_id'):
            _fail('forbidden_role', 'Collaboration projections require Task or project scope')
    if principal.get('task_id') and not task_id and tool != 'merge_queue':
        _fail('forbidden_role', 'This credential requires an explicit matching Task')
    access.require_scope(con, principal, task_id, assignment_id,
                         own=role == 'worker' and bool(assignment_id))
    if tool == 'merge.review':
        binding = merge._bound_reviewer(con, principal['agent_id'], principal.get('binding_id'),
                                        principal.get('binding_version'))
        merge._independent(con, principal['project_id'], task_id, principal['agent_id'], binding)


def _domain_query(con, principal, tool, payload):
    project = principal['project_id']
    if tool in knowledge.QUERIES:
        return knowledge.query(con, project, principal, tool, payload)
    if tool in collaboration.QUERIES:
        return collaboration.query(con, project, principal, tool, payload)
    if tool == 'merge_queue':
        filters = dict(payload)
        if principal.get('task_id'):
            filters['task_id'] = principal['task_id']
        rows = merge.read_queue(con, project, principal, filters)
        items = []
        for item in rows:
            if principal.get('assignment_id') and item['assignment_id'] != principal['assignment_id']:
                continue
            row, _ = merge._mr(con, project, item['merge_request_id'])
            if _policy_root(con, principal, row['policy_id']):
                items.append(item)
        return {'items': items}
    if set(payload) != {'task_id'}:
        _fail('invalid_arguments', 'merge_intents requires only task_id')
    items = []
    for item in merge.read_intents(con, project, principal, payload['task_id']):
        _, bundle = merge._intent(con, project, item['intent_id'])
        accepted = bundle.get('accepted') or bundle.get('spec', {}).get('accepted') or {}
        if principal.get('assignment_id'):
            if accepted.get('assignment_id') != principal['assignment_id']:
                continue
        if principal['role'] == 'worker':
            owner = con.execute('SELECT agent_id FROM v2_assignment WHERE assignment_id=? AND task_id=?',
                                (accepted.get('assignment_id'), item['task_id'])).fetchone()
            if not owner or owner['agent_id'] != principal['agent_id']:
                continue
        if _policy_root(con, principal, bundle['policy_id']):
            items.append(item)
    return {'items': items}


def _domain_command(con, principal, tool, payload):
    domain = knowledge if tool in knowledge.COMMANDS else (
        merge if tool in MERGE_COMMANDS else collaboration)
    # Domain commands reserve intents or validate persisted receipts only.
    # External Git/provider/verifier runners belong to explicit post-commit CLI.
    return domain.apply_command(con, principal['project_id'], principal, tool, payload)


def call(state, project_root, token, tool, arguments):
    from service_api import _validate
    specs = {item['name']: item['inputSchema'] for item in tool_specs()}
    if tool not in specs:
        _fail('invalid_arguments', 'Unknown project extension')
    _validate(arguments, specs[tool])
    try:
        if len(json.dumps(arguments, allow_nan=False).encode('utf-8')) > knowledge.MAX_PAYLOAD_BYTES:
            _fail('invalid_arguments', 'Extension request exceeds its byte limit')
    except (ValueError, TypeError, RecursionError) as exc:
        raise APIError('invalid_arguments', 'Invalid extension JSON') from exc
    write = tool in COMMANDS
    con = service._connect(service._db_path(state), readonly=not write)
    principal = None
    try:
        con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        principal = access.authenticate(con, project_root, token)
        payload = arguments['payload']
        _authorize(con, principal, tool, payload)
        if not write:
            result = _domain_query(con, principal, tool, payload)
            con.rollback()
            return result
        state_row = service._state_row(con)
        if arguments['expected_epoch'] != state_row['controller_epoch']:
            _fail('revision_conflict', 'Controller epoch changed')
        if state_row['frozen'] or not state_row['v2_enabled']:
            _fail('state_conflict', 'Application writes are frozen or disabled')
        env = service._command_envelope({'tool': tool, 'payload': payload,
            'command_id': arguments['command_id'], 'expected_epoch': arguments['expected_epoch']},
            COMMANDS)
        sha = service._request_sha(env)
        response = service._replay(con, principal['project_id'], principal['principal_id'], env, sha)
        if response is None:
            response = _domain_command(con, principal, tool, payload)
            response['replayed'] = False
            service._record(con, principal['project_id'], principal['principal_id'], env, sha, response)
            domain = 'knowledge' if tool in knowledge.COMMANDS else (
                'merge' if tool in MERGE_COMMANDS else 'collaboration')
            identity = response.get('entity_id') or response.get('merge_request_id') or response.get(
                'intent_id') or response.get('verification_id') or payload.get('task_id', '')
            service._event(con, principal['project_id'], domain, identity,
                           'extension.' + tool, principal['principal_id'], 'v2-api', None,
                           {'command_id': arguments['command_id'], 'revision': response.get('revision')})
        con.commit()
        return response
    except (ValueError, OSError, sqlite3.Error) as exc:
        con.rollback()
        code = _error_code(exc)
        if write and principal is not None:
            try:
                con.execute('BEGIN IMMEDIATE')
                service._event(con, principal['project_id'], 'command', arguments['command_id'],
                               'command.rejected', principal['principal_id'], 'v2-api', None,
                               {'tool': tool, 'code': code})
                con.commit()
            except sqlite3.Error:
                con.rollback()
        raise APIError(code, str(exc)) from exc
    finally:
        con.close()
