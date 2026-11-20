'''Read-only project views. Caller owns authorization and the read transaction.'''
from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import math

VIEW_NAMES = ('project', 'milestones', 'task_board', 'registry', 'assignments',
              'queue', 'workspaces', 'transcript', 'evidence', 'review', 'budget',
              'resources', 'recovery')
BUCKETS = ('Coming In', 'Going Out', 'Needs You', 'Blocked', 'Completed')
TITLES = ('Project overview', 'Milestones and dependency graph', 'Task board',
          'Agent registry', 'Assignments and slots', 'Queue and admission',
          'Workspaces and integration', 'Run transcript and inspector',
          'Evidence and recorded hashes', 'Independent review', 'Budget and usage',
          'Resource reservations', 'Failures and recovery')
COMPLETED = frozenset(('accepted', 'landed', 'finalized'))
MAX_JSON_BYTES = 1048576


class ViewError(ValueError):
    '''The projection cannot safely describe this input.'''


# Explicit columns keep credentials and runtime config out of projections.
TABLES = {
    'v2_project': ('project_id created_at', 'project'),
    'v2_service_state': ('project_id v2_enabled frozen controller_epoch updated_at', 'project'),
    'v2_task': ('task_id project_id title priority task_state execution_mode fanout_slots '
                'accepted_evidence_id revision created_at updated_at', 'project'),
    'v2_agent': ('agent_id project_id handle role harness harness_version host os lifecycle '
                 'last_seen_at revision', 'project'),
    'v2_agent_capability': ('agent_id capability_key level checked_at evidence_sha256', 'agent'),
    'v2_assignment': ('assignment_id task_id agent_id generation slot_id holds_slot state '
                      'review_status workspace_id revision created_at updated_at', 'task'),
    'v2_scheduler_queue': ('task_id project_id enqueued_at due_at contract_json', 'project'),
    'v2_priority_override': ('task_id previous_priority expires_at', 'task'),
    'v2_scheduler_intent': ('assignment_id run_id send_state attempted_at submitted_at contract_json', 'assignment'),
    'v2_run_link': ('run_id assignment_id generation created_at', 'assignment'),
    'v2_workspace': ('workspace_id task_id assignment_id host kind status revision spec_sha256 '
                     'ready_at released_at snapshot_json', 'task'),
    'v2_evidence': ('evidence_id task_id assignment_id kind sealed_sha256 current_sha256 bytes '
                    'sealed state created_at', 'task'),
    'v2_review': ('review_id task_id assignment_id event_id verdict note_sha256 state reviewed_at', 'task'),
    'v2_budget': ('budget_id project_id scope_type scope_id limits_json state version', 'project'),
    'v2_budget_reservation': ('reservation_id budget_id assignment_id run_id dimension amount '
                              'settled_amount state', 'budget'),
    'v2_usage_close': ('run_id assignment_id generation state usage_json revision closed_at', 'assignment'),
    'v2_event': ('event_id project_id entity_type entity_id event_type global_seq at payload_json', 'project'),
    'v2_message': ('message_id project_id kind thread_id reply_to task_id assignment_id created_at', 'project'),
    'v2_message_delivery': ('delivery_id project_id message_id state', 'project'),
}


def _rows(con, sql, args=()):
    cur = con.execute(sql, args)
    names = [item[0] for item in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _json(value):
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_JSON_BYTES:
        raise ViewError('Recorded JSON exceeds the limit or is missing.')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate recorded JSON field')
            result[key] = item
        return result
    try:
        return json.loads(value, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError) as exc:
        raise ViewError('Recorded JSON is invalid; inspection is required.') from exc


def _object(value):
    result = _json(value)
    if not isinstance(result, dict):
        raise ViewError('A recorded object has an unexpected shape.')
    return result


class _Reader:
    def __init__(self, con, project_id, limit):
        if not con.in_transaction:
            raise ViewError('Caller must begin a consistent read transaction.')
        if not isinstance(project_id, str) or not project_id or len(project_id) > 256:
            raise ViewError('A bounded project ID is required.')
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ViewError('Row limit must be an integer from 1 to 10000.')
        self.con, self.project, self.limit = con, project_id, limit
        self.tables = {row[0] for row in con.execute(
            'SELECT name FROM sqlite_master WHERE type=?', ('table',))}
        self.available, self.truncated, self.data = {}, {}, {}

    def read(self, table):
        if table in self.data:
            return self.data[table]
        columns, scope = TABLES[table]
        needed = {'task': ('v2_task',), 'agent': ('v2_agent',),
                  'assignment': ('v2_assignment', 'v2_task'), 'budget': ('v2_budget',)}.get(scope, ())
        exists = table in self.tables and all(name in self.tables for name in needed)
        for dependency in needed:
            required = {'v2_task': {'task_id', 'project_id'},
                        'v2_agent': {'agent_id', 'project_id'},
                        'v2_assignment': {'assignment_id', 'task_id'},
                        'v2_budget': {'budget_id', 'project_id'}}[dependency]
            fields = {r[0] for r in self.con.execute(
                'SELECT name FROM pragma_table_info(?)', (dependency,))}
            exists = exists and required <= fields
        self.available[table], self.truncated[table] = exists, False
        if not exists:
            self.data[table] = []
            return []
        actual = {row[0] for row in self.con.execute('SELECT name FROM pragma_table_info(?)', (table,))}
        if not set(columns.split()) <= actual:
            self.available[table] = False
            self.data[table] = []
            return []
        where = {
            'project': 'x.project_id=?',
            'task': 'EXISTS (SELECT 1 FROM v2_task t WHERE t.task_id=x.task_id AND t.project_id=?)',
            'agent': 'EXISTS (SELECT 1 FROM v2_agent a WHERE a.agent_id=x.agent_id AND a.project_id=?)',
            'assignment': 'EXISTS (SELECT 1 FROM v2_assignment a JOIN v2_task t ON t.task_id=a.task_id '
                          'WHERE a.assignment_id=x.assignment_id AND t.project_id=?)',
            'budget': 'EXISTS (SELECT 1 FROM v2_budget b WHERE b.budget_id=x.budget_id AND b.project_id=?)',
        }[scope]
        order = 'x.global_seq DESC' if table == 'v2_event' else 'x.' + columns.split()[0]
        if table == 'v2_agent_capability':
            order += ',x.capability_key'
        rows = _rows(self.con, 'SELECT ' + ','.join('x.' + c for c in columns.split()) +
                     ' FROM ' + table + ' x WHERE ' + where + ' ORDER BY ' + order + ' LIMIT ?',
                     (self.project, self.limit + 1))
        self.truncated[table] = len(rows) > self.limit
        self.data[table] = rows[:self.limit]
        return self.data[table]


_EXPLANATIONS = {
    'draft': ('Awaiting approval', 'Work is not approved for execution.', 'Ask the controller to review the request.'),
    'ready': ('Ready for scheduling', 'Final admission still applies.', 'Check queue admission before dispatch.'),
    'queued': ('Waiting for admission', 'Dependencies, capacity and authorization need a fresh check.', 'Review the queue details.'),
    'active': ('Work in progress', 'Execution or a workspace is reserved for ongoing work.', 'Inspect the latest recorded progress.'),
    'running': ('Work in progress', 'The last observation reports execution.', 'Inspect the transcript and observation time.'),
    'reserved': ('Reserved for execution', 'Submission is not yet confirmed.', 'Check the recorded send intent.'),
    'completed': ('Delivered; review pending', 'Worker delivery is not independent acceptance.', 'Arrange an independent evidence review.'),
    'review_ready': ('Delivered; ready for review', 'Delivery awaits independent review.', 'Ask the independent reviewer to inspect the evidence.'),
    'accepted': ('Accepted; not yet landed', 'Review passed; target integration is not established.', 'Check integration or non-code finalization.'),
    'landed': ('Landed', 'The task records target integration.', 'Inspect the retained integration evidence.'),
    'finalized': ('Finalized non-code delivery', 'The accepted artifact is finalized; no code merge is required.', 'Inspect the archived evidence.'),
    'blocked': ('Waiting on a prerequisite', 'A dependency, environment or resource gate needs attention.', 'Inspect the blocker before retrying.'),
    'repair_required': ('Changes requested', 'A repair cycle is needed.', 'Read the review and arrange an authorized repair.'),
    'rejected': ('Review rejected', 'The work was not accepted.', 'Read the review decision before proposing new work.'),
    'uncertain': ('Execution outcome unknown', 'Submission or worker outcome is not reconciled.', 'Inspect external results; do not automatically resend.'),
    'attempted': ('Submission attempted; outcome unconfirmed', 'Durable send intent exists without confirmed delivery.', 'Reconcile the original attempt before any resend.'),
    'historical_registered': ('Historical execution needs inspection', 'Registration does not establish execution or release.', 'Ask the controller to inspect the historical record.'),
    'no_monitor': ('No recent monitoring observation', 'There is no verified monitoring update.', 'Ask the controller to reconnect monitoring and inspect the worker.'),
    'workspace_lost': ('Workspace is missing', 'The recorded workspace is unavailable.', 'Preserve claims and investigate the missing workspace.'),
    'orphaned': ('Workspace needs reconciliation', 'Ownership does not prove an earlier writer stopped.', 'Verify writer quiescence before recovery.'),
    'quarantined': ('Workspace isolated for inspection', 'Creation or ownership was not verified safely.', 'Inspect ownership and content before further action.'),
    'failed': ('An operation failed', 'Failure does not establish writer termination.', 'Read retained evidence and reconcile resources.'),
    'unknown_blocked': ('Cost needs reconciliation', 'Missing usage prevents a reliable spending decision.', 'Reconcile usage or request a budget decision.'),
    'approval_required': ('Approval required', 'A human decision is needed.', 'Ask the authorized decision maker to review the gate.'),
    'migration_uncertain': ('Historical data needs review', 'History could not be mapped confidently.', 'Inspect migration evidence.'),
    'sealed': ('Evidence sealed', 'A historical hash is recorded; source bytes were not checked here.', 'Use the evidence inspector for verification.'),
    'changed': ('Evidence changed', 'Recorded content differs from its sealed hash.', 'Inspect the original sealed evidence before review.'),
    'missing': ('Evidence unavailable', 'Required evidence is not available in the recorded observation.', 'Locate and verify the original evidence.'),
    'unknown': ('Observation unknown', 'No reliable value was recorded.', 'Inspect the original evidence or usage source.'),
    'ok': ('Budget within recorded policy', 'This is a recorded policy state, not a fresh spend decision.', 'Inspect actual, estimated and unknown usage.'),
    'warning': ('Budget warning', 'Recorded usage is approaching a policy threshold.', 'Inspect remaining headroom and open reservations.'),
    'exceeded': ('Budget exceeded', 'The recorded budget gate is closed.', 'Ask the controller for a budget decision.'),
    'paused': ('Budget paused', 'The budget policy has paused work.', 'Inspect the recorded budget decision.'),
    'pause_requested': ('Budget pause requested', 'A pause is requested, but worker termination is not established.', 'Inspect worker status and retained reservations.'),
    'cancel_requested': ('Budget cancellation requested', 'Cancellation does not prove the worker has stopped.', 'Reconcile the worker and usage.'),
    'overrun_pending': ('Budget overrun needs reconciliation', 'An outstanding overrun needs a policy decision.', 'Ask the controller to inspect the budget gate.'),
    'requested': ('Workspace requested', 'Provisioning has not been confirmed.', 'Inspect the workspace reservation.'),
    'provisioning': ('Workspace creation pending', 'External creation may still need reconciliation.', 'Inspect the persisted creation operation.'),
    'frozen': ('Workspace frozen', 'The workspace is retained for inspection or release.', 'Inspect ownership, content and writer quiescence.'),
    'released': ('Workspace released', 'Release is recorded; this is not task acceptance.', 'Inspect release evidence if needed.'),
    'submitted': ('Submission recorded', 'A submission response is recorded; completion is not established.', 'Inspect monitoring and evidence.'),
    'closed': ('Record closed', 'This lifecycle is closed; task acceptance remains separate.', 'Inspect the associated task and review.'),
    'cancelled': ('Work cancelled', 'Cancellation is not successful completion.', 'Inspect retained claims and cancellation evidence.'),
    'preempted': ('Work interrupted', 'Execution was interrupted; resources may need reconciliation.', 'Inspect the interrupted assignment before retrying.'),
}


def explain_state(state, *, entity='task'):
    label, explanation, action = _EXPLANATIONS.get(state, (
        'Status needs inspection', 'The recorded ' + entity + ' status has no verified interpretation.',
        'Inspect the original record through the authorized service.'))
    return dict(label=label, explanation=explanation, suggested_action=action)


def bucket_for_task(task, *, blockers=(), needs_attention=False):
    state = task.get('task_state')
    if state in COMPLETED:
        return 'Completed'
    if needs_attention or state in ('draft', 'review_ready', 'repair_required', 'rejected'):
        return 'Needs You'
    if blockers or state not in ('ready', 'queued', 'active'):
        return 'Blocked'
    return 'Going Out' if state == 'active' else 'Coming In'


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _workspace_row(row):
    result = _friendly(row, 'status', 'workspace')
    result['snapshot'] = None
    if row.get('snapshot_json'):
        snapshot = _object(row['snapshot_json'])
        result['snapshot'] = {k: snapshot.get(k) for k in
                              ('manifest_sha256', 'inventory_sha256', 'scope_ok', 'dirty')}
        for key in ('manifest_sha256', 'inventory_sha256'):
            value = result['snapshot'][key]
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdefABCDEF' for c in value):
                result['snapshot'][key] = None
        for key in ('scope_ok', 'dirty'):
            if type(result['snapshot'][key]) is not bool:
                result['snapshot'][key] = None
    return result


def _friendly(row, state='state', entity='record'):
    result = {k: v for k, v in row.items() if not k.endswith('_json') and k != state}
    result.update(explain_state(row.get(state), entity=entity))
    if row.get(state) not in _EXPLANATIONS:
        # Keep the recorded value for diagnosis; the plain label stays primary.
        result['recorded_state'] = row.get(state)
    return result


def _contract(row):
    value = _object(row['contract_json'])
    dependencies = value.get('dependencies', [])
    if not isinstance(dependencies, list) or any(not isinstance(x, str) for x in dependencies):
        raise ViewError('Recorded dependencies have an unexpected shape.')
    resources = value.get('resources', {})
    if not isinstance(resources, dict):
        raise ViewError('Recorded resources have an unexpected shape.')
    for key in ('dag_run', 'dag_node', 'agent_id'):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise ViewError('Recorded contract identifiers have an unexpected shape.')
    capabilities = value.get('required_capabilities', [])
    if not isinstance(capabilities, list) or any(not isinstance(x, str) for x in capabilities):
        raise ViewError('Recorded capabilities have an unexpected shape.')
    return {'dependencies': dependencies, 'dag_run': value.get('dag_run'),
            'dag_node': value.get('dag_node'), 'agent_id': value.get('agent_id'),
            'required_capabilities': capabilities, 'resources': {
                k: _number(v) for k, v in resources.items()
                if isinstance(k, str) and len(k) <= 128}}


def _legacy_dag_rows(reader, tasks):
    '''Project only nodes linked to known project tasks; never return requests.'''
    name = 'dag_runs'
    fields = {r[0] for r in reader.con.execute('SELECT name FROM pragma_table_info(?)', (name,))}
    reader.available[name] = name in reader.tables and {'id', 'data'} <= fields
    reader.truncated[name] = False
    if not reader.available[name]:
        return [], 'Legacy dag_runs(id,data) is unavailable.'
    raw = _rows(reader.con, 'SELECT id,data FROM dag_runs ORDER BY id LIMIT ?', (reader.limit + 1,))
    reader.truncated[name] = len(raw) > reader.limit
    result, malformed = [], False
    for run in raw[:reader.limit]:
        try:
            value = _object(run['data'])
            graph = value.get('graph')
            nodes = graph.get('nodes') if isinstance(graph, dict) else None
            if not isinstance(nodes, list) or len(nodes) > 10000:
                raise ViewError('Malformed graph')
            scoped = {}
            for node in nodes:
                if not isinstance(node, dict) or not isinstance(node.get('id'), str):
                    raise ViewError('Malformed node')
                request = node.get('request')
                task_id = request.get('task_id') if isinstance(request, dict) else None
                if isinstance(task_id, str) and task_id in tasks:
                    if node['id'] in scoped:
                        raise ViewError('Duplicate graph node')
                    scoped[node['id']] = (node, task_id)
            pending_rows = []
            for node_id, (node, task_id) in scoped.items():
                needs = node.get('needs', [])
                if not isinstance(needs, list) or any(not isinstance(x, str) for x in needs):
                    raise ViewError('Malformed graph dependency')
                pending_rows.append({'task_id': task_id, 'milestone_run': run['id'], 'node': node_id,
                                     'dependencies': [scoped[n][1] for n in needs if n in scoped],
                                     'unresolved_dependency_count': sum(n not in scoped for n in needs),
                                     'label': 'Persisted DAG node declaration',
                                     'source': 'Legacy DAG; project-linked nodes only',
                                     'task_observation': explain_state(tasks[task_id]['task_state'])['label']})
            result.extend(pending_rows)
        except ViewError:
            malformed = True
    if len(result) > reader.limit:
        reader.truncated[name] = True
    if malformed:
        reader.available[name] = False
    return result[:reader.limit], ('Some persisted DAG data is malformed; those runs were omitted.' if malformed
                                  else 'Legacy DAG scan is bounded; only nodes linked to visible project tasks are shown.')


def _budget_rows(reader):
    assignments = reader.read('v2_assignment')
    closes = {r['assignment_id']: r for r in reader.read('v2_usage_close')}
    reservations = reader.read('v2_budget_reservation')
    links = {r['assignment_id']: r['run_id'] for r in reader.read('v2_run_link')}
    result = []
    for budget in reader.read('v2_budget'):
        limits = _object(budget['limits_json'])
        members = [a for a in assignments if budget['scope_type'] == 'workflow'
                   or (budget['scope_type'] == 'task' and a['task_id'] == budget['scope_id'])
                   or (budget['scope_type'] == 'run'
                       and links.get(a['assignment_id']) == budget['scope_id'])]
        held = [r for r in reservations if r['budget_id'] == budget['budget_id']]
        usage = {a['assignment_id']: _object(closes[a['assignment_id']]['usage_json'])
                 if a['assignment_id'] in closes else {} for a in members}
        dimensions = set(limits) | {r['dimension'] for r in held}
        for item in usage.values():
            dimensions.update(item)
        if not dimensions:
            dimensions.add('cost_usd')
        row = _friendly(budget, entity='budget')
        row['dimensions'] = {}
        for dimension in sorted(dimensions):
            actual, estimated, unknown = [], [], 0
            for item in usage.values():
                metric = item.get(dimension, {})
                value = _number(metric.get('value')) if isinstance(metric, dict) else None
                state = metric.get('state') if isinstance(metric, dict) else None
                if state == 'actual' and value is not None:
                    actual.append(value)
                elif state == 'estimated' and value is not None:
                    estimated.append(value)
                else:
                    unknown += 1
            matching = [r for r in held if r['dimension'] == dimension and r['state'] == 'held']
            amounts = [_number(r['amount']) for r in matching]
            incomplete = any(not reader.available.get(t) or reader.truncated.get(t) for t in
                             ('v2_assignment', 'v2_usage_close', 'v2_budget_reservation', 'v2_run_link'))
            row['dimensions'][dimension] = {
                'limit': _number(limits.get(dimension)),
                'known_actual': _number(sum(actual)) if actual else None,
                'known_estimated': _number(sum(estimated)) if estimated else None,
                'unknown_assignments': unknown,
                'total_actual': _number(sum(actual)) if members and not unknown and not estimated
                                and not incomplete else None,
                'held_reservation': _number(sum(amounts)) if amounts and None not in amounts else None,
                'coverage': 'incomplete' if incomplete else 'recorded assignments only',
            }
        result.append(row)
    if not result:
        result.append({'label': 'Budget information is not recorded',
                       'cost': None, 'explanation': 'Unknown cost is not zero spending.'})
    return result


def build_runtime_views(con, project_id, *, limit=200, now=None):
    '''Build bounded, project-scoped observations inside the caller's transaction.

    This function neither grants admission nor probes live workers or files.
    '''
    reader = _Reader(con, project_id, limit)
    data = {name: reader.read(name) for name in TABLES}
    if not data['v2_project']:
        raise ViewError('Project is missing or its schema is unavailable.')
    tasks = {r['task_id']: r for r in data['v2_task']}
    agents = {r['agent_id']: r for r in data['v2_agent']}
    assignments = data['v2_assignment']
    intents = {r['assignment_id']: r for r in data['v2_scheduler_intent']}
    workspaces = {r['workspace_id']: r for r in data['v2_workspace']}
    queues = {r['task_id']: r for r in data['v2_scheduler_queue']}
    contracts = {k: _contract(v) for k, v in queues.items()}
    state = data['v2_service_state'][0] if data['v2_service_state'] else None
    views, buckets = {}, {name: [] for name in BUCKETS}

    def section(name, rows, sources, explanation, **extras):
        available = all(reader.available.get(t, False) for t in sources)
        views[name] = {'title': TITLES[VIEW_NAMES.index(name)], 'available': available,
                       'rows': rows, 'truncated': any(reader.truncated.get(t) for t in sources),
                       'explanation': explanation if available else
                       'Some required source tables are unavailable. ' + explanation,
                       **extras}

    # Questions remain human attention until an answer or delivery outcome says otherwise.
    answered = {m['reply_to'] for m in data['v2_message'] if m['kind'] == 'answer'}
    pending = [m for m in data['v2_message'] if m['kind'] in ('question', 'state_request')
               and m['message_id'] not in answered
               and not any(d['message_id'] == m['message_id'] and d['state'] in ('answered', 'expired')
                           for d in data['v2_message_delivery'])]
    attention = {m['task_id'] for m in pending if m['task_id']}
    board = []
    for task in tasks.values():
        task_id = task['task_id']
        related = [a for a in assignments if a['task_id'] == task_id]
        blockers = []
        if not state:
            blockers.append('Service availability is not recorded.')
        elif state['frozen'] or not state['v2_enabled']:
            blockers.append('Scheduling is paused or disabled.')
        if task['task_state'] == 'queued' and task_id not in queues:
            blockers.append('The queue entry is unavailable.')
        for dependency in contracts.get(task_id, {}).get('dependencies', []):
            if dependency not in tasks or tasks[dependency]['task_state'] not in COMPLETED:
                blockers.append('A dependency is unresolved or outside this snapshot: ' + dependency)
        for assignment in related:
            intent = intents.get(assignment['assignment_id'])
            if assignment['state'] in ('uncertain', 'blocked', 'historical_registered'):
                blockers.append(explain_state(assignment['state'])['explanation'])
            if intent and intent['send_state'] in ('attempted', 'uncertain'):
                blockers.append(explain_state(intent['send_state'])['explanation'])
            if assignment['holds_slot'] and task['task_state'] in ('ready', 'queued'):
                blockers.append('An earlier assignment still holds an execution slot.')
            if assignment['workspace_id']:
                workspace = workspaces.get(assignment['workspace_id'])
                if not workspace or workspace['status'] not in ('ready', 'active', 'frozen', 'released'):
                    blockers.append('Workspace readiness needs inspection.')
        if task['task_state'] in ('ready', 'queued') and any(
                not reader.available[t] or reader.truncated[t] for t in
                ('v2_assignment', 'v2_scheduler_queue', 'v2_scheduler_intent', 'v2_workspace')):
            blockers.append('Admission-related observations are incomplete.')
        run_ids = {intents[a['assignment_id']]['run_id'] for a in related
                   if a['assignment_id'] in intents}
        applicable_budgets = [b for b in data['v2_budget'] if b['scope_type'] == 'workflow'
                              or b['scope_type'] == 'task' and b['scope_id'] == task_id
                              or b['scope_type'] == 'run' and b['scope_id'] in run_ids]
        human_gate = any(b['state'] == 'approval_required' for b in applicable_budgets)
        for budget in applicable_budgets:
            if budget['state'] in ('unknown_blocked', 'paused', 'pause_requested', 'cancel_requested',
                                    'overrun_pending', 'exceeded'):
                blockers.append(explain_state(budget['state'])['explanation'])
        contract = contracts.get(task_id, {})
        if contract.get('agent_id'):
            agent = agents.get(contract['agent_id'])
            if not agent or agent['lifecycle'] != 'active':
                blockers.append('The assigned agent is unavailable or not approved.')
            for capability in contract.get('required_capabilities', []):
                if not any(c['agent_id'] == contract['agent_id'] and c['capability_key'] == capability
                           and c['level'] == 'supported' for c in data['v2_agent_capability']):
                    blockers.append('A required agent capability is not established: ' + capability)
        row = _friendly(task, 'task_state', 'task')
        row.update(bucket=bucket_for_task(task, blockers=blockers,
                                         needs_attention=task_id in attention or human_gate),
                   blockers=blockers, admission='Not authorized by this observation',
                   integration='Accepted; landing not established' if task['task_state'] == 'accepted'
                               else ('Recorded landed' if task['task_state'] == 'landed'
                                     else 'Recorded finalized' if task['task_state'] == 'finalized'
                                     else 'Not established'))
        board.append(row)
        buckets[row['bucket']].append(row)
    section('task_board', board, ('v2_task', 'v2_assignment', 'v2_scheduler_queue',
                                  'v2_scheduler_intent', 'v2_workspace', 'v2_service_state',
                                  'v2_message', 'v2_message_delivery', 'v2_budget',
                                  'v2_agent', 'v2_agent_capability'),
            'Review-ready work needs independent review. Accepted, landed and finalized are distinct.')
    section('project', [{'project_id': project_id, 'created_at': data['v2_project'][0]['created_at'],
                         'scheduling': 'Unknown' if state is None else
                         'Paused' if state['frozen'] or not state['v2_enabled'] else 'Enabled',
                         'observed_tasks': len(board),
                         'bucket_counts_in_snapshot': {k: len(v) for k, v in buckets.items()}}],
            ('v2_project', 'v2_task', 'v2_service_state'),
            'Counts describe this bounded snapshot. Knowledge and docs require their authorized subpage.',
            messages=[{k: m[k] for k in ('message_id', 'task_id', 'thread_id', 'created_at')}
                      for m in pending],
            knowledge_available=False)
    # Overview counts and message attention derive from the whole board snapshot.
    views['project']['truncated'] |= views['task_board']['truncated']
    views['project']['available'] &= views['task_board']['available']
    legacy_nodes, legacy_reason = _legacy_dag_rows(reader, tasks)
    queued_nodes = [
        {'task_id': key, 'milestone_run': value['dag_run'], 'node': value['dag_node'],
         'dependencies': value['dependencies'],
         'label': 'Queued dependency declaration', 'source': 'Frozen queue contract'}
        for key, value in contracts.items() if value['dag_run'] or value['dependencies']]
    milestone_rows = legacy_nodes + queued_nodes
    section('milestones', milestone_rows[:limit], ('v2_scheduler_queue', 'v2_task', 'dag_runs'),
            'Shows persisted DAG nodes and queued dependency declarations. ' + legacy_reason,
            complete_graph=False, milestone_metadata_available=False,
            unavailable_reason='No dedicated milestone/phase metadata schema exists in this baseline; '
                               'unmigrated or out-of-project legacy nodes are not exposed.')
    views['milestones']['available'] = bool(reader.available['v2_task'] and
                                            (reader.available['v2_scheduler_queue'] or reader.available['dag_runs']))
    views['milestones']['truncated'] |= len(milestone_rows) > limit
    if not milestone_rows:
        views['milestones']['explanation'] += ' No project-linked graph declarations were found in this bounded snapshot.'
    registry = []
    for agent in agents.values():
        row = _friendly(agent, 'lifecycle', 'agent')
        row.update({'label': {'active': 'Agent approved', 'pending': 'Agent approval pending',
                              'revoked': 'Agent access revoked'}.get(agent['lifecycle'], row['label']),
                    'explanation': 'Registry approval and last-seen time do not prove live availability.',
                    'capabilities': [r for r in data['v2_agent_capability']
                                     if r['agent_id'] == agent['agent_id']]})
        registry.append(row)
    section('registry', registry, ('v2_agent', 'v2_agent_capability'),
            'Declared capabilities and last observations are not fresh execution probes.')
    assignment_rows = []
    for assignment in assignments:
        row = _friendly(assignment, entity='assignment')
        row['slot_claim'] = 'Held' if assignment['holds_slot'] else 'Not held'
        intent = intents.get(assignment['assignment_id'])
        row['send_observation'] = explain_state(intent['send_state']) if intent else {
            'label': 'Send observation unavailable', 'suggested_action': 'Inspect the original assignment.'}
        assignment_rows.append(row)
    section('assignments', assignment_rows, ('v2_assignment', 'v2_scheduler_intent'),
            'Slot ownership is retained through uncertainty; delivery is not acceptance.')
    queue_rows = []
    from priority_scheduler import admission_key
    instant = now if now is not None else datetime.now(timezone.utc)
    overrides = {r['task_id']: r for r in data['v2_priority_override']}
    for key, item in queues.items():
        task = tasks.get(key)
        row = {'task_id': key, 'enqueued_at': item['enqueued_at'], 'due_at': item['due_at'],
               'priority': task['priority'] if task else None}
        override = overrides.get(key)
        if override:
            row.update(previous_priority=override['previous_priority'],
                       override_expires_at=override['expires_at'])
        try:
            order = admission_key(row, instant)
            row.update(effective_priority='P' + str(order[0]), ordering='Known within snapshot')
        except (ValueError, TypeError, KeyError, IndexError):
            order = None
            row['ordering'] = 'Cannot determine priority from the recorded values'
        row.update(dependencies=contracts[key]['dependencies'],
                   suggested_action='Ask the scheduler for a fresh admission decision.')
        queue_rows.append((order, row))
    queue_rows.sort(key=lambda item: (item[0] is None, item[0] or (), item[1]['task_id']))
    section('queue', [row for _, row in queue_rows],
            ('v2_scheduler_queue', 'v2_task', 'v2_priority_override'),
            'Uses the scheduler comparator within this snapshot; rank is not a claim or permission.')
    section('workspaces', [_workspace_row(r) for r in workspaces.values()],
            ('v2_workspace',),
            'Recorded workspace states only. Files, writers, diffs and target integration were not probed.',
            integration_available=False)
    complete_evidence = bool(reader.available.get('v2_evidence')) and not reader.truncated.get('v2_evidence')
    inspector = []
    for assignment in assignments:
        evidence = [r for r in data['v2_evidence'] if r['assignment_id'] == assignment['assignment_id']]
        transcript = [r['evidence_id'] for r in evidence if r['kind'] == 'transcript']
        diff = [r['evidence_id'] for r in evidence if r['kind'] == 'diff']
        inspector.append({
            'assignment_id': assignment['assignment_id'], 'task_id': assignment['task_id'],
            'run_id': intents.get(assignment['assignment_id'], {}).get('run_id'),
            'transcript_evidence': transcript,
            'diff_evidence': diff,
            'verification_evidence': [r['evidence_id'] for r in evidence
                                      if r['kind'] in ('test-report', 'verification')],
            'transcript_available': bool(transcript),
            'diff_available': bool(diff),
            'evidence_note': ('References only; artifact contents are not read.'
                              if transcript or diff else
                              'No transcript or diff evidence is recorded.'
                              if complete_evidence else
                              'No transcript or diff evidence is visible in this bounded snapshot.'),
            'inspection': 'Not performed; references do not establish passing verification'})
    missing_transcript = [row['assignment_id'] for row in inspector
                          if not row['transcript_evidence'] and not row['diff_evidence']]
    if not missing_transcript:
        reason = None
    elif not reader.available.get('v2_evidence'):
        # An unavailable source cannot prove absence; say so instead of guessing.
        reason = 'Evidence source is unavailable; absence cannot be determined.'
    elif reader.truncated.get('v2_evidence'):
        reason = 'Evidence result is truncated; absence cannot be determined for: ' + \
                 ', '.join(missing_transcript)
    else:
        reason = 'No transcript or diff evidence is recorded for: ' + ', '.join(missing_transcript)
    section('transcript', inspector, ('v2_assignment', 'v2_evidence', 'v2_scheduler_intent', 'v2_event'),
            'Inspector shows metadata and evidence references; artifact contents are not read.',
            transcript_metadata_available=bool(inspector) and not missing_transcript and complete_evidence,
            unavailable_reason=reason,
            observations=[{k: e[k] for k in ('event_id', 'entity_type', 'entity_id', 'global_seq', 'at')}
                          for e in data['v2_event']])
    section('evidence', [_friendly(r, entity='evidence') for r in data['v2_evidence']],
            ('v2_evidence',), 'Hashes are recorded claims, not a fresh verification of artifact bytes.')
    review_rows = [_friendly(r, entity='review evidence') for r in data['v2_review']]
    review_rows.extend({'task_id': t['task_id'], **explain_state('review_ready')}
                       for t in tasks.values() if t['task_state'] == 'review_ready')
    section('review', review_rows, ('v2_review', 'v2_task'),
            'Review is independent of worker completion. Acceptance is separate from landing.')
    section('budget', _budget_rows(reader),
            ('v2_budget', 'v2_budget_reservation', 'v2_usage_close', 'v2_assignment', 'v2_run_link'),
            'Known actual, estimates, unknown usage and held reservations are separate; unknown is not zero.')
    resources = []
    for assignment in assignments:
        if assignment['holds_slot']:
            intent = intents.get(assignment['assignment_id'])
            resources.append({'assignment_id': assignment['assignment_id'], 'task_id': assignment['task_id'],
                              'slot_id': assignment['slot_id'],
                              'reserved_resources': _contract(intent)['resources'] if intent else None,
                              'capacity': None, 'label': 'Recorded claim retained',
                              'explanation': 'A claim is not proof of process termination or OS isolation.'})
    section('resources', resources, ('v2_assignment', 'v2_scheduler_intent'),
            'Shows v2 slot/resource declarations only; legacy claims and live capacity are not established.',
            capacity_available=False)
    recovery = []
    for assignment in assignments:
        if assignment['state'] in ('uncertain', 'blocked', 'failed', 'historical_registered'):
            recovery.append(_friendly(assignment, entity='assignment'))
    recovery.extend(_friendly(r, 'send_state', 'send intent') for r in intents.values()
                    if r['send_state'] in ('attempted', 'uncertain'))
    recovery.extend(_friendly(r, 'status', 'workspace') for r in workspaces.values()
                    if r['status'] in ('workspace_lost', 'orphaned', 'quarantined', 'failed'))
    recovery.extend({'task_id': t['task_id'], **explain_state(t['task_state'])}
                    for t in tasks.values() if t['task_state'] in ('blocked', 'migration_uncertain'))
    section('recovery', recovery, ('v2_assignment', 'v2_scheduler_intent', 'v2_workspace', 'v2_task'),
            'Reconcile retained claims and the original attempt; these suggestions do not authorize resend.')
    return {'format_version': 1, 'project_id': project_id,
            'source_max_event_seq': max((r['global_seq'] for r in data['v2_event']), default=None),
            'views': {name: views[name] for name in VIEW_NAMES}, 'buckets': buckets,
            'sources': {name: {'available': reader.available[name], 'truncated': reader.truncated[name]}
                        for name in reader.available},
            'limitations': [
                'Caller authorizes this project and owns the consistent read transaction.',
                'All facts are recorded observations; no live admission, monitoring or file verification.',
                'Missing or truncated relations can make dependencies and totals incomplete.',
                'Milestones, integration, knowledge, legacy resource claims and capacity are partial or unavailable.',
                'No action authority is created; use the existing authenticated service gateway.']}


def render_html(snapshot, *, view=None):
    '''Return an escaped, passive HTML fragment for the authorized dashboard.'''
    if view is not None and view not in VIEW_NAMES:
        raise ViewError('Unknown view.')

    def escape(value):
        return html.escape(str(value), quote=True)

    def value_html(value):
        if value is None:
            return 'Unknown / not recorded'
        if isinstance(value, dict):
            return '<dl>' + ''.join('<dt>' + escape(k.replace('_', ' ')) +
                                    '</dt><dd>' + value_html(v) + '</dd>'
                                    for k, v in value.items()) + '</dl>'
        if isinstance(value, (tuple, list)):
            return '<ul>' + ''.join('<li>' + value_html(v) + '</li>' for v in value) + '</ul>'
        return escape(value)

    output = ['<section class="awo-observability" aria-label="Workflow observations">',
              '<h2>Workflow observations</h2><p>Project: ' + escape(snapshot['project_id']) + '</p>',
              '<nav aria-label="Observation views"><ul>']
    selected = (view,) if view else VIEW_NAMES
    for name in selected:
        output.append('<li><a href="#awo-' + name + '">' + escape(TITLES[VIEW_NAMES.index(name)]) + '</a></li>')
    output.append('</ul></nav><h3>Work at a glance</h3><ul>')
    for bucket in BUCKETS:
        output.append('<li>' + escape(bucket) + ': ' +
                      str(len(snapshot['buckets'].get(bucket, []))) + ' observed tasks</li>')
    output.append('</ul>')
    for name in selected:
        section = snapshot['views'][name]
        output.append('<section id="awo-' + name + '"><h3>' + escape(section['title']) + '</h3>')
        output.append('<p>' + escape(section['explanation']) + '</p>')
        if not section['available']:
            output.append('<p>Source unavailable or incomplete.</p>')
        if section['truncated']:
            output.append('<p>Result limit reached. This is a partial snapshot.</p>')
        # Availability flags are part of the plain-language contract: a page must
        # say that a transcript, diff, graph, integration or capacity observation
        # is missing instead of rendering an empty list that reads as success.
        for key, value in section.items():
            if value is False and (key.endswith('_available') or key == 'complete_graph'):
                output.append('<p>Not available in this snapshot: ' +
                              escape(key.replace('_', ' ')) + '.</p>')
            elif key == 'unavailable_reason' and value:
                output.append('<p>' + escape(value) + '</p>')
        # Per-row flags matter too: one assignment can have a transcript but no
        # diff, and the page must say so instead of showing a bare False.
        per_row = []
        for row in section['rows']:
            if not isinstance(row, dict):
                continue
            entity = (row.get('assignment_id') or row.get('task_id') or
                      row.get('workspace_id') or row.get('evidence_id') or 'record')
            for key, value in row.items():
                if value is False and (key.endswith('_available') or key == 'complete_graph'):
                    per_row.append(key.replace('_', ' ').replace(' available', '') +
                                   ' unavailable for ' + str(entity))
        if per_row:
            output.append('<p>' + escape('; '.join(per_row)) + '.</p>')
        if section['rows']:
            output.append('<ol>' + ''.join('<li>' + value_html(row) + '</li>'
                                          for row in section['rows']) + '</ol>')
        elif section['available']:
            output.append('<p>No matching records in this snapshot.</p>')
        for extra in ('messages', 'observations'):
            if section.get(extra):
                output.append(value_html(section[extra]))
        output.append('</section>')
    output.append(value_html(snapshot['limitations']) + '</section>')
    return ''.join(output)
