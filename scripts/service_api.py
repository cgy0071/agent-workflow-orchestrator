"""Shared authenticated application operations for CLI, MCP and HTTP.

Transports neither own task state nor issue authorization. Commands execute in
the existing workflow ledger with role/scope checks before durable replay.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import uuid

import awo_service as service
import service_access as access

READ_TOOLS = frozenset(('awo_task_get', 'awo_task_list', 'awo_task_next',
                        'awo_assignment_get', 'awo_message_read'))
ROLES = {
    'awo_task_get': ('controller', 'worker', 'reviewer', 'observer'),
    'awo_task_list': ('controller', 'worker', 'reviewer', 'observer'),
    'awo_task_create': ('controller',),
    'awo_task_next': ('controller', 'worker'),
    'awo_assignment_claim': ('controller', 'worker'),
    'awo_assignment_get': ('controller', 'worker', 'reviewer', 'observer'),
    'awo_assignment_complete': ('worker',),
    'awo_agent_register': ('enrollment',),
    'awo_agent_heartbeat': ('controller', 'worker', 'reviewer'),
    'awo_evidence_record': ('worker',),
    'awo_review_submit': ('reviewer',),
    'awo_resource_claim': ('controller', 'worker'),
    'awo_resource_release': ('controller', 'worker'),
    'awo_message_send': ('controller', 'worker', 'reviewer'),
    'awo_message_read': ('controller', 'worker', 'reviewer', 'observer'),
    'awo_message_ack': ('controller', 'worker', 'reviewer'),
}


class APIError(service.ServiceError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise APIError(code, message)


def _str(maximum=128, minimum=1):
    return {'type': 'string', 'minLength': minimum, 'maxLength': maximum}


def _int(minimum=1, maximum=2147483647):
    return {'type': 'integer', 'minimum': minimum, 'maximum': maximum}


def _obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


def _array(items=None, minimum=0, maximum=64):
    return {'type': 'array', 'items': items or _str(), 'minItems': minimum, 'maxItems': maximum}


def tool_specs(messages_enabled=False):
    """Version-one schemas, exactly 13 core tools (16 with messaging)."""
    ident, rev = _str(), _int()
    digest = dict(_str(64, 64), pattern='^[0-9a-f]{64}$')
    aid = {'assignment_id': ident, 'expected_revision': rev}
    definitions = [
        ('awo_task_get', 'Read one scoped Task and immutable evidence references.',
         _obj({'task_id': ident}, ('task_id',))),
        ('awo_task_list', 'List project Tasks in stable order, respecting credential scope.',
         _obj({'limit': _int(1, 200), 'after': ident, 'task_state': {'enum': list(service.TASK_STATES)}})),
        ('awo_task_create', 'Controller creates a draft Task; does not authorize dispatch.',
         _obj({'task_id': ident, 'title': _str(512), 'body': _str(32000, 0),
               'priority': {'enum': list(service.PRIORITIES)},
               'execution_mode': {'enum': list(service.EXECUTION_MODES)},
               'fanout_slots': _int(1, 8)}, ('title',))),
        ('awo_task_next', 'Read a queue candidate; final atomic admission may still refuse it.',
         _obj({})),
        ('awo_assignment_claim', 'Consume a Controller-approved queue entry for this Agent.',
         _obj({'task_id': ident, 'expected_revision': rev, 'agent_id': ident,
               'slot_id': _int(0, 8)}, ('task_id', 'expected_revision'))),
        ('awo_assignment_get', 'Read a scoped Assignment, claim and evidence metadata.',
         _obj({'assignment_id': ident}, ('assignment_id',))),
        ('awo_assignment_complete', 'Request real adapter observation of completion; never self-accept.',
         _obj(dict(aid, generation=rev, outcome={'enum': ['completed', 'failed', 'blocked']},
                   evidence_ids=_array(minimum=1), summary=_str(8000)),
              ('assignment_id', 'expected_revision', 'generation', 'outcome', 'evidence_ids', 'summary'))),
        ('awo_agent_register', 'Consume a root-bound enrollment bearer grant; return one Agent credential.',
         _obj({'handle': _str(32, 2), 'harness': _str(128), 'harness_version': _str(128),
               'host': _str(255), 'os': _str(128), 'capabilities': _array(_str(64))},
              ('handle', 'harness', 'harness_version', 'host', 'os'))),
        ('awo_agent_heartbeat', 'Update only the authenticated Agent liveness.',
         _obj({'expected_revision': rev, 'status': _str(128), 'detail': _str(2000, 0)},
              ('expected_revision',))),
        ('awo_evidence_record', 'Rehash a regular file inside the Assignment delivery root.',
         _obj(dict(aid, kind={'enum': ['log', 'test-report', 'diff', 'artifact', 'transcript', 'data']},
                   path=_str(1024), sha256=digest, bytes=_int(0, 16777216)),
              ('assignment_id', 'expected_revision', 'kind', 'path', 'sha256', 'bytes'))),
        ('awo_review_submit', 'Independently seal a verdict; Controller closure releases the claim.',
         _obj(dict(aid, task_id=ident, event_id=ident, evidence_ids=_array(minimum=1),
                   verdict={'enum': ['accept', 'repair', 'reject', 'blocked']},
                   review_note_path=_str(1024), review_note_sha256=digest),
              ('assignment_id', 'expected_revision', 'task_id', 'event_id', 'evidence_ids',
               'verdict', 'review_note_path', 'review_note_sha256'))),
        ('awo_resource_claim', 'Bind an existing Controller-approved reservation; cannot expand capacity.',
         _obj(dict(aid, resources={'type': 'object', 'maxProperties': 8,
                                  'additionalProperties': _int()}),
              ('assignment_id', 'expected_revision', 'resources'))),
        ('awo_resource_release', 'Confirm release only after authoritative Controller closure.',
         _obj(aid, ('assignment_id', 'expected_revision'))),
    ]
    if messages_enabled:
        definitions += [
            ('awo_message_send', 'Persist a message; no Task state change or claimed live injection.',
             _obj({'to': _array(minimum=1, maximum=50), 'kind': {'enum': [
                 'question', 'answer', 'handoff', 'notice', 'consult', 'state_request', 'steer']},
                   'body': _str(65536), 'task_id': ident, 'assignment_id': ident,
                   'generation': rev, 'thread_id': ident, 'reply_to': ident,
                   'priority': {'enum': list(service.PRIORITIES)}, 'expires_at': _str(64)},
                  ('to', 'kind', 'body'))),
            ('awo_message_read', 'Read persisted deliveries without acknowledging them.',
             _obj({'limit': _int(1, 200), 'offset': _int(0), 'thread_id': ident,
                   'task_id': ident, 'assignment_id': ident, 'message_id': ident,
                   'delivery_id': ident, 'kind': _str(32),
                   'state': {'enum': ['held', 'sending', 'pushed', 'notified', 'read',
                                      'acknowledged', 'answered', 'expired']}})),
            ('awo_message_ack', 'Advance one owned delivery using a revision guard.',
             _obj({'delivery_id': ident, 'state': {'enum': ['read', 'acknowledged']},
                   'expected_revision': rev}, ('delivery_id', 'state', 'expected_revision'))),
        ]
    result = []
    for name, description, schema in definitions:
        read = name in READ_TOOLS
        if not read:
            schema['properties'].update(command_id=dict(_str(128, 8), pattern='^[A-Za-z0-9._:-]{8,128}$'),
                                        expected_epoch=_int(0))
            schema['required'] += ['command_id', 'expected_epoch']
        result.append({'name': name, 'description': description, 'inputSchema': schema,
                       'annotations': {'readOnlyHint': read, 'destructiveHint': not read,
                                       'idempotentHint': True, 'openWorldHint': False}})
    return result


def _validate(value, schema, path='arguments'):
    """Validate the supported schema subset again at the application boundary."""
    if 'enum' in schema and value not in schema['enum']:
        _fail('invalid_arguments', path + ': value is not permitted')
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    if kind and type(value) is not types[kind]:
        _fail('invalid_arguments', path + ': invalid type')
    if kind == 'object':
        if len(value) > schema.get('maxProperties', 128) or set(schema.get('required', ())) - set(value):
            _fail('invalid_arguments', path + ': missing fields or too many fields')
        props, extra = schema.get('properties', {}), schema.get('additionalProperties', False)
        for key, item in value.items():
            if not isinstance(key, str) or (key not in props and extra is False):
                _fail('invalid_arguments', path + ': unknown field')
            if key in props or isinstance(extra, dict):
                _validate(item, props.get(key, extra), path + '.' + key)
    elif kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 200):
            _fail('invalid_arguments', path + ': array size')
        for item in value:
            _validate(item, schema['items'], path)
    elif kind == 'string':
        if (not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 65536)
                or chr(0) in value or ('pattern' in schema and not re.search(schema['pattern'], value))):
            _fail('invalid_arguments', path + ': invalid string')
    elif kind == 'integer':
        if not schema.get('minimum', 0) <= value <= schema.get('maximum', 2147483647):
            _fail('invalid_arguments', path + ': integer outside range')


def _authorize(con, principal, tool, payload):
    if principal.get('knowledge_id'):
        _fail('forbidden_role', 'A knowledge credential does not authorize Task tools')
    if principal['role'] not in ROLES.get(tool, ()):
        _fail('forbidden_role', 'This credential may not call ' + tool)
    if principal.get('assignment_id') and tool in ('awo_assignment_claim', 'awo_task_next'):
        _fail('forbidden_role', 'Assignment credentials cannot acquire another task')
    if tool == 'awo_task_create':
        if principal.get('task_id') or principal.get('assignment_id'):
            _fail('forbidden_role', 'Scoped credentials cannot create a different Task')
    else:
        access.require_scope(con, principal, payload.get('task_id'), payload.get('assignment_id'),
                             own=principal['role'] == 'worker' and bool(payload.get('assignment_id')))
    if principal.get('task_id') and tool == 'awo_task_next':
        _fail('forbidden_role', 'Task credentials cannot inspect a project queue')
    if (tool == 'awo_assignment_claim' and principal['role'] == 'worker'
            and payload.get('agent_id', principal['agent_id']) != principal['agent_id']):
        _fail('forbidden_role', 'Worker cannot claim for another Agent')
    if tool.startswith('awo_message_') and principal.get('task_id'):
        # A task/run credential grants neither a project-wide inbox nor replies
        # to a foreign context inherited indirectly through reply_to/delivery_id.
        if tool == 'awo_message_read':
            payload['task_id'] = principal['task_id']
            if principal.get('assignment_id'):
                payload['assignment_id'] = principal['assignment_id']
        else:
            row = None
            if tool == 'awo_message_ack':
                row = con.execute('SELECT m.task_id,m.assignment_id FROM v2_message_delivery d '
                                  'JOIN v2_message m ON m.message_id=d.message_id WHERE d.delivery_id=?',
                                  (payload['delivery_id'],)).fetchone()
            elif payload.get('reply_to'):
                row = con.execute('SELECT task_id,assignment_id FROM v2_message WHERE message_id=?',
                                  (payload['reply_to'],)).fetchone()
            if row is not None:
                if row['task_id'] != principal['task_id']:
                    _fail('forbidden_role', 'Message context is outside this credential')
                if principal.get('assignment_id') and row['assignment_id'] != principal['assignment_id']:
                    _fail('forbidden_role', 'Message belongs to a different Assignment context')
                access.require_scope(con, principal, row['task_id'], row['assignment_id'])
            elif tool == 'awo_message_send':
                if payload.get('task_id') != principal['task_id']:
                    _fail('forbidden_role', 'Scoped messages must declare their authorized task')
                if principal.get('assignment_id') and payload.get('assignment_id') != principal['assignment_id']:
                    _fail('forbidden_role', 'Scoped messages must declare their authorized Assignment')
            else:
                _fail('not_found', 'Message delivery is unavailable')


def _revision(row, expected):
    if row['revision'] != expected:
        _fail('revision_conflict', 'Entity revision changed')


def _assignment(con, principal, payload):
    import priority_scheduler as scheduler
    value = scheduler.assignment_get(con, principal['project_id'], payload['assignment_id'])
    if 'expected_revision' in payload:
        _revision(value, payload['expected_revision'])
    if 'generation' in payload and value['generation'] != payload['generation']:
        _fail('state_conflict', 'Assignment generation changed')
    return value


def _evidence(con, assignment_id):
    return [dict(row) for row in con.execute(
        'SELECT evidence_id,kind,path,sealed_sha256,bytes,state FROM v2_evidence '
        'WHERE assignment_id=? ORDER BY evidence_id', (assignment_id,))]


def _safe_file(root, relative):
    import workspace_manager as workspace
    if not isinstance(relative, str) or '\\' in relative or ':' in relative:
        _fail('invalid_arguments', 'Use a project-relative slash-separated file path')
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(p in ('', '.', '..') for p in relative.split('/')):
        _fail('invalid_arguments', 'File path must stay within the approved root')
    root = Path(root).resolve(strict=True)
    path = root.joinpath(*parts.parts)
    workspace._no_links(path)
    if not path.resolve(strict=True).is_relative_to(root):
        _fail('invalid_arguments', 'File escapes the approved root')
    raw = workspace._read_bytes(path, limit=16 * 1024 * 1024)
    return path, raw, hashlib.sha256(raw).hexdigest()


def _delivery_root(con, assignment):
    row = con.execute('SELECT data FROM tasks WHERE id=?', (assignment['legacy_task_id'],)).fetchone()
    if not row:
        _fail('state_conflict', 'Assignment has no frozen delivery request')
    return json.loads(row['data'])['request']['cwd']


def _selected_evidence(con, assignment, ids):
    if len(ids) != len(set(ids)):
        _fail('invalid_arguments', 'Duplicate evidence IDs')
    selected = []
    for identity in ids:
        row = con.execute('SELECT * FROM v2_evidence WHERE evidence_id=? AND assignment_id=?',
                          (identity, assignment['assignment_id'])).fetchone()
        if not row or not row['sealed'] or row['state'] != 'sealed':
            _fail('state_conflict', 'Evidence is missing, unsealed or from another Assignment')
        root = Path(_delivery_root(con, assignment)).resolve(strict=True)
        try:
            relative = Path(row['path']).relative_to(root).as_posix()
        except ValueError:
            _fail('state_conflict', 'Evidence path is outside the Assignment')
        path, raw, digest = _safe_file(root, relative)
        if digest != row['sealed_sha256'] or len(raw) != row['bytes']:
            _fail('state_conflict', 'EVIDENCE_CHANGED: independently review a fresh delivery')
        selected.append(dict(row))
    return selected


def _query(con, principal, tool, payload):
    import priority_scheduler as scheduler
    project = principal['project_id']
    if tool == 'awo_task_get':
        task = dict(con.execute('SELECT * FROM v2_task WHERE task_id=? AND project_id=?',
                                (payload['task_id'], project)).fetchone())
        sql, params = 'SELECT * FROM v2_assignment WHERE task_id=?', [task['task_id']]
        if principal.get('assignment_id'):
            sql += ' AND assignment_id=?'
            params.append(principal['assignment_id'])
        task['assignments'] = [dict(row) for row in con.execute(sql + ' ORDER BY generation,slot_id', params)]
        for assignment in task['assignments']:
            assignment['evidence'] = _evidence(con, assignment['assignment_id'])
        return {'status': 'TASK', 'task': task}
    if tool == 'awo_task_list':
        sql, params = 'SELECT * FROM v2_task WHERE project_id=?', [project]
        if principal.get('task_id'):
            sql += ' AND task_id=?'
            params.append(principal['task_id'])
        if payload.get('task_state'):
            sql += ' AND task_state=?'
            params.append(payload['task_state'])
        if payload.get('after'):
            sql += ' AND task_id>?'
            params.append(payload['after'])
        params.append(payload.get('limit', 100))
        return {'status': 'TASKS', 'items': [dict(row) for row in con.execute(
            sql + ' ORDER BY task_id LIMIT ?', params)]}
    if tool == 'awo_task_next':
        return scheduler.next_task(con, project,
                                   principal['agent_id'] if principal['role'] == 'worker' else None)
    if tool == 'awo_assignment_get':
        assignment = _assignment(con, principal, payload)
        assignment['evidence'] = _evidence(con, assignment['assignment_id'])
        if con.execute('SELECT 1 FROM v2_scheduler_intent WHERE assignment_id=?',
                       (assignment['assignment_id'],)).fetchone():
            assignment['reservation'] = scheduler.reservation_status(con, project, assignment['assignment_id'])
        return {'status': 'ASSIGNMENT', 'assignment': assignment}
    if tool == 'awo_message_read':
        import agent_messages
        return agent_messages.read_messages(con, project, principal, payload)
    _fail('invalid_arguments', 'Unknown query')


def _registration_secret(grant_secret, command_id, root):
    data = ('awo-access-v1|' + command_id + '|' + access.root_hash(root)).encode()
    return 'awo_access_' + hmac.new(grant_secret.encode(), data, hashlib.sha256).hexdigest()


def _register(con, principal, payload, token, command_id):
    grant = con.execute('SELECT * FROM v2_enrollment_grant WHERE grant_id=?',
                        (principal['grant_id'],)).fetchone()
    secret = _registration_secret(token, command_id, principal['project_root'])
    result = service._register_agent_tx(con, principal['project_id'], grant, payload,
                                        service._secret_hash(secret))
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=8)).isoformat()
    token_id = 'key-' + uuid.uuid4().hex[:24]
    con.execute('INSERT INTO v2_access_token(token_id,secret_hash,project_id,root_path,root_sha256,'
                'agent_id,role,issued_by,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (token_id, service._secret_hash(secret), principal['project_id'], principal['project_root'],
                 access.root_hash(principal['project_root']), result['agent_id'], result['role'],
                 grant['issued_by'], now.isoformat(), expires))
    result.update(token_id=token_id, expires_at=expires, credential_delivery='response')
    return result


def _mutate(con, principal, tool, payload, token, command_id):
    import priority_scheduler as scheduler
    project, actor = principal['project_id'], principal['principal_id']
    if tool == 'awo_task_create':
        return service._task_create(con, project, principal['binding_id'], payload)
    if tool == 'awo_assignment_claim':
        claim = {key: payload[key] for key in ('task_id', 'expected_revision')}
        if 'slot_id' in payload:
            claim['slot_id'] = payload['slot_id']
        if principal['role'] == 'worker':
            return scheduler.claim_for_agent(con, project, principal['agent_id'], claim)
        if 'agent_id' in payload:
            claim['agent_id'] = payload['agent_id']
        return scheduler.apply_command(con, project, principal['binding_id'], 'scheduler.claim', claim)
    if tool == 'awo_agent_register':
        return _register(con, principal, payload, token, command_id)
    if tool == 'awo_agent_heartbeat':
        row = con.execute('SELECT * FROM v2_agent WHERE agent_id=?', (principal['agent_id'],)).fetchone()
        _revision(row, payload['expected_revision'])
        now = service._now()
        con.execute('UPDATE v2_agent SET last_seen_at=?,revision=revision+1 WHERE agent_id=?',
                    (now, principal['agent_id']))
        service._event(con, project, 'agent', principal['agent_id'], 'agent.heartbeat', actor,
                       'v2-api', None, {'last_seen_at': now, 'status': payload.get('status')})
        return {'status': 'HEARTBEAT', 'agent_id': principal['agent_id'],
                'revision': row['revision'] + 1, 'last_seen_at': now}
    if tool in ('awo_message_send', 'awo_message_ack'):
        import agent_messages
        return agent_messages.apply_command(con, project, principal,
                                            'message.send' if tool.endswith('send') else 'message.ack', payload)
    assignment = _assignment(con, principal, payload)
    if tool == 'awo_evidence_record':
        if (assignment['state'] not in ('running', 'completed') or not assignment['holds_slot']
                or assignment['review_status'] not in ('none', 'awaiting_review')):
            _fail('state_conflict', 'Evidence may only be recorded on an open execution')
        path, raw, digest = _safe_file(_delivery_root(con, assignment), payload['path'])
        if digest != payload['sha256'] or len(raw) != payload['bytes']:
            _fail('state_conflict', 'Declared hash/size differs from the actual file')
        old = con.execute('SELECT * FROM v2_evidence WHERE task_id=? AND path=? AND sealed_sha256=?',
                          (assignment['task_id'], str(path), digest)).fetchone()
        if old and old['assignment_id'] != assignment['assignment_id']:
            _fail('state_conflict', 'Evidence belongs to another Assignment')
        if old:
            return {'status': 'EVIDENCE_RECORDED', 'evidence_id': old['evidence_id'],
                    'sha256': digest, 'bytes': len(raw), 'revision': assignment['revision']}
        eid = 'evd-' + uuid.uuid4().hex[:24]
        con.execute('INSERT INTO v2_evidence(evidence_id,task_id,assignment_id,kind,path,'
                    'sealed_sha256,current_sha256,bytes,sealed,state,source,created_at) '
                    "VALUES (?,?,?,?,?,?,?,?,1,'sealed','v2-api',?)",
                    (eid, assignment['task_id'], assignment['assignment_id'], payload['kind'],
                     str(path), digest, digest, len(raw), service._now()))
        con.execute('UPDATE v2_assignment SET revision=revision+1,updated_at=? WHERE assignment_id=?',
                    (service._now(), assignment['assignment_id']))
        service._event(con, project, 'evidence', eid, 'evidence.recorded', actor, 'v2-api', None,
                       {'assignment_id': assignment['assignment_id'], 'sha256': digest, 'bytes': len(raw)})
        return {'status': 'EVIDENCE_RECORDED', 'evidence_id': eid, 'sha256': digest,
                'bytes': len(raw), 'revision': assignment['revision'] + 1}
    if tool == 'awo_assignment_complete':
        _selected_evidence(con, assignment, payload['evidence_ids'])
        result = scheduler.complete_for_agent(con, project, principal['agent_id'], assignment['assignment_id'])
        result['reported_outcome'] = payload['outcome']
        result['independently_accepted'] = False
        return result
    if tool == 'awo_review_submit':
        if assignment['agent_id'] == principal['agent_id']:
            _fail('forbidden_role', 'Executors cannot review their own delivery')
        _selected_evidence(con, assignment, payload['evidence_ids'])
        path, raw, digest = _safe_file(principal['project_root'], payload['review_note_path'])
        if digest != payload['review_note_sha256']:
            _fail('state_conflict', 'Review note hash differs from actual bytes')
        event = con.execute('SELECT task FROM events WHERE id=?', (payload['event_id'],)).fetchone()
        if not event or event['task'] != assignment['legacy_task_id']:
            _fail('state_conflict', 'Review event belongs to another Assignment')
        result = scheduler.review_in_transaction(con, project, principal['binding_id'],
                                                  principal['binding_version'], 'main', payload['event_id'],
                                                  str(path), payload['verdict'])
        return {'status': 'REVIEW_RECORDED', 'review': result, 'claims_retained': True,
                'next_action': 'Controller assignment.close after authoritative quiescence'}
    if tool in ('awo_resource_claim', 'awo_resource_release'):
        claim = scheduler.reservation_status(con, project, assignment['assignment_id'])
        if tool == 'awo_resource_claim':
            if not assignment['holds_slot'] or payload['resources'] != claim['resources']:
                _fail('state_conflict', 'Resources must equal the existing frozen admission reservation')
            status = 'CLAIM_CONFIRMED'
        else:
            if assignment['holds_slot']:
                _fail('state_conflict', 'Controller closure with authoritative quiescence is required')
            status = 'RELEASE_CONFIRMED'
        service._event(con, project, 'assignment', assignment['assignment_id'],
                       'resource.' + status.lower(), actor, 'v2-api', None,
                       {'resources': claim['resources'], 'holds_slot': claim['holds_slot']})
        return dict(claim, status=status, revision=assignment['revision'])
    _fail('invalid_arguments', 'Unknown command')


def _error_code(exc):
    if getattr(exc, 'code', None):
        return exc.code
    text = str(exc).lower()
    if 'project_mismatch' in text:
        return 'project_mismatch'
    if 'credential' in text or 'grant' in text and ('expired' in text or 'revoked' in text):
        return 'unauthenticated'
    if 'command_id' in text:
        return 'idempotency_conflict'
    if 'revision' in text or 'epoch' in text:
        return 'revision_conflict'
    return 'state_conflict'


def call(state, project_root, token, tool, arguments):
    schemas = {item['name']: item['inputSchema'] for item in tool_specs(messages_enabled=True)}
    if tool not in schemas:
        _fail('invalid_arguments', 'Unknown application tool')
    _validate(arguments, schemas[tool])
    write = tool not in READ_TOOLS
    con = service._connect(service._db_path(state), readonly=not write)
    principal = None
    try:
        con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        principal = access.authenticate(con, project_root, token)
        payload = {key: value for key, value in arguments.items()
                   if key not in ('command_id', 'expected_epoch')}
        _authorize(con, principal, tool, payload)
        current = service._state_row(con)
        if not write:
            result = _query(con, principal, tool, payload)
            con.rollback()
            return result
        if arguments['expected_epoch'] != current['controller_epoch']:
            _fail('revision_conflict', 'Controller epoch changed')
        if tool != 'awo_agent_heartbeat' and (current['frozen'] or not current['v2_enabled']):
            _fail('state_conflict', 'Application writes are frozen or disabled')
        env = {'tool': tool, 'command_id': arguments['command_id'], 'payload': payload,
               'expected_epoch': arguments['expected_epoch']}
        sha = service._request_sha(env)
        response = service._replay(con, principal['project_id'], principal['principal_id'], env, sha)
        if response is None:
            response = _mutate(con, principal, tool, payload, token, arguments['command_id'])
            response['replayed'] = False
            service._record(con, principal['project_id'], principal['principal_id'], env, sha, response)
        if tool == 'awo_agent_register':
            # Re-derive only for the exact authenticated grant replay. Neither
            # request nor command/event logs contain a plaintext credential.
            secret = _registration_secret(token, arguments['command_id'], principal['project_root'])
            row = con.execute('SELECT * FROM v2_access_token WHERE token_id=?', (response['token_id'],)).fetchone()
            agent = con.execute('SELECT lifecycle FROM v2_agent WHERE agent_id=?', (response['agent_id'],)).fetchone()
            if (not row or row['revoked_at'] or row['secret_hash'] != service._secret_hash(secret)
                    or access._time(row['expires_at']) <= datetime.now(timezone.utc)
                    or not agent or agent['lifecycle'] != 'active'):
                _fail('unauthenticated', 'Registered credential is no longer active')
            response = dict(response, access_token=secret)
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
