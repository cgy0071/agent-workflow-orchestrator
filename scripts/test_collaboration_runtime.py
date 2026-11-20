"""Focused real SQLite/process tests; synthetic adapters, no provider claims.

The attach helper models the parent's future same-transaction persistence seam.
It is fixture wiring, not an assertion that parent fanout dispatch is integrated.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import awo_service as service
import budget
import collaboration_runtime as col
import monitor_task as monitor
import priority_scheduler as scheduler
import test_priority_scheduler as base
import workflow_control as control
import workflow_scheduler as legacy


# Fixture verifier: publish the PID atomically so an observer can only see the
# final path absent or complete. AWO_EFFECT_PAUSE_FILE is a test-only seam that
# pauses the writer between the temporary write and the replace.
ATOMIC_EFFECT_CODE = "\n".join([
    "from pathlib import Path",
    "import os, time",
    "p = Path('effect.txt'); q = Path('effect.txt.part')",
    "q.write_text(str(os.getpid()))",
    "gate = os.environ.get('AWO_EFFECT_PAUSE_FILE')",
    "while gate and not Path(gate).exists():",
    "    time.sleep(0.01)",
    "os.replace(q, p)",
    "time.sleep(20)",
])


def owned_alive(pid):
    """Whether this fixture's actual PID is still running, without name matching."""
    if os.name == "nt":
        import ctypes
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_owned(pid, timeout=5.0):
    """Bounded SIGTERM and wait for the exact PID emitted by this fixture."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not owned_alive(pid):
            return True
        time.sleep(0.05)
    return False


def race_child(path, project, principal, operation, payload):
    """Independent process, actual SQLite lock/transaction, no mock transport."""
    con = sqlite3.connect(path, timeout=15)
    con.execute('PRAGMA foreign_keys=ON')
    try:
        con.execute('BEGIN IMMEDIATE')
        if operation == 'admit':
            value = col.admit_in_transaction(con, project, principal, payload['ticket_id'])
        elif operation == 'prepare':
            value = col.prepare_enqueue(con, project, principal, **payload)
        else:
            value = col.apply_command(con, project, principal, operation, payload)
        con.commit()
        return {'ok': True, 'result': value}
    except (ValueError, sqlite3.Error) as exc:
        con.rollback()
        return {'ok': False, 'code': getattr(exc, 'code', type(exc).__name__), 'error': str(exc)}
    finally:
        con.close()


class CollaborationTests(unittest.TestCase):
    # Reuse fixture utilities, without inheriting the scheduler's test methods.
    spec = base.PrioritySchedulerTests.spec
    status = base.PrioritySchedulerTests.status
    request = base.PrioritySchedulerTests.request
    db = base.PrioritySchedulerTests.db
    envelope = base.PrioritySchedulerTests.envelope
    create = base.PrioritySchedulerTests.create
    rows = base.PrioritySchedulerTests.rows
    mutate = base.PrioritySchedulerTests.mutate
    snapshot = base.PrioritySchedulerTests.snapshot
    set_limits = base.PrioritySchedulerTests.set_limits

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='awo-collaboration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.worker_script = self.root / 'local_worker.py'
        self.worker_script.write_text(base.WORKER, encoding='utf-8', newline='\n')
        self.config = {'roles': {r: {} for r in ('planner', 'executor', 'second', 'third')},
                       'workflow': {'reviewer_role': 'planner',
                                    'transitions': {'accept': 'next', 'repair': 'repair', 'reject': 'stop'}},
                       'scheduler': {'max_concurrency': 4}}
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
            col.create_collaboration_schema(con)
        self.project = service.service_status(self.state)['project_id']
        self.agents = {r: service._agent_id_for_binding(b['instance_id']) for r, b in self.bindings.items()}
        self.epoch = service.set_enabled(self.state, *self.actor, True)['epoch']
        self.principal = dict(role='controller', agent_id=self.agents['planner'], principal_id='controller-test')
        self.create()

    def revision(self, task='T-A'):
        return self.rows('SELECT revision FROM v2_collaboration_policy WHERE task_id=?', (task,))[0]['revision']

    def configure(self, mode='fanout', task='T-A', max_attempts=6, max_repairs=2, code='print("verified")', **verifier):
        roles = [(0, 'executor')] if mode == 'single' else [(1, 'executor'), (2, 'second')]
        if mode == 'ensemble':
            roles.append((0, 'third'))
        payload = dict(task_id=task, expected_revision=1, mode=mode,
                       slots=[dict(slot_id=s, agent_id=self.agents[r]) for s, r in roles],
                       max_attempts=max_attempts, max_repairs=max_repairs,
                       verifier=dict(argv=[sys.executable, '-B', '-c', code], timeout_seconds=3,
                                     max_output_bytes=4096, max_runs_per_assignment=2, **{}))
        payload['verifier'].update(verifier)
        with self.db() as con:
            return col.apply_command(con, self.project, self.principal, 'collaboration_configure', payload)

    def contract(self, role='executor', attempt=None, task='T-A', request=None):
        request = request or self.request(attempt or 'ATTEMPT-' + uuid.uuid4().hex[:12], role)
        return dict(task_id=task, agent_id=self.agents[role], request=request,
                    message=json.dumps({k: request[k] for k in ('task_id', 'nonce', 'cwd')}),
                    budget={'tokens.input': 10}, approved_controller_id=self.actor[0],
                    approved_controller_version=self.actor[1], approved_epoch=self.epoch)

    def prepare(self, slot=1, role='executor', task='T-A', contract=None):
        with self.db() as con:
            return col.prepare_enqueue(con, self.project, self.principal, task, slot,
                                       contract or self.contract(role, task=task), self.revision(task))

    def attach(self, con, admitted):
        """TEST FIXTURE of parent persistence seam; actual native tables."""
        c = admitted['contract']
        r, aid = c['request'], admitted['assignment_id']
        now = service._now()
        data = dict(request=r, worker_binding=c['worker_binding'], controller=self.bindings['planner'])
        con.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',
                    (r['task_id'], r['nonce'], control.digest(r), r['worker']['role'], 'running', json.dumps(data)))
        con.execute('INSERT INTO v2_legacy_map(legacy_task_id,v2_task_id,v2_assignment_id,nonce,request_sha256,'
                    'generation,source) VALUES (?,?,?,?,?,?,?)',
                    (r['task_id'], c['task_id'], aid, r['nonce'], control.digest(r), r['generation'], 'collaboration-fixture'))
        legacy.reserve(con, r, self.bindings['planner'], dict(generation=r['generation'], resources={}))
        con.execute("INSERT INTO v2_scheduler_intent(assignment_id,run_id,attempt_id,contract_json,"
                    "controller_id,controller_version,controller_epoch,send_state,created_at) VALUES (?,?,?,?,?,?,?,'ready',?)",
                    (aid, admitted['run_id'], r['task_id'], json.dumps(c), self.actor[0], self.actor[1], self.epoch, now))
        con.execute("UPDATE v2_task SET task_state='active',runtime_owner='v2' WHERE task_id=?", (c['task_id'],))

    def admit(self, ticket):
        with self.db() as con:
            result = col.admit_in_transaction(con, self.project, self.principal, ticket['ticket_id'])
            self.attach(con, result)
            return result

    def complete(self, admission):
        r = admission['contract']['request']
        # Explicit synthetic local worker. The process writes real task-bound files.
        argv = [sys.executable, '-B', str(self.worker_script),
                r['worker']['expected_identity']['launch_cwd'], 'send',
                json.dumps({k: r[k] for k in ('task_id', 'nonce', 'cwd')})]
        subprocess.run(argv, check=True, capture_output=True, timeout=10)
        with self.db() as con:
            con.execute("UPDATE v2_scheduler_intent SET send_state='submitted' WHERE assignment_id=?",
                        (admission['assignment_id'],))
            result = scheduler.reconcile_in_transaction(con, self.project, self.actor[0], admission['assignment_id'])
            self.assertEqual(result['status'], 'REVIEW_READY', result)
            return result['event_id']

    def ready(self, slot=1, role='executor'):
        a = self.admit(self.prepare(slot, role))
        return a, self.complete(a)

    def command(self, command, task='T-A', **payload):
        payload.update(task_id=task, expected_revision=self.revision(task))
        with self.db() as con:
            return col.apply_command(con, self.project, self.principal, command, payload)

    def verify(self, a, event):
        intent = self.command('collaboration_verify', assignment_id=a['assignment_id'], event_id=event)
        return self.run_intent(intent['verification_id'])

    def run_intent(self, verification_id):
        con = service._connect(service._db_path(self.state))
        try:
            return col.run_verification(con, self.project, self.principal, verification_id)
        finally:
            con.close()

    def propose(self, a, event, **extra):
        return self.command('collaboration_propose', assignment_id=a['assignment_id'], event_id=event, **extra)

    def review(self, a, event, verdict='accept'):
        r = a['contract']['request']
        path = Path(r['cwd']) / 'REVIEW.md'
        path.write_text(r['task_id'] + ' ' + r['nonce'] + '\nVerdict: ' + verdict + '\n', encoding='utf-8')
        with self.db() as con:
            col.review_guard(con, self.project, self.principal, 'T-A')
            return scheduler.review_in_transaction(con, self.project, *self.actor, event, str(path), verdict)

    def close_repair(self, a, event):
        # Existing single-Task native close, including actual adapter quiescence.
        # Parent must wire closure_projection when closing parallel competitors.
        with self.db() as con:
            revision = con.execute('SELECT revision FROM v2_assignment WHERE assignment_id=?',
                                   (a['assignment_id'],)).fetchone()[0]
            return scheduler.apply_command(con, self.project, self.actor[0], 'assignment.close',
                                           dict(assignment_id=a['assignment_id'], expected_revision=revision,
                                                outcome='repair_required', event_id=event))

    def followup(self, a, event, attempt=None):
        book = self.root / 'TASK.md'
        book.write_text('Repair the existing objective; local fixture only.\n', encoding='utf-8')
        return legacy.follow_up(self.state, event, attempt or 'REPAIR-' + uuid.uuid4().hex[:12], book,
                                actor=self.actor[0], version=self.actor[1], context='main',
                                source='Owner-authorized regression fixture')

    def repair_contract(self, followup):
        c = self.contract(request=followup['request'])
        c['message'] = followup['message']
        return c

    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(col.CollaborationError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def race(self, operation, payload):
        data = dict(path=str(service._db_path(self.state)), project=self.project, principal=self.principal,
                    operation=operation, payload=payload)
        code = 'import json,sys; from test_collaboration_runtime import race_child; print(json.dumps(race_child(**json.loads(sys.argv[1]))))'
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
        processes = [subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-c', code, json.dumps(data)],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) for _ in range(2)]
        outcomes = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=25)
            self.assertEqual(process.returncode, 0, stderr.decode('utf-8'))
            outcomes.append(json.loads(stdout))
        self.assertEqual(sum(r['ok'] for r in outcomes), 1, outcomes)
        return outcomes

    def test_schema_and_commands_require_caller_transaction(self):
        con = sqlite3.connect(service._db_path(self.state))
        self.addCleanup(con.close)
        self.assert_code('transaction_required', col.create_collaboration_schema, con)
        self.assert_code('transaction_required', col.apply_command, con, self.project, self.principal, 'bad', {})
        con.execute('BEGIN IMMEDIATE')
        col.create_collaboration_schema(con)
        self.assertTrue(con.in_transaction)
        con.rollback()

    def test_configuration_acl_and_native_revision(self):
        self.configure()
        with self.db() as con:
            worker = dict(role='worker', agent_id=self.agents['executor'], principal_id='worker')
            self.assert_code('forbidden_role', col.apply_command, con, self.project, worker, 'collaboration_verify', {})
            self.assert_code('invalid_principal', col.query, con, self.project, {}, 'collaboration_get', {'task_id': 'T-A'})
            self.assert_code('wrong_project', col.query, con, 'wrong-project', self.principal, 'collaboration_get', {'task_id': 'T-A'})
            c = self.contract()
            self.assert_code('revision_conflict', col.prepare_enqueue, con, self.project, self.principal, 'T-A', 1, c, 999)

    def test_distinct_slots_agents_generations_and_one_task(self):
        self.configure()
        first, second = self.prepare(), self.prepare(2, 'second')
        self.assertEqual((first['generation'], second['generation']), (1, 2))
        a, b = self.admit(second), self.admit(first)
        self.assertNotEqual(a['assignment_id'], b['assignment_id'])
        self.assertEqual(len(self.rows('SELECT * FROM v2_task')), 1)
        self.assertEqual({r['slot_id'] for r in self.rows('SELECT * FROM v2_assignment WHERE holds_slot=1')}, {1, 2})
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 6)

    def test_unapproved_slot_wrong_worker_and_duplicate_nonce(self):
        self.configure()
        c = self.contract()
        with self.db() as con:
            self.assert_code('ownership_conflict', col.prepare_enqueue, con, self.project, self.principal, 'T-A', 0, c, 1)
            self.assert_code('ownership_conflict', col.prepare_enqueue, con, self.project, self.principal, 'T-A', 2, c, 1)
        self.prepare(contract=c)
        c2 = self.contract('second')
        c2['request']['nonce'] = c['request']['nonce']
        c2['request']['completion']['marker'] = 'TASK_DONE ' + c2['request']['task_id'] + ' ' + c2['request']['nonce']
        c2['message'] = json.dumps({k: c2['request'][k] for k in ('task_id', 'nonce', 'cwd')})
        self.assert_code('attempt_reused', self.prepare, 2, 'second', contract=c2)

    def test_overlapping_competitor_workspaces_rejected(self):
        self.configure()
        one = self.prepare()
        base = Path(one['contract']['request']['cwd'])
        for nested in (False, True):
            with self.subTest(containment=nested):
                c = self.contract('second')
                cwd = base / 'nested' if nested else base
                cwd.mkdir(exist_ok=True)
                c['request']['cwd'] = str(cwd)
                self.assert_code('workspace_conflict', self.prepare, 2, 'second', contract=c)

    def test_admission_race_uses_real_sqlite_processes(self):
        self.configure()
        ticket = self.prepare()
        self.race('admit', {'ticket_id': ticket['ticket_id']})
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment')), 1)
        self.assertEqual(len(self.rows('SELECT * FROM v2_budget_reservation')), 3)

    def test_rollback_removes_assignment_and_reservations(self):
        self.configure()
        ticket = self.prepare()
        con = sqlite3.connect(service._db_path(self.state))
        try:
            con.execute('BEGIN IMMEDIATE')
            result = col.admit_in_transaction(con, self.project, self.principal, ticket['ticket_id'])
            self.assertEqual(len(result['reservation_ids']), 3)
            self.assertTrue(con.in_transaction)
            con.rollback()
        finally:
            con.close()
        self.assertEqual(self.rows('SELECT * FROM v2_assignment'), [])
        self.assertEqual(self.rows('SELECT * FROM v2_budget_reservation'), [])
        self.assertEqual(self.rows('SELECT state FROM v2_collaboration_ticket')[0]['state'], 'queued')

    def test_first_finisher_never_accepted_and_unique_winner(self):
        self.configure()
        a, event = self.ready()
        self.assert_code('verification_required', self.propose, a, event)
        result = self.verify(a, event)
        self.assertTrue(result['receipt']['passed'])
        self.assertGreater(result['receipt']['pid'], 0)
        self.assertEqual(result['receipt']['exit_code'], 0)
        proposal = self.propose(a, event)
        self.assertFalse(proposal['accepted'])
        self.assertIsNone(self.rows('SELECT accepted_evidence_id FROM v2_task')[0]['accepted_evidence_id'])
        self.assert_code('state_conflict', self.propose, a, event)
        with self.db() as con:
            self.assert_code('native_gate', col.acceptance_guard, con, self.project, self.principal, a['assignment_id'], event)
        self.review(a, event)
        with self.db() as con:
            self.assertTrue(col.acceptance_guard(con, self.project, self.principal, a['assignment_id'], event)['allowed'])

    def test_winner_race_and_withdrawal_retains_history(self):
        self.configure()
        a, event = self.ready()
        self.verify(a, event)
        self.race('collaboration_propose', dict(task_id='T-A', expected_revision=self.revision(),
                  assignment_id=a['assignment_id'], event_id=event))
        self.command('collaboration_withdraw', reason='reconsider candidate')
        self.propose(a, event)
        self.assertEqual(len(self.rows('SELECT * FROM v2_collaboration_proposal')), 2)

    def test_changed_candidate_bytes_cannot_accept(self):
        self.configure()
        a, event = self.ready()
        self.verify(a, event)
        self.propose(a, event)
        self.review(a, event)
        (Path(a['contract']['request']['cwd']) / 'RESULT.md').write_text('changed', encoding='utf-8')
        with self.db() as con:
            self.assert_code('evidence_changed', col.acceptance_guard, con, self.project, self.principal, a['assignment_id'], event)

    def test_loser_and_old_worker_identity_cannot_review_winner(self):
        self.configure()
        with self.db() as con:
            _, plan = col._plan(con, self.project, 'T-A')
            # Direct identity test covers historical/loser aliases regardless of
            # registry role changes; public worker role is separately rejected.
            for role in ('executor', 'second'):
                self.assert_code('self_review', col._independent, con, 'T-A', plan, self.bindings[role])
            alias = copy.deepcopy(self.bindings['planner'])
            alias['observed_identity'] = self.bindings['second']['observed_identity']
            self.assert_code('self_review', col._independent, con, 'T-A', plan, alias)

    def test_partial_failure_and_unknown_reservations_never_release(self):
        self.configure()
        a, b = self.admit(self.prepare()), self.admit(self.prepare(2, 'second'))
        before = self.rows('SELECT * FROM v2_budget_reservation')
        with self.db() as con:
            result = col.closure_projection(con, self.project, self.principal, a['assignment_id'], 'failed')
            self.assertEqual(result['suggested_task_state'], 'active')
            self.assertEqual(result['other_held_assignments'], [b['assignment_id']])
            self.assertEqual(result['release_reservations'], [])
        self.assertEqual(self.rows('SELECT * FROM v2_budget_reservation'), before)
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment WHERE holds_slot=1')), 2)

    def test_loser_close_cannot_overwrite_accepted_task(self):
        self.configure()
        a, event = self.ready()
        self.verify(a, event)
        self.propose(a, event)
        self.review(a, event)
        with self.db() as con:
            self.assertEqual(col.closure_projection(con, self.project, self.principal, a['assignment_id'], 'accepted')
                             ['suggested_task_state'], 'accepted')
            con.execute("UPDATE v2_task SET task_state='accepted' WHERE task_id='T-A'")
            self.assertEqual(col.closure_projection(con, self.project, self.principal, a['assignment_id'], 'cancelled')
                             ['suggested_task_state'], 'accepted')

    def test_ensemble_requires_synthesis_and_distinct_contributors(self):
        self.configure('ensemble')
        a, ea = self.ready()
        b, eb = self.ready(2, 'second')
        s, es = self.ready(0, 'third')
        self.verify(a, ea)
        self.assert_code('invalid_payload', self.propose, a, ea, contributors=[ea, eb])
        self.verify(s, es)
        self.assert_code('invalid_payload', self.propose, s, es, contributors=[ea, ea])
        self.propose(s, es, contributors=[ea, eb])
        self.review(s, es)
        with self.db() as con:
            self.assertTrue(col.acceptance_guard(con, self.project, self.principal, s['assignment_id'], es)['allowed'])
        (Path(b['contract']['request']['cwd']) / 'RESULT.md').write_text('tampered contributor', encoding='utf-8')
        with self.db() as con:
            self.assert_code('evidence_changed', col.acceptance_guard, con, self.project, self.principal, s['assignment_id'], es)

    def test_real_verifier_nonzero_and_retry_limit(self):
        self.configure(code='import sys; print("failed"); sys.exit(7)', max_runs_per_assignment=1)
        a, event = self.ready()
        result = self.verify(a, event)
        self.assertFalse(result['receipt']['passed'])
        self.assertEqual(result['receipt']['exit_code'], 7)
        self.assert_code('verification_required', self.propose, a, event)
        self.assert_code('verify_limit', self.verify, a, event)

    def test_real_verifier_timeout_records_exit_and_keeps_slot(self):
        self.configure(code='import time; time.sleep(15)', timeout_seconds=1)
        a, event = self.ready()
        result = self.verify(a, event)
        self.assertTrue(result['receipt']['timed_out'])
        self.assertIsNotNone(result['receipt']['exit_code'])
        self.assertFalse(result['receipt']['passed'])
        self.assertEqual(self.rows('SELECT holds_slot FROM v2_assignment')[0]['holds_slot'], 1)

    def test_real_verifier_output_is_bounded_and_fails_closed(self):
        self.configure(code='import sys; sys.stdout.write("x"*1000000)', max_output_bytes=128)
        a, event = self.ready()
        receipt = self.verify(a, event)['receipt']
        self.assertFalse(receipt['passed'])
        self.assertTrue(receipt['output_limited'])
        self.assertLessEqual(sum(o['retained_bytes'] for o in receipt['output'].values()), 128)

    def test_real_verifier_mutation_invalidates_successful_exit(self):
        self.configure(code='from pathlib import Path; Path("RESULT.md").write_text("mutable")')
        a, event = self.ready()
        receipt = self.verify(a, event)['receipt']
        self.assertEqual(receipt['exit_code'], 0)
        self.assertFalse(receipt['passed'])
        self.assertIn('evidence_error', receipt)

    @unittest.skipUnless(shutil.which('git'), 'Git not installed; real Git coverage unavailable')
    def test_real_git_hash_object_verifier_no_commit_or_push(self):
        self.configure(argv=[shutil.which('git'), 'hash-object', '--no-filters', 'RESULT.md'])
        a, event = self.ready()
        cwd = a['contract']['request']['cwd']
        subprocess.run([shutil.which('git'), 'init', '-q', cwd], check=True, capture_output=True, timeout=10)
        receipt = self.verify(a, event)['receipt']
        self.assertTrue(receipt['passed'], receipt)
        raw = (Path(cwd) / 'RESULT.md').read_bytes()
        expected = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        self.assertEqual(receipt['output']['stdout']['text'].strip(), expected)

    def test_repair_consumes_real_sealed_followup_and_new_generation(self):
        self.configure('single', max_attempts=3, max_repairs=1)
        a, event = self.ready(0)
        self.review(a, event, 'repair')
        self.close_repair(a, event)
        followup = self.followup(a, event)
        c = self.repair_contract(followup)
        next_ticket = self.prepare(0, contract=c)
        self.assertEqual(next_ticket['generation'], 2)
        self.assertEqual(len(self.rows('SELECT * FROM v2_task')), 1)
        # A repair generation must not reuse the closed generation's delivery
        # directory; the freshness comes from follow_up and is asserted here so
        # the scenario does not depend on an implicit helper side effect. The
        # launch directory stays the same worker-role identity by design.
        previous = a['contract']['request']
        repaired = next_ticket['contract']['request']
        self.assertNotEqual(repaired['cwd'], previous['cwd'])
        b = self.admit(next_ticket)
        eb = self.complete(b)
        self.review(b, eb, 'repair')
        self.close_repair(b, eb)
        self.assert_code('repair_limit', self.prepare, 0, contract=self.repair_contract(self.followup(b, eb)))

    def test_repair_never_releases_held_slot_from_ttl_or_status(self):
        self.configure('single')
        a, event = self.ready(0)
        self.review(a, event, 'repair')
        self.mutate("UPDATE v2_assignment SET state='uncertain' WHERE assignment_id=?", (a['assignment_id'],))
        c = self.contract()
        with self.db() as con:
            self.assert_code('slot_held', col.prepare_enqueue, con, self.project, self.principal, 'T-A', 0, c, self.revision())
        self.assertEqual(self.rows('SELECT holds_slot FROM v2_assignment')[0]['holds_slot'], 1)

    def test_changed_repair_note_rejected_at_admission(self):
        self.configure('single')
        a, event = self.ready(0)
        self.review(a, event, 'repair')
        self.close_repair(a, event)
        followup = self.followup(a, event)
        ticket = self.prepare(0, contract=self.repair_contract(followup))
        Path(followup['request']['parent_review']['path']).write_text('changed review', encoding='utf-8')
        self.assert_code('native_gate', self.admit, ticket)
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment')), 1)

    def test_repair_parent_is_single_use_even_after_ticket_cancel(self):
        self.configure('single')
        a, event = self.ready(0)
        self.review(a, event, 'repair')
        self.close_repair(a, event)
        one = self.prepare(0, contract=self.repair_contract(self.followup(a, event)))
        with self.db() as con:
            col.apply_command(con, self.project, self.principal, 'collaboration_cancel_ticket',
                              dict(ticket_id=one['ticket_id'], expected_revision=self.revision(), reason='cancel approval'))
        self.assert_code('review_consumed', self.prepare, 0, contract=self.repair_contract(self.followup(a, event)))

    def test_concurrent_repair_consumption_real_connections(self):
        self.configure('single')
        a, event = self.ready(0)
        self.review(a, event, 'repair')
        self.close_repair(a, event)
        contract = self.repair_contract(self.followup(a, event))
        self.race('prepare', dict(task_id='T-A', slot_id=0, contract=contract, expected_revision=self.revision()))
        self.assertEqual(len(self.rows('SELECT * FROM v2_collaboration_ticket WHERE parent_event_id IS NOT NULL')), 1)

    def test_shared_attempt_and_native_cumulative_budget_caps(self):
        self.configure(max_attempts=2)
        self.set_limits({'tokens.input': 15}, scope='task', identity='T-A')
        one, two = self.prepare(), self.prepare(2, 'second')
        self.admit(one)
        self.assert_code('native_gate', self.admit, two)
        self.assertEqual(len(self.rows('SELECT * FROM v2_assignment')), 1)

    def test_unknown_native_budget_blocks_without_releasing_reservations(self):
        self.configure()
        a = self.admit(self.prepare())
        self.set_limits({'tokens.input': 100}, scope='task', identity='T-A')
        self.mutate("UPDATE v2_budget SET state='unknown_blocked' WHERE scope_type='task'")
        before = self.rows('SELECT * FROM v2_budget_reservation')
        self.assert_code('native_gate', self.prepare, 2, 'second')
        self.assertEqual(self.rows('SELECT * FROM v2_budget_reservation'), before)
        self.assertEqual(self.rows('SELECT holds_slot FROM v2_assignment')[0]['holds_slot'], 1)

    def test_queries_are_read_only_and_worker_scope_is_checked(self):
        self.configure()
        self.prepare()
        before = self.snapshot()
        with self.db(readonly=True) as con:
            for name in ('collaboration_get', 'awo_collaboration_history'):
                self.assertIsInstance(col.query(con, self.project, self.principal, name, {'task_id': 'T-A'}), dict)
            outsider = dict(role='worker', agent_id=self.agents['third'], principal_id='outsider')
            self.assert_code('ownership_conflict', col.query, con, self.project, outsider, 'collaboration_get', {'task_id': 'T-A'})
        self.assertEqual(before, self.snapshot())

    def test_immutable_policy_ticket_and_verification(self):
        self.configure()
        a, event = self.ready()
        self.verify(a, event)
        with self.db() as con:
            for sql in ("UPDATE v2_collaboration_policy SET config_sha256='bad'",
                        "UPDATE v2_collaboration_ticket SET nonce='bad'",
                        "DELETE FROM v2_collaboration_verification",
                        "UPDATE v2_collaboration_verification SET receipt_json='{}'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute(sql)

    def test_verifier_approval_rollback_has_no_external_effect(self):
        self.configure(code='from pathlib import Path; Path("effect.txt").write_text("ran")')
        a, event = self.ready()
        con = service._connect(service._db_path(self.state))
        try:
            con.execute('BEGIN IMMEDIATE')
            intent = col.apply_command(con, self.project, self.principal, 'collaboration_verify',
                                       dict(task_id='T-A', assignment_id=a['assignment_id'], event_id=event,
                                            expected_revision=self.revision()))
            self.assertFalse(intent['process_started'])
            self.assert_code('idle_connection_required', col.run_verification, con, self.project,
                             self.principal, intent['verification_id'])
            con.rollback()
            self.assert_code('wrong_project', col.run_verification, con, self.project, self.principal, intent['verification_id'])
        finally:
            con.close()
        self.assertFalse((Path(a['contract']['request']['cwd']) / 'effect.txt').exists())

    def test_verifier_reentry_replays_receipt_without_rerunning_effect(self):
        self.configure(code='from pathlib import Path; p=Path("effect.txt"); p.write_text(p.read_text()+"x" if p.exists() else "x")')
        a, event = self.ready()
        result = self.verify(a, event)
        again = self.run_intent(result['verification_id'])
        self.assertTrue(again['replayed'])
        self.assertEqual(again['receipt_sha256'], result['receipt_sha256'])
        self.assertEqual((Path(a['contract']['request']['cwd']) / 'effect.txt').read_text(), 'x')

    def test_verifier_concurrent_runners_execute_once(self):
        self.configure(code='from pathlib import Path; p=Path("effect.txt"); p.write_text(p.read_text()+"x" if p.exists() else "x")')
        a, event = self.ready()
        intent = self.command('collaboration_verify', assignment_id=a['assignment_id'], event_id=event)
        code = ('import json,sqlite3,sys; import collaboration_runtime as c; '
                'x=json.loads(sys.argv[1]); con=sqlite3.connect(x.pop("db"),timeout=15); '
                'print(json.dumps(c.run_verification(con,**x))); con.close()')
        value = dict(db=str(service._db_path(self.state)), project_id=self.project, principal=self.principal,
                     verification_id=intent['verification_id'])
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
        children = [subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-c', code, json.dumps(value)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) for _ in range(2)]
        results = []
        for child in children:
            output, error = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, error.decode())
            results.append(json.loads(output))
        self.assertEqual(sum(r.get('replayed') is False for r in results), 1)
        self.assertEqual((Path(a['contract']['request']['cwd']) / 'effect.txt').read_text(), 'x')

    def test_crash_before_popen_retains_attempt_and_never_retries(self):
        self.configure()
        a, event = self.ready()
        intent = self.command('collaboration_verify', assignment_id=a['assignment_id'], event_id=event)
        # LABELED FAULT INJECTION: exit immediately at the external-effect seam.
        code = ('import json,sqlite3,sys,os; import collaboration_runtime as c; '
                'x=json.loads(sys.argv[1]); con=sqlite3.connect(x.pop("db")); '
                'c._run_verifier=lambda *args: os._exit(23); c.run_verification(con,**x)')
        value = dict(db=str(service._db_path(self.state)), project_id=self.project, principal=self.principal,
                     verification_id=intent['verification_id'])
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
        child = subprocess.run([sys.executable, '-B', '-c', code, json.dumps(value)], capture_output=True, env=env, timeout=15)
        self.assertEqual(child.returncode, 23, child.stderr.decode())
        self.assertEqual(self.run_intent(intent['verification_id'])['status'], 'UNCERTAIN')
        self.assertEqual(self.rows('SELECT state FROM v2_collaboration_verify_intent')[0]['state'], 'attempted')
        self.assertEqual(self.rows('SELECT * FROM v2_collaboration_verification'), [])
        self.assert_code('verification_pending', self.command, 'collaboration_verify', assignment_id=a['assignment_id'], event_id=event)

    def test_actual_runner_crash_after_effect_does_not_blindly_rerun(self):
        self.configure(code=ATOMIC_EFFECT_CODE, timeout_seconds=30)
        a, event = self.ready()
        intent = self.command('collaboration_verify', assignment_id=a['assignment_id'], event_id=event)
        code = ('import json,sqlite3,sys; import collaboration_runtime as c; '
                'x=json.loads(sys.argv[1]); con=sqlite3.connect(x.pop("db")); c.run_verification(con,**x)')
        value = dict(db=str(service._db_path(self.state)), project_id=self.project, principal=self.principal,
                     verification_id=intent['verification_id'])
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
        child = subprocess.Popen([sys.executable, '-B', '-c', code, json.dumps(value)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        path = Path(a['contract']['request']['cwd']) / 'effect.txt'
        owned_verifier = None
        try:
            deadline = time.monotonic() + 10
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(path.exists(), 'real verifier did not start')
            owned_verifier = int(path.read_text())
            child.kill()
            child.wait(timeout=10)
            before = path.read_bytes()
            self.assertEqual(self.run_intent(intent['verification_id'])['status'], 'UNCERTAIN')
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.rows('SELECT * FROM v2_collaboration_verification'), [])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            if owned_verifier:
                # Only the actual local child PID emitted by this fixture.
                self.assertTrue(terminate_owned(owned_verifier),
                                "owned verifier did not exit after a bounded SIGTERM")

    def test_atomic_effect_publication_never_exposes_partial_pid(self):
        work = self.root / "effect-proof"
        work.mkdir()
        gate = work / "release"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent),
                   AWO_EFFECT_PAUSE_FILE=str(gate))
        child = subprocess.Popen([sys.executable, "-B", "-c", ATOMIC_EFFECT_CODE], cwd=work, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path, part = work / "effect.txt", work / "effect.txt.part"
        try:
            deadline = time.monotonic() + 10
            while not part.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(part.exists(), "writer did not reach the injected pause")
            self.assertEqual(part.read_text().strip(), str(child.pid))
            for _ in range(50):
                self.assertFalse(path.exists(), "final effect PID appeared before the atomic replace")
                time.sleep(0.01)
            gate.write_text("go", encoding="utf-8")
            deadline = time.monotonic() + 10
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(path.exists(), "final effect PID did not appear after the replace")
            self.assertEqual(path.read_text().strip(), str(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_verifier_epoch_change_and_unknown_retry_fail_closed(self):
        self.configure()
        a, event = self.ready()
        self.verify(a, event)
        intent = self.command('collaboration_verify', assignment_id=a['assignment_id'], event_id=event)
        self.assert_code('verification_required', self.propose, a, event)
        self.mutate('UPDATE v2_service_state SET controller_epoch=controller_epoch+1')
        self.assert_code('stale_epoch', self.run_intent, intent['verification_id'])
        self.assertEqual(self.rows("SELECT count(*) AS n FROM v2_collaboration_verify_intent WHERE state='attempted'")[0]['n'], 0)

    def test_verifier_changed_input_between_approval_and_launch(self):
        self.configure()
        a, event = self.ready()
        intent = self.command('collaboration_verify', assignment_id=a['assignment_id'], event_id=event)
        (Path(a['contract']['request']['cwd']) / 'RESULT.md').write_text('new bytes', encoding='utf-8')
        self.assert_code('evidence_changed', self.run_intent, intent['verification_id'])
        self.assertEqual(self.rows('SELECT state FROM v2_collaboration_verify_intent')[0]['state'], 'ready')

    def test_wrong_task_candidate_and_event_are_rejected(self):
        self.configure()
        a, event = self.ready()
        self.create('T-B')
        self.configure('single', task='T-B')
        self.assert_code('ownership_conflict', self.command, 'collaboration_verify', task='T-B',
                         assignment_id=a['assignment_id'], event_id=event)
        self.assert_code('evidence_changed', self.verify, a, 'missing-event')

    def test_dead_review_and_forged_closure_cannot_repair(self):
        self.configure('single')
        a, event = self.ready(0)
        self.review(a, event, 'repair')
        self.close_repair(a, event)
        c = self.repair_contract(self.followup(a, event))
        self.mutate("UPDATE v2_scheduler_intent SET closure_json='{}' WHERE assignment_id=?", (a['assignment_id'],))
        self.assert_code('invalid_review', self.prepare, 0, contract=c)
        self.mutate("UPDATE events SET status='cancelled' WHERE id=?", (event,))
        self.assert_code('evidence_changed', self.prepare, 0, contract=c)

    def test_shared_attempt_cap_counts_repair_and_all_competitors(self):
        self.configure(max_attempts=2)
        a, event = self.ready()
        self.admit(self.prepare(2, 'second'))
        self.review(a, event, 'repair')
        self.close_repair(a, event)
        c = self.repair_contract(self.followup(a, event))
        self.assert_code('attempt_limit', self.prepare, contract=c)

    def test_review_hash_and_nonce_tamper_rejected(self):
        self.configure('single')
        a, event = self.ready(0)
        self.review(a, event, 'repair')
        self.close_repair(a, event)
        followup = self.followup(a, event)
        for field in ('sha256', 'nonce', 'task_id', 'event_id'):
            with self.subTest(field=field):
                tampered = copy.deepcopy(followup)
                tampered['request']['parent_review'][field] = 'tampered'
                with self.assertRaises(ValueError):
                    self.prepare(0, contract=self.repair_contract(tampered))

    def test_verifier_attempt_cannot_be_reset_by_sql(self):
        self.configure()
        a, event = self.ready()
        result = self.verify(a, event)
        with self.db() as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE v2_collaboration_verify_intent SET state='ready' WHERE verification_id=?",
                            (result['verification_id'],))

    def test_changed_review_bytes_block_acceptance_after_proposal(self):
        self.configure()
        a, event = self.ready()
        self.verify(a, event)
        self.propose(a, event)
        review = self.review(a, event)
        Path(review['path']).write_text('altered review', encoding='utf-8')
        with self.db() as con:
            self.assert_code('native_gate', col.acceptance_guard, con, self.project, self.principal, a['assignment_id'], event)

    def test_pending_intent_is_visible_in_read_only_query(self):
        self.configure()
        a, event = self.ready()
        intent = self.command('collaboration_verify', assignment_id=a['assignment_id'], event_id=event)
        before = self.snapshot()
        with self.db(readonly=True) as con:
            result = col.query(con, self.project, self.principal, 'collaboration_get', {'task_id': 'T-A'})
        self.assertEqual(result['verifications'][0]['verification_id'], intent['verification_id'])
        self.assertEqual(result['verifications'][0]['state'], 'ready')
        self.assertIsNone(result['verifications'][0]['receipt_sha256'])
        self.assertEqual(before, self.snapshot())

    def test_no_new_work_until_current_proposal_is_withdrawn(self):
        self.configure()
        a, event = self.ready()
        second = self.prepare(2, 'second')
        self.verify(a, event)
        self.propose(a, event)
        self.assert_code('state_conflict', self.admit, second)
        self.command('collaboration_withdraw', reason='need second candidate')
        self.assertEqual(self.admit(second)['status'], 'RESERVED')

    def test_registered_workers_cannot_share_physical_identity(self):
        # LABELED identity-tamper fixture: active binding alias between workers.
        binding = copy.deepcopy(self.bindings['second'])
        binding['observed_identity'] = self.bindings['executor']['observed_identity']
        self.mutate('UPDATE bindings SET data=? WHERE id=?', (json.dumps(binding), binding['instance_id']))
        self.assert_code('invalid_worker', self.configure)

    def test_malformed_request_is_rejected_before_native_binding_lookup(self):
        self.configure()
        c = self.contract()
        c['request'] = {}
        self.assert_code('native_gate', self.prepare, contract=c)

    def test_cancelled_ticket_cannot_reactivate_or_release_assignment(self):
        self.configure()
        ticket = self.prepare()
        with self.db() as con:
            col.apply_command(con, self.project, self.principal, 'collaboration_cancel_ticket',
                              dict(ticket_id=ticket['ticket_id'], expected_revision=self.revision(), reason='cancel'))
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE v2_collaboration_ticket SET state='queued' WHERE ticket_id=?", (ticket['ticket_id'],))
        self.assert_code('state_conflict', self.admit, ticket)
        self.assertEqual(self.rows('SELECT * FROM v2_assignment'), [])

    def test_in_memory_and_nondurable_verifier_runners_are_rejected(self):
        con = sqlite3.connect(':memory:')
        try:
            self.assert_code('durability_required', col.run_verification, con, self.project, self.principal, 'missing')
        finally:
            con.close()
        con = sqlite3.connect(service._db_path(self.state))
        try:
            con.execute('PRAGMA synchronous=NORMAL')
            self.assert_code('durability_required', col.run_verification, con, self.project, self.principal, 'missing')
        finally:
            con.close()



if __name__ == '__main__':
    unittest.main()
