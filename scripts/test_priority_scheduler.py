"""Real local subprocess/SQLite regressions, not live model-provider tests."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import awo_service as service
import budget
import priority_scheduler as scheduler
import workflow_control as control
import workflow_scheduler as legacy


WORKER = '''import json, sys
from pathlib import Path
root, mode = Path(sys.argv[1]), sys.argv[2]
path = root / 'status.json'
state = json.loads(path.read_text(encoding='utf-8'))
if mode == 'status':
    print(json.dumps(state))
elif mode == 'output':
    print((root / 'output.txt').read_text(encoding='utf-8') if (root / 'output.txt').exists() else '')
elif mode == 'send':
    task = json.loads(sys.argv[3])
    with (root / 'sends.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(task) + chr(10))
    state['status'] = 'working'
    path.write_text(json.dumps(state), encoding='utf-8')
    destination = Path(task['cwd'])
    text = task['task_id'] + ' ' + task['nonce'] + chr(10)
    (destination / 'RESULT.md').write_text(text + 'Local fixture result.' + chr(10), encoding='utf-8')
    (destination / 'HANDOFF.md').write_text(text + 'TASK_DONE ' + task['task_id'] + ' ' + task['nonce'] + chr(10), encoding='utf-8')
    (root / 'output.txt').write_text(text, encoding='utf-8')
    state['completion_seq'] += 1
    state['status'] = 'idle'
    path.write_text(json.dumps(state), encoding='utf-8')
    print('local subprocess completed')
'''


def child_claim(state, actor, version, epoch, barrier=None, payload=None):
    """A real second process with its own SQLite connection/transaction."""
    import time
    if barrier:
        deadline = time.monotonic() + 15
        while not Path(barrier).exists():
            if time.monotonic() > deadline:
                raise RuntimeError('barrier timeout')
            time.sleep(0.01)
    con = service._connect(service._db_path(state))
    try:
        con.execute('BEGIN IMMEDIATE')
        service._controller(con, actor, version, 'main')
        current = service._state_row(con)
        if current['controller_epoch'] != epoch:
            raise ValueError('stale epoch')
        result = scheduler.apply_command(con, current['project_id'], actor, 'scheduler.claim', payload or {})
        con.commit()
        return result
    finally:
        con.close()


class PriorityComparatorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)

    def row(self, task, priority='P3', minutes=0, due=None):
        return {'task_id': task, 'priority': priority,
                'enqueued_at': (self.now - timedelta(minutes=minutes)).isoformat(), 'due_at': due}

    def test_equal_priority_fifo_and_stable_id_tiebreak(self):
        rows = [self.row('B', minutes=2), self.row('C', minutes=5), self.row('A', minutes=2)]
        self.assertEqual([r['task_id'] for r in sorted(rows, key=lambda r: scheduler.admission_key(r, self.now))], ['C', 'A', 'B'])

    def test_native_p0_and_p1_outrank_promotions(self):
        rows = [self.row('old-P4', 'P4', 10081), self.row('aging', 'P2', 500),
                self.row('native-P1', 'P1'), self.row('incident', 'P0')]
        self.assertEqual([r['task_id'] for r in sorted(rows, key=lambda r: scheduler.admission_key(r, self.now))],
                         ['incident', 'native-P1', 'old-P4', 'aging'])

    def test_aging_caps_and_seven_day_exception(self):
        for priority, minutes, effective in [('P1', 900, 1), ('P2', 500, 1), ('P3', 241, 1),
                                             ('P4', 9999, 2), ('P4', 10080, 1)]:
            with self.subTest(priority=priority, minutes=minutes):
                self.assertEqual(scheduler.admission_key(self.row('T', priority, minutes), self.now)[0], effective)

    def test_deadline_promotes_at_24_hours_never_p0(self):
        for hours, expected in [(24.1, 4), (24, 1), (-1, 1)]:
            row = self.row('T', 'P4', due=(self.now + timedelta(hours=hours)).isoformat())
            self.assertEqual(scheduler.admission_key(row, self.now)[0], expected)

    def test_timestamp_timezone_required_and_offsets_normalized(self):
        a = self.row('T')
        b = dict(a, enqueued_at='2026-11-10T20:00:00+08:00')
        self.assertEqual(scheduler.admission_key(a, self.now), scheduler.admission_key(b, self.now))
        with self.assertRaises(ValueError):
            scheduler.admission_key(dict(a, enqueued_at='2026-11-10T12:00:00'), self.now)

    def test_expired_p0_returns_to_prior_priority_without_writes(self):
        row = dict(self.row('T', 'P0'), previous_priority='P4', override_expires_at=self.now.isoformat())
        self.assertEqual(scheduler.admission_key(row, self.now)[0], 4)
        self.assertEqual(row['priority'], 'P0')

    def test_service_schema_without_legacy_meta_is_additive_and_rollbackable(self):
        con = sqlite3.connect(':memory:')
        con.row_factory = sqlite3.Row
        try:
            con.execute('BEGIN IMMEDIATE')
            service.create_schema(con)
            self.assertFalse(service._table_exists(con, 'meta'))
            self.assertTrue(service._table_exists(con, 'dispatches'))
            self.assertTrue(service._table_exists(con, 'v2_scheduler_queue'))
            self.assertTrue(con.in_transaction)
            con.rollback()
            self.assertFalse(service._table_exists(con, 'v2_scheduler_queue'))
            self.assertFalse(service._table_exists(con, 'dispatches'))
        finally:
            con.close()


class PrioritySchedulerTests(unittest.TestCase):
    def setUp(self):
        if getattr(self, 'evidence_root', None):
            self.root = Path(self.evidence_root).resolve()
            self.root.mkdir(parents=True, exist_ok=False)
        else:
            self.temp = tempfile.TemporaryDirectory(prefix='awo-priority-')
            self.addCleanup(self.temp.cleanup)
            self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.worker_script = self.root / 'local_worker.py'
        self.worker_script.write_text(WORKER, encoding='utf-8', newline='\n')
        extra_roles = tuple(getattr(self, 'extra_roles', ()))
        self.config = {'roles': {r: {} for r in ('planner', 'executor', 'second') + extra_roles},
                       'workflow': {'reviewer_role': 'planner',
                                    'transitions': {'accept': 'next', 'repair': 'repair', 'reject': 'stop'}},
                       'scheduler': {'max_concurrency': getattr(self, 'fixture_concurrency', 2), 'resources': {'gpu': 1}}}
        control.initialize(self.state, self.config)
        self.specs, self.bindings = {}, {}
        for role in self.config['roles']:
            spec = self.spec(role)
            binding = control.bind(self.state, role, spec, spec['expected_identity'],
                                   'controller' if role == 'planner' else 'worker')
            self.specs[role], self.bindings[role] = binding['spec'], binding
        planner = self.bindings['planner']
        self.actor = (planner['instance_id'], planner['version'], 'main')
        service.apply_migration(self.state, *self.actor)
        with self.db() as con:
            scheduler.create_scheduler_schema(con)
        self.project = service.service_status(self.state)['project_id']
        self.agents = {role: service._agent_id_for_binding(b['instance_id']) for role, b in self.bindings.items()}
        self.epoch = service.set_enabled(self.state, *self.actor, True)['epoch']
        self.capfile = self.root / 'local-capability-evidence.json'
        self.capfile.write_text(json.dumps({'kind': 'synthetic-local-command-fixture',
                                          'python': sys.version, 'argv': [sys.executable, str(self.worker_script)],
                                          'worker_sha256': service._file_sha(self.worker_script)}), encoding='utf-8')
        for role in ('executor', 'second') + extra_roles:
            for capability in scheduler.DEFAULT_CAPABILITIES:
                agent = service.agent_get(self.state, *self.actor, self.agents[role])['agent']
                service.set_agent_capability(self.state, *self.actor, self.envelope('agent.capability', {
                    'agent_id': agent['agent_id'], 'capability_key': capability, 'level': 'supported',
                    'harness_version': 'fixture-1', 'os': sys.platform, 'host': 'local',
                    'checked_at': service._now(), 'evidence_path': str(self.capfile),
                    'evidence_sha256': service._file_sha(self.capfile), 'source': 'regression_test',
                    'expected_revision': agent['revision']}))

    def spec(self, role):
        directory = self.root / ('worker-' + role)
        directory.mkdir()
        identity = {'kind': 'synthetic-local-fixture', 'terminal': 'terminal-' + role, 'launch_cwd': str(directory)}
        (directory / 'status.json').write_text(json.dumps({'identity': identity, 'status': 'idle', 'completion_seq': 10}), encoding='utf-8')
        argv = [sys.executable, '-B', str(self.worker_script), str(directory)]
        return {'role': role, 'adapter': 'command', 'harness': 'local-fixture', 'version': 'fixture-1',
                'expected_identity': identity, 'status_argv': argv + ['status'], 'identity_argv': argv + ['status'],
                'prompt_argv': argv + ['send', '{message}'], 'output_argv': argv + ['output']}

    @contextmanager
    def db(self, readonly=False):
        con = service._connect(service._db_path(self.state), readonly=readonly)
        try:
            con.execute('BEGIN' if readonly else 'BEGIN IMMEDIATE')
            yield con
            if not readonly:
                con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def envelope(self, tool, payload, command_id=None):
        return {'tool': tool, 'payload': payload, 'expected_epoch': self.epoch,
                'command_id': command_id or 'test-' + uuid.uuid4().hex}

    def command(self, tool, payload):
        with self.db() as con:
            service._controller(con, *self.actor)
            self.assertEqual(service._state_row(con)['controller_epoch'], self.epoch)
            return scheduler.apply_command(con, self.project, self.actor[0], tool, payload)

    def rows(self, sql, args=()):
        with self.db(readonly=True) as con:
            return [dict(r) for r in con.execute(sql, args)]

    def snapshot(self):
        with self.db(readonly=True) as con:
            tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {table: [dict(row) for row in con.execute('SELECT * FROM "' + table.replace('"', '""') + '"')]
                    for table in tables}

    def request(self, attempt='ATTEMPT-A', role='executor', **changes):
        cwd = self.root / attempt
        cwd.mkdir(exist_ok=True)
        status = self.status(role)
        request = {'task_id': attempt, 'nonce': 'nonce-' + attempt + '-' + uuid.uuid4().hex[:8], 'cwd': str(cwd),
                   'workflow_state': str(self.state), 'worker': dict(self.specs[role], baseline_completion_seq=status['completion_seq']),
                   'reviewer': self.specs['planner'], 'dispatch_confirmed': False,
                   'authorization': {'source': 'Owner-approved local regression fixture', 'dispatch_worker': True, 'notify_reviewer': False},
                   'completion': {'binding_artifact': 'HANDOFF.md', 'artifacts': ['RESULT.md'], 'marker_source': 'artifact'}}
        request['completion']['marker'] = 'TASK_DONE ' + attempt + ' ' + request['nonce']
        request.update(changes)
        return request

    def status(self, role='executor', **changes):
        path = self.root / ('worker-' + role) / 'status.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        if changes:
            value.update(changes)
            path.write_text(json.dumps(value), encoding='utf-8')
        return value

    def create(self, task='T-A', priority='P3'):
        return service.execute_command(self.state, *self.actor, self.envelope('task.create',
                                       {'task_id': task, 'title': task, 'priority': priority}))

    def enqueue(self, task='T-A', role='executor', priority='P3', request=None, create=True, **extra):
        if create:
            self.create(task, priority)
        request = request or self.request('ATTEMPT-' + task, role)
        revision = self.rows('SELECT revision FROM v2_task WHERE task_id=?', (task,))[0]['revision']
        payload = {'task_id': task, 'expected_revision': revision, 'request': request,
                   'message': json.dumps({key: request[key] for key in ('task_id', 'nonce', 'cwd')}),
                   'agent_id': self.agents[role], 'budget': {'tokens.input': 10}}
        payload.update(extra)
        return self.command('scheduler.enqueue', payload), request

    def claim(self, **payload):
        return self.command('scheduler.claim', payload)

    def dispatch(self, assignment):
        return scheduler.dispatch_assignment(self.state, assignment, *self.actor, expected_epoch=self.epoch)

    def reconcile(self, assignment):
        return scheduler.reconcile_assignment(self.state, assignment, *self.actor, expected_epoch=self.epoch)

    def get_assignment(self, assignment):
        with self.db(readonly=True) as con:
            return scheduler.assignment_get(con, self.project, assignment)

    def review(self, completion, request, verdict='accept', same_connection=False):
        note = Path(request['cwd']) / 'REVIEW.md'
        note.write_text(request['task_id'] + ' ' + request['nonce'] + '\nVerdict: ' + verdict + '\n', encoding='utf-8')
        if same_connection:
            with self.db() as con:
                result = scheduler.review_in_transaction(con, self.project, *self.actor,
                                                         completion['event_id'], str(note), verdict)
        else:
            control.acknowledge(self.state, completion['event_id'], *self.actor)
            result = control.record_review(self.state, completion['event_id'], *self.actor, note, verdict)
        return result

    def close(self, assignment, completion=None, outcome='accepted', **extras):
        payload = {'assignment_id': assignment, 'expected_revision': self.get_assignment(assignment)['revision'], 'outcome': outcome}
        if completion:
            payload['event_id'] = completion['event_id']
        payload.update(extras)
        return self.command('assignment.close', payload)

    def finished(self, task='T-A', role='executor', verdict='accept', **extra):
        _, request = self.enqueue(task, role, **extra)
        reserved = self.claim(task_id=task, expected_revision=2)
        self.assertEqual(reserved['status'], 'RESERVED', reserved)
        aid = reserved['assignment_id']
        self.assertEqual(self.dispatch(aid)['status'], 'SUBMITTED')
        completion = self.reconcile(aid)
        self.assertEqual(completion['status'], 'REVIEW_READY', completion)
        self.review(completion, request, verdict)
        self.close(aid, completion, {'accept': 'accepted', 'repair': 'repair_required', 'reject': 'rejected'}[verdict])
        return reserved, request, completion

    def set_limits(self, limits, scope='workflow', identity=None):
        return budget.budget_limit(self.state, *self.actor, self.envelope('budget.limit',
            {'scope_type': scope, 'scope_id': identity or self.project, 'limits': limits,
             'policy': {'on_unknown': 'warn'}, 'expected_revision': 1}))

    def mutate(self, sql, args=()):
        with self.db() as con:
            con.execute(sql, args)

    def next(self, agent=None, now=None):
        with self.db(readonly=True) as con:
            return scheduler.next_task(con, self.project, agent, now)

    def ready(self, **extra):
        _, request = self.enqueue(**extra)
        reserved = self.claim(task_id=extra.get('task', 'T-A'), expected_revision=2)
        self.assertEqual(reserved['status'], 'RESERVED', reserved)
        aid = reserved['assignment_id']
        self.assertEqual(self.dispatch(aid)['status'], 'SUBMITTED')
        completion = self.reconcile(aid)
        self.assertEqual(completion['status'], 'REVIEW_READY', completion)
        return aid, request, completion

    def assert_waiting(self, result, contains):
        self.assertEqual(result['status'], 'WAITING', result)
        self.assertIn(contains.lower(), json.dumps(result).lower())

    def test_local_subprocess_cycle_review_and_downstream(self):
        aid, request, completion = self.ready(resources={'gpu': 1})
        self.assertTrue(self.get_assignment(aid)['holds_slot'])
        self.assertEqual(self.rows('SELECT task_state FROM v2_task')[0]['task_state'], 'review_ready')
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 3)
        _, downstream = self.enqueue('T-B', 'second', dependencies=['T-A'])
        self.assert_waiting(self.claim(task_id='T-B', expected_revision=2), 'independent acceptance')
        self.review(completion, request, same_connection=True)
        self.assertEqual(self.rows('SELECT task_state FROM v2_task WHERE task_id=?', ('T-A',))[0]['task_state'], 'review_ready')
        self.assertTrue(self.get_assignment(aid)['holds_slot'])
        self.close(aid, completion)
        self.assertFalse(self.get_assignment(aid)['holds_slot'])
        self.assertEqual(len(self.rows('SELECT * FROM v2_review')), 1)
        self.assertEqual(len(self.rows('SELECT * FROM v2_evidence')), 2)
        self.assertEqual({r['state'] for r in self.rows('SELECT * FROM v2_budget_reservation')}, {'held'})
        next_run = self.claim(task_id='T-B', expected_revision=2)
        self.assertEqual(next_run['status'], 'RESERVED', next_run)
        self.assertNotEqual(aid, next_run['assignment_id'])
        self.assertEqual(len((self.root / 'worker-executor' / 'sends.jsonl').read_text().splitlines()), 1)
        self.assertFalse((self.root / 'worker-second' / 'sends.jsonl').exists())

    def test_controller_close_requires_review_and_cannot_replay(self):
        aid, request, completion = self.ready()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'review'):
            self.close(aid, completion)
        self.assertEqual(before, self.snapshot())
        self.review(completion, request, same_connection=True)
        self.close(aid, completion)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'already closed'):
            self.close(aid, completion)
        self.assertEqual(before, self.snapshot())

    def test_legacy_review_cannot_accept_v2_or_overwrite_gateway_review(self):
        aid, request, completion = self.ready()
        self.review(completion, request)
        before = self.snapshot()
        self.assertEqual(self.rows('SELECT task_state FROM v2_task')[0]['task_state'], 'review_ready')
        self.assertTrue(self.get_assignment(aid)['holds_slot'])
        self.assertEqual(self.rows('SELECT * FROM v2_review'), [])
        with self.assertRaisesRegex(ValueError, 'pending'):
            with self.db() as con:
                scheduler.review_in_transaction(con, self.project, *self.actor, completion['event_id'],
                                                str(Path(request['cwd']) / 'REVIEW.md'), 'accept')
        self.assertEqual(before, self.snapshot())
        self.close(aid, completion)
        self.assertEqual(self.rows('SELECT task_state FROM v2_task')[0]['task_state'], 'accepted')
        with self.assertRaises(ValueError):
            control.record_review(self.state, completion['event_id'], *self.actor,
                                  Path(request['cwd']) / 'REVIEW.md', 'reject')
        self.assertEqual(self.rows('SELECT verdict FROM v2_review')[0]['verdict'], 'accept')

    def test_completion_and_review_replay_preserve_first_receipt(self):
        aid, request, completion = self.ready()
        before = self.snapshot()
        self.assertEqual(self.reconcile(aid)['event_id'], completion['event_id'])
        self.assertEqual(before, self.snapshot())
        path = Path(request['cwd']) / 'RESULT.md'
        path.write_text(path.read_text() + 'changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            self.reconcile(aid)
        self.assertEqual(before, self.snapshot())
        with self.assertRaisesRegex(ValueError, 'evidence is invalid'):
            self.review(completion, request, same_connection=True)
        self.assertEqual(before, self.snapshot())

    def test_review_tamper_and_nonidle_block_close(self):
        aid, request, completion = self.ready()
        self.review(completion, request, same_connection=True)
        for state in ('working', 'blocked', 'disconnected', 'unknown'):
            with self.subTest(state=state):
                self.status(status=state)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'idle'):
                    self.close(aid, completion)
                self.assertEqual(before, self.snapshot())
        self.status(status='idle', completion_seq=12)
        with self.assertRaisesRegex(ValueError, 'sequence changed'):
            self.close(aid, completion)
        self.status(completion_seq=11)
        (Path(request['cwd']) / 'REVIEW.md').write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            self.close(aid, completion)
        self.assertTrue(self.get_assignment(aid)['holds_slot'])

    def test_worker_claim_complete_cannot_expand_or_self_review(self):
        _, request = self.enqueue()
        before = self.snapshot()
        for payload in ({'agent_id': self.agents['second']}, {'resources': {'gpu': 9}},
                        {'request': request}, {'task_id': 'T-A'}, {'task_id': 'T-A', 'expected_revision': True}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    with self.db() as con:
                        scheduler.claim_for_agent(con, self.project, self.agents['executor'], payload)
                self.assertEqual(before, self.snapshot())
        with self.assertRaises(ValueError):
            with self.db() as con:
                scheduler.claim_for_agent(con, self.project, self.agents['second'], {'task_id': 'T-A', 'expected_revision': 2})
        with self.db() as con:
            reserved = scheduler.claim_for_agent(con, self.project, self.agents['executor'], {'task_id': 'T-A', 'expected_revision': 2})
        aid = reserved['assignment_id']
        with self.db() as con:
            self.assertEqual(scheduler.complete_for_agent(con, self.project, self.agents['executor'], aid)['status'], 'NOT_SENT')
        self.dispatch(aid)
        with self.assertRaisesRegex(ValueError, 'different Agent'):
            with self.db() as con:
                scheduler.complete_for_agent(con, self.project, self.agents['second'], aid)
        with self.db() as con:
            completion = scheduler.complete_for_agent(con, self.project, self.agents['executor'], aid)
        self.assertEqual(completion['status'], 'REVIEW_READY')
        worker = self.bindings['executor']
        with self.assertRaises(ValueError):
            with self.db() as con:
                scheduler.review_in_transaction(con, self.project, worker['instance_id'], worker['version'],
                                                'main', completion['event_id'], str(Path(request['cwd']) / 'REVIEW.md'), 'accept')
        self.assertTrue(self.get_assignment(aid)['holds_slot'])

    def test_next_checks_dependency_capability_capacity_budget_readonly(self):
        self.create('UPSTREAM')
        self.enqueue('BLOCKED', priority='P1', dependencies=['UPSTREAM'])
        self.enqueue('ELIGIBLE', 'second', priority='P3')
        before = self.snapshot()
        result = self.next()
        self.assertEqual(result['candidate']['task_id'], 'ELIGIBLE')
        self.assertIn('independent acceptance', json.dumps(result))
        self.assertTrue(result['live_worker_check_pending'])
        self.assertEqual(before, self.snapshot())
        self.mutate("UPDATE v2_agent_capability SET level='unknown' WHERE agent_id=? AND capability_key='send'", (self.agents['second'],))
        self.assert_waiting(self.next(), 'unverified')
        self.mutate("UPDATE v2_agent_capability SET level='supported' WHERE agent_id=?", (self.agents['second'],))
        self.set_limits({'tokens.input': 5})
        before = self.snapshot()
        self.assert_waiting(self.next(), 'budget cap')
        self.assertEqual(before, self.snapshot())
        self.assert_waiting(self.claim(), 'budget cap')
        self.assertEqual(before, self.snapshot())

    def test_readonly_next_never_polls_harness_or_creates_budget(self):
        self.enqueue()
        (self.root / 'worker-executor' / 'status.json').unlink()
        before = self.snapshot()
        self.assertEqual(self.next()['status'], 'CANDIDATE')
        self.assertEqual(before, self.snapshot())
        self.assert_waiting(self.claim(), 'status')
        self.assertEqual(before, self.snapshot())

    def test_unknown_tampered_capability_and_revoked_agent_block(self):
        self.enqueue()
        self.mutate("UPDATE v2_agent_capability SET level='unknown' WHERE agent_id=? AND capability_key='send'", (self.agents['executor'],))
        self.assert_waiting(self.claim(), 'unverified')
        self.mutate("UPDATE v2_agent_capability SET level='supported' WHERE agent_id=?", (self.agents['executor'],))
        original = self.capfile.read_bytes()
        self.capfile.write_bytes(b'changed capability evidence')
        self.assert_waiting(self.next(), 'missing or changed')
        self.assert_waiting(self.claim(), 'missing or changed')
        self.capfile.write_bytes(original)
        self.mutate("UPDATE v2_agent SET lifecycle='revoked' WHERE agent_id=?", (self.agents['executor'],))
        self.assert_waiting(self.claim(), 'approved worker')
        self.assertEqual(self.rows('SELECT * FROM v2_assignment'), [])

    def test_live_identity_baseline_and_worker_binding_drift(self):
        self.enqueue()
        old = self.status()
        self.status(identity={'kind': 'wrong'}, status='idle')
        self.assert_waiting(self.claim(), 'identity changed')
        self.status(identity=old['identity'], completion_seq=99)
        self.assert_waiting(self.claim(), 'baseline changed')
        self.status(completion_seq=10, status='blocked')
        self.assert_waiting(self.claim(), 'not idle')
        self.status(status='idle')
        self.mutate("UPDATE bindings SET status='revoked' WHERE id=?", (self.bindings['executor']['instance_id'],))
        self.assertEqual(self.claim()['status'], 'WAITING')
        self.assertEqual(self.rows('SELECT * FROM v2_assignment'), [])

    def test_stale_revision_epoch_freeze_project_and_side_context(self):
        self.enqueue()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.claim(task_id='T-A', expected_revision=1)
        with self.assertRaises(ValueError):
            with self.db() as con:
                scheduler.apply_command(con, 'wrong-project', self.actor[0], 'scheduler.claim', {})
        with self.assertRaises(ValueError):
            with self.db() as con:
                service._controller(con, self.actor[0], self.actor[1], 'side')
        self.assertEqual(before, self.snapshot())
        self.mutate('UPDATE v2_service_state SET controller_epoch=controller_epoch+1')
        self.epoch += 1
        self.assert_waiting(self.next(), 'obsolete')
        with self.assertRaisesRegex(ValueError, 'approval changed'):
            with self.db() as con:
                scheduler.claim_for_agent(con, self.project, self.agents['executor'])
        self.mutate('UPDATE v2_service_state SET frozen=1')
        self.assert_waiting(self.next(), 'frozen')
        with self.assertRaisesRegex(ValueError, 'frozen'):
            self.claim()
        self.assertEqual(self.rows('SELECT * FROM v2_assignment'), [])

    def test_fifo_urgent_next_tick_and_no_running_preemption(self):
        self.enqueue('FIRST')
        self.enqueue('SECOND', 'second')
        self.assertEqual(self.next()['candidate']['task_id'], 'FIRST')
        self.command('scheduler.priority', {'task_id': 'SECOND', 'expected_revision': 2, 'priority': 'P1', 'reason': 'Owner escalation'})
        chosen = self.claim()
        self.assertEqual(chosen['task_id'], 'SECOND')
        with self.assertRaisesRegex(ValueError, 'running'):
            self.command('scheduler.priority', {'task_id': 'SECOND', 'expected_revision': chosen['task_revision'], 'priority': 'P2', 'reason': 'later'})
        self.assertTrue(self.get_assignment(chosen['assignment_id'])['holds_slot'])

    def test_aging_persists_restart_and_watchdog_event_deduplicates(self):
        now = datetime.now(timezone.utc)
        self.enqueue(priority='P4', due_at=(now + timedelta(hours=1)).isoformat())
        old = (now - timedelta(days=8)).isoformat()
        self.mutate('UPDATE v2_scheduler_queue SET enqueued_at=?', (old,))
        self.status(status='blocked')
        before = self.snapshot()
        for _ in range(2):
            self.assertEqual(self.next(now=now)['candidate']['effective_priority'], 'P1')
        self.assertEqual(before, self.snapshot())
        for _ in range(2):
            self.assertEqual(self.claim()['status'], 'WAITING')
        events = self.rows("SELECT event_type FROM v2_event WHERE event_type='queue.deadline_risk'")
        self.assertEqual(len(events), 1)
        self.assertEqual(self.rows('SELECT enqueued_at FROM v2_scheduler_queue')[0]['enqueued_at'], old)
        self.mutate('UPDATE v2_scheduler_queue SET due_at=NULL')
        for _ in range(2):
            self.claim()
        self.assertEqual(len(self.rows("SELECT event_type FROM v2_event WHERE event_type='queue.starved'")), 1)

    def test_p0_requires_expiry_and_native_p1_stays_ahead_of_aging(self):
        self.create()
        with self.assertRaises(ValueError):
            self.command('scheduler.priority', {'task_id': 'T-A', 'expected_revision': 1, 'priority': 'P0', 'reason': 'incident'})
        result = self.command('scheduler.priority', {'task_id': 'T-A', 'expected_revision': 1, 'priority': 'P0',
            'reason': 'incident', 'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        self.enqueue(create=False)
        self.assertEqual(result['revision'], 2)
        self.assertEqual(self.next()['candidate']['effective_priority'], 'P0')
        self.assertEqual(self.next(now=datetime.now(timezone.utc) + timedelta(hours=2))['candidate']['base_priority'], 'P3')

    def test_resource_host_harness_and_global_limits(self):
        self.enqueue('T-A', resources={'gpu': 1})
        self.enqueue('T-B', 'second', resources={'gpu': 1})
        self.assertEqual(self.claim(task_id='T-A', expected_revision=2)['status'], 'RESERVED')
        self.assert_waiting(self.next(), 'resource capacity')
        self.assert_waiting(self.claim(), 'resource capacity')
        for setting, match in (({'max_concurrency': 1}, 'slots'), ({'per_host': {'local': 1}}, 'host execution'),
                               ({'per_harness': {'local-fixture': 1}}, 'harness execution')):
            cfg = json.loads(json.dumps(self.config))
            cfg['scheduler'].update(setting)
            self.mutate("UPDATE meta SET value=? WHERE key='config'", (json.dumps(cfg),))
            self.assert_waiting(self.claim(), match)
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment')), 1)

    def test_directory_overlap_is_not_bypassed_by_different_worker(self):
        _, first = self.enqueue()
        self.claim()
        nested = Path(first['cwd']) / 'nested'
        nested.mkdir()
        request = self.request('ATTEMPT-B', 'second', cwd=str(nested))
        self.enqueue('T-B', 'second', request=request)
        self.assert_waiting(self.claim(), 'directory')

    def test_resolved_legacy_state_does_not_release_v2_claim(self):
        aid, request, completion = self.ready(resources={'gpu': 1})
        self.review(completion, request)
        self.mutate("UPDATE tasks SET status='resolved' WHERE id=?", (request['task_id'],))
        self.enqueue('T-B', 'second', resources={'gpu': 1})
        self.assert_waiting(self.claim(task_id='T-B', expected_revision=2), 'resource capacity')
        self.assertTrue(self.get_assignment(aid)['holds_slot'])

    def test_budget_rejection_and_late_trigger_roll_back_all_tables(self):
        self.enqueue()
        self.set_limits({'tokens.input': 5})
        before = self.snapshot()
        self.assert_waiting(self.claim(), 'budget cap')
        self.assertEqual(before, self.snapshot())
        self.mutate("UPDATE v2_budget SET limits_json='{}'")
        with self.db() as con:
            con.execute("CREATE TRIGGER fail_late BEFORE INSERT ON v2_scheduler_intent BEGIN SELECT CASE WHEN (SELECT count(*) FROM v2_budget_reservation)=3 AND (SELECT count(*) FROM dispatches)=1 THEN RAISE(ABORT,'late_after_budget_and_dispatch') ELSE RAISE(ABORT,'test_did_not_reach_late_stage') END; END")
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'late_after_budget_and_dispatch'):
            self.claim()
        self.assertEqual(before, self.snapshot())
        self.mutate('DROP TRIGGER fail_late')
        self.assertEqual(self.claim()['status'], 'RESERVED')

    def test_single_queue_schema_upgrade_preserves_approval_and_rolls_back(self):
        self.enqueue()
        self.mutate('UPDATE v2_scheduler_queue SET starved_notified=1,deadline_notified=1')
        fields = ('task_id,project_id,attempt_id,enqueued_at,due_at,task_revision,contract_json,'
                  'starved_notified,deadline_notified')
        original = self.rows('SELECT ' + fields + ' FROM v2_scheduler_queue')
        with self.db() as con:
            con.execute('CREATE TABLE old_queue ('
                        'task_id TEXT PRIMARY KEY REFERENCES v2_task(task_id),'
                        'project_id TEXT NOT NULL REFERENCES v2_project(project_id),'
                        'attempt_id TEXT NOT NULL UNIQUE,enqueued_at TEXT NOT NULL,due_at TEXT,'
                        'task_revision INTEGER NOT NULL,contract_json TEXT NOT NULL,'
                        'starved_notified INTEGER NOT NULL DEFAULT 0,deadline_notified INTEGER NOT NULL DEFAULT 0)')
            con.execute('INSERT INTO old_queue SELECT ' + fields + ' FROM v2_scheduler_queue')
            con.execute('DROP TABLE v2_scheduler_queue')
            con.execute('ALTER TABLE old_queue RENAME TO v2_scheduler_queue')
        before = self.snapshot()
        con = service._connect(service._db_path(self.state))
        try:
            con.execute('BEGIN IMMEDIATE')
            scheduler.create_scheduler_schema(con)
            self.assertTrue(con.in_transaction)
            self.assertEqual(con.execute('SELECT slot_id FROM v2_scheduler_queue').fetchone()[0], 0)
            con.rollback()
            self.assertNotIn('slot_id', [r[1] for r in con.execute('PRAGMA table_info(v2_scheduler_queue)')])
        finally:
            con.close()
        self.assertEqual(before, self.snapshot())
        with self.db() as con:
            scheduler.create_scheduler_schema(con)
            scheduler.create_scheduler_schema(con)
            self.assertEqual(list(con.execute('PRAGMA foreign_key_check')), [])
        self.assertEqual(original, self.rows('SELECT ' + fields + ' FROM v2_scheduler_queue'))
        self.assertIsNone(self.rows('SELECT collaboration_ticket_id FROM v2_scheduler_queue')[0]['collaboration_ticket_id'])
        self.assertEqual(self.claim()['status'], 'RESERVED')

    def test_real_two_process_claim_race_one_slot_no_partial_loser(self):
        self.enqueue()
        barrier = self.root / 'start.flag'
        program = 'import json,sys; from test_priority_scheduler import child_claim; print(json.dumps(child_claim(sys.argv[1],sys.argv[2],int(sys.argv[3]),int(sys.argv[4]),sys.argv[5])))'
        args = [sys.executable, '-B', '-c', program, str(self.state), self.actor[0], str(self.actor[1]), str(self.epoch), str(barrier)]
        children = [subprocess.Popen(args, cwd=Path(__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            barrier.write_text('go', encoding='ascii')
            results = []
            for process in children:
                stdout, stderr = process.communicate(timeout=30)
                (self.root / ('race-process-%d.json' % len(results))).write_text(
                    json.dumps({'argv': args, 'returncode': process.returncode, 'stdout': stdout, 'stderr': stderr}),
                    encoding='utf-8')
                self.assertEqual(process.returncode, 0, stderr)
                results.append(json.loads(stdout))
        finally:
            for process in children:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
        self.assertCountEqual([r['status'] for r in results], ['RESERVED', 'WAITING'])
        for table in ('v2_assignment', 'v2_run_link', 'tasks', 'dispatches', 'v2_legacy_map', 'v2_scheduler_intent'):
            self.assertEqual(len(self.rows('SELECT * FROM ' + table)), 1, table)
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 3)
        self.assertFalse((self.root / 'worker-executor' / 'sends.jsonl').exists())

    def test_real_process_crash_after_send_never_resends_and_recovers(self):
        self.enqueue()
        reserved = self.claim()
        aid = reserved['assignment_id']
        program = '''import json,os,sys
import priority_scheduler as s
class Crash(s.monitor.Transport):
    def send(self,spec,message):
        super().send(spec,message)
        os._exit(73)
con=s.service._connect(s.service._db_path(sys.argv[1]),readonly=True)
_,_,contract=s._intent(con,sys.argv[6],sys.argv[2])
con.close()
s.dispatch_assignment(sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]),expected_epoch=int(sys.argv[5]),transport=Crash(contract['request']))
'''
        result = subprocess.run([sys.executable, '-B', '-c', program, str(self.state), aid, self.actor[0],
                                 str(self.actor[1]), str(self.epoch), self.project], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=30)
        (self.root / 'crash-process.json').write_text(
            json.dumps({'argv': result.args, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}),
            encoding='utf-8')
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(self.get_assignment(aid)['send_state'], 'attempted')
        self.assertEqual(self.dispatch(aid)['status'], 'ALREADY_ATTEMPTED')
        self.assertEqual(len((self.root / 'worker-executor' / 'sends.jsonl').read_text().splitlines()), 1)
        self.assertEqual(self.reconcile(aid)['status'], 'REVIEW_READY')
        self.assertTrue(self.get_assignment(aid)['holds_slot'])

    def test_timeout_disconnect_and_cancel_request_retain_claims(self):
        aid, request, completion = self.ready()
        self.status(status='disconnected')
        result = self.reconcile(aid)
        self.assertEqual(result['assignment']['state'], 'uncertain')
        self.assertTrue(result['claims_retained'])
        class TimeoutTransport:
            def status(self, spec):
                raise subprocess.TimeoutExpired('explicit synthetic timeout probe', 1)
        with self.db() as con:
            result = scheduler.reconcile_in_transaction(con, self.project, self.actor[0], aid, transport=TimeoutTransport())
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertTrue(self.get_assignment(aid)['holds_slot'])
        self.assertEqual(len(self.rows('SELECT * FROM events')), 1)

    def test_cancellation_requires_bound_note_and_actual_idle(self):
        _, request = self.enqueue()
        aid = self.claim()['assignment_id']
        note = Path(request['cwd']) / 'TERMINATED.md'
        note.write_text('Outcome: terminated', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.close(aid, outcome='cancelled', evidence_path=str(note))
        note.write_text(request['task_id'] + ' ' + request['nonce'] + '\nOutcome: terminated\n', encoding='utf-8')
        self.status(status='blocked')
        with self.assertRaisesRegex(ValueError, 'idle'):
            self.close(aid, outcome='cancelled', evidence_path=str(note))
        self.status(status='idle')
        self.close(aid, outcome='cancelled', evidence_path=str(note))
        self.assertFalse(self.get_assignment(aid)['holds_slot'])
        self.assertEqual(self.rows('SELECT * FROM v2_review'), [])

    def test_tampered_dependency_and_unmaterialized_code_fail_closed(self):
        reserved, request, _ = self.finished()
        self.enqueue('T-B', 'second', dependencies=['T-A'])
        path = Path(request['cwd']) / 'RESULT.md'
        raw = path.read_bytes()
        path.write_bytes(raw + b' changed')
        self.assert_waiting(self.next(), 'evidence')
        self.assert_waiting(self.claim(), 'evidence')
        path.write_bytes(raw)
        intent = self.rows('SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?', (reserved['assignment_id'],))[0]
        contract = json.loads(intent['contract_json'])
        contract['task_kind'] = 'code'
        self.mutate('UPDATE v2_scheduler_intent SET contract_json=? WHERE assignment_id=?', (json.dumps(contract), reserved['assignment_id']))
        # A code dependency without a frozen Git base fails closed at the same gate.
        self.assert_waiting(self.next(), 'git_worktree')
        self.assert_waiting(self.claim(), 'git_worktree')

    def test_git_worktree_frozen_spec_rejects_incomplete_or_adopted_inputs(self):
        self.create('T-B')
        base = 'a' * 40
        cases = (
            ({'workspace_mode': 'worktree', 'task_kind': 'code', 'workspace_base_commit': base},
             'workspace_mode'),
            ({'workspace_mode': 'git_worktree', 'task_kind': 'artifact'}, 'task_kind=code'),
            ({'workspace_mode': 'git_worktree', 'task_kind': 'code'}, 'workspace_base_commit'),
            ({'workspace_mode': 'git_worktree', 'task_kind': 'code', 'workspace_base_commit': 'abc'},
             'workspace_base_commit'),
            ({'workspace_mode': 'directory', 'task_kind': 'code', 'workspace_base_commit': base},
             'workspace_mode=git_worktree'),
        )
        before = self.snapshot()
        for extra, message in cases:
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, message):
                    self.enqueue('T-B', 'second', create=False, **extra)
        # The default fixture request launches in an existing directory. A Git
        # source workspace must be new so an existing tree is never adopted.
        with self.assertRaisesRegex(ValueError, 'must be new'):
            self.enqueue('T-B', 'second', create=False, workspace_mode='git_worktree',
                         task_kind='code', workspace_base_commit=base)
        request = self.request('ATTEMPT-T-B', 'second')
        request['worker'] = dict(request['worker'])
        request['worker']['expected_identity'] = dict(request['worker']['expected_identity'],
                                                      launch_cwd='relative/new-source')
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.enqueue('T-B', 'second', create=False, request=request,
                         workspace_mode='git_worktree', task_kind='code',
                         workspace_base_commit=base)
        self.assertEqual(before, self.snapshot())

    def test_code_dependency_materialization_gate_covers_admission_and_send(self):
        reserved, _, _ = self.finished()
        intent = self.rows('SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?',
                           (reserved['assignment_id'],))[0]
        contract = json.loads(intent['contract_json'])
        contract['task_kind'] = 'code'
        self.mutate('UPDATE v2_scheduler_intent SET contract_json=? WHERE assignment_id=?',
                    (json.dumps(contract), reserved['assignment_id']))
        queued, _ = self.enqueue('T-B', 'second', dependencies=['T-A'])
        base = 'c' * 40
        frozen = str((self.root / 'frozen-source').resolve())
        row = self.rows("SELECT contract_json FROM v2_scheduler_queue WHERE task_id='T-B'")[0]
        downstream = json.loads(row['contract_json'])
        downstream.update(workspace_mode='git_worktree', task_kind='code',
                          workspace_base_commit=base, workspace_path=frozen)
        self.mutate("UPDATE v2_scheduler_queue SET contract_json=? WHERE task_id='T-B'",
                    (json.dumps(downstream),))
        import merge_queue as merge
        with mock.patch.object(merge, 'materialization_gate',
                               return_value={'ready': False, 'reason': 'unmaterialized_dependency'}):
            self.assert_waiting(self.claim(task_id='T-B', expected_revision=queued['revision']),
                                'materialization')
        with mock.patch.object(merge, 'materialization_gate',
                               return_value={'ready': True, 'materialized_from': []}):
            self.assert_waiting(self.claim(task_id='T-B', expected_revision=queued['revision']),
                                'do not cover code dependencies')
        with mock.patch.object(merge, 'materialization_gate',
                               return_value={'ready': True, 'materialized_from': ['T-A']}) as gate:
            reserved_b = self.claim(task_id='T-B', expected_revision=queued['revision'])
            self.assertEqual(reserved_b['status'], 'RESERVED', reserved_b)
            self.assertEqual(gate.call_args.args[1:], (self.project, 'T-B', base))
        # The same proof is revalidated immediately before the external send.
        with mock.patch.object(merge, 'materialization_gate', return_value={'ready': False}):
            with self.assertRaisesRegex(ValueError, 'materialization'):
                self.dispatch(reserved_b['assignment_id'])
        self.assertFalse((self.root / 'worker-second' / 'sends.jsonl').exists())
        with mock.patch.object(merge, 'materialization_gate',
                               return_value={'ready': True, 'materialized_from': ['T-A']}):
            with self.assertRaisesRegex(ValueError, 'reserved managed workspace'):
                self.dispatch(reserved_b['assignment_id'])
        self.assertFalse((self.root / 'worker-second' / 'sends.jsonl').exists())
        self.assertEqual(self.get_assignment(reserved_b['assignment_id'])['holds_slot'], 1)
        with self.db() as con:
            with self.assertRaisesRegex(ValueError, 'git_worktree'):
                scheduler._dependencies(con, self.project, {'task_id': 'T-B', 'dependencies': ['T-A'],
                                                            'workspace_mode': 'directory'})

    def test_cycles_worktree_fanout_and_generation_rejected(self):
        self.create('T-A')
        self.create('T-B')
        self.enqueue('T-A', create=False, dependencies=['T-B'])
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.enqueue('T-B', 'second', create=False, dependencies=['T-A'])
        for option in ({'workspace_mode': 'worktree'}, {'request': self.request('BAD-GEN', generation=True)}):
            with self.subTest(option=option):
                with self.assertRaises(ValueError):
                    self.enqueue('T-B', 'second', create=False, **option)
        self.mutate("UPDATE v2_task SET execution_mode='fanout',fanout_slots=2 WHERE task_id='T-B'")
        with self.assertRaisesRegex(ValueError, 'Fanout'):
            self.enqueue('T-B', 'second', create=False)

    def test_repair_new_generation_preserves_history_and_requires_provenance(self):
        reserved, request, completion = self.finished(verdict='repair')
        old = self.rows('SELECT * FROM v2_assignment')[0]
        with self.assertRaisesRegex(ValueError, 'Repair must reference'):
            self.enqueue(create=False, request=self.request('REPAIR-BAD'))
        review = json.loads(self.rows('SELECT data FROM reviews')[0]['data'])
        repair = self.request('REPAIR-GOOD', parent_task=request['task_id'], parent_review=review)
        queued, _ = self.enqueue(create=False, request=repair)
        fresh = self.claim(task_id='T-A', expected_revision=queued['revision'])
        self.assertEqual(fresh['status'], 'RESERVED', fresh)
        self.assertEqual(fresh['generation'], 2)
        self.assertNotEqual(fresh['assignment_id'], reserved['assignment_id'])
        self.assertEqual(old, self.rows('SELECT * FROM v2_assignment WHERE assignment_id=?', (old['assignment_id'],))[0])
        self.assertEqual(len(self.rows('SELECT * FROM v2_evidence')), 2)

    def test_api_evidence_reuse_preserves_id_and_rejects_assignment_mismatch(self):
        aid, request, completion = self.ready()
        self.review(completion, request, same_connection=True)
        path = Path(request['cwd']) / 'RESULT.md'
        digest = service._file_sha(path)
        self.mutate("INSERT INTO v2_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ('evd-api-existing', 'T-A', None, 'artifact', str(path), digest, digest, path.stat().st_size, 1, 'sealed', 'v2-api', service._now()))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'immutable evidence'):
            self.close(aid, completion)
        self.assertEqual(before, self.snapshot())
        self.mutate("UPDATE v2_evidence SET assignment_id=? WHERE evidence_id='evd-api-existing'", (aid,))
        original = self.rows("SELECT * FROM v2_evidence WHERE evidence_id='evd-api-existing'")[0]
        self.close(aid, completion)
        self.assertEqual(original, self.rows("SELECT * FROM v2_evidence WHERE evidence_id='evd-api-existing'")[0])
        self.assertEqual(len(self.rows('SELECT * FROM v2_evidence')), 2)

    def test_final_send_revalidates_budget_and_freeze_no_external_effect(self):
        self.enqueue()
        reserved = self.claim()
        self.set_limits({'tokens.input': 5})
        with self.assertRaisesRegex(ValueError, 'Budget'):
            self.dispatch(reserved['assignment_id'])
        self.assertEqual(self.get_assignment(reserved['assignment_id'])['send_state'], 'ready')
        self.mutate('UPDATE v2_service_state SET frozen=1')
        with self.assertRaisesRegex(ValueError, 'frozen'):
            self.dispatch(reserved['assignment_id'])
        self.assertFalse((self.root / 'worker-executor' / 'sends.jsonl').exists())
        self.assertTrue(self.get_assignment(reserved['assignment_id'])['holds_slot'])

    def test_registry_alias_cannot_claim_another_actual_worker(self):
        self.create()
        before = self.snapshot()
        # Both imported workers have identical harness/version/host. That is
        # capability context, not proof that they own the same native session.
        with self.assertRaisesRegex(ValueError, 'actual Worker binding'):
            self.enqueue(create=False, agent_id=self.agents['second'])
        self.assertEqual(before, self.snapshot())
        self.enqueue(create=False)
        row = self.rows('SELECT contract_json FROM v2_scheduler_queue')[0]
        contract = json.loads(row['contract_json'])
        contract['agent_id'] = self.agents['second']
        self.mutate('UPDATE v2_scheduler_queue SET contract_json=?', (json.dumps(contract),))
        before = self.snapshot()
        self.assert_waiting(self.next(self.agents['second']), 'actual Worker binding')
        with self.db() as con:
            result = scheduler.claim_for_agent(con, self.project, self.agents['second'])
        self.assert_waiting(result, 'actual Worker binding')
        self.assertEqual(before, self.snapshot())

    def test_enrolled_identity_without_binding_bridge_is_not_execution_identity(self):
        self.create()
        imported = self.rows('SELECT * FROM v2_agent WHERE agent_id=?', (self.agents['executor'],))[0]
        imported.update(agent_id='agt-enrolled-other', handle='enrolled-other', source='enrollment')
        columns = list(imported)
        self.mutate('INSERT INTO v2_agent(' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                    tuple(imported[key] for key in columns))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'binding bridge'):
            self.enqueue(create=False, agent_id=imported['agent_id'])
        self.assertEqual(before, self.snapshot())

    def test_new_controller_version_requires_fresh_queue_approval(self):
        self.enqueue()
        self.mutate('UPDATE bindings SET version=version+1 WHERE id=?', (self.actor[0],))
        self.assert_waiting(self.next(), 'stale')
        with self.assertRaises(ValueError):
            with self.db() as con:
                scheduler.claim_for_agent(con, self.project, self.agents['executor'])
        self.assertEqual(self.rows('SELECT * FROM v2_assignment'), [])

    def test_legacy_dispatch_and_remigration_cannot_take_over_v2_task(self):
        _, request = self.enqueue()
        aid = self.claim()['assignment_id']
        before = self.snapshot()
        with self.assertRaises(ValueError):
            legacy.dispatch(self.state, request, json.dumps(request), *self.actor)
        with self.assertRaises(ValueError):
            service.apply_migration(self.state, *self.actor)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(self.get_assignment(aid)['holds_slot'])

    def test_service_cli_envelopes_replay_and_runtime_cycle(self):
        def cli(command, *extra):
            argv = [sys.executable, '-B', str(Path(service.__file__)), '--state', str(self.state),
                    command, '--actor', self.actor[0], '--version', str(self.actor[1]), *extra]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            with (self.root / 'cli-transcript.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'argv': argv, 'returncode': result.returncode,
                                         'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        def command_file(name, tool, payload):
            path = self.root / (name + '.json')
            path.write_text(json.dumps(self.envelope(tool, payload, 'cli-' + name)), encoding='utf-8')
            return path

        create = command_file('create', 'task.create', {'task_id': 'CLI-TASK', 'title': 'Local CLI cycle'})
        self.assertEqual(cli('command', '--envelope', str(create))['status'], 'CREATED')
        request = self.request('CLI-ATTEMPT')
        queued = command_file('enqueue', 'scheduler.enqueue', {
            'task_id': 'CLI-TASK', 'expected_revision': 1, 'request': request,
            'message': json.dumps({key: request[key] for key in ('task_id', 'nonce', 'cwd')}),
            'agent_id': self.agents['executor'], 'budget': {'tokens.input': 10}})
        self.assertEqual(cli('command', '--envelope', str(queued))['status'], 'QUEUED')
        self.assertEqual(cli('scheduler-queue')['items'][0]['task_id'], 'CLI-TASK')
        claim = command_file('claim', 'scheduler.claim', {'task_id': 'CLI-TASK', 'expected_revision': 2})
        first = cli('command', '--envelope', str(claim))
        aid = first['assignment_id']
        before = self.snapshot()
        self.assertEqual(cli('command', '--envelope', str(claim))['assignment_id'], aid)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(cli('assignment-dispatch', '--assignment', aid, '--expected-epoch', str(self.epoch))['status'], 'SUBMITTED')
        completion = cli('assignment-reconcile', '--assignment', aid, '--expected-epoch', str(self.epoch))
        self.assertEqual(completion['status'], 'REVIEW_READY')
        self.review(completion, request, same_connection=True)
        close = command_file('close', 'assignment.close', {
            'assignment_id': aid, 'expected_revision': self.get_assignment(aid)['revision'],
            'outcome': 'accepted', 'event_id': completion['event_id']})
        self.assertEqual(cli('command', '--envelope', str(close))['status'], 'CLOSED')
        self.assertFalse(self.get_assignment(aid)['holds_slot'])
        self.assertEqual(len((self.root / 'worker-executor' / 'sends.jsonl').read_text().splitlines()), 1)

    def test_existing_frozen_dag_requires_controller_close_before_downstream(self):
        first = self.request('DAG-A', generation=1)
        second = self.request('DAG-B', 'second', generation=1)
        def message(request):
            return json.dumps({key: request[key] for key in ('task_id', 'nonce', 'cwd')})
        graph = {'nodes': [{'id': 'A', 'request': first, 'message': message(first)},
                           {'id': 'B', 'request': second, 'message': message(second), 'needs': ['A']}]}
        legacy.load_dag(self.state, 'test-dag', graph, *self.actor)
        self.enqueue('T-A', request=first, dag_run='test-dag', dag_node='A')
        self.enqueue('T-B', 'second', request=second, dag_run='test-dag', dag_node='B')
        self.assert_waiting(self.claim(task_id='T-B', expected_revision=2), 'DAG dependency')
        aid = self.claim(task_id='T-A', expected_revision=2)['assignment_id']
        self.dispatch(aid)
        completed = self.reconcile(aid)
        self.review(completed, first, same_connection=True)
        self.assert_waiting(self.claim(task_id='T-B', expected_revision=2), 'independent acceptance')
        self.close(aid, completed)
        self.assertEqual(self.claim(task_id='T-B', expected_revision=2)['status'], 'RESERVED')

    def test_attempt_cap_lowered_after_reservation_blocks_send(self):
        self.enqueue(budget={'tokens.input': 10, 'attempts': 1})
        self.set_limits({'tokens.input': 100, 'attempts': 2})
        aid = self.claim()['assignment_id']
        self.mutate('UPDATE v2_budget SET limits_json=? WHERE scope_type=?',
                    (json.dumps({'tokens.input': 100, 'attempts': 0}), 'workflow'))
        with self.assertRaisesRegex(ValueError, 'Attempt cap'):
            self.dispatch(aid)
        self.assertEqual(self.get_assignment(aid)['send_state'], 'ready')
        self.assertFalse((self.root / 'worker-executor' / 'sends.jsonl').exists())

    def test_status_alias_cannot_turn_disconnect_into_quiescence(self):
        request = self.request(status_map={'disconnected': 'idle', 'unknown': 'idle', 'cancel_requested': 'idle'})
        self.enqueue(request=request)
        for status in ('disconnected', 'unknown', 'cancel_requested'):
            with self.subTest(status=status):
                self.status(status=status)
                self.assert_waiting(self.claim(), 'not idle')
        self.status(status='idle')
        aid = self.claim()['assignment_id']
        self.dispatch(aid)
        completion = self.reconcile(aid)
        self.review(completion, request, same_connection=True)
        self.status(status='disconnected')
        with self.assertRaisesRegex(ValueError, 'authoritatively idle'):
            self.close(aid, completion)
        self.assertTrue(self.get_assignment(aid)['holds_slot'])

    def test_frozen_dag_omitted_generation_preserves_template_hash(self):
        request = self.request('DAG-DEFAULT-GEN')
        message = json.dumps({key: request[key] for key in ('task_id', 'nonce', 'cwd')})
        graph = {'nodes': [{'id': 'A', 'request': request, 'message': message}]}
        legacy.load_dag(self.state, 'default-generation', graph, *self.actor)
        before = self.rows('SELECT * FROM dag_runs')
        self.enqueue(request=request, dag_run='default-generation', dag_node='A')
        contract = json.loads(self.rows('SELECT contract_json FROM v2_scheduler_queue')[0]['contract_json'])
        self.assertEqual(control.digest(contract['request']), control.digest(request))
        result = self.claim()
        self.assertEqual(result['status'], 'RESERVED', result)
        self.assertEqual(result['generation'], 1)
        self.assertEqual(self.dispatch(result['assignment_id'])['status'], 'SUBMITTED')
        self.assertEqual(self.reconcile(result['assignment_id'])['status'], 'REVIEW_READY')
        self.assertEqual(before, self.rows('SELECT * FROM dag_runs'))


class SchedulerCollaborationTests(unittest.TestCase):
    """Real sidecar + scheduler transactions and local subprocess integration."""
    extra_roles = ('third',)
    fixture_concurrency = 3
    spec = PrioritySchedulerTests.spec
    status = PrioritySchedulerTests.status
    request = PrioritySchedulerTests.request
    db = PrioritySchedulerTests.db
    envelope = PrioritySchedulerTests.envelope
    command = PrioritySchedulerTests.command
    create = PrioritySchedulerTests.create
    enqueue = PrioritySchedulerTests.enqueue
    claim = PrioritySchedulerTests.claim
    dispatch = PrioritySchedulerTests.dispatch
    reconcile = PrioritySchedulerTests.reconcile
    get_assignment = PrioritySchedulerTests.get_assignment
    close = PrioritySchedulerTests.close
    rows = PrioritySchedulerTests.rows
    mutate = PrioritySchedulerTests.mutate
    snapshot = PrioritySchedulerTests.snapshot
    set_limits = PrioritySchedulerTests.set_limits
    next = PrioritySchedulerTests.next
    assert_waiting = PrioritySchedulerTests.assert_waiting

    def setUp(self):
        import collaboration_runtime
        self.collaboration = collaboration_runtime
        PrioritySchedulerTests.setUp(self)
        with self.db() as con:
            self.collaboration.create_collaboration_schema(con)
        self.principal = scheduler._principal(self.bindings['planner'])

    def configure(self, task='T-A', mode='fanout', max_attempts=8, max_repairs=2):
        self.create(task)
        slots = [(0, 'executor')] if mode == 'single' else [(1, 'executor'), (2, 'second')]
        if mode == 'ensemble':
            slots.append((0, 'third'))
        payload = {'task_id': task, 'expected_revision': self.task_revision(task), 'mode': mode,
                   'slots': [{'slot_id': slot, 'agent_id': self.agents[role]} for slot, role in slots],
                   'max_attempts': max_attempts, 'max_repairs': max_repairs,
                   'verifier': {'argv': [sys.executable, '-B', '-c', 'print("verified local fixture")'],
                                'timeout_seconds': 3, 'max_output_bytes': 4096, 'max_runs_per_assignment': 2}}
        with self.db() as con:
            return self.collaboration.apply_command(con, self.project, self.principal, 'collaboration_configure', payload)

    def task_revision(self, task='T-A'):
        return self.rows('SELECT revision FROM v2_task WHERE task_id=?', (task,))[0]['revision']

    def policy_revision(self, task='T-A'):
        return self.rows('SELECT revision FROM v2_collaboration_policy WHERE task_id=?', (task,))[0]['revision']

    def slot_queue(self, slot=1, role='executor', task='T-A', request=None, **extra):
        request = request or self.request('SLOT-' + str(slot) + '-' + uuid.uuid4().hex[:10], role)
        return self.enqueue(task, role, create=False, request=request, slot_id=slot,
                            expected_collaboration_revision=self.policy_revision(task), **extra)

    def slot_claim(self, slot=1, task='T-A'):
        result = self.claim(task_id=task, expected_revision=self.task_revision(task), slot_id=slot)
        self.assertEqual(result['status'], 'RESERVED', result)
        self.assertEqual(result['slot_id'], slot)
        return result

    def finish_slot(self, admission):
        aid = admission['assignment_id']
        self.assertEqual(self.dispatch(aid)['status'], 'SUBMITTED')
        result = self.reconcile(aid)
        self.assertEqual(result['status'], 'REVIEW_READY', result)
        return result

    def review(self, completion, request, verdict='accept'):
        return PrioritySchedulerTests.review(self, completion, request, verdict, same_connection=True)

    def collaborate(self, tool, task='T-A', **payload):
        payload.update(task_id=task, expected_revision=self.policy_revision(task))
        with self.db() as con:
            scheduler._guard(con, self.project, self.actor[0])
            return self.collaboration.apply_command(con, self.project, self.principal, tool, payload)

    def verify(self, admission, completion):
        result = self.collaborate('collaboration_verify', assignment_id=admission['assignment_id'], event_id=completion['event_id'])
        self.assertEqual(result['status'], 'READY')
        con = service._connect(service._db_path(self.state))
        try:
            receipt = self.collaboration.run_verification(con, self.project, self.principal, result['verification_id'])
        finally:
            con.close()
        (self.root / (result['verification_id'] + '.json')).write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        self.assertEqual(receipt['status'], 'RECORDED', receipt)
        return receipt

    def propose(self, admission, completion, **extra):
        return self.collaborate('collaboration_propose', assignment_id=admission['assignment_id'],
                                event_id=completion['event_id'], **extra)

    def terminate(self, admission, request):
        path = Path(request['cwd']) / 'TERMINATED.md'
        path.write_text(request['task_id'] + ' ' + request['nonce'] + '\nOutcome: terminated\n', encoding='utf-8')
        return self.close(admission['assignment_id'], outcome='cancelled', evidence_path=str(path))

    def test_multi_slot_reverse_admission_and_sibling_progress_survive_restart(self):
        self.configure()
        first, _ = self.slot_queue()
        original = self.rows('SELECT * FROM v2_scheduler_queue WHERE slot_id=1')[0]
        self.slot_queue(2, 'second')
        before = self.snapshot()
        self.assertEqual(self.next(self.agents['executor'])['candidate']['slot_id'], 1)
        self.assertEqual(self.next(self.agents['second'])['candidate']['slot_id'], 2)
        self.assertEqual(before, self.snapshot())
        second = self.slot_claim(2)
        self.assertEqual(second['generation'], 2)
        self.finish_slot(second)
        before = self.snapshot()
        self.assertEqual(self.next(self.agents['executor'])['candidate']['slot_id'], 1)
        self.assertEqual(before, self.snapshot())
        first = self.slot_claim(1)
        self.assertEqual(first['generation'], 1)
        self.finish_slot(first)
        self.assertEqual(self.rows('SELECT enqueued_at FROM v2_scheduler_queue WHERE slot_id=1')[0]['enqueued_at'], original['enqueued_at'])
        for table in ('v2_assignment', 'v2_run_link', 'tasks', 'dispatches', 'v2_legacy_map', 'v2_scheduler_intent'):
            self.assertEqual(len(self.rows('SELECT * FROM ' + table)), 2, table)
        self.assertEqual(len(self.rows('SELECT * FROM v2_task')), 1)
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 6)
        for role in ('executor', 'second'):
            self.assertEqual(len((self.root / ('worker-' + role) / 'sends.jsonl').read_text().splitlines()), 1)

    def test_policy_cas_strict_slots_and_worker_claim_cannot_expand_contract(self):
        self.configure()
        for value in (True, -1, 9, 1.0, '1'):
            with self.subTest(slot=value):
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.slot_queue(value)
                self.assertEqual(before, self.snapshot())
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'expected_revision'):
            self.enqueue(create=False, slot_id=1)
        self.assertEqual(before, self.snapshot())
        self.slot_queue()
        stale = self.task_revision()
        self.slot_queue(2, 'second')
        before = self.snapshot()
        with self.db() as con:
            with self.assertRaises(ValueError):
                scheduler.claim_for_agent(con, self.project, self.agents['second'],
                                          {'task_id': 'T-A', 'expected_revision': stale, 'slot_id': 1})
            with self.assertRaises(ValueError):
                scheduler.claim_for_agent(con, self.project, self.agents['executor'], {'resources': {'gpu': 99}})
        self.assertEqual(before, self.snapshot())
        with self.db() as con:
            result = scheduler.claim_for_agent(con, self.project, self.agents['second'],
                                              {'task_id': 'T-A', 'expected_revision': self.task_revision(), 'slot_id': 2})
        self.assertEqual(result['slot_id'], 2)

    def test_sibling_progress_does_not_heal_external_revision_drift(self):
        self.configure()
        self.slot_queue()
        self.slot_queue(2, 'second')
        first = self.slot_claim()
        self.mutate("UPDATE v2_task SET revision=revision+1 WHERE task_id='T-A'")
        before = self.snapshot()
        self.assert_waiting(self.next(self.agents['second']), 'revision')
        self.assertEqual(before, self.snapshot())
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.dispatch(first['assignment_id'])
        self.command('scheduler.priority', {'task_id': 'T-A', 'expected_revision': self.task_revision(),
                                           'priority': 'P2', 'reason': 'Queue urgency only'})
        self.assert_waiting(self.next(self.agents['second']), 'revision')

    def test_live_claim_and_dispatch_keep_all_native_gates(self):
        self.configure()
        self.slot_queue()
        self.slot_queue(2, 'second')
        self.status(status='disconnected')
        before = self.snapshot()
        self.assert_waiting(self.claim(agent_id=self.agents['executor']), 'idle')
        self.assertEqual(before, self.snapshot())
        self.status(status='idle')
        first = self.slot_claim()
        self.finish_slot(self.slot_claim(2))
        self.mutate("UPDATE v2_agent_capability SET level='unknown' WHERE agent_id=? AND capability_key='send'", (self.agents['executor'],))
        with self.assertRaisesRegex(ValueError, 'capability'):
            self.dispatch(first['assignment_id'])
        self.assertFalse((self.root / 'worker-executor' / 'sends.jsonl').exists())
        self.assertTrue(self.get_assignment(first['assignment_id'])['holds_slot'])

    def test_budget_and_late_native_failure_roll_back_ticket_assignment_and_claims(self):
        self.configure()
        self.slot_queue()
        self.slot_queue(2, 'second')
        with self.db() as con:
            con.execute("CREATE TRIGGER fail_collaboration_late BEFORE INSERT ON v2_scheduler_intent "
                        "BEGIN SELECT CASE WHEN (SELECT count(*) FROM v2_budget_reservation)=3 "
                        "AND (SELECT count(*) FROM v2_assignment)=1 AND (SELECT count(*) FROM dispatches)=1 "
                        "THEN RAISE(ABORT,'collaboration_late') ELSE RAISE(ABORT,'did_not_reach_late_stage') END; END")
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'collaboration_late'):
            self.claim()
        self.assertEqual(before, self.snapshot())
        self.mutate('DROP TRIGGER fail_collaboration_late')
        self.set_limits({'tokens.input': 15})
        self.slot_claim()
        before = self.snapshot()
        self.assert_waiting(self.claim(agent_id=self.agents['second']), 'cap')
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment')), 1)
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 3)

    def test_ticket_bytes_and_common_dependency_scope_cannot_be_replaced(self):
        self.configure()
        self.create('UPSTREAM')
        self.slot_queue(dependencies=['UPSTREAM'])
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'dependencies'):
            self.slot_queue(2, 'second')
        self.assertEqual(before, self.snapshot())
        self.slot_queue(2, 'second', dependencies=['UPSTREAM'])
        self.assert_waiting(self.next(), 'acceptance')
        row = self.rows('SELECT contract_json FROM v2_scheduler_queue WHERE slot_id=1')[0]
        contract = json.loads(row['contract_json'])
        contract['dependencies'] = []
        self.mutate('UPDATE v2_scheduler_queue SET contract_json=? WHERE slot_id=1', (json.dumps(contract),))
        before = self.snapshot()
        self.assert_waiting(self.next(self.agents['executor']), 'immutable ticket')
        self.assertEqual(before, self.snapshot())

    def test_selected_winner_survives_loser_completion_and_controller_close(self):
        self.configure()
        _, first_request = self.slot_queue()
        _, second_request = self.slot_queue(2, 'second')
        first, second = self.slot_claim(), self.slot_claim(2)
        first_done = self.finish_slot(first)
        self.assertEqual(self.dispatch(second['assignment_id'])['status'], 'SUBMITTED')
        self.review(first_done, first_request)
        self.verify(first, first_done)
        self.propose(first, first_done)
        self.close(first['assignment_id'], first_done)
        accepted = self.rows("SELECT accepted_evidence_id FROM v2_task WHERE task_id='T-A'")[0]['accepted_evidence_id']
        self.assertTrue(accepted)
        self.assertTrue(self.get_assignment(second['assignment_id'])['holds_slot'])
        second_done = self.reconcile(second['assignment_id'])
        self.assertEqual(second_done['status'], 'REVIEW_READY')
        self.review(second_done, second_request, 'reject')
        result = self.close(second['assignment_id'], second_done, 'rejected')
        self.assertEqual(result['task_state'], 'accepted')
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-A'")[0]
        self.assertEqual(task['task_state'], 'accepted')
        self.assertEqual(task['accepted_evidence_id'], accepted)
        self.assertEqual({r['state'] for r in self.rows('SELECT * FROM v2_budget_reservation')}, {'held'})

    def test_two_passing_reviews_require_unique_verified_controller_selection(self):
        self.configure()
        _, r1 = self.slot_queue()
        _, r2 = self.slot_queue(2, 'second')
        a1, a2 = self.slot_claim(), self.slot_claim(2)
        e1, e2 = self.finish_slot(a1), self.finish_slot(a2)
        self.review(e1, r1)
        self.review(e2, r2)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'selected'):
            self.close(a1['assignment_id'], e1)
        self.assertEqual(before, self.snapshot())
        self.verify(a1, e1)
        self.propose(a1, e1)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'selected'):
            self.close(a2['assignment_id'], e2)
        self.assertEqual(before, self.snapshot())
        self.close(a1['assignment_id'], e1)
        self.assertTrue(self.get_assignment(a2['assignment_id'])['holds_slot'])

    def test_winner_proposal_fences_unsent_sibling_without_releasing_it(self):
        self.configure()
        _, request = self.slot_queue()
        self.slot_queue(2, 'second')
        first, second = self.slot_claim(), self.slot_claim(2)
        done = self.finish_slot(first)
        self.review(done, request)
        self.verify(first, done)
        self.propose(first, done)
        with self.assertRaisesRegex(ValueError, 'winner'):
            self.dispatch(second['assignment_id'])
        self.assertTrue(self.get_assignment(second['assignment_id'])['holds_slot'])
        self.assertEqual(self.get_assignment(second['assignment_id'])['send_state'], 'ready')
        self.assertFalse((self.root / 'worker-second' / 'sends.jsonl').exists())

    def test_cancelled_ticket_drops_only_pending_candidate(self):
        self.configure()
        first, _ = self.slot_queue()
        self.slot_queue(2, 'second')
        with self.db() as con:
            self.collaboration.apply_command(con, self.project, self.principal, 'collaboration_cancel_ticket',
                {'ticket_id': first['collaboration_ticket_id'], 'expected_revision': self.policy_revision(), 'reason': 'Owner cancels unsent approval'})
        before = self.snapshot()
        self.assertEqual(self.next(self.agents['executor'])['status'], 'WAITING')
        self.assertEqual(self.next(self.agents['second'])['candidate']['slot_id'], 2)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.rows('SELECT * FROM v2_collaboration_ticket')), 2)
        self.slot_claim(2)

    def test_single_policy_uses_same_runtime_with_explicit_selection_gate(self):
        self.configure(mode='single')
        _, request = self.slot_queue(0)
        admission = self.slot_claim(0)
        done = self.finish_slot(admission)
        self.review(done, request)
        self.verify(admission, done)
        self.propose(admission, done)
        self.assertEqual(self.close(admission['assignment_id'], done)['task_state'], 'accepted')
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment')), 1)
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 3)

    def race_claims(self, agents):
        barrier = self.root / 'collaboration-start.flag'
        program = ('import json,sys; from test_priority_scheduler import child_claim; '
                   'print(json.dumps(child_claim(sys.argv[1],sys.argv[2],int(sys.argv[3]),'
                   'int(sys.argv[4]),sys.argv[5],json.loads(sys.argv[6]))))')
        children = []
        for agent in agents:
            argv = [sys.executable, '-B', '-c', program, str(self.state), self.actor[0], str(self.actor[1]),
                    str(self.epoch), str(barrier), json.dumps({'agent_id': agent})]
            process = subprocess.Popen(argv, cwd=Path(__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            children.append((process, argv))
        try:
            barrier.write_text('go', encoding='ascii')
            results = []
            for process, argv in children:
                stdout, stderr = process.communicate(timeout=30)
                (self.root / ('collaboration-race-%d.json' % len(results))).write_text(
                    json.dumps({'argv': argv, 'returncode': process.returncode, 'stdout': stdout, 'stderr': stderr}), encoding='utf-8')
                self.assertEqual(process.returncode, 0, stderr)
                results.append(json.loads(stdout))
            return results
        finally:
            for process, _ in children:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)

    def test_real_process_race_same_slot_never_duplicates_admission(self):
        self.configure()
        self.slot_queue()
        self.slot_queue(2, 'second')
        results = self.race_claims([self.agents['executor']] * 2)
        self.assertCountEqual([r['status'] for r in results], ['RESERVED', 'WAITING'])
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment')), 1)
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 3)
        self.assertEqual(self.next(self.agents['second'])['candidate']['slot_id'], 2)

    def test_real_process_race_distinct_slots_reserves_both_once(self):
        self.configure()
        self.slot_queue()
        self.slot_queue(2, 'second')
        results = self.race_claims([self.agents['executor'], self.agents['second']])
        self.assertEqual([r['status'] for r in results], ['RESERVED', 'RESERVED'])
        self.assertEqual({r['slot_id'] for r in results}, {1, 2})
        self.assertEqual({r['generation'] for r in results}, {1, 2})
        for table in ('v2_assignment', 'v2_run_link', 'tasks', 'dispatches', 'v2_legacy_map', 'v2_scheduler_intent'):
            self.assertEqual(len(self.rows('SELECT * FROM ' + table)), 2, table)
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 6)

    def test_loser_cancellation_keeps_other_slot_and_directory_claim(self):
        self.configure()
        _, first_request = self.slot_queue(resources={'gpu': 1})
        _, second_request = self.slot_queue(2, 'second')
        first, second = self.slot_claim(), self.slot_claim(2)
        self.status('executor', status='disconnected')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'idle'):
            self.terminate(first, first_request)
        self.assertEqual(before, self.snapshot())
        self.terminate(second, second_request)
        self.assertTrue(self.get_assignment(first['assignment_id'])['holds_slot'])
        self.assertFalse(self.get_assignment(second['assignment_id'])['holds_slot'])
        self.assertEqual(self.rows("SELECT task_state FROM v2_task WHERE task_id='T-A'")[0]['task_state'], 'active')
        self.enqueue('RESOURCE-WAITER', 'third', resources={'gpu': 1})
        self.assert_waiting(self.claim(agent_id=self.agents['third']), 'resource capacity')

    def test_alias_of_any_competitor_cannot_review_other_slot(self):
        self.configure()
        _, request = self.slot_queue()
        admission = self.slot_claim()
        completion = self.finish_slot(admission)
        # Corrupt the controller's observed physical identity to a different
        # frozen competitor. Native current-worker-only self-review misses it.
        raw = self.rows('SELECT data FROM bindings WHERE id=?', (self.actor[0],))[0]['data']
        alias = json.loads(raw)
        alias['observed_identity'] = self.bindings['second']['observed_identity']
        self.mutate('UPDATE bindings SET data=? WHERE id=?', (json.dumps(alias), self.actor[0]))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'independent'):
            self.review(completion, request)
        self.assertEqual(before, self.snapshot())
        self.mutate('UPDATE bindings SET data=? WHERE id=?', (raw, self.actor[0]))
        self.review(completion, request)

    def test_repair_reuses_original_review_and_closed_slot_with_global_generation(self):
        self.configure(max_repairs=1)
        _, request = self.slot_queue()
        self.slot_queue(2, 'second')
        first, second = self.slot_claim(), self.slot_claim(2)
        completion = self.finish_slot(first)
        self.review(completion, request, 'repair')
        self.close(first['assignment_id'], completion, 'repair_required')
        self.assertTrue(self.get_assignment(second['assignment_id'])['holds_slot'])
        taskbook = self.root / 'TASK.md'
        taskbook.write_text('Repair the same objective; isolated test.', encoding='utf-8')
        followup = legacy.follow_up(self.state, completion['event_id'], 'REPAIR-COMPETITOR', taskbook,
                                   actor=self.actor[0], version=self.actor[1], context='main',
                                   source='Owner-approved local regression fixture')
        repair = followup['request']
        # Task-wide generation is assigned by the queue, not legacy per-attempt numbering.
        repair.pop('generation', None)
        old = self.get_assignment(first['assignment_id'])
        self.slot_queue(request=repair)
        fresh = self.slot_claim()
        self.assertEqual(fresh['generation'], 3)
        self.assertEqual(old, self.get_assignment(first['assignment_id']))
        self.assertTrue(self.get_assignment(second['assignment_id'])['holds_slot'])
        self.assertEqual(len(self.rows('SELECT * FROM v2_task')), 1)
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment')), 3)
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 9)
        self.assertEqual(self.dispatch(fresh['assignment_id'])['status'], 'SUBMITTED')

    def test_ensemble_only_synthesis_with_distinct_contributors_can_accept(self):
        self.configure(mode='ensemble')
        _, r1 = self.slot_queue()
        self.slot_queue(2, 'second')
        _, r0 = self.slot_queue(0, 'third')
        a1, a2, a0 = self.slot_claim(), self.slot_claim(2), self.slot_claim(0)
        e1, e2, e0 = self.finish_slot(a1), self.finish_slot(a2), self.finish_slot(a0)
        self.review(e1, r1)
        self.verify(a1, e1)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.propose(a1, e1, contributors=[e1['event_id'], e2['event_id']])
        self.assertEqual(before, self.snapshot())
        self.review(e0, r0)
        self.verify(a0, e0)
        self.propose(a0, e0, contributors=[e1['event_id'], e2['event_id']])
        self.assertEqual(self.close(a0['assignment_id'], e0)['task_state'], 'accepted')
        self.assertTrue(self.get_assignment(a1['assignment_id'])['holds_slot'])
        self.assertTrue(self.get_assignment(a2['assignment_id'])['holds_slot'])

    def test_collaborative_send_crash_is_not_retried_and_does_not_block_sibling(self):
        self.configure()
        self.slot_queue()
        self.slot_queue(2, 'second')
        first, second = self.slot_claim(), self.slot_claim(2)
        program = '''import os,sys
import priority_scheduler as s
class Crash(s.monitor.Transport):
    def send(self,spec,message):
        super().send(spec,message)
        os._exit(73)
con=s.service._connect(s.service._db_path(sys.argv[1]),readonly=True)
_,_,contract=s._intent(con,sys.argv[6],sys.argv[2])
con.close()
s.dispatch_assignment(sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]),expected_epoch=int(sys.argv[5]),transport=Crash(contract['request']))
'''
        process = subprocess.run([sys.executable, '-B', '-c', program, str(self.state), first['assignment_id'],
                                  self.actor[0], str(self.actor[1]), str(self.epoch), self.project],
                                 cwd=Path(__file__).parent, capture_output=True, text=True, timeout=30)
        (self.root / 'collaboration-crash.json').write_text(json.dumps({'argv': process.args, 'returncode': process.returncode,
                                                                      'stdout': process.stdout, 'stderr': process.stderr}), encoding='utf-8')
        self.assertEqual(process.returncode, 73, process.stderr)
        self.assertEqual(self.get_assignment(first['assignment_id'])['send_state'], 'attempted')
        self.assertEqual(self.dispatch(first['assignment_id'])['status'], 'ALREADY_ATTEMPTED')
        self.finish_slot(second)
        self.assertEqual(self.reconcile(first['assignment_id'])['status'], 'REVIEW_READY')
        self.assertEqual(len((self.root / 'worker-executor' / 'sends.jsonl').read_text().splitlines()), 1)
        self.assertTrue(self.get_assignment(first['assignment_id'])['holds_slot'])
        self.assertTrue(self.get_assignment(second['assignment_id'])['holds_slot'])

    def test_selected_evidence_tamper_rolls_back_controller_close(self):
        self.configure()
        _, request = self.slot_queue()
        admission = self.slot_claim()
        done = self.finish_slot(admission)
        self.review(done, request)
        self.verify(admission, done)
        self.propose(admission, done)
        path = Path(request['cwd']) / 'RESULT.md'
        original = path.read_bytes()
        path.write_bytes(original + b'changed after selection')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.close(admission['assignment_id'], done)
        self.assertEqual(before, self.snapshot())
        path.write_bytes(original)
        self.close(admission['assignment_id'], done)


if __name__ == '__main__':
    unittest.main()
