"""Bounded collaboration; native Task/Assignment/review/budget remain authoritative.

Caller owns authentication, replay, epoch fencing and transactions. This module
does not dispatch workers, commit, accept Tasks or release claims. Only approved
local verification commands execute subprocesses.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

import awo_service as service
import budget
import monitor_task as monitor
import priority_scheduler as scheduler
import workflow_control as control
import workflow_scheduler as legacy

SCHEMA_VERSION = 1
MAX_JSON = 1024 * 1024
COMMANDS = frozenset(('collaboration_configure', 'collaboration_propose',
                      'collaboration_withdraw', 'collaboration_verify',
                      'collaboration_cancel_ticket'))
QUERIES = frozenset(('collaboration_get', 'collaboration_history'))
TERMINAL = ('accepted', 'landed', 'finalized', 'rejected', 'cancelled')


class CollaborationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise CollaborationError(code, message)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _fail('invalid_payload', 'finite JSON values required')


def _sha(value):
    return hashlib.sha256(_json(value).encode('ascii')).hexdigest()


def _text(value, label, limit=256):
    if not isinstance(value, str) or not value.strip() or chr(0) in value:
        _fail('invalid_payload', label + ' must be nonempty text')
    try:
        if len(value.encode('utf-8')) > limit:
            _fail('invalid_payload', label + ' is too long')
    except UnicodeError:
        _fail('invalid_payload', label + ' must be UTF-8')
    return value


def _integer(value, label, low=1, high=2147483647):
    if type(value) is not int or not low <= value <= high:
        _fail('invalid_payload', label + ' must be an integer in range')
    return value


def _fields(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        _fail('invalid_payload', 'missing or unknown fields')
    if len(_json(data)) > MAX_JSON:
        _fail('invalid_payload', 'payload exceeds 1 MiB canonical JSON')


def _rows(con, sql, args=()):
    cursor = con.execute(sql, args)
    return [dict(zip((d[0] for d in cursor.description), row)) for row in cursor.fetchall()]


def _one(con, sql, args=()):
    values = _rows(con, sql, args)
    return values[0] if values else None


def _native(con, function, *args, **kwargs):
    old = con.row_factory
    try:
        con.row_factory = sqlite3.Row
        try:
            return function(con, *args, **kwargs)
        except (ValueError, OSError) as exc:
            if isinstance(exc, CollaborationError):
                raise
            _fail('native_gate', str(exc))
    finally:
        con.row_factory = old


def _tx(con):
    if not con.in_transaction:
        _fail('transaction_required', 'caller must begin a transaction; use BEGIN IMMEDIATE')


def create_collaboration_schema(con):
    _tx(con)
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='v2_collaboration_meta'").fetchone():
        row = con.execute('SELECT version FROM v2_collaboration_meta WHERE id=1').fetchone()
        if row and row[0] != SCHEMA_VERSION:
            _fail('schema_conflict', 'unsupported collaboration schema')
    ddl = (
        'CREATE TABLE IF NOT EXISTS v2_collaboration_meta (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)',
        """CREATE TABLE IF NOT EXISTS v2_collaboration_policy (
          task_id TEXT PRIMARY KEY REFERENCES v2_task(task_id), project_id TEXT NOT NULL,
          config_json TEXT NOT NULL, config_sha256 TEXT NOT NULL, revision INTEGER NOT NULL,
          approved_principal_id TEXT NOT NULL, approved_agent_id TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS v2_collaboration_ticket (
          ticket_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES v2_task(task_id),
          slot_id INTEGER NOT NULL, generation INTEGER NOT NULL, agent_id TEXT NOT NULL REFERENCES v2_agent(agent_id),
          attempt_id TEXT NOT NULL UNIQUE, nonce TEXT NOT NULL UNIQUE, contract_json TEXT NOT NULL,
          contract_sha256 TEXT NOT NULL, parent_event_id TEXT UNIQUE, parent_review_sha256 TEXT,
          state TEXT NOT NULL CHECK(state IN ('queued','admitted','cancelled')),
          assignment_id TEXT UNIQUE REFERENCES v2_assignment(assignment_id),
          created_at TEXT NOT NULL, UNIQUE(task_id,generation))""",
        "CREATE UNIQUE INDEX IF NOT EXISTS v2_collaboration_queued_slot ON v2_collaboration_ticket(task_id,slot_id) WHERE state='queued'",
        """CREATE TABLE IF NOT EXISTS v2_collaboration_proposal (
          proposal_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES v2_task(task_id),
          assignment_id TEXT NOT NULL REFERENCES v2_assignment(assignment_id), event_id TEXT NOT NULL,
          manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, proposed_by TEXT NOT NULL,
          created_at TEXT NOT NULL, withdrawn_at TEXT, withdrawn_reason TEXT)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS v2_collaboration_current_proposal ON v2_collaboration_proposal(task_id) WHERE withdrawn_at IS NULL",
        """CREATE TABLE IF NOT EXISTS v2_collaboration_verification (
          verification_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES v2_task(task_id),
          assignment_id TEXT NOT NULL REFERENCES v2_assignment(assignment_id), event_id TEXT NOT NULL,
          receipt_json TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS v2_collaboration_verify_intent (
          verification_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES v2_task(task_id),
          assignment_id TEXT NOT NULL REFERENCES v2_assignment(assignment_id), event_id TEXT NOT NULL,
          approval_json TEXT NOT NULL, approval_sha256 TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('ready','attempted','recorded')),
          attempt_token TEXT, attempted_at TEXT, created_at TEXT NOT NULL)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS v2_collaboration_verify_pending
          ON v2_collaboration_verify_intent(assignment_id) WHERE state IN ('ready','attempted')""",
        """CREATE TABLE IF NOT EXISTS v2_collaboration_audit (
          seq INTEGER PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
          action TEXT NOT NULL, principal_id TEXT NOT NULL, agent_id TEXT NOT NULL,
          data_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS v2_collaboration_policy_immutable BEFORE UPDATE OF
          task_id,project_id,config_json,config_sha256,approved_principal_id,approved_agent_id,created_at
          ON v2_collaboration_policy BEGIN SELECT RAISE(ABORT,'immutable collaboration policy'); END""",
        """CREATE TRIGGER IF NOT EXISTS v2_collaboration_ticket_immutable BEFORE UPDATE OF
          ticket_id,task_id,slot_id,generation,agent_id,attempt_id,nonce,contract_json,contract_sha256,
          parent_event_id,parent_review_sha256,created_at ON v2_collaboration_ticket
          BEGIN SELECT RAISE(ABORT,'immutable collaboration ticket'); END""",
        """CREATE TRIGGER IF NOT EXISTS v2_collaboration_ticket_transition BEFORE UPDATE
          ON v2_collaboration_ticket WHEN NOT (OLD.state='queued' AND
            ((NEW.state='admitted' AND NEW.assignment_id IS NOT NULL)
              OR (NEW.state='cancelled' AND NEW.assignment_id IS NULL)))
          BEGIN SELECT RAISE(ABORT,'ticket cannot be reset'); END""",
        """CREATE TRIGGER IF NOT EXISTS v2_collaboration_proposal_immutable BEFORE UPDATE OF
          proposal_id,task_id,assignment_id,event_id,manifest_json,manifest_sha256,proposed_by,created_at
          ON v2_collaboration_proposal BEGIN SELECT RAISE(ABORT,'immutable winner proposal'); END""",
        """CREATE TRIGGER IF NOT EXISTS v2_collaboration_verify_intent_immutable BEFORE UPDATE OF
          verification_id,task_id,assignment_id,event_id,approval_json,approval_sha256,created_at
          ON v2_collaboration_verify_intent BEGIN SELECT RAISE(ABORT,'immutable verifier approval'); END""",
        """CREATE TRIGGER IF NOT EXISTS v2_collaboration_verify_transition BEFORE UPDATE
          ON v2_collaboration_verify_intent WHEN NOT (
            (OLD.state='ready' AND NEW.state='attempted' AND NEW.attempt_token IS NOT NULL AND NEW.attempted_at IS NOT NULL)
            OR (OLD.state='attempted' AND NEW.state='recorded' AND NEW.attempt_token=OLD.attempt_token
                AND NEW.attempted_at=OLD.attempted_at))
          BEGIN SELECT RAISE(ABORT,'verifier attempt cannot be reset'); END""",
    )
    for sql in ddl:
        con.execute(sql)
    for table in ('policy', 'ticket', 'proposal', 'verification', 'verify_intent', 'audit'):
        operations = ('UPDATE', 'DELETE') if table in ('verification', 'audit') else ('DELETE',)
        name = 'v2_collaboration_' + table
        for operation in operations:
            con.execute('CREATE TRIGGER IF NOT EXISTS ' + name + '_no_' + operation.lower() +
                        ' BEFORE ' + operation + ' ON ' + name +
                        " BEGIN SELECT RAISE(ABORT,'retained collaboration history'); END")
    con.execute('INSERT OR IGNORE INTO v2_collaboration_meta VALUES (1,?)', (SCHEMA_VERSION,))


def _principal(con, project_id, principal, roles=None):
    if not isinstance(principal, dict) or principal.get('role') not in ('controller', 'reviewer', 'worker', 'observer'):
        _fail('invalid_principal', 'service-derived registered principal required')
    for key in ('agent_id', 'principal_id'):
        try:
            _text(principal.get(key), key)
        except CollaborationError:
            _fail('invalid_principal', key + ' is required')
    if principal.get('project_id', project_id) != project_id:
        _fail('wrong_project', 'principal belongs to another project')
    agent = _one(con, 'SELECT * FROM v2_agent WHERE agent_id=?', (principal['agent_id'],))
    if agent and agent['project_id'] != project_id:
        _fail('wrong_project', 'principal belongs to another project')
    if not agent or agent['lifecycle'] != 'active' or (agent['role'] != principal['role'] and principal['role'] != 'observer'):
        _fail('invalid_principal', 'principal does not match an active project agent')
    if roles is not None and principal['role'] not in roles:
        _fail('forbidden_role', 'role cannot perform this operation')
    return principal


def _task(con, project_id, task_id):
    _text(task_id, 'task_id')
    task = _one(con, 'SELECT * FROM v2_task WHERE task_id=? AND project_id=?', (task_id, project_id))
    if not task:
        _fail('wrong_project', 'Task does not belong to this project')
    return task


def _plan(con, project_id, task_id, writable=False):
    task = _task(con, project_id, task_id)
    row = _one(con, 'SELECT * FROM v2_collaboration_policy WHERE task_id=? AND project_id=?', (task_id, project_id))
    if not row:
        _fail('not_found', 'collaboration policy is not configured')
    config = json.loads(row['config_json'])
    count = len(config['slots']) - (config['mode'] == 'ensemble')
    if (_sha(config) != row['config_sha256'] or task['execution_mode'] != config['mode']
            or task['fanout_slots'] != count):
        _fail('integrity_error', 'policy or Task mode changed')
    if writable and task['task_state'] in TERMINAL:
        _fail('state_conflict', 'terminal Task cannot schedule or select new work')
    row['config'] = config
    return task, row


def _expected(row, value):
    if _integer(value, 'expected_revision') != row['revision']:
        _fail('revision_conflict', 'stale expected_revision')


def _audit(con, project_id, task_id, principal, action, data):
    con.execute('INSERT INTO v2_collaboration_audit(project_id,task_id,action,principal_id,agent_id,data_json,created_at) VALUES (?,?,?,?,?,?,?)',
                (project_id, task_id, action, principal['principal_id'], principal['agent_id'], _json(data), service._now()))


def _advance(con, plan):
    cur = con.execute('UPDATE v2_collaboration_policy SET revision=revision+1 WHERE task_id=? AND revision=?',
                      (plan['task_id'], plan['revision']))
    if cur.rowcount != 1:
        _fail('revision_conflict', 'policy changed concurrently')
    plan['revision'] += 1


def _file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(65536), b''):
            h.update(data)
    return h.hexdigest()


def _binding(con, agent_id):
    for row in _rows(con, "SELECT data FROM bindings WHERE status='active'"):
        binding = json.loads(row['data'])
        if service._agent_id_for_binding(binding['instance_id']) == agent_id:
            return binding
    _fail('invalid_principal', 'Agent has no current native binding')


def _agent(con, project_id, agent_id):
    row = _one(con, 'SELECT * FROM v2_agent WHERE project_id=? AND agent_id=?', (project_id, agent_id))
    if not row or row['lifecycle'] != 'active' or row['role'] != 'worker':
        _fail('invalid_worker', 'approved project worker required')
    return row


def _configure(con, project_id, principal, payload):
    keys = ('task_id', 'expected_revision', 'mode', 'slots', 'max_attempts', 'max_repairs', 'verifier')
    _fields(payload, keys, keys)
    task = _task(con, project_id, payload['task_id'])
    _expected(task, payload['expected_revision'])
    if task['task_state'] not in ('draft', 'ready'):
        _fail('state_conflict', 'configure a draft or ready Task')
    for table in ('v2_assignment', 'v2_scheduler_queue', 'v2_collaboration_policy'):
        if con.execute('SELECT 1 FROM ' + table + ' WHERE task_id=?', (task['task_id'],)).fetchone():
            _fail('state_conflict', 'policy must precede scheduling and is immutable')
    mode, slots = payload['mode'], payload['slots']
    if mode not in ('single', 'fanout', 'ensemble') or not isinstance(slots, list) or not 1 <= len(slots) <= 9:
        _fail('invalid_payload', 'invalid execution mode or slot count')
    frozen, ids = [], set()
    for slot in slots:
        _fields(slot, ('slot_id', 'agent_id'), ('slot_id', 'agent_id'))
        number = _integer(slot['slot_id'], 'slot_id', 0, 8)
        agent_id = _text(slot['agent_id'], 'agent_id')
        _agent(con, project_id, agent_id)
        binding = _binding(con, agent_id)
        if binding['mode'] != 'worker' or number in ids or any(
                agent_id == old['agent_id'] or control.same_binding_agent(binding, old['binding']) for old in frozen):
            _fail('invalid_worker', 'slots require distinct worker identities and slot IDs')
        frozen.append(dict(slot_id=number, agent_id=agent_id, binding=binding))
        ids.add(number)
    competitors = len(slots) - (mode == 'ensemble')
    expected = {0} if mode == 'single' else set(range(1, competitors + 1)) | ({0} if mode == 'ensemble' else set())
    if ids != expected or (mode == 'single' and len(slots) != 1) or (mode != 'single' and not 2 <= competitors <= 8):
        _fail('invalid_payload', 'single uses 0; competitors use 1..N; ensemble also requires synthesis slot 0')
    attempts = _integer(payload['max_attempts'], 'max_attempts', len(slots), 128)
    repairs = _integer(payload['max_repairs'], 'max_repairs', 0, 64)
    verifier = copy.deepcopy(payload['verifier'])
    vk = ('argv', 'timeout_seconds', 'max_output_bytes', 'max_runs_per_assignment')
    _fields(verifier, vk, vk)
    argv = verifier['argv']
    if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
        _fail('invalid_payload', 'verifier argv must contain 1..64 arguments')
    for value in argv:
        _text(value, 'argv element', 16384)
    executable = shutil.which(argv[0])
    if not executable or Path(executable).suffix.lower() in ('.bat', '.cmd'):
        _fail('invalid_payload', 'verifier requires an executable, not a shell batch file')
    argv[0] = str(Path(executable).resolve())
    verifier['executable_sha256'] = _file_hash(argv[0])
    _integer(verifier['timeout_seconds'], 'timeout_seconds', 1, 300)
    _integer(verifier['max_output_bytes'], 'max_output_bytes', 1, 1024 * 1024)
    _integer(verifier['max_runs_per_assignment'], 'max_runs_per_assignment', 1, 8)
    config = dict(mode=mode, slots=sorted(frozen, key=lambda x: x['slot_id']),
                  max_attempts=attempts, max_repairs=repairs, verifier=verifier)
    now = service._now()
    con.execute('INSERT INTO v2_collaboration_policy VALUES (?,?,?,?,1,?,?,?)',
                (task['task_id'], project_id, _json(config), _sha(config), principal['principal_id'], principal['agent_id'], now))
    con.execute('UPDATE v2_task SET execution_mode=?,fanout_slots=?,revision=revision+1,updated_at=? WHERE task_id=?',
                (mode, competitors, now, task['task_id']))
    _audit(con, project_id, task['task_id'], principal, 'configured', {'config_sha256': _sha(config)})
    return dict(task_id=task['task_id'], revision=1, task_revision=task['revision'] + 1, config_sha256=_sha(config))


def _assignment(con, project_id, assignment_id):
    row = _one(con, 'SELECT a.* FROM v2_assignment a JOIN v2_task t ON t.task_id=a.task_id '
               'WHERE t.project_id=? AND a.assignment_id=?', (project_id, _text(assignment_id, 'assignment_id')))
    if not row:
        _fail('wrong_project', 'Assignment does not belong to this project')
    return row


def _independent(con, task_id, plan, reviewer):
    agent_id = service._agent_id_for_binding(reviewer['instance_id'])
    identities = [(s['agent_id'], s['binding']) for s in plan['config']['slots']]
    for row in _rows(con, 'SELECT a.agent_id,t.data FROM v2_assignment a LEFT JOIN tasks t '
                     'ON t.id=a.legacy_task_id WHERE a.task_id=?', (task_id,)):
        frozen = json.loads(row['data']).get('worker_binding') if row['data'] else None
        identities.append((row['agent_id'], frozen))
    for identity, binding in identities:
        if identity == agent_id or (binding and control.same_binding_agent(reviewer, binding)):
            _fail('self_review', 'Reviewer must be independent of every competitor and synthesis worker')


def review_guard(con, project_id, principal, task_id):
    _principal(con, project_id, principal, ('controller', 'reviewer'))
    _, plan = _plan(con, project_id, task_id)
    reviewer = _binding(con, principal['agent_id'])
    if reviewer['mode'] not in ('controller', 'reviewer'):
        _fail('invalid_principal', 'native reviewer binding required')
    _independent(con, task_id, plan, reviewer)
    return dict(allowed=True, task_id=task_id, reviewer_agent_id=principal['agent_id'])


def _delivery(con, project_id, assignment_id, event_id):
    assignment = _assignment(con, project_id, assignment_id)
    _text(event_id, 'event_id')
    mapping = _one(con, 'SELECT * FROM v2_legacy_map WHERE v2_assignment_id=?', (assignment_id,))
    task = _one(con, 'SELECT * FROM tasks WHERE id=?', (assignment['legacy_task_id'],))
    event = _one(con, 'SELECT * FROM events WHERE id=? AND task=?', (event_id, assignment['legacy_task_id']))
    if not mapping or not task or not event or event['status'] not in ('pending', 'acknowledged', 'reviewed'):
        _fail('evidence_changed', 'native mapped completion event required')
    data, record = json.loads(task['data']), json.loads(event['data'])
    request, receipt = data['request'], record.get('receipt') or {}
    if (mapping['v2_task_id'] != assignment['task_id'] or mapping['legacy_task_id'] != task['id']
            or mapping['generation'] != assignment['generation'] or request.get('generation') != assignment['generation']
            or request.get('task_id') != task['id'] or not request.get('dispatch_confirmed')
            or any(n != assignment['nonce'] for n in (task['nonce'], request.get('nonce'), event['nonce'], mapping['nonce']))
            or task['request_hash'] != control.digest(request)
            or mapping['request_sha256'] != task['request_hash'] or record.get('request_sha256') != task['request_hash']
            or type(receipt.get('completion_seq')) is not int
            or receipt['completion_seq'] <= request['worker']['baseline_completion_seq']):
        _fail('evidence_changed', 'completion identity, generation, request hash or sequence changed')
    if service._agent_id_for_binding(data['worker_binding']['instance_id']) != assignment['agent_id']:
        _fail('evidence_changed', 'Assignment worker differs from frozen delivery worker')
    evidence, issues = service._artifact_checks(task, request, event)
    paths = {str(Path(row[0]).resolve()) for row in evidence}
    for item in [request['completion']['binding_artifact']] + request['completion'].get('artifacts', []):
        name = item if isinstance(item, str) else item['path']
        if str((Path(request['cwd']) / name).resolve()) not in paths:
            issues.append('frozen deliverable absent')
    if issues or not evidence:
        _fail('evidence_changed', '; '.join(issues) or 'empty evidence')
    artifacts = sorted([dict(path=str(x[0]), sha256=x[1], bytes=x[3]) for x in evidence], key=lambda x: x['path'])
    snapshot = dict(assignment_id=assignment_id, generation=assignment['generation'], event_id=event_id,
                    event_sha256=_sha(record), request_sha256=task['request_hash'], artifacts=artifacts)
    return assignment, request, snapshot


def _sealed_review(con, project_id, plan, assignment_id, event_id, verdict):
    assignment, _, snapshot = _delivery(con, project_id, assignment_id, event_id)
    review, _ = _native(con, scheduler._verified_delivery, assignment['legacy_task_id'], event_id)
    if review['verdict'] != verdict:
        _fail('invalid_review', 'original sealed ' + verdict + ' review required')
    if review.get('workflow_config_sha256') != control.digest(_native(con, control.config_of)):
        _fail('invalid_review', 'review workflow authority changed')
    _independent(con, assignment['task_id'], plan, review['actor'])
    return review, snapshot


def _overlap(left, right):
    a, b = Path(left).resolve(), Path(right).resolve()
    return a == b or a in b.parents or b in a.parents


def _repair(con, project_id, task, plan, slot_id, request):
    previous = _one(con, 'SELECT * FROM v2_assignment WHERE task_id=? AND slot_id=? '
                    'ORDER BY generation DESC LIMIT 1', (task['task_id'], slot_id))
    if previous is None:
        if request.get('parent_task') or request.get('parent_review'):
            _fail('invalid_review', 'initial slot cannot borrow an unrelated repair')
        return None, None
    intent = _one(con, 'SELECT * FROM v2_scheduler_intent WHERE assignment_id=?', (previous['assignment_id'],))
    if previous['holds_slot'] or not intent or intent['send_state'] != 'closed' or not intent['closure_json']:
        _fail('slot_held', 'authoritative close is required; TTL and cancellation requests do not release slots')
    parent = request.get('parent_review')
    if request.get('parent_task') != previous['legacy_task_id'] or not isinstance(parent, dict):
        _fail('invalid_review', 'repair must consume latest same-slot Assignment review')
    review, _ = _sealed_review(con, project_id, plan, previous['assignment_id'], parent.get('event_id'), 'repair')
    closure = json.loads(intent['closure_json'])
    previous_request = json.loads(intent['contract_json'])['request']
    mapping = _one(con, 'SELECT * FROM v2_legacy_map WHERE v2_assignment_id=?', (previous['assignment_id'],))
    native_review = _one(con, "SELECT * FROM v2_review WHERE assignment_id=? AND event_id=? AND verdict='repair' AND state='sealed'",
                         (previous['assignment_id'], review['event_id']))
    if (closure.get('outcome') != 'repair_required' or closure.get('event_id') != review['event_id']
            or closure.get('review_sha256') != review['sha256'] or not native_review
            or native_review['note_sha256'] != review['sha256']
            or not mapping or mapping['review_event_id'] != review['event_id'] or mapping['review_sha256'] != review['sha256']
            or not isinstance(closure.get('observation'), dict)
            or not legacy._worker_quiescent(previous_request, closure['observation'])
            or not monitor.identity_matches(closure['observation'].get('identity', {}),
                                            previous_request['worker']['expected_identity'])):
        _fail('invalid_review', 'native sealed repair closure and quiescence provenance required')
    fields = ('event_id', 'task_id', 'nonce', 'path', 'sha256', 'verdict')
    if any(parent.get(k) != review.get(k) for k in fields) or 'repair' not in request.get('allowed_parent_verdicts', []):
        _fail('invalid_review', 'follow-up differs from existing sealed repair authority')
    if previous['review_status'] != 'repair':
        _fail('invalid_review', 'previous Assignment is not authoritatively closed for repair')
    return review['event_id'], _sha(review)


def _contract_check(con, project_id, task, plan, slot_id, contract, ticket=None):
    slots = {s['slot_id']: s for s in plan['config']['slots']}
    _integer(slot_id, 'slot_id', 0, 8)
    slot = slots.get(slot_id)
    if not slot or contract.get('task_id') != task['task_id'] or contract.get('agent_id') != slot['agent_id']:
        _fail('ownership_conflict', 'contract must use the controller-approved Task, slot and worker')
    _agent(con, project_id, contract['agent_id'])
    request = contract['request']
    _native(con, scheduler._request_check, request, contract['message'],
            allow_missing_launch_cwd=contract.get('workspace_mode') == 'git_worktree')
    worker = _native(con, scheduler._worker_binding, request, slot['binding'])
    if _sha(worker) != _sha(contract['worker_binding']):
        _fail('ownership_conflict', 'frozen worker binding changed')
    reviewer = _native(con, control.active, request['reviewer']['role'])
    _independent(con, task['task_id'], plan, reviewer)
    if con.execute('SELECT 1 FROM v2_assignment WHERE task_id=? AND slot_id=? AND holds_slot=1',
                   (task['task_id'], slot_id)).fetchone():
        _fail('slot_held', 'slot is still owned by its earlier Assignment')
    if con.execute('SELECT 1 FROM tasks WHERE id=? OR nonce=?', (request['task_id'], request['nonce'])).fetchone():
        _fail('attempt_reused', 'attempt ID and nonce must be fresh')
    tid = ticket['ticket_id'] if ticket else ''
    for other in _rows(con, 'SELECT * FROM v2_collaboration_ticket WHERE ticket_id<>?', (tid,)):
        if other['attempt_id'] == request['task_id'] or other['nonce'] == request['nonce']:
            _fail('attempt_reused', 'attempt ID or nonce has already been frozen')
        if other['task_id'] == task['task_id'] and other['slot_id'] == slot_id and other['state'] == 'queued':
            _fail('slot_held', 'slot already has a queued approval')
        if other['task_id'] == task['task_id'] and other['slot_id'] != slot_id:
            old = json.loads(other['contract_json'])['request']
            for left in (request['cwd'], request['worker']['expected_identity']['launch_cwd']):
                for right in (old['cwd'], old['worker']['expected_identity']['launch_cwd']):
                    if _overlap(left, right):
                        _fail('workspace_conflict', 'competitors require disjoint workspaces')
    # The legacy queue still participates in ID/nonce uniqueness.
    for other in _rows(con, 'SELECT task_id,contract_json FROM v2_scheduler_queue'):
        old = json.loads(other['contract_json'])['request']
        if (old['task_id'] == request['task_id'] or old['nonce'] == request['nonce']) and not (
                ticket and other['task_id'] == task['task_id'] and old == request):
            _fail('attempt_reused', 'attempt ID or nonce belongs to another native queue entry')
    count = con.execute('SELECT count(*) FROM v2_assignment WHERE task_id=?', (task['task_id'],)).fetchone()[0]
    queued = con.execute("SELECT count(*) FROM v2_collaboration_ticket WHERE task_id=? AND state='queued' AND ticket_id<>?",
                         (task['task_id'], tid)).fetchone()[0]
    if count + queued + 1 > plan['config']['max_attempts']:
        _fail('attempt_limit', 'shared Task attempt cap reached')
    parent_event, parent_sha = _repair(con, project_id, task, plan, slot_id, request)
    if parent_event:
        if con.execute('SELECT 1 FROM v2_collaboration_ticket WHERE parent_event_id=? AND ticket_id<>?',
                       (parent_event, tid)).fetchone():
            _fail('review_consumed', 'repair parent event was already consumed')
        repairs = con.execute('SELECT count(*) FROM v2_collaboration_ticket WHERE task_id=? '
                              'AND parent_event_id IS NOT NULL AND ticket_id<>?', (task['task_id'], tid)).fetchone()[0]
        if repairs + 1 > plan['config']['max_repairs']:
            _fail('repair_limit', 'shared Task repair cap reached')
    if ticket and (parent_event != ticket['parent_event_id'] or parent_sha != ticket['parent_review_sha256']):
        _fail('evidence_changed', 'repair parent changed after enqueue')
    _native(con, scheduler._budget_eligibility, project_id, task['task_id'], contract['budget'])
    return parent_event, parent_sha


def prepare_enqueue(con, project_id, principal, task_id, slot_id, contract, expected_revision):
    _tx(con)
    _principal(con, project_id, principal, ('controller',))
    task, plan = _plan(con, project_id, task_id, True)
    _expected(plan, expected_revision)
    if _proposal(con, task_id):
        _fail('state_conflict', 'withdraw the winner proposal before authorizing more work')
    if con.execute('SELECT count(*) FROM v2_collaboration_ticket WHERE task_id=?', (task_id,)).fetchone()[0] >= 256:
        _fail('attempt_limit', 'Task approval history cap reached')
    allowed = ('task_id', 'agent_id', 'request', 'message', 'budget', 'required_capabilities', 'resources',
               'dependencies', 'worker_binding', 'workspace_mode', 'task_kind', 'workspace_base_commit',
               'workspace_path', 'dag_run', 'dag_node',
               'approved_controller_id', 'approved_controller_version', 'approved_epoch')
    _fields(contract, allowed, ('task_id', 'agent_id', 'request', 'message', 'budget'))
    c = copy.deepcopy(contract)
    if not isinstance(c['request'], dict):
        _fail('invalid_payload', 'request must be an object')
    mode = c.get('workspace_mode', 'directory')
    if mode not in ('directory', 'git_worktree'):
        _fail('invalid_payload', 'workspace_mode must be directory or git_worktree')
    if mode == 'git_worktree':
        launch = c['request'].get('worker', {}).get('expected_identity', {}).get('launch_cwd')
        path = c.get('workspace_path')
        if (not isinstance(launch, str) or not isinstance(path, str)
                or Path(launch).resolve() != Path(path).resolve()):
            _fail('invalid_payload', 'frozen workspace path differs from the request launch_cwd')
    generation = con.execute('SELECT COALESCE(MAX(generation),0)+1 FROM ('
                             'SELECT generation FROM v2_assignment WHERE task_id=? UNION ALL '
                             'SELECT generation FROM v2_collaboration_ticket WHERE task_id=?)', (task_id, task_id)).fetchone()[0]
    c['request']['generation'] = generation
    _native(con, scheduler._request_check, c['request'], c['message'],
            allow_missing_launch_cwd=mode == 'git_worktree')
    c['worker_binding'] = _native(con, scheduler._worker_binding, c['request'])
    try:
        c['budget'] = budget._dimensions(c['budget'])
    except ValueError as exc:
        _fail('invalid_payload', str(exc))
    c.setdefault('required_capabilities', list(scheduler.DEFAULT_CAPABILITIES))
    c.setdefault('resources', {})
    c.setdefault('dependencies', [])
    c.setdefault('workspace_mode', 'directory')
    c.setdefault('task_kind', 'artifact')
    c.setdefault('dag_run', None)
    c.setdefault('dag_node', None)
    parent_event, parent_sha = _contract_check(con, project_id, task, plan, slot_id, c)
    ticket_id = 'col-ticket-' + uuid.uuid4().hex
    con.execute("INSERT INTO v2_collaboration_ticket VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',NULL,?)",
                (ticket_id, task_id, slot_id, generation, c['agent_id'], c['request']['task_id'], c['request']['nonce'],
                 _json(c), _sha(c), parent_event, parent_sha, service._now()))
    _advance(con, plan)
    _audit(con, project_id, task_id, principal, 'enqueued', dict(ticket_id=ticket_id, parent_event_id=parent_event))
    return dict(ticket_id=ticket_id, task_id=task_id, slot_id=slot_id, generation=generation,
                revision=plan['revision'], contract=c, contract_sha256=_sha(c))


def _ticket(con, project_id, ticket_id):
    row = _one(con, 'SELECT c.* FROM v2_collaboration_ticket c JOIN v2_task t ON t.task_id=c.task_id '
               'WHERE c.ticket_id=? AND t.project_id=?', (_text(ticket_id, 'ticket_id'), project_id))
    if not row:
        _fail('wrong_project', 'ticket does not belong to this project')
    contract = json.loads(row['contract_json'])
    if _sha(contract) != row['contract_sha256'] or (
            contract['request']['generation'], contract['agent_id'], contract['request']['task_id'],
            contract['request']['nonce']) != (row['generation'], row['agent_id'], row['attempt_id'], row['nonce']):
        _fail('integrity_error', 'ticket content changed')
    return row, contract


def check_admission(con, project_id, principal, ticket_id):
    _principal(con, project_id, principal, ('controller',))
    ticket, c = _ticket(con, project_id, ticket_id)
    task, plan = _plan(con, project_id, ticket['task_id'], True)
    if ticket['state'] != 'queued':
        _fail('state_conflict', 'only a queued ticket may be admitted')
    if _proposal(con, task['task_id']):
        _fail('state_conflict', 'withdraw the winner proposal before admitting more work')
    _contract_check(con, project_id, task, plan, ticket['slot_id'], c, ticket)
    return dict(allowed=True, ticket_id=ticket_id, task_id=task['task_id'], slot_id=ticket['slot_id'],
                generation=ticket['generation'], revision=plan['revision'], contract=c)


def admit_in_transaction(con, project_id, principal, ticket_id):
    _tx(con)
    result = check_admission(con, project_id, principal, ticket_id)
    c, task_id = result['contract'], result['task_id']
    _, plan = _plan(con, project_id, task_id, True)
    aid, rid, now = 'asg-' + uuid.uuid4().hex, 'run-' + uuid.uuid4().hex, service._now()
    con.execute("INSERT INTO v2_assignment(assignment_id,task_id,agent_id,generation,slot_id,holds_slot,state,"
                "nonce,legacy_task_id,legacy_dispatch_status,created_at,updated_at) VALUES (?,?,?,?,?,1,'reserved',?,?,'reserved',?,?)",
                (aid, task_id, c['agent_id'], result['generation'], result['slot_id'], c['request']['nonce'],
                 c['request']['task_id'], now, now))
    reservations = _native(con, budget._reserve_in_transaction, project_id, rid, aid, result['generation'],
                           c['budget'], 'collaboration_scheduler_estimate')
    con.execute("UPDATE v2_collaboration_ticket SET state='admitted',assignment_id=? WHERE ticket_id=? AND state='queued'",
                (aid, ticket_id))
    _advance(con, plan)
    _audit(con, project_id, task_id, principal, 'admitted', dict(ticket_id=ticket_id, assignment_id=aid, run_id=rid))
    return dict(result, status='RESERVED', assignment_id=aid, run_id=rid, reservation_ids=reservations,
                revision=plan['revision'], sent=False)


def _candidate(con, project_id, plan, assignment_id, event_id):
    assignment, request, snapshot = _delivery(con, project_id, assignment_id, event_id)
    if assignment['task_id'] != plan['task_id']:
        _fail('ownership_conflict', 'candidate belongs to another Task')
    latest = con.execute('SELECT MAX(generation) FROM v2_assignment WHERE task_id=? AND slot_id=?',
                         (assignment['task_id'], assignment['slot_id'])).fetchone()[0]
    if (assignment['generation'] != latest or assignment['state'] != 'completed'
            or assignment['review_status'] in ('repair', 'rejected')
            or con.execute("SELECT 1 FROM v2_collaboration_ticket WHERE task_id=? AND slot_id=? AND state='queued'",
                           (assignment['task_id'], assignment['slot_id'])).fetchone()):
        _fail('state_conflict', 'candidate must be the current completed Assignment for its slot')
    return assignment, request, snapshot


def _run_verifier(config, cwd):
    # Drain pipes continuously with bounded retained bytes; no unbounded capture
    # or spool file. Inherited descendant pipes fail closed if EOF is unavailable.
    limit = config['max_output_bytes']
    streams = {name: dict(data=bytearray(), observed=0, eof=False) for name in ('stdout', 'stderr')}
    overflow, lock = threading.Event(), threading.Lock()
    started = service._now()
    receipt = dict(argv=config['argv'], cwd=cwd, started_at=started, pid=None, exit_code=None,
                   timed_out=False, output_limited=False, streams_complete=False, spawn_error=None)
    try:
        process = subprocess.Popen(config['argv'], cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        receipt.update(spawn_error=str(exc), finished_at=service._now(), passed=False)
        return receipt
    receipt['pid'] = process.pid
    def drain(pipe, name):
        try:
            while True:
                block = pipe.read1(65536)
                if not block:
                    with lock:
                        streams[name]['eof'] = True
                    break
                with lock:
                    streams[name]['observed'] += len(block)
                    remaining = max(0, limit - sum(len(s['data']) for s in streams.values()))
                    streams[name]['data'].extend(block[:remaining])
                    if len(block) > remaining:
                        overflow.set()
        finally:
            pipe.close()
    threads = [threading.Thread(target=drain, args=(getattr(process, name), name), daemon=True) for name in streams]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + config['timeout_seconds']
    try:
        while process.poll() is None:
            if overflow.is_set() or time.monotonic() >= deadline:
                receipt['timed_out'] = time.monotonic() >= deadline
                process.kill()
                break
            overflow.wait(0.01)
        receipt['exit_code'] = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=1)
    with lock:
        receipt['streams_complete'] = all(s['eof'] for s in streams.values())
        receipt['output_limited'] = overflow.is_set()
        receipt['output'] = {name: dict(observed_bytes=s['observed'], retained_bytes=len(s['data']),
                            retained_sha256=hashlib.sha256(s['data']).hexdigest(),
                            text=s['data'].decode('utf-8', errors='replace'), eof=s['eof']) for name, s in streams.items()}
    receipt['finished_at'] = service._now()
    receipt['passed'] = (receipt['exit_code'] == 0 and not receipt['timed_out']
                         and not receipt['output_limited'] and receipt['streams_complete'])
    return receipt


def _verify(con, project_id, principal, payload):
    """Approve only. No subprocess runs until the caller commits this intent."""
    keys = ('task_id', 'expected_revision', 'assignment_id', 'event_id')
    _fields(payload, keys, keys)
    _, plan = _plan(con, project_id, payload['task_id'], True)
    _expected(plan, payload['expected_revision'])
    aid, eid = payload['assignment_id'], payload['event_id']
    _, request, before = _candidate(con, project_id, plan, aid, eid)
    verifier = plan['config']['verifier']
    count = con.execute('SELECT count(*) FROM v2_collaboration_verify_intent WHERE assignment_id=?', (aid,)).fetchone()[0]
    if count >= verifier['max_runs_per_assignment']:
        _fail('verify_limit', 'bounded verifier run limit reached')
    if con.execute("SELECT 1 FROM v2_collaboration_verify_intent WHERE assignment_id=? AND state IN ('ready','attempted')",
                   (aid,)).fetchone():
        _fail('verification_pending', 'existing ready or uncertain verifier intent blocks another run')
    if _file_hash(verifier['argv'][0]) != verifier['executable_sha256']:
        _fail('evidence_changed', 'approved verifier executable changed')
    binding = _binding(con, principal['agent_id'])
    state, _ = _native(con, scheduler._guard, project_id, binding['instance_id'])
    approval = dict(before=before, verifier=verifier, cwd=request['cwd'], agent_id=principal['agent_id'],
                    principal_id=principal['principal_id'], controller_epoch=state['controller_epoch'])
    vid = 'col-verify-' + uuid.uuid4().hex
    con.execute("INSERT INTO v2_collaboration_verify_intent VALUES (?,?,?,?,?,?,'ready',NULL,NULL,?)",
                (vid, plan['task_id'], aid, eid, _json(approval), _sha(approval), service._now()))
    _advance(con, plan)
    _audit(con, project_id, plan['task_id'], principal, 'verification_approved',
           dict(verification_id=vid, approval_sha256=_sha(approval)))
    return dict(verification_id=vid, revision=plan['revision'], status='READY', process_started=False)


def run_verification(con, project_id, principal, verification_id):
    """Explicit external-effect runner; use a dedicated idle SQLite connection.

    Unlike the domain helpers, this function owns two short transactions. The
    durable attempt marker is committed BEFORE Popen. A crash at either side of
    Popen is conservatively uncertain. Never reset it or automatically retry.
    """
    if con.in_transaction:
        _fail('idle_connection_required', 'commit caller intent first; use a dedicated idle connection')
    database = next(row[2] for row in con.execute('PRAGMA database_list') if row[1] == 'main')
    if not database or con.execute('PRAGMA synchronous').fetchone()[0] < 2 or (
            con.execute('PRAGMA journal_mode').fetchone()[0].lower() in ('off', 'memory')):
        _fail('durability_required', 'verifier runner needs persistent journaled SQLite with synchronous=FULL or EXTRA')
    _text(verification_id, 'verification_id')
    con.execute('BEGIN IMMEDIATE')
    try:
        _principal(con, project_id, principal, ('controller',))
        intent = _one(con, 'SELECT i.* FROM v2_collaboration_verify_intent i JOIN v2_task t ON t.task_id=i.task_id '
                      'WHERE i.verification_id=? AND t.project_id=?', (verification_id, project_id))
        if not intent:
            _fail('wrong_project', 'committed verifier intent does not belong to this project')
        approval = json.loads(intent['approval_json'])
        if _sha(approval) != intent['approval_sha256']:
            _fail('integrity_error', 'verifier approval changed')
        if (approval['agent_id'], approval['principal_id']) != (principal['agent_id'], principal['principal_id']):
            _fail('ownership_conflict', 'verifier intent belongs to another principal')
        if intent['state'] == 'recorded':
            row = _one(con, 'SELECT * FROM v2_collaboration_verification WHERE verification_id=?', (verification_id,))
            if not row or _sha(json.loads(row['receipt_json'])) != row['receipt_sha256']:
                _fail('integrity_error', 'recorded verifier receipt missing or changed')
            con.rollback()
            return dict(status='RECORDED', verification_id=verification_id, replayed=True,
                        receipt=json.loads(row['receipt_json']), receipt_sha256=row['receipt_sha256'])
        if intent['state'] == 'attempted':
            con.rollback()
            return dict(status='UNCERTAIN', verification_id=verification_id, process_started=False,
                        reason='Prior attempt may still run or may have exited; inspect externally, never auto retry.')
        _, plan = _plan(con, project_id, intent['task_id'], True)
        binding = _binding(con, principal['agent_id'])
        state, _ = _native(con, scheduler._guard, project_id, binding['instance_id'])
        if state['controller_epoch'] != approval['controller_epoch']:
            _fail('stale_epoch', 'verifier approval belongs to an earlier controller epoch')
        _, request, before = _candidate(con, project_id, plan, intent['assignment_id'], intent['event_id'])
        verifier = approval['verifier']
        if (before != approval['before'] or request['cwd'] != approval['cwd']
                or verifier != plan['config']['verifier']
                or _file_hash(verifier['argv'][0]) != verifier['executable_sha256']):
            _fail('evidence_changed', 'verifier input or executable changed before launch')
        token = uuid.uuid4().hex
        con.execute("UPDATE v2_collaboration_verify_intent SET state='attempted',attempt_token=?,attempted_at=? "
                    "WHERE verification_id=? AND state='ready'", (token, service._now(), verification_id))
        _advance(con, plan)
        _audit(con, project_id, plan['task_id'], principal, 'verification_attempted', dict(verification_id=verification_id))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    # No SQL transaction spans the external effect. Failure here leaves the
    # committed marker in attempted; a caller must not fabricate a success.
    receipt = _run_verifier(verifier, approval['cwd'])
    receipt.update(before=before, verifier_sha256=_sha(verifier), after=None, attempt_token=token)
    con.execute('BEGIN IMMEDIATE')
    try:
        current = _one(con, 'SELECT * FROM v2_collaboration_verify_intent WHERE verification_id=?', (verification_id,))
        if current['state'] != 'attempted' or current['attempt_token'] != token:
            _fail('state_conflict', 'verifier attempt ownership changed')
        _, plan = _plan(con, project_id, intent['task_id'])
        try:
            _, _, after = _candidate(con, project_id, plan, intent['assignment_id'], intent['event_id'])
            receipt['after'] = after
            if after != before or _file_hash(verifier['argv'][0]) != verifier['executable_sha256']:
                _fail('evidence_changed', 'evidence or executable changed during verification')
            state = _native(con, service._state_row)
            if state['controller_epoch'] != approval['controller_epoch'] or state['frozen'] or not state['v2_enabled']:
                _fail('stale_epoch', 'controller authority changed during verification')
            _principal(con, project_id, principal, ('controller',))
            current_binding = _binding(con, principal['agent_id'])
            _native(con, scheduler._guard, project_id, current_binding['instance_id'])
        except (ValueError, OSError) as exc:
            receipt['passed'] = False
            receipt['evidence_error'] = str(exc)
        con.execute('INSERT INTO v2_collaboration_verification VALUES (?,?,?,?,?,?,?)',
                    (verification_id, plan['task_id'], intent['assignment_id'], intent['event_id'],
                     _json(receipt), _sha(receipt), service._now()))
        con.execute("UPDATE v2_collaboration_verify_intent SET state='recorded' WHERE verification_id=?", (verification_id,))
        _advance(con, plan)
        _audit(con, project_id, plan['task_id'], principal, 'verified',
               dict(verification_id=verification_id, receipt_sha256=_sha(receipt), passed=receipt['passed']))
        con.commit()
        return dict(status='RECORDED', verification_id=verification_id, replayed=False, revision=plan['revision'],
                    receipt=receipt, receipt_sha256=_sha(receipt))
    except BaseException:
        con.rollback()
        raise


def _verification(con, plan, snapshot):
    pending = con.execute("SELECT 1 FROM v2_collaboration_verify_intent WHERE assignment_id=? AND state<>'recorded'",
                          (snapshot['assignment_id'],)).fetchone()
    if pending:
        _fail('verification_required', 'pending or uncertain verification blocks acceptance')
    row = _one(con, 'SELECT * FROM v2_collaboration_verification WHERE assignment_id=? ORDER BY rowid DESC LIMIT 1',
               (snapshot['assignment_id'],))
    if not row:
        _fail('verification_required', 'successful current verification required')
    receipt = json.loads(row['receipt_json'])
    if _sha(receipt) != row['receipt_sha256']:
        _fail('integrity_error', 'verification receipt changed')
    if (receipt.get('passed') is not True or receipt.get('exit_code') != 0
            or not receipt.get('pid') or receipt.get('timed_out') or receipt.get('output_limited')
            or not receipt.get('streams_complete') or receipt.get('spawn_error')
            or receipt.get('before') != snapshot or receipt.get('after') != snapshot
            or receipt.get('verifier_sha256') != _sha(plan['config']['verifier'])):
        _fail('verification_required', 'latest verification failed or belongs to different evidence')
    return row['verification_id']


def _proposal(con, task_id):
    row = _one(con, 'SELECT * FROM v2_collaboration_proposal WHERE task_id=? AND withdrawn_at IS NULL', (task_id,))
    if row and _sha(json.loads(row['manifest_json'])) != row['manifest_sha256']:
        _fail('integrity_error', 'winner manifest changed')
    return row


def _propose(con, project_id, principal, payload):
    keys = ('task_id', 'expected_revision', 'assignment_id', 'event_id', 'contributors')
    _fields(payload, keys, keys[:-1])
    _, plan = _plan(con, project_id, payload['task_id'], True)
    _expected(plan, payload['expected_revision'])
    if _proposal(con, plan['task_id']):
        _fail('state_conflict', 'withdraw the existing proposal before selecting another')
    assignment, _, snapshot = _candidate(con, project_id, plan, payload['assignment_id'], payload['event_id'])
    verification = _verification(con, plan, snapshot)
    contributors, frozen = payload.get('contributors', []), []
    if not isinstance(contributors, list) or len(contributors) > 8 or len(set(map(str, contributors))) != len(contributors):
        _fail('invalid_payload', 'contributors must be distinct event IDs')
    if plan['config']['mode'] == 'ensemble':
        if assignment['slot_id'] != 0 or len(contributors) < 2:
            _fail('invalid_payload', 'ensemble requires synthesis slot 0 and at least two contributors')
        slots = set()
        for event_id in contributors:
            _text(event_id, 'contributor event_id')
            mapped = _one(con, 'SELECT m.v2_assignment_id FROM v2_legacy_map m JOIN events e '
                          'ON e.task=m.legacy_task_id WHERE e.id=? AND m.v2_task_id=?', (event_id, plan['task_id']))
            if not mapped:
                _fail('ownership_conflict', 'contributor event belongs to another Task')
            candidate, _, part = _candidate(con, project_id, plan, mapped['v2_assignment_id'], event_id)
            if candidate['slot_id'] == 0 or candidate['slot_id'] in slots:
                _fail('invalid_payload', 'contributors must use distinct competitor slots')
            slots.add(candidate['slot_id'])
            frozen.append(part)
    elif contributors:
        _fail('invalid_payload', 'contributors apply only to ensemble')
    manifest = dict(candidate=snapshot, verification_id=verification,
                    contributors=sorted(frozen, key=lambda x: x['assignment_id']))
    pid = 'col-proposal-' + uuid.uuid4().hex
    con.execute('INSERT INTO v2_collaboration_proposal VALUES (?,?,?,?,?,?,?,?,NULL,NULL)',
                (pid, plan['task_id'], assignment['assignment_id'], payload['event_id'], _json(manifest),
                 _sha(manifest), principal['principal_id'], service._now()))
    _advance(con, plan)
    _audit(con, project_id, plan['task_id'], principal, 'proposed', dict(proposal_id=pid, manifest_sha256=_sha(manifest)))
    return dict(proposal_id=pid, revision=plan['revision'], manifest_sha256=_sha(manifest), accepted=False)


def acceptance_guard(con, project_id, principal, assignment_id, event_id):
    _principal(con, project_id, principal, ('controller',))
    assignment = _assignment(con, project_id, assignment_id)
    task, plan = _plan(con, project_id, assignment['task_id'])
    if task['accepted_evidence_id'] or task['task_state'] in TERMINAL:
        _fail('state_conflict', 'Task already has a final disposition')
    proposal = _proposal(con, task['task_id'])
    if not proposal or (proposal['assignment_id'], proposal['event_id']) != (assignment_id, event_id):
        _fail('proposal_required', 'only the uniquely selected candidate can be accepted')
    manifest = json.loads(proposal['manifest_json'])
    review, snapshot = _sealed_review(con, project_id, plan, assignment_id, event_id, 'accept')
    _candidate(con, project_id, plan, assignment_id, event_id)
    _verification(con, plan, snapshot)
    if snapshot != manifest['candidate']:
        _fail('evidence_changed', 'selected evidence changed after proposal')
    for old in manifest['contributors']:
        _, _, current = _candidate(con, project_id, plan, old['assignment_id'], old['event_id'])
        if old != current:
            _fail('evidence_changed', 'ensemble contributor changed after proposal')
    return dict(allowed=True, proposal_id=proposal['proposal_id'], task_id=task['task_id'],
                manifest_sha256=proposal['manifest_sha256'], review_sha256=review['sha256'])


def closure_projection(con, project_id, principal, assignment_id, outcome):
    _principal(con, project_id, principal, ('controller',))
    assignment = _assignment(con, project_id, assignment_id)
    task, _ = _plan(con, project_id, assignment['task_id'])
    if outcome not in ('accepted', 'repair_required', 'rejected', 'cancelled', 'failed'):
        _fail('invalid_payload', 'unsupported closure outcome')
    held = [r['assignment_id'] for r in _rows(con, 'SELECT assignment_id FROM v2_assignment '
            'WHERE task_id=? AND holds_slot=1 AND assignment_id<>?', (task['task_id'], assignment_id))]
    pending = con.execute("SELECT 1 FROM v2_collaboration_ticket WHERE task_id=? AND state='queued'", (task['task_id'],)).fetchone()
    if task['task_state'] in TERMINAL or task['accepted_evidence_id']:
        state = task['task_state']
    elif outcome == 'accepted':
        proposal = _proposal(con, task['task_id'])
        acceptance_guard(con, project_id, principal, assignment_id, proposal['event_id'] if proposal else '')
        state = 'accepted'
    elif held or pending:
        state = 'active'
    else:
        # Other completed candidates survive a loser's close. Final business
        # rejection/cancellation is a separate parent decision.
        completed = con.execute("SELECT 1 FROM v2_assignment WHERE task_id=? AND assignment_id<>? "
                                "AND state='completed' AND review_status NOT IN ('repair','rejected')",
                                (task['task_id'], assignment_id)).fetchone()
        state = 'review_ready' if completed else 'repair_required' if outcome == 'repair_required' else 'blocked'
    return dict(task_id=task['task_id'], suggested_task_state=state, other_held_assignments=held,
                release_assignments=[], release_reservations=[], projection_only=True)


def apply_command(con, project_id, principal, tool, payload):
    _tx(con)
    _principal(con, project_id, principal, ('controller',))
    name = tool.removeprefix('awo_') if isinstance(tool, str) else ''
    if name not in COMMANDS:
        _fail('unknown_command', 'unsupported collaboration command')
    if name == 'collaboration_configure':
        return _configure(con, project_id, principal, payload)
    if name == 'collaboration_verify':
        return _verify(con, project_id, principal, payload)
    if name == 'collaboration_propose':
        return _propose(con, project_id, principal, payload)
    if name == 'collaboration_withdraw':
        _fields(payload, ('task_id', 'expected_revision', 'reason'), ('task_id', 'expected_revision', 'reason'))
        _, plan = _plan(con, project_id, payload['task_id'], True)
        _expected(plan, payload['expected_revision'])
        reason = _text(payload['reason'], 'reason', 2048)
        row = _proposal(con, plan['task_id'])
        if not row:
            _fail('state_conflict', 'no current proposal to withdraw')
        con.execute('UPDATE v2_collaboration_proposal SET withdrawn_at=?,withdrawn_reason=? WHERE proposal_id=?',
                    (service._now(), reason, row['proposal_id']))
        action, detail = 'withdrawn', dict(proposal_id=row['proposal_id'], reason=reason)
    else:
        _fields(payload, ('ticket_id', 'expected_revision', 'reason'), ('ticket_id', 'expected_revision', 'reason'))
        ticket, _ = _ticket(con, project_id, payload['ticket_id'])
        _, plan = _plan(con, project_id, ticket['task_id'], True)
        _expected(plan, payload['expected_revision'])
        reason = _text(payload['reason'], 'reason', 2048)
        if ticket['state'] != 'queued':
            _fail('state_conflict', 'cannot cancel an admitted or cancelled ticket')
        con.execute("UPDATE v2_collaboration_ticket SET state='cancelled' WHERE ticket_id=?", (ticket['ticket_id'],))
        action, detail = 'ticket_cancelled', dict(ticket_id=ticket['ticket_id'], reason=reason)
    _advance(con, plan)
    _audit(con, project_id, plan['task_id'], principal, action, detail)
    return dict(detail, revision=plan['revision'])


def query(con, project_id, principal, query, payload):
    _principal(con, project_id, principal)
    name = query.removeprefix('awo_') if isinstance(query, str) else ''
    if name not in QUERIES:
        _fail('unknown_query', 'unsupported collaboration query')
    allowed = ('task_id', 'after_seq', 'limit') if name == 'collaboration_history' else ('task_id',)
    _fields(payload, allowed, ('task_id',))
    task, plan = _plan(con, project_id, payload['task_id'])
    if principal['role'] == 'worker' and principal['agent_id'] not in {s['agent_id'] for s in plan['config']['slots']}:
        _fail('ownership_conflict', 'worker is not approved for this Task')
    if name == 'collaboration_history':
        after = _integer(payload.get('after_seq', 0), 'after_seq', 0)
        limit = _integer(payload.get('limit', 100), 'limit', 1, 100)
        rows = _rows(con, 'SELECT * FROM v2_collaboration_audit WHERE project_id=? AND task_id=? AND seq>? '
                     'ORDER BY seq LIMIT ?', (project_id, task['task_id'], after, limit))
        return dict(events=rows, next_after_seq=rows[-1]['seq'] if rows else after)
    tickets = _rows(con, 'SELECT ticket_id,slot_id,generation,agent_id,attempt_id,state,assignment_id,parent_event_id '
                    'FROM v2_collaboration_ticket WHERE task_id=? ORDER BY generation', (task['task_id'],))
    verifications = _rows(con, 'SELECT i.verification_id,i.assignment_id,i.event_id,i.state,i.attempted_at,'
                          'i.created_at,v.receipt_sha256 FROM v2_collaboration_verify_intent i '
                          'LEFT JOIN v2_collaboration_verification v ON v.verification_id=i.verification_id '
                          'WHERE i.task_id=? ORDER BY i.rowid', (task['task_id'],))
    return dict(task=task, revision=plan['revision'], config=plan['config'], tickets=tickets,
                verifications=verifications, proposal=_proposal(con, task['task_id']), projection_only=True)
