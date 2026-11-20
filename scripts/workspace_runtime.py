"""Controller workspace operations around durable local Git/filesystem effects."""
import json
from pathlib import Path
import uuid

import awo_service as service
import priority_scheduler as scheduler
import workspace_manager as workspace


def create_runtime_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS v2_workspace_review (
      assignment_id TEXT PRIMARY KEY REFERENCES v2_assignment(assignment_id),
      workspace_id TEXT NOT NULL REFERENCES v2_workspace(workspace_id),
      inventory_json TEXT NOT NULL, inventory_sha256 TEXT NOT NULL,
      verdict TEXT NOT NULL, reviewed_at TEXT NOT NULL)""")


def _open(state, actor, version, context, epoch=None):
    con = service._registry_open(state)
    try:
        service._controller(con, actor, version, context)
        current = service._state_row(con)
        if not current['v2_enabled'] or current['frozen']:
            raise service.ServiceError('workspace writes require an enabled unfrozen service')
        if epoch is not None and epoch != current['controller_epoch']:
            raise service.ServiceError('stale controller epoch')
        return con, current['project_id']
    except BaseException:
        con.close()
        raise


def public(ws):
    fields = ('workspace_id', 'assignment_id', 'task_id', 'host', 'kind', 'path',
              'status', 'revision', 'base_commit', 'branch', 'repository_id', 'error')
    return {key: ws.get(key) for key in fields}


def _assignment(con, project, identity, expected):
    assignment, intent, contract = scheduler._intent(con, project, identity)
    if type(expected) is not int or assignment['revision'] != expected:
        raise service.ServiceError('stale Assignment revision')
    return assignment, intent, contract


def create(state, actor, version, context, envelope):
    env = service._command_envelope(envelope, ('workspace.create',))
    p = env['payload']
    allowed = {'assignment_id', 'expected_revision', 'kind', 'root', 'path', 'allowed_paths',
               'repository_id', 'repo_root', 'base_commit', 'branch'}
    required = {'assignment_id', 'expected_revision', 'kind', 'root', 'path', 'allowed_paths'}
    if set(p) - allowed or required - set(p):
        raise service.ServiceError('workspace.create fields are incomplete or unknown')
    principal, sha = 'controller:' + actor, service._request_sha(env)
    con, project = _open(state, actor, version, context, env['expected_epoch'])
    try:
        old = service._replay(con, project, principal, env, sha)
        if old:
            return old
        assignment, intent, contract = _assignment(con, project, p['assignment_id'], p['expected_revision'])
        if (intent['send_state'] != 'ready' or not assignment['holds_slot']
                or assignment['state'] != 'reserved' or assignment['workspace_id']):
            raise service.ServiceError('workspace creation requires an unsent reserved Assignment')
        if (actor, version, env['expected_epoch']) != (
                intent['controller_id'], intent['controller_version'], intent['controller_epoch']):
            raise service.ServiceError('workspace operation differs from the frozen Controller approval')
        if p['kind'] == 'git_worktree' and contract['task_kind'] != 'code':
            raise service.ServiceError('a Git workspace requires a Controller-approved code Task')
        mode = contract.get('workspace_mode', 'directory')
        if mode != 'git_worktree' and p['kind'] == 'git_worktree':
            # A directory-mode Task froze an existing launch directory. Binding a
            # fresh Git worktree elsewhere would leave the send gate unable to
            # compare the worker's frozen launch cwd with the provisioned source.
            raise service.ServiceError('a directory-mode Task cannot bind a Git source workspace')
        if mode == 'git_worktree':
            if p['kind'] != 'git_worktree':
                raise service.ServiceError('the frozen Task requires a Git source workspace')
            planned = contract.get('workspace_path')
            if not planned or Path(p['path']).resolve() != Path(planned).resolve():
                raise service.ServiceError('workspace path differs from the frozen worker launch directory')
            given = p.get('base_commit')
            # Git object names are case-insensitive hex; _enqueue freezes the
            # lowercase spelling that the send gate compares against.
            if not isinstance(given, str) or given.lower() != contract.get('workspace_base_commit'):
                raise service.ServiceError('workspace base differs from the frozen materialized base commit')
        con.rollback()
    finally:
        con.close()
    # External read-only preflight is outside the write transaction. The entire
    # specification and path identities are rechecked when reserving below.
    options = {key: p[key] for key in ('kind', 'root', 'path', 'allowed_paths',
               'repository_id', 'repo_root', 'base_commit', 'branch') if key in p}
    spec = workspace.prepare_workspace(task_id=assignment['task_id'],
        assignment_id=assignment['assignment_id'], host=contract['host'],
        harness=contract['harness'], **options)
    con, project = _open(state, actor, version, context, env['expected_epoch'])
    try:
        old = service._replay(con, project, principal, env, sha)
        if old:
            return old
        current, intent, contract = _assignment(con, project, p['assignment_id'], p['expected_revision'])
        if intent['send_state'] != 'ready' or current['workspace_id'] or current['state'] != 'reserved':
            raise service.ServiceError('Assignment changed during workspace preflight')
        workspace.reserve_workspace(con, spec)
        ws = workspace.begin_provision(con, spec['workspace_id'])
        response = {'status': 'PROVISIONING', 'workspace': public(ws), 'replayed': False,
                    'next_action': 'Inspect this workspace if execution is interrupted; do not recreate it'}
        service._record(con, project, principal, env, sha, response)
        service._event(con, project, 'workspace', ws['workspace_id'], 'workspace.create_reserved',
                       actor, 'v2', None, public(ws))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    report = workspace.provision_workspace(service._db_path(state), ws['workspace_id'], ws['operation_token'])
    con = None
    try:
        con, project = _open(state, actor, version, context, env['expected_epoch'])
        ws = workspace.finalize_workspace(con, ws['workspace_id'], report)
        response = {'status': 'WORKSPACE_' + ws['status'].upper(), 'workspace': public(ws), 'replayed': False}
        con.execute('UPDATE v2_command_log SET response_json=? WHERE project_id=? AND principal=? AND command_id=?',
                    (json.dumps(response, sort_keys=True), project, principal, env['command_id']))
        service._event(con, project, 'workspace', ws['workspace_id'], 'workspace.provisioned',
                       actor, 'v2', None, public(ws))
        con.commit()
        return response
    except Exception as exc:
        if con:
            con.rollback()
        return {'status': 'WORKSPACE_UNCERTAIN', 'workspace_id': ws['workspace_id'],
                'claims_retained': True, 'retry_allowed': False, 'reason': str(exc)}
    finally:
        if con:
            con.close()


def inspect(state, actor, version, context, workspace_id):
    con = service._connect(service._db_path(state), readonly=True)
    try:
        service._controller(con, actor, version, context)
        ws = workspace.get_workspace(con, workspace_id)
        if not ws:
            raise service.ServiceError('workspace is missing')
        scheduler.assignment_get(con, service._project_id(con), ws['assignment_id'])
    finally:
        con.close()
    report = workspace.inspect_workspace(ws)
    return {'workspace': public(ws), 'inspection': {
        key: report.get(key) for key in ('ok', 'status', 'dirty', 'head', 'error', 'inventory')},
        'writer_quiescence': 'unknown'}


def review_gate(con, assignment, verdict='accept'):
    """Seal source inventory in the SAME transaction as the independent review."""
    ws = workspace.get_workspace(con, assignment['workspace_id'])
    if not ws or ws['assignment_id'] != assignment['assignment_id'] or ws['status'] not in ('active', 'frozen'):
        raise service.ServiceError('managed workspace is not available for independent review')
    report = workspace.inspect_workspace(ws)
    if not report['ok']:
        raise service.ServiceError('workspace inspection failed: ' + str(report.get('error')))
    inventory = report['inventory']
    if verdict == 'accept' and not inventory['scope_ok']:
        raise service.ServiceError('workspace scope violations require repair: ' +
                                   ', '.join(inventory['undeclared_paths'] + inventory['problems']))
    encoded, digest = workspace._json(inventory), workspace._digest(inventory)
    con.execute('INSERT INTO v2_workspace_review VALUES (?,?,?,?,?,?)',
                (assignment['assignment_id'], ws['workspace_id'], encoded, digest, verdict, service._now()))
    service._event(con, service._project_id(con), 'workspace', ws['workspace_id'],
                   'workspace.review_inventory', 'independent-review-gate', 'v2', None,
                   {'assignment_id': assignment['assignment_id'], 'inventory_sha256': digest,
                    'verdict': verdict})
    return {'inventory_sha256': digest, 'scope_ok': inventory['scope_ok']}


def verify_reviewed_workspace(con, assignment_id):
    assignment = con.execute('SELECT * FROM v2_assignment WHERE assignment_id=?', (assignment_id,)).fetchone()
    if not assignment or not assignment['workspace_id']:
        return None
    sealed = con.execute('SELECT * FROM v2_workspace_review WHERE assignment_id=?', (assignment_id,)).fetchone()
    if not sealed or sealed['verdict'] != 'accept':
        raise service.ServiceError('managed workspace has no independent accepted inventory')
    inventory = json.loads(sealed['inventory_json'])
    if workspace._digest(inventory) != sealed['inventory_sha256']:
        raise service.ServiceError('workspace review inventory hash changed')
    ws = workspace.get_workspace(con, sealed['workspace_id'])
    if not ws or ws['assignment_id'] != assignment_id:
        raise service.ServiceError('workspace identity changed')
    current = workspace.collect_inventory(ws)
    if current != inventory or not current['scope_ok']:
        raise service.ServiceError('EVIDENCE_CHANGED: workspace changed after independent review')
    return inventory


def _closed_quiescence(con, ws, actor, evidence_root):
    """Reobserve the exact closed worker; missing heartbeat is never evidence."""
    import monitor_task as monitor
    import workflow_scheduler as legacy
    assignment, intent, contract = scheduler._intent(con, service._project_id(con), ws['assignment_id'])
    if assignment['holds_slot'] or intent['send_state'] != 'closed' or not intent['closure_json']:
        raise service.ServiceError('workspace cleanup needs Controller-closed execution')
    closure = json.loads(intent['closure_json'])
    expected = closure.get('observation')
    observed = monitor.Transport(contract['request']).status(contract['request']['worker'])
    if (not expected or not monitor.identity_matches(observed.get('identity', {}),
            contract['request']['worker']['expected_identity'])
            or not legacy._worker_quiescent(contract['request'], observed)
            or monitor.sequence(observed.get('completion_seq')) != monitor.sequence(expected.get('completion_seq'))):
        raise service.ServiceError('worker quiescence/sequence cannot be revalidated')
    root = Path(workspace._absolute(evidence_root))
    if workspace.paths_overlap(root, ws['path']):
        raise service.ServiceError('quiescence evidence must be outside the workspace')
    root.mkdir(parents=True, exist_ok=True)
    path = root / ('quiescence-' + uuid.uuid4().hex + '.json')
    workspace._exclusive_json(path, {'assignment_id': ws['assignment_id'], 'workspace_id': ws['workspace_id'],
                                    'observation': observed, 'closure': closure, 'actor': actor,
                                    'checked_at': service._now()})
    return {'verified': True, 'workspace_id': ws['workspace_id'], 'assignment_id': ws['assignment_id'],
            'host': ws['host'], 'revision': ws['revision'], 'controller': actor,
            'reason': 'closed Assignment and exact live idle identity/sequence',
            'evidence_path': str(path), 'evidence_sha256': service._file_sha(path)}


def freeze(state, actor, version, context, envelope):
    """Keep an accepted workspace for materialization after fresh quiescence."""
    env = service._command_envelope(envelope, ('workspace.freeze',))
    p = env['payload']
    if set(p) != {'workspace_id', 'expected_revision', 'evidence_root'}:
        raise service.ServiceError('freeze requires workspace_id, expected_revision and evidence_root')
    con, project = _open(state, actor, version, context, env['expected_epoch'])
    try:
        principal, sha = 'controller:' + actor, service._request_sha(env)
        replay = service._replay(con, project, principal, env, sha)
        if replay:
            return replay
        ws = workspace.get_workspace(con, p['workspace_id'])
        if not ws or type(p['expected_revision']) is not int or ws['revision'] != p['expected_revision']:
            raise service.ServiceError('workspace missing or stale revision')
        assignment = scheduler.assignment_get(con, project, ws['assignment_id'])
        if assignment['review_status'] != 'accepted' or assignment['holds_slot']:
            raise service.ServiceError('freeze requires a closed independently accepted Assignment')
        verify_reviewed_workspace(con, assignment['assignment_id'])
        quiet = _closed_quiescence(con, ws, actor, p['evidence_root'])
        report = workspace.inspect_workspace(ws)
        frozen = workspace.recover_workspace(con, ws['workspace_id'], report, quiescence=quiet)
        if frozen['status'] != 'frozen':
            raise service.ServiceError('workspace could not be frozen')
        response = {'status': 'WORKSPACE_FROZEN', 'workspace': public(frozen), 'replayed': False}
        service._record(con, project, principal, env, sha, response)
        service._event(con, project, 'workspace', ws['workspace_id'], 'workspace.frozen',
                       actor, 'v2', None, {'assignment_id': assignment['assignment_id'],
                                         'quiescence_sha256': quiet['evidence_sha256']})
        con.commit()
        return response
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def recover(state, actor, version, context, envelope):
    env = service._command_envelope(envelope, ('workspace.recover',))
    p = env['payload']
    if set(p) != {'workspace_id', 'expected_revision'}:
        raise service.ServiceError('recover requires workspace_id and expected_revision')
    con, project = _open(state, actor, version, context, env['expected_epoch'])
    try:
        principal, sha = 'controller:' + actor, service._request_sha(env)
        replay = service._replay(con, project, principal, env, sha)
        if replay:
            return replay
        ws = workspace.get_workspace(con, p['workspace_id'])
        if not ws or type(p['expected_revision']) is not int or ws['revision'] != p['expected_revision']:
            raise service.ServiceError('workspace missing or stale revision')
        assignment = scheduler.assignment_get(con, project, ws['assignment_id'])
        report = workspace.inspect_workspace(ws)
        result = workspace.recover_workspace(con, ws['workspace_id'], report)
        if result['status'] in ('workspace_lost', 'quarantined', 'orphaned') and assignment['holds_slot']:
            con.execute("UPDATE v2_assignment SET state='blocked',revision=revision+1,updated_at=? WHERE assignment_id=?",
                        (service._now(), assignment['assignment_id']))
        response = {'status': 'WORKSPACE_' + result['status'].upper(), 'workspace': public(result),
                    'claims_retained': True, 'replayed': False}
        service._record(con, project, principal, env, sha, response)
        con.commit()
        return response
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def release(state, actor, version, context, envelope):
    env = service._command_envelope(envelope, ('workspace.release',))
    p = env['payload']
    if set(p) != {'workspace_id', 'expected_revision', 'evidence_root'}:
        raise service.ServiceError('release requires workspace_id, expected_revision and evidence_root')
    principal, sha = 'controller:' + actor, service._request_sha(env)
    con, project = _open(state, actor, version, context, env['expected_epoch'])
    try:
        replay = service._replay(con, project, principal, env, sha)
        if replay:
            return replay
        ws = workspace.get_workspace(con, p['workspace_id'])
        if not ws or type(p['expected_revision']) is not int or ws['revision'] != p['expected_revision']:
            raise service.ServiceError('workspace missing or stale revision')
        scheduler.assignment_get(con, project, ws['assignment_id'])
        quiescence = _closed_quiescence(con, ws, actor, p['evidence_root'])
        report = workspace.inspect_workspace(ws)
        snapshot = workspace.snapshot_workspace(ws, p['evidence_root'])
        ws = workspace.begin_release(con, ws['workspace_id'], report, snapshot, quiescence=quiescence)
        response = {'status': 'RELEASE_PENDING', 'workspace': public(ws), 'replayed': False}
        service._record(con, project, principal, env, sha, response)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    report = workspace.release_workspace(service._db_path(state), ws['workspace_id'], ws['operation_token'])
    con, project = _open(state, actor, version, context, env['expected_epoch'])
    try:
        ws = workspace.finalize_release(con, ws['workspace_id'], report)
        response = {'status': 'WORKSPACE_' + ws['status'].upper(), 'workspace': public(ws), 'replayed': False}
        con.execute('UPDATE v2_command_log SET response_json=? WHERE project_id=? AND principal=? AND command_id=?',
                    (json.dumps(response, sort_keys=True), project, principal, env['command_id']))
        con.commit()
        return response
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
