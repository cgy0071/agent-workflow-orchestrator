'''Local supervision and paired drafts in the caller-owned SQLite transaction.

No domain function opens, commits, rolls back or implicitly migrates a database.
The parent authenticates principals and owns command replay and rejection audit.
The CLI is a transaction-owning caller for existing service tools only.
'''
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

import awo_service as service
import service_access as access
import service_api as api

SCHEMA_VERSION = 1
PROTOCOL = 'awo.paired-draft.v1'
INTENT_VERSION = 'awo.supervision.intent.v1'
MAX_ENVELOPE_BYTES = 65536
MAX_MESSAGE_SECONDS = 900
READ_TOOLS = frozenset(('awo_task_get', 'awo_task_list', 'awo_assignment_get'))
CONTROLLER_TOOLS = frozenset(('awo_task_create', 'awo_assignment_claim',
                              'awo_resource_claim', 'awo_resource_release'))
COMMANDS = frozenset(('supervision_bind', 'supervision_revoke_binding',
                      'supervision_approve', 'supervision_revoke_approval',
                      'supervision_pair', 'supervision_revoke_pair'))


class SupervisionError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise SupervisionError(code, message)


def _now():
    return int(time.time())


def _text(value, name, maximum=128, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail('invalid_arguments', name + ' must be text')
    try:
        if len(value.encode('utf-8')) > maximum or '\x00' in value:
            raise ValueError()
    except (UnicodeError, ValueError):
        _fail('invalid_arguments', name + ' is invalid or too large')
    return value


def _id(value, name='identity'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value):
        _fail('invalid_arguments', name + ' must be a bounded identifier')
    return value


def _integer(value, name, low=0, high=2147483647):
    if type(value) is not int or not low <= value <= high:
        _fail('invalid_arguments', name + ' must be an integer in range')
    return value


def _keys(value, allowed, required=None):
    if (not isinstance(value, dict) or set(value) - set(allowed)
            or set(allowed if required is None else required) - set(value)):
        _fail('invalid_arguments', 'Missing or unknown fields')


def _json(value):
    def check(item, depth=0):
        if depth > 12:
            _fail('invalid_arguments', 'JSON nesting limit exceeded')
        if isinstance(item, dict):
            if not all(isinstance(k, str) for k in item):
                _fail('invalid_arguments', 'JSON keys must be text')
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif item is not None and type(item) not in (str, int, bool):
            _fail('invalid_arguments', 'Only integral JSON values are supported')
    check(value)
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        if len(data) > MAX_ENVELOPE_BYTES:
            _fail('invalid_arguments', 'JSON byte limit exceeded')
        return data
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail('invalid_arguments', 'Invalid JSON')


def _sha(value):
    return hashlib.sha256(_json(value).encode('ascii')).hexdigest()


def _one(con, sql, params=()):
    cursor = con.execute(sql, params)
    row = cursor.fetchone()
    return dict(zip((x[0] for x in cursor.description), row)) if row is not None else None


def _transaction(con):
    if not isinstance(con, sqlite3.Connection) or not con.in_transaction:
        _fail('transaction_required', 'Caller must begin a transaction')
    if con.row_factory is not sqlite3.Row:
        _fail('transaction_required', 'Service connection must use sqlite3.Row')


def create_supervision_schema(con):
    '''Parent installs module tables explicitly in its ledger transaction.'''
    # The parent's generic schema installer owns the caller's connection and may
    # hand over plain tuple rows; only the DDL below runs here, so use the
    # supervision Row convention temporarily and restore the caller's factory.
    factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        _transaction(con)
        if not service.schema_present(con):
            _fail('schema_required', 'Install the parent service ledger schema first')
        present = con.execute('SELECT 1 FROM sqlite_master WHERE name=?', ('v2_supervision_meta',)).fetchone()
        if present:
            row = con.execute('SELECT version FROM v2_supervision_meta WHERE id=1').fetchone()
            if not row or row[0] != SCHEMA_VERSION:
                _fail('schema_conflict', 'Unsupported supervision schema')
        statements = (
        'CREATE TABLE IF NOT EXISTS v2_supervision_meta (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS v2_supervision_owner_identity (
          project_id TEXT NOT NULL REFERENCES v2_project(project_id), principal_id TEXT NOT NULL,
          auth_generation INTEGER NOT NULL CHECK(auth_generation>0), linked_agent_id TEXT,
          expires_at INTEGER NOT NULL, revoked_at INTEGER, PRIMARY KEY(project_id,principal_id))''',
        '''CREATE TABLE IF NOT EXISTS v2_supervision_entry (
          project_id TEXT NOT NULL REFERENCES v2_project(project_id), agent_id TEXT NOT NULL,
          binding_id TEXT NOT NULL, binding_version INTEGER NOT NULL, epoch INTEGER NOT NULL,
          owner_id TEXT NOT NULL, owner_generation INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          revision INTEGER NOT NULL CHECK(revision>0), revoked_at INTEGER, PRIMARY KEY(project_id,agent_id))''',
        '''CREATE TABLE IF NOT EXISTS v2_supervision_approval (
          approval_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          target_agent_id TEXT NOT NULL, owner_id TEXT NOT NULL, owner_generation INTEGER NOT NULL,
          intent_json TEXT NOT NULL, intent_sha256 TEXT NOT NULL, expires_at INTEGER NOT NULL,
          revision INTEGER NOT NULL CHECK(revision>0), used_at INTEGER, revoked_at INTEGER)''',
        '''CREATE TABLE IF NOT EXISTS v2_supervision_pair (
          pair_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          local_id TEXT NOT NULL, peer_id TEXT NOT NULL, key_sha256 TEXT NOT NULL,
          owner_id TEXT NOT NULL, owner_generation INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          revision INTEGER NOT NULL CHECK(revision>0), revoked_at INTEGER, CHECK(local_id<>peer_id))''',
        '''CREATE TABLE IF NOT EXISTS v2_supervision_draft (
          draft_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          pair_id TEXT NOT NULL REFERENCES v2_supervision_pair(pair_id), message_id TEXT NOT NULL,
          envelope_sha256 TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
          received_by TEXT NOT NULL, received_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('proposed','converted')), revision INTEGER NOT NULL,
          command_id TEXT, converted_by TEXT, task_id TEXT, UNIQUE(project_id,pair_id,message_id))''',
        'CREATE INDEX IF NOT EXISTS v2_supervision_draft_project ON v2_supervision_draft(project_id,draft_id)',
    )
        for statement in statements:
            con.execute(statement)
        con.execute('INSERT OR IGNORE INTO v2_supervision_meta(id,version) VALUES (1,?)', (SCHEMA_VERSION,))
    finally:
        con.row_factory = factory


def _state(con, project_id, epoch=None, write=False):
    current = service._state_row(con)
    if current['project_id'] != project_id:
        _fail('wrong_project', 'Project does not match this ledger')
    if epoch is not None and (type(epoch) is not int or epoch != current['controller_epoch']):
        _fail('revision_conflict', 'Controller epoch changed')
    if write and (current['frozen'] or not current['v2_enabled']):
        _fail('state_conflict', 'Service is frozen or disabled')
    return current


def _owner_row(con, project_id, owner_id, generation):
    row = _one(con, 'SELECT * FROM v2_supervision_owner_identity WHERE project_id=? AND principal_id=?',
               (project_id, owner_id))
    if (not row or row['revoked_at'] is not None or row['expires_at'] <= _now()
            or type(generation) is not int or row['auth_generation'] != generation):
        _fail('invalid_principal', 'Owner identity is unavailable or stale')
    return row


def _principal(con, project_id, principal, owner=False):
    _transaction(con)
    current = _state(con, project_id)
    if not isinstance(principal, dict) or principal.get('project_id') != project_id:
        _fail('invalid_principal', 'Authenticated project principal required')
    if owner:
        pid = principal.get('principal_id')
        if (principal.get('role') != 'owner' or not isinstance(pid, str)
                or not re.fullmatch(r'owner:[A-Za-z0-9._:-]{1,100}', pid)
                or principal.get('agent_id') is not None):
            _fail('forbidden_role', 'Authenticated human Owner required')
        return _owner_row(con, project_id, pid, principal.get('auth_generation'))
    if principal.get('role') not in ('controller', 'worker', 'reviewer', 'observer'):
        _fail('invalid_principal', 'Unsupported authenticated role')
    if type(principal.get('controller_epoch')) is not int or principal['controller_epoch'] != current['controller_epoch']:
        _fail('invalid_principal', 'Principal epoch is stale')
    row = _one(con, 'SELECT * FROM v2_access_token WHERE token_id=?', (principal.get('token_id'),))
    if not row or row['revoked_at'] or access._time(row['expires_at']) <= datetime.now(timezone.utc):
        _fail('invalid_principal', 'Credential is missing, expired or revoked')
    # Schema 5 adds knowledge-only credentials; old baselines lack this column.
    # Check both representations so a cached/stripped principal cannot widen scope.
    if principal.get('knowledge_id') is not None or row.get('knowledge_id') is not None:
        _fail('forbidden_role', 'Knowledge credentials cannot access supervision or Tasks')
    for field in ('project_id', 'agent_id', 'role', 'binding_id', 'binding_version', 'task_id', 'assignment_id', 'generation'):
        if principal.get(field) != row[field] or type(principal.get(field)) is not type(row[field]):
            _fail('invalid_principal', 'Credential identity or scope mismatch')
    if (principal.get('principal_id') != 'agent:' + row['agent_id']
            or principal.get('project_root') != row['root_path']
            or access.root_hash(row['root_path']) != row['root_sha256']):
        _fail('invalid_principal', 'Principal or project root changed')
    agent = _one(con, 'SELECT * FROM v2_agent WHERE agent_id=?', (row['agent_id'],))
    if (not agent or agent['project_id'] != project_id or agent['lifecycle'] != 'active'
            or (row['role'] not in ('controller', 'reviewer', 'observer') and agent['role'] != row['role'])):
        _fail('invalid_principal', 'Agent role or lifecycle changed')
    if row['role'] in ('controller', 'reviewer'):
        service.control.guard(con, row['binding_id'], row['binding_version'], 'main',
                              'dispatch' if row['role'] == 'controller' else 'review')
        if service._agent_id_for_binding(row['binding_id']) != row['agent_id']:
            _fail('invalid_principal', 'Binding does not identify this agent')
    if row['assignment_id']:
        access.require_scope(con, principal, row['task_id'], row['assignment_id'], own=row['role'] == 'worker')
        ass = _one(con, 'SELECT * FROM v2_assignment WHERE assignment_id=?', (row['assignment_id'],))
        if row['role'] == 'worker' and (ass['generation'] != row['generation']
                or ass['state'] not in ('reserved', 'running', 'completed')
                or (ass['state'] == 'completed' and ass['review_status'] not in ('awaiting_review', 'reviewing'))):
            _fail('invalid_principal', 'Assignment credential is stale')
    return dict(principal)


def _controller(con, project_id, principal, context='main'):
    p = _principal(con, project_id, principal)
    if p['role'] != 'controller' or p.get('task_id') or p.get('assignment_id') or context != 'main':
        _fail('forbidden_role', 'An unscoped active main controller is required')
    service._controller(con, p['binding_id'], p['binding_version'], context)
    return p


def _independent(owner, agent_id):
    if owner['linked_agent_id'] == agent_id:
        _fail('self_approval', 'Owner may not approve its linked agent')


def _expiry(value, maximum):
    _integer(value, 'expires_at', 1, 4102444800)
    if not _now() < value <= _now() + maximum:
        _fail('expired', 'Expiry is past or exceeds the allowed lifetime')
    return value


def _revision(row, value):
    if not row or type(value) is not int or row['revision'] != value:
        _fail('revision_conflict', 'Record is missing or its revision changed')


def _audit(con, project_id, principal, action, entity_id, data):
    service._event(con, project_id, 'supervision', entity_id, action,
                   principal['principal_id'], 'supervision', None, data)


def _entry(con, project_id, principal):
    row = _one(con, 'SELECT * FROM v2_supervision_entry WHERE project_id=? AND agent_id=?',
               (project_id, principal['agent_id']))
    if (not row or row['revoked_at'] is not None or row['expires_at'] <= _now()
            or row['binding_id'] != principal['binding_id']
            or row['binding_version'] != principal['binding_version']
            or row['epoch'] != _state(con, project_id)['controller_epoch']):
        _fail('forbidden_role', 'Explicit controller entry binding is missing or stale')
    owner = _owner_row(con, project_id, row['owner_id'], row['owner_generation'])
    _independent(owner, principal['agent_id'])
    return row


def _tool_arguments(tool, arguments):
    if tool not in CONTROLLER_TOOLS:
        _fail('forbidden_role', 'Controller entry does not expose this tool')
    schema = next(item['inputSchema'] for item in api.tool_specs() if item['name'] == tool)
    _json(arguments)
    api._validate(arguments, schema)


def _pair(con, project_id, pair_id):
    row = _one(con, 'SELECT * FROM v2_supervision_pair WHERE project_id=? AND pair_id=?',
               (project_id, pair_id))
    if not row or row['revoked_at'] is not None or row['expires_at'] <= _now():
        _fail('pair_unavailable', 'Pair is missing, revoked or expired')
    _owner_row(con, project_id, row['owner_id'], row['owner_generation'])
    return row


def _draft(con, project_id, draft_id):
    row = _one(con, 'SELECT * FROM v2_supervision_draft WHERE project_id=? AND draft_id=?',
               (project_id, draft_id))
    if not row:
        _fail('not_found', 'Draft is missing from this project')
    _pair(con, project_id, row['pair_id'])
    if row['expires_at'] <= _now():
        _fail('expired', 'Draft has expired')
    return row


def command_intent(con, project_id, principal, tool, arguments, *, draft_id=None):
    '''Read-only exact intent for independent Owner approval; not authorization.'''
    p = _controller(con, project_id, principal)
    _tool_arguments(tool, arguments)
    _state(con, project_id, arguments['expected_epoch'], write=True)
    entry = _entry(con, project_id, p)
    intent = {
        'version': INTENT_VERSION, 'project_id': project_id,
        'principal_id': p['principal_id'], 'agent_id': p['agent_id'],
        'binding_id': p['binding_id'], 'binding_version': p['binding_version'], 'entry_revision': entry['revision'],
        'tool': tool, 'arguments': json.loads(_json(arguments)), 'draft': None,
    }
    if draft_id is not None:
        row = _draft(con, project_id, _id(draft_id))
        if (tool != 'awo_task_create' or arguments.get('title') != row['title']
                or arguments.get('body') != row['body']
                or arguments.get('priority') != 'P3'
                or arguments.get('execution_mode') != 'single' or arguments.get('fanout_slots') != 1):
            _fail('invalid_arguments', 'Conversion must preserve the exact proposed title/body')
        intent['draft'] = {'draft_id': draft_id, 'envelope_sha256': row['envelope_sha256'],
                           'revision': 1}
    return intent


def _validate_intent(con, project_id, intent):
    _keys(intent, ('version', 'project_id', 'principal_id', 'agent_id', 'binding_id',
                   'binding_version', 'entry_revision', 'tool', 'arguments', 'draft'))
    if intent['version'] != INTENT_VERSION or intent['project_id'] != project_id:
        _fail('wrong_project', 'Intent protocol or project mismatch')
    aid = _id(intent['agent_id'])
    if intent['principal_id'] != 'agent:' + aid:
        _fail('invalid_principal', 'Intent target principal mismatch')
    agent = _one(con, 'SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?', (aid, project_id))
    if not agent or agent['lifecycle'] != 'active' or agent['role'] != 'controller':
        _fail('invalid_principal', 'Intent target must be an active controller')
    _integer(intent['binding_version'], 'binding_version', 1)
    service._controller(con, intent['binding_id'], intent['binding_version'], 'main')
    if service._agent_id_for_binding(intent['binding_id']) != aid:
        _fail('invalid_principal', 'Intent target binding mismatch')
    _tool_arguments(intent['tool'], intent['arguments'])
    _state(con, project_id, intent['arguments']['expected_epoch'], write=True)
    _revision(_entry(con, project_id, intent), intent['entry_revision'])
    if intent['draft'] is not None:
        d = intent['draft']
        _keys(d, ('draft_id', 'revision', 'envelope_sha256'))
        row = _draft(con, project_id, _id(d['draft_id']))
        if (row['state'] != 'proposed' or type(d['revision']) is not int
                or d['revision'] != row['revision'] or d['envelope_sha256'] != row['envelope_sha256']):
            _fail('revision_conflict', 'Draft changed before Owner approval')
        args = intent['arguments']
        if (intent['tool'] != 'awo_task_create' or args.get('title') != row['title']
                or args.get('body') != row['body'] or args.get('priority') != 'P3'
                or args.get('execution_mode') != 'single' or args.get('fanout_slots') != 1):
            _fail('invalid_arguments', 'Conversion intent does not match immutable proposal')


def apply_command(con, project_id, principal, operation, payload):
    '''Owner decisions only. Parent owns auth, envelope/replay and transaction.'''
    _id(operation)
    _json(payload)
    if operation not in COMMANDS:
        _fail('forbidden_role', 'Unknown supervision command')
    owner = _principal(con, project_id, principal, owner=True)
    _state(con, project_id, write=True)
    if operation == 'supervision_bind':
        _keys(payload, ('agent_id', 'binding_id', 'binding_version', 'expected_epoch',
                        'expires_at', 'expected_revision'),
              ('agent_id', 'binding_id', 'binding_version', 'expected_epoch', 'expires_at'))
        aid = _id(payload['agent_id'])
        _independent(owner, aid)
        _state(con, project_id, payload['expected_epoch'], write=True)
        _integer(payload['binding_version'], 'binding_version', 1)
        service._controller(con, payload['binding_id'], payload['binding_version'], 'main')
        agent = _one(con, 'SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?', (aid, project_id))
        if (not agent or agent['role'] != 'controller' or agent['lifecycle'] != 'active'
                or aid != service._agent_id_for_binding(payload['binding_id'])):
            _fail('invalid_principal', 'Entry must bind the active controller agent')
        old = _one(con, 'SELECT * FROM v2_supervision_entry WHERE project_id=? AND agent_id=?', (project_id, aid))
        if old:
            _revision(old, payload.get('expected_revision'))
        elif 'expected_revision' in payload:
            _fail('revision_conflict', 'New binding has no prior revision')
        expiry = min(_expiry(payload['expires_at'], 14400), owner['expires_at'])
        revision = old['revision'] + 1 if old else 1
        con.execute('''INSERT INTO v2_supervision_entry VALUES (?,?,?,?,?,?,?,?,?,NULL)
          ON CONFLICT(project_id,agent_id) DO UPDATE SET binding_id=excluded.binding_id,
          binding_version=excluded.binding_version,epoch=excluded.epoch,owner_id=excluded.owner_id,
          owner_generation=excluded.owner_generation,expires_at=excluded.expires_at,
          revision=excluded.revision,revoked_at=NULL''',
                    (project_id, aid, payload['binding_id'], payload['binding_version'],
                     payload['expected_epoch'], owner['principal_id'], owner['auth_generation'], expiry, revision))
        identity = aid
        result = {'agent_id': aid, 'revision': revision, 'expires_at': expiry}
    elif operation == 'supervision_approve':
        _keys(payload, ('approval_id', 'intent', 'expires_at'))
        identity = _id(payload['approval_id'])
        intent = payload['intent']
        _validate_intent(con, project_id, intent)
        _independent(owner, intent['agent_id'])
        expiry = min(_expiry(payload['expires_at'], 3600), owner['expires_at'])
        if con.execute('SELECT 1 FROM v2_supervision_approval WHERE approval_id=?', (identity,)).fetchone():
            _fail('state_conflict', 'Approval ID already exists')
        con.execute('INSERT INTO v2_supervision_approval VALUES (?,?,?,?,?,?,?,?,1,NULL,NULL)',
                    (identity, project_id, intent['agent_id'], owner['principal_id'], owner['auth_generation'],
                     _json(intent), _sha(intent), expiry))
        result = {'approval_id': identity, 'intent_sha256': _sha(intent), 'revision': 1}
    elif operation == 'supervision_pair':
        _keys(payload, ('pair_id', 'local_id', 'peer_id', 'key_sha256', 'expires_at'))
        identity = _id(payload['pair_id'])
        local_id, peer_id = _id(payload['local_id']), _id(payload['peer_id'])
        digest = payload['key_sha256']
        if local_id == peer_id or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            _fail('invalid_arguments', 'Pair needs distinct identities and a key fingerprint')
        expiry = min(_expiry(payload['expires_at'], 86400), owner['expires_at'])
        if con.execute('SELECT 1 FROM v2_supervision_pair WHERE pair_id=?', (identity,)).fetchone():
            _fail('state_conflict', 'Pair ID cannot be reused; create a fresh pair')
        con.execute('INSERT INTO v2_supervision_pair VALUES (?,?,?,?,?,?,?,?,1,NULL)',
                    (identity, project_id, local_id, peer_id, digest, owner['principal_id'],
                     owner['auth_generation'], expiry))
        result = {'pair_id': identity, 'revision': 1, 'expires_at': expiry, 'protocol': PROTOCOL}
    else:
        table, key = {
            'supervision_revoke_binding': ('v2_supervision_entry', 'agent_id'),
            'supervision_revoke_approval': ('v2_supervision_approval', 'approval_id'),
            'supervision_revoke_pair': ('v2_supervision_pair', 'pair_id'),
        }[operation]
        _keys(payload, (key, 'expected_revision'))
        identity = _id(payload[key])
        row = _one(con, 'SELECT * FROM ' + table + ' WHERE project_id=? AND ' + key + '=?', (project_id, identity))
        _revision(row, payload['expected_revision'])
        con.execute('UPDATE ' + table + ' SET revoked_at=?,revision=revision+1 WHERE project_id=? AND ' + key + '=?',
                    (_now(), project_id, identity))
        result = {key: identity, 'revision': row['revision'] + 1, 'revoked': True}
    _audit(con, project_id, principal, operation, identity, result)
    return result


def _approval(con, project_id, principal, approval_id, intent):
    row = _one(con, 'SELECT * FROM v2_supervision_approval WHERE project_id=? AND approval_id=?',
               (project_id, _id(approval_id)))
    if (not row or row['revoked_at'] is not None or row['expires_at'] <= _now()
            or row['target_agent_id'] != principal['agent_id']
            or row['intent_sha256'] != _sha(intent) or row['intent_json'] != _json(intent)):
        _fail('approval_required', 'Exact current independent Owner approval required')
    owner = _owner_row(con, project_id, row['owner_id'], row['owner_generation'])
    _independent(owner, principal['agent_id'])
    return row


def _execute_existing_service(con, project_id, principal, tool, arguments):
    '''Use the real existing service's authorization, mutation and command log.'''
    payload = {k: v for k, v in arguments.items() if k not in ('command_id', 'expected_epoch')}
    api._authorize(con, principal, tool, payload)
    env = {'tool': tool, 'command_id': arguments['command_id'], 'expected_epoch': arguments['expected_epoch'],
           'payload': payload}
    sha = service._request_sha(env)
    result = service._replay(con, project_id, principal['principal_id'], env, sha)
    if result is None:
        result = api._mutate(con, principal, tool, payload, None, arguments['command_id'])
        result['replayed'] = False
        service._record(con, project_id, principal['principal_id'], env, sha, result)
    return result


def _execute_approved(con, project_id, principal, intent, approval_id):
    approval = _approval(con, project_id, principal, approval_id, intent)
    result = _execute_existing_service(con, project_id, principal, intent['tool'], intent['arguments'])
    if approval['used_at'] is None:
        con.execute('UPDATE v2_supervision_approval SET used_at=?,revision=revision+1 WHERE approval_id=?',
                    (_now(), approval_id))
        _audit(con, project_id, principal, 'supervision.executed', approval_id,
               {'tool': intent['tool'], 'command_id': intent['arguments']['command_id'],
                'intent_sha256': approval['intent_sha256']})
    return result


def controller_entry(con, project_id, principal, tool, arguments, approval_id, *, context='main'):
    p = _controller(con, project_id, principal, context)
    intent = command_intent(con, project_id, p, tool, arguments)
    return _execute_approved(con, project_id, p, intent, approval_id)


def overseer(con, project_id, principal, tool, arguments, *, mode='advisor', approval_id=None, context='main'):
    '''Default advisor is read-only even when its underlying token is broader.'''
    _id(tool)
    p = _principal(con, project_id, principal)
    if mode not in ('advisor', 'controller_entry'):
        _fail('forbidden_role', 'Unknown Overseer mode')
    if tool in READ_TOOLS:
        schema = next(item['inputSchema'] for item in api.tool_specs() if item['name'] == tool)
        args = json.loads(_json(arguments))
        api._validate(args, schema)
        api._authorize(con, p, tool, args)
        return api._query(con, p, tool, args)
    if mode != 'controller_entry':
        _fail('forbidden_role', 'Advisor cannot issue a write command')
    return controller_entry(con, project_id, p, tool, arguments, approval_id, context=context)


def decode_envelope(raw):
    '''Strict bounded JSON decoder for the local protocol and CLI request.'''
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_ENVELOPE_BYTES:
        _fail('invalid_arguments', 'Input exceeds its byte limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail('invalid_arguments', 'Duplicate JSON key')
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw,
                           object_pairs_hook=pairs,
                           parse_constant=lambda _: _fail('invalid_arguments', 'Nonfinite JSON'))
        _json(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        _fail('invalid_arguments', 'Malformed or excessive JSON input')


def _key(key):
    if not isinstance(key, bytes) or not 32 <= len(key) <= 128:
        _fail('invalid_arguments', 'Pair key must contain 32..128 secret bytes')
    return key


def _envelope_fields(envelope, signed=True):
    fields = ('protocol', 'project_id', 'pair_id', 'sender', 'recipient', 'message_id',
              'issued_at', 'expires_at', 'payload')
    _keys(envelope, fields + (('signature',) if signed else ()))
    if envelope['protocol'] != PROTOCOL:
        _fail('unsupported_protocol', 'Only awo.paired-draft.v1 is implemented')
    _text(envelope['project_id'], 'project_id', 256)
    for name in ('pair_id', 'sender', 'recipient', 'message_id'):
        _id(envelope[name], name)
    for name in ('issued_at', 'expires_at'):
        _integer(envelope[name], name, 1, 4102444800)
    if not 0 < envelope['expires_at'] - envelope['issued_at'] <= MAX_MESSAGE_SECONDS:
        _fail('expired', 'Message lifetime must be 1..900 seconds')
    _keys(envelope['payload'], ('title', 'body'))
    _text(envelope['payload']['title'], 'title', 512)
    _text(envelope['payload']['body'], 'body', 32000, empty=True)
    if signed and (not isinstance(envelope['signature'], str)
                   or not re.fullmatch('[0-9a-f]{64}', envelope['signature'])):
        _fail('invalid_signature', 'Invalid HMAC signature encoding')
    _json(envelope)


def make_envelope(key, *, project_id, pair_id, sender, recipient, message_id, payload,
                  issued_at=None, expires_at=None):
    '''Pure local envelope construction; performs no send or business operation.'''
    issued_at = _now() if issued_at is None else issued_at
    value = {'protocol': PROTOCOL, 'project_id': project_id, 'pair_id': pair_id,
             'sender': sender, 'recipient': recipient, 'message_id': message_id,
             'issued_at': issued_at, 'expires_at': issued_at + 300 if expires_at is None else expires_at,
             'payload': json.loads(_json(payload))}
    _envelope_fields(value, signed=False)
    value['signature'] = hmac.new(_key(key), (PROTOCOL + '\n' + _json(value)).encode('ascii'),
                                  hashlib.sha256).hexdigest()
    _json(value)
    return value


def receive_envelope(con, project_id, principal, envelope, key):
    '''Authenticated local import creates only an immutable untrusted proposal.'''
    p = _controller(con, project_id, principal)
    _state(con, project_id, write=True)
    _envelope_fields(envelope)
    pair = _pair(con, project_id, envelope['pair_id'])
    if (envelope['project_id'] != project_id or envelope['sender'] != pair['peer_id']
            or envelope['recipient'] != pair['local_id']):
        _fail('wrong_project', 'Envelope project or paired identities do not match')
    if (envelope['expires_at'] <= _now() or envelope['issued_at'] > _now() + 30
            or envelope['expires_at'] > pair['expires_at']):
        _fail('expired', 'Envelope is expired, future-dated or exceeds pairing lifetime')
    if not hmac.compare_digest(hashlib.sha256(_key(key)).hexdigest(), pair['key_sha256']):
        _fail('invalid_signature', 'Key does not match the approved pairing')
    unsigned = {k: v for k, v in envelope.items() if k != 'signature'}
    expected = hmac.new(key, (PROTOCOL + '\n' + _json(unsigned)).encode('ascii'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, envelope['signature']):
        _fail('invalid_signature', 'Envelope authentication failed')
    digest = _sha(envelope)
    old = _one(con, 'SELECT * FROM v2_supervision_draft WHERE project_id=? AND pair_id=? AND message_id=?',
               (project_id, pair['pair_id'], envelope['message_id']))
    if old:
        if old['envelope_sha256'] != digest:
            _fail('idempotency_conflict', 'Paired message ID was reused with different content')
        return {'draft_id': old['draft_id'], 'revision': old['revision'], 'state': old['state'],
                'replayed': True, 'untrusted': True, 'dispatch_authorized': False}
    draft_id = 'draft-' + uuid.uuid4().hex
    con.execute('INSERT INTO v2_supervision_draft VALUES (?,?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,NULL)',
                (draft_id, project_id, pair['pair_id'], envelope['message_id'], digest,
                 envelope['payload']['title'], envelope['payload']['body'], p['principal_id'],
                 _now(), envelope['expires_at'], 'proposed'))
    _audit(con, project_id, p, 'supervision.draft_received', draft_id,
           {'pair_id': pair['pair_id'], 'message_id': envelope['message_id'], 'envelope_sha256': digest})
    return {'draft_id': draft_id, 'revision': 1, 'state': 'proposed', 'replayed': False,
            'untrusted': True, 'dispatch_authorized': False}


def conversion_intent(con, project_id, principal, payload):
    '''Pure exact conversion-intent builder for the independent Owner.'''
    _keys(payload, ('draft_id', 'expected_revision', 'command_id', 'expected_epoch', 'task_id'),
          ('draft_id', 'expected_revision', 'command_id', 'expected_epoch'))
    _controller(con, project_id, principal)
    row = _draft(con, project_id, _id(payload['draft_id']))
    if type(payload['expected_revision']) is not int or payload['expected_revision'] != 1:
        _fail('revision_conflict', 'Conversion requires the original immutable draft revision')
    args = {'title': row['title'], 'body': row['body'], 'priority': 'P3', 'execution_mode': 'single',
            'fanout_slots': 1, 'command_id': payload['command_id'], 'expected_epoch': payload['expected_epoch']}
    if 'task_id' in payload:
        args['task_id'] = payload['task_id']
    return command_intent(con, project_id, principal, 'awo_task_create', args, draft_id=row['draft_id'])


def convert_draft(con, project_id, principal, payload):
    '''Atomically create only a draft Task through the real service, never SQL.'''
    _keys(payload, ('draft_id', 'expected_revision', 'command_id', 'expected_epoch', 'task_id', 'approval_id'),
          ('draft_id', 'expected_revision', 'command_id', 'expected_epoch', 'approval_id'))
    p = _controller(con, project_id, principal)
    intent = conversion_intent(con, project_id, p, {k: v for k, v in payload.items() if k != 'approval_id'})
    row = _draft(con, project_id, payload['draft_id'])
    if row['state'] == 'converted' and (row['command_id'] != payload['command_id']
                                       or row['converted_by'] != p['principal_id']):
        _fail('state_conflict', 'Draft already converted by another command')
    result = _execute_approved(con, project_id, p, intent, payload['approval_id'])
    if row['state'] == 'proposed':
        con.execute('UPDATE v2_supervision_draft SET state=?,revision=revision+1,command_id=?,converted_by=?,task_id=? '
                    'WHERE project_id=? AND draft_id=? AND revision=? AND state=?',
                    ('converted', payload['command_id'], p['principal_id'], result['task_id'],
                     project_id, row['draft_id'], 1, 'proposed'))
        _audit(con, project_id, p, 'supervision.draft_converted', row['draft_id'],
               {'task_id': result['task_id'], 'command_id': payload['command_id'], 'dispatch_authorized': False})
    return dict(result, draft_id=row['draft_id'], draft_revision=2, dispatch_authorized=False)


def query(con, project_id, principal, operation, payload):
    '''Bounded read-only projections. Suggestions never persist or authorize.'''
    _id(operation)
    p = _principal(con, project_id, principal)
    _json(payload)
    if operation == 'advisor_snapshot':
        _keys(payload, ('limit', 'after'), ())
        tasks = overseer(con, project_id, p, 'awo_task_list', payload)['items']
        return {'mode': 'advisor', 'tasks': tasks, 'bounded_snapshot': True,
                'controller_epoch': _state(con, project_id)['controller_epoch'], 'can_write': False}
    if operation == 'advisor_suggest':
        _keys(payload, ('goal',))
        goal = _text(payload['goal'], 'goal', 8000)
        tasks = overseer(con, project_id, p, 'awo_task_list', {'limit': 200})['items']
        rules = {'draft': 'Review the draft scope before any queue decision',
                 'blocked': 'Inspect the recorded blocker; do not infer worker termination',
                 'repair_required': 'Inspect independent review findings before proposing repair',
                 'review_ready': 'Request independent review; delivery is not acceptance',
                 'accepted': 'Inspect the materialization gate; acceptance is not landing'}
        checks = [{'task_id': row['task_id'], 'revision': row['revision'], 'task_state': row['task_state'],
                   'suggestion': rules[row['task_state']]} for row in tasks if row['task_state'] in rules]
        return {'mode': 'advisor', 'suggestion': {'goal': goal, 'state': 'unapproved_draft',
                'method': 'deterministic_state_rules', 'checks': checks, 'scanned_tasks': len(tasks),
                'scan_limit': 200},
                'persisted': False, 'dispatch_authorized': False}
    if operation not in ('draft_get', 'draft_list', 'pair_list'):
        _fail('forbidden_role', 'Unsupported read-only query')
    if p['role'] not in ('controller', 'observer') or p.get('task_id') or p.get('assignment_id'):
        _fail('forbidden_role', 'Project-wide read scope required')
    if operation == 'draft_get':
        _keys(payload, ('draft_id',))
        row = _one(con, 'SELECT * FROM v2_supervision_draft WHERE project_id=? AND draft_id=?',
                   (project_id, _id(payload['draft_id'])))
        if row is None:
            _fail('not_found', 'Draft not found')
        return dict(row, untrusted=True, dispatch_authorized=False)
    _keys(payload, ('limit', 'after'), ())
    limit = _integer(payload.get('limit', 100), 'limit', 1, 200)
    after = _text(payload.get('after', ''), 'after', empty=True)
    table, key = ('v2_supervision_pair', 'pair_id') if operation == 'pair_list' else ('v2_supervision_draft', 'draft_id')
    rows = [dict(row) for row in con.execute('SELECT * FROM ' + table + ' WHERE project_id=? AND '
                                            + key + '>? ORDER BY ' + key + ' LIMIT ?', (project_id, after, limit))]
    return {'items': rows, 'read_only': True, 'dispatch_authorized': False}


def _file(path, limit):
    with Path(path).open('rb') as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        _fail('invalid_arguments', 'Input file exceeds its byte limit')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local Overseer, advisor by default; no external sends.')
    parser.add_argument('--state', required=True)
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--request-file', required=True)
    parser.add_argument('--mode', choices=('advisor', 'controller_entry'), default='advisor')
    parser.add_argument('--approval-id')
    args = parser.parse_args(argv)
    con = None
    principal = None
    write = False
    request = {}
    try:
        token = _file(args.token_file, 16384).decode('utf-8').strip()
        request = decode_envelope(_file(args.request_file, MAX_ENVELOPE_BYTES))
        _keys(request, ('tool', 'arguments'))
        _id(request['tool'])
        write = args.mode == 'controller_entry' and request['tool'] not in READ_TOOLS
        con = service._connect(service._db_path(args.state), readonly=not write)
        con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        principal = access.authenticate(con, args.project_root, token)
        result = overseer(con, principal['project_id'], principal, request['tool'], request['arguments'],
                          mode=args.mode, approval_id=args.approval_id)
        output = _json(result)
        if token and token in output:
            _fail('internal', 'Response failed redaction')
        if write:
            con.commit()
        else:
            con.rollback()
        print(output)
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        if con is not None:
            con.rollback()
        code = getattr(exc, 'code', 'supervision_rejected')
        if not isinstance(code, str) or not re.fullmatch('[a-z_]{1,64}', code):
            code = 'supervision_rejected'
        if write and principal is not None:
            try:
                con.execute('BEGIN IMMEDIATE')
                _audit(con, principal['project_id'], principal, 'supervision.command_rejected',
                       'rejection', {'code': code})
                con.commit()
            except sqlite3.Error:
                con.rollback()
        # Stable machine code only; credentials, request text and paths stay private.
        print(json.dumps({'error': {'code': code}}))
        return 2
    finally:
        if con is not None:
            con.close()


if __name__ == '__main__':
    raise SystemExit(main())
