"""Scoped credentials shared by local CLI, MCP and HTTP application entrypoints.

Tokens authenticate an existing agent/binding, not a caller-provided role.
This is application authorization, not isolation from the same OS user.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import uuid

import awo_service as service


def create_access_schema(con):
    for column, declaration in (('root_path', 'TEXT'), ('root_sha256', 'TEXT'),
                                ('issuer_version', 'INTEGER')):
        service._ensure_column(con, 'v2_enrollment_grant', column, declaration)
    con.execute('''CREATE TABLE IF NOT EXISTS v2_access_token (
      token_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL UNIQUE,
      project_id TEXT NOT NULL REFERENCES v2_project(project_id),
      root_path TEXT NOT NULL, root_sha256 TEXT NOT NULL,
      agent_id TEXT NOT NULL REFERENCES v2_agent(agent_id),
      role TEXT NOT NULL CHECK(role IN ('controller','worker','reviewer','observer')),
      binding_id TEXT, binding_version INTEGER,
      task_id TEXT REFERENCES v2_task(task_id),
      assignment_id TEXT REFERENCES v2_assignment(assignment_id), generation INTEGER,
      issued_by TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
      revoked_at TEXT, revision INTEGER NOT NULL DEFAULT 1
    )''')
    service._ensure_column(con, 'v2_access_token', 'knowledge_id', 'TEXT')


def canonical_root(value):
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise service.ServiceError('project root must be an absolute existing directory')
    root = Path(value).resolve(strict=True)
    if not root.is_dir():
        raise service.ServiceError('project root must be a directory')
    return os.path.normcase(str(root))


def root_hash(value):
    return hashlib.sha256(canonical_root(value).encode('utf-8')).hexdigest()


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('missing timezone')
        return parsed
    except (ValueError, TypeError, AttributeError) as exc:
        raise service.ServiceError('invalid credential expiry') from exc


def issue_token(state, actor, version, context, envelope, token_out):
    env = service._command_envelope(envelope, ('access.issue',))
    p = env['payload']
    allowed = {'project_root', 'agent_id', 'role', 'binding_id', 'binding_version',
               'task_id', 'assignment_id', 'knowledge_id', 'expires_minutes'}
    if set(p) - allowed:
        raise service.ServiceError('unknown access.issue fields')
    minutes = p.get('expires_minutes', 480)
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise service.ServiceError('expires_minutes must be an integer in 1..1440')
    root = canonical_root(p.get('project_root'))
    con = service._registry_open(state)
    target = None
    try:
        current = service._registry_controller(con, actor, version, context, env)
        service._refuse_frozen(current)
        project = current['project_id']
        principal = 'controller:' + actor
        request_sha = service._request_sha(env)
        replay = service._replay(con, project, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        agent = con.execute('SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?',
                            (p.get('agent_id'), project)).fetchone()
        if not agent or agent['lifecycle'] != 'active':
            raise service.ServiceError('credential requires an active project agent')
        role = p.get('role', agent['role'])
        if role not in ('controller', 'worker', 'reviewer', 'observer'):
            raise service.ServiceError('invalid access role')
        binding_id, binding_version = p.get('binding_id'), p.get('binding_version')
        if role in ('controller', 'reviewer'):
            if not isinstance(binding_id, str) or type(binding_version) is not int:
                raise service.ServiceError('privileged access requires a current binding')
            binding = service.control.guard(con, binding_id, binding_version, 'main',
                                            'dispatch' if role == 'controller' else 'review')
            if service._agent_id_for_binding(binding_id) != agent['agent_id']:
                raise service.ServiceError('credential agent does not match the privileged binding')
        elif binding_id is not None or binding_version is not None:
            raise service.ServiceError('unprivileged credential cannot carry a privileged binding')
        elif role != agent['role'] and role != 'observer':
            raise service.ServiceError('credential cannot upgrade an agent role')
        task_id, assignment_id = p.get('task_id'), p.get('assignment_id')
        generation = None
        if task_id is not None and not con.execute(
                'SELECT 1 FROM v2_task WHERE task_id=? AND project_id=?', (task_id, project)).fetchone():
            raise service.ServiceError('credential task belongs to another project or is missing')
        if assignment_id is not None:
            assignment = con.execute('SELECT a.* FROM v2_assignment a JOIN v2_task t '
                                     'ON a.task_id=t.task_id WHERE a.assignment_id=? AND t.project_id=?',
                                     (assignment_id, project)).fetchone()
            if not assignment or (task_id and assignment['task_id'] != task_id):
                raise service.ServiceError('credential assignment scope mismatch')
            if role == 'worker' and assignment['agent_id'] != agent['agent_id']:
                raise service.ServiceError('worker can only access its own assignment')
            task_id = assignment['task_id']
            generation = assignment['generation']
        knowledge_id = p.get('knowledge_id')
        if knowledge_id is not None:
            if task_id is not None or not isinstance(knowledge_id, str) or not con.execute(
                    'SELECT 1 FROM v2_knowledge WHERE entity_id=? AND project_id=?',
                    (knowledge_id, project)).fetchone():
                raise service.ServiceError('knowledge credential scope is missing or conflicts with task scope')
        if role == 'reviewer' and task_id is None and knowledge_id is None:
            raise service.ServiceError('reviewer credential must be scoped to a task or knowledge entity')
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(minutes=minutes)).isoformat()
        token_id = 'key-' + uuid.uuid4().hex[:24]
        secret = 'awo_access_' + secrets.token_urlsafe(32)
        target = service._secret_file(token_out, secret)
        con.execute('INSERT INTO v2_access_token(token_id,secret_hash,project_id,root_path,'
                    'root_sha256,agent_id,role,binding_id,binding_version,task_id,assignment_id,'
                    'generation,issued_by,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (token_id, service._secret_hash(secret), project, root, root_hash(root),
                     agent['agent_id'], role, binding_id, binding_version, task_id, assignment_id,
                     generation, actor, now.isoformat(), expires))
        if knowledge_id is not None:
            con.execute('UPDATE v2_access_token SET knowledge_id=? WHERE token_id=?', (knowledge_id, token_id))
        result = {'status': 'ACCESS_ISSUED', 'token_id': token_id, 'token_file': target,
                  'agent_id': agent['agent_id'], 'role': role, 'expires_at': expires,
                  'project_id': project, 'replayed': False}
        service._event(con, project, 'credential', token_id, 'access.issued', actor, 'v2', None,
                       {k: result[k] for k in ('token_id', 'agent_id', 'role', 'expires_at')})
        service._record(con, project, principal, env, request_sha, result)
        con.commit()
        return result
    except Exception:
        con.rollback()
        if target:
            service._remove_secret_file(target)
        raise
    finally:
        con.close()


def revoke_token(state, actor, version, context, envelope):
    env = service._command_envelope(envelope, ('access.revoke',))
    p = env['payload']
    if set(p) != {'token_id', 'expected_revision'} or type(p.get('expected_revision')) is not int:
        raise service.ServiceError('access.revoke requires token_id and integer expected_revision')
    con = service._registry_open(state)
    try:
        current = service._registry_controller(con, actor, version, context, env)
        project = current['project_id']
        principal = 'controller:' + actor
        sha = service._request_sha(env)
        replay = service._replay(con, project, principal, env, sha)
        if replay is not None:
            con.rollback()
            return replay
        row = con.execute('SELECT * FROM v2_access_token WHERE token_id=? AND project_id=?',
                          (p['token_id'], project)).fetchone()
        if not row or row['revision'] != p['expected_revision']:
            raise service.ServiceError('missing credential or stale revision')
        con.execute('UPDATE v2_access_token SET revoked_at=?,revision=revision+1 WHERE token_id=?',
                    (service._now(), p['token_id']))
        service._event(con, project, 'credential', p['token_id'], 'access.revoked', actor, 'v2', None, {})
        result = {'status': 'ACCESS_REVOKED', 'token_id': p['token_id'],
                  'revision': row['revision'] + 1, 'replayed': False}
        service._record(con, project, principal, env, sha, result)
        con.commit()
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def authenticate(con, project_root, token):
    if not isinstance(token, str) or not 16 <= len(token) <= 512:
        raise service.ServiceError('invalid access credential')
    if not service._table_exists(con, 'v2_access_token'):
        raise service.ServiceError('access schema is absent; run schema-upgrade')
    if token.startswith('awo_enroll_'):
        grant = con.execute('SELECT * FROM v2_enrollment_grant WHERE secret_hash=?',
                            (service._secret_hash(token),)).fetchone()
        current = service._state_row(con)
        if (not grant or grant['revoked_at'] or grant['state'] not in ('pending', 'used')
                or _time(grant['expires_at']) <= datetime.now(timezone.utc)):
            raise service.ServiceError('enrollment credential is unknown, expired or revoked')
        if (not grant['root_path'] or grant['project_id'] != current['project_id']
                or root_hash(project_root) != grant['root_sha256']
                or root_hash(grant['root_path']) != grant['root_sha256']):
            raise service.ServiceError('project_mismatch: enrollment grant must bind this root')
        service._controller(con, grant['issued_by'], grant['issuer_version'], 'main')
        return {'principal_id': 'grant:' + grant['grant_id'], 'role': 'enrollment',
                'agent_id': None, 'grant_id': grant['grant_id'],
                'project_id': grant['project_id'], 'project_root': grant['root_path'],
                'controller_epoch': current['controller_epoch']}
    row = con.execute('SELECT * FROM v2_access_token WHERE secret_hash=?',
                      (service._secret_hash(token),)).fetchone()
    if not row or row['revoked_at'] or _time(row['expires_at']) <= datetime.now(timezone.utc):
        raise service.ServiceError('access credential is unknown, expired or revoked')
    current = service._state_row(con)
    if row['project_id'] != current['project_id'] or root_hash(project_root) != row['root_sha256']:
        raise service.ServiceError('project_mismatch: credential does not authorize this root')
    # Resolve the issued root again: an alias retarget cannot silently switch projects.
    if root_hash(row['root_path']) != row['root_sha256']:
        raise service.ServiceError('project root changed after credential issuance')
    agent = con.execute('SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?',
                        (row['agent_id'], row['project_id'])).fetchone()
    if not agent or agent['lifecycle'] != 'active':
        raise service.ServiceError('credential agent is no longer active')
    if row['role'] in ('controller', 'reviewer'):
        service.control.guard(con, row['binding_id'], row['binding_version'], 'main',
                              'dispatch' if row['role'] == 'controller' else 'review')
        if service._agent_id_for_binding(row['binding_id']) != row['agent_id']:
            raise service.ServiceError('credential binding identity changed')
    elif row['role'] != agent['role'] and row['role'] != 'observer':
        raise service.ServiceError('credential role no longer matches agent')
    if row['assignment_id'] and row['role'] == 'worker':
        ass = con.execute('SELECT * FROM v2_assignment WHERE assignment_id=?',
                          (row['assignment_id'],)).fetchone()
        if (not ass or ass['agent_id'] != row['agent_id'] or ass['generation'] != row['generation']
                or ass['state'] not in ('reserved', 'running', 'completed')
                or (ass['state'] == 'completed' and ass['review_status'] not in ('awaiting_review', 'reviewing'))):
            raise service.ServiceError('assignment credential is stale or closed')
    return {'principal_id': 'agent:' + row['agent_id'], 'agent_id': row['agent_id'],
            'role': row['role'], 'project_id': row['project_id'], 'project_root': row['root_path'],
            'binding_id': row['binding_id'], 'binding_version': row['binding_version'],
            'task_id': row['task_id'], 'assignment_id': row['assignment_id'],
            'generation': row['generation'], 'token_id': row['token_id'],
            'knowledge_id': row['knowledge_id'],
            'expires_at': row['expires_at'], 'controller_epoch': current['controller_epoch']}


def principal(state, project_root, token):
    con = service._connect(service._db_path(state), readonly=True)
    try:
        return authenticate(con, project_root, token)
    finally:
        con.close()


def require_scope(con, principal, task_id=None, assignment_id=None, own=False):
    if assignment_id:
        row = con.execute('SELECT a.*,t.project_id FROM v2_assignment a JOIN v2_task t '
                          'ON a.task_id=t.task_id WHERE a.assignment_id=?', (assignment_id,)).fetchone()
        if not row or row['project_id'] != principal['project_id']:
            raise service.ServiceError('assignment missing or belongs to another project')
        if task_id is not None and task_id != row['task_id']:
            raise service.ServiceError('assignment and task mismatch')
        task_id = row['task_id']
        if own and row['agent_id'] != principal['agent_id']:
            raise service.ServiceError('agent can only access its own assignment')
        if principal.get('assignment_id') and principal['assignment_id'] != assignment_id:
            raise service.ServiceError('credential assignment scope mismatch')
    if task_id:
        if principal.get('task_id') and principal['task_id'] != task_id:
            raise service.ServiceError('credential task scope mismatch')
        if not con.execute('SELECT 1 FROM v2_task WHERE task_id=? AND project_id=?',
                           (task_id, principal['project_id'])).fetchone():
            raise service.ServiceError('task missing or belongs to another project')
