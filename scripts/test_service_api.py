"""Real SQLite and local subprocess service integration (no live model calls)."""
import hashlib
import json
from pathlib import Path
import unittest
import uuid

import awo_service as service
import service_access as access
import service_api as api
import test_priority_scheduler as fixtures


class ServiceAPITests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PrioritySchedulerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.state, self.root = self.f.state, self.f.root
        self.controller = self.issue('controller', 'planner')
        self.worker = self.issue('worker', 'executor')
        self.other = self.issue('worker', 'second')

    def issue(self, role, agent, **scope):
        payload = dict(project_root=str(self.root), role=role, agent_id=self.f.agents[agent], **scope)
        if role in ('controller', 'reviewer'):
            payload.update(binding_id=self.f.actor[0], binding_version=self.f.actor[1])
        path = self.root / ('secret-' + uuid.uuid4().hex)
        access.issue_token(self.state, *self.f.actor, self.f.envelope('access.issue', payload), str(path))
        return path.read_text().strip()

    def args(self, **payload):
        return dict(payload, command_id='api-' + uuid.uuid4().hex, expected_epoch=self.f.epoch)

    def call(self, token, name, arguments=None):
        return service.gateway_call(self.state, str(self.root), token, name, arguments or {})

    def evidence(self, aid, name='RESULT.md'):
        assignment = self.f.get_assignment(aid)
        record = self.f.rows('SELECT data FROM tasks WHERE id=?', (assignment['legacy_task_id'],))[0]
        root = Path(json.loads(record['data'])['request']['cwd'])
        raw = (root / name).read_bytes()
        return self.call(self.worker, 'awo_evidence_record', self.args(
            assignment_id=aid, expected_revision=assignment['revision'], kind='artifact',
            path=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))

    def reserve(self, resources=None):
        queued, request = self.f.enqueue(resources=resources or {})
        result = self.call(self.worker, 'awo_assignment_claim',
                           self.args(task_id='T-A', expected_revision=queued['revision']))
        self.assertEqual(result['status'], 'RESERVED', result)
        return result['assignment_id'], request

    def delivery(self):
        aid, request = self.reserve()
        self.assertEqual(self.f.dispatch(aid)['status'], 'SUBMITTED')
        self.assertEqual(self.f.reconcile(aid)['status'], 'REVIEW_READY')
        evidence = self.evidence(aid)
        assignment = self.f.get_assignment(aid)
        result = self.call(self.worker, 'awo_assignment_complete', self.args(
            assignment_id=aid, generation=assignment['generation'], expected_revision=assignment['revision'],
            outcome='completed', evidence_ids=[evidence['evidence_id']], summary='Local process completed.'))
        return aid, request, evidence, result

    def test_exact_catalog_and_application_revalidates_schema(self):
        self.assertEqual(len(service.gateway_tool_specs()), 13)
        self.assertEqual(len(service.gateway_tool_specs(True)), 16)
        for payload in ({'title': 'x'}, self.args(title='x', principal='controller'),
                        dict(self.args(title='x'), expected_epoch=True)):
            with self.assertRaises(api.APIError):
                self.call(self.controller, 'awo_task_create', payload)

    def test_create_list_read_and_durable_replay(self):
        args = self.args(task_id='API-TASK', title='API task')
        result = self.call(self.controller, 'awo_task_create', args)
        self.assertEqual(result['status'], 'CREATED')
        self.assertTrue(self.call(self.controller, 'awo_task_create', args)['replayed'])
        self.assertEqual(len(self.call(self.worker, 'awo_task_list')['items']), 1)
        self.assertEqual(self.call(self.worker, 'awo_task_get', {'task_id': 'API-TASK'})['task']['title'], 'API task')
        with self.assertRaises(api.APIError):
            self.call(self.controller, 'awo_task_create', dict(args, title='Changed'))
        with self.assertRaises(api.APIError):
            self.call(self.worker, 'awo_task_create', args)

    def test_project_binding_and_scoped_reads(self):
        self.f.create('T-A')
        self.f.create('T-B')
        scoped = self.issue('worker', 'executor', task_id='T-A')
        self.assertEqual([t['task_id'] for t in self.call(scoped, 'awo_task_list')['items']], ['T-A'])
        with self.assertRaises(api.APIError):
            self.call(scoped, 'awo_task_get', {'task_id': 'T-B'})
        with self.assertRaises(api.APIError):
            self.call(scoped, 'awo_task_next')
        with self.assertRaises(api.APIError):
            service.gateway_call(self.state, str(self.state), self.worker, 'awo_task_list', {})

    def test_worker_claim_requires_controller_queue_own_agent_and_fencing(self):
        self.f.create()
        with self.assertRaises(api.APIError):
            self.call(self.worker, 'awo_assignment_claim', self.args(task_id='T-A', expected_revision=1))
        queued, request = self.f.enqueue(create=False)
        with self.assertRaises(api.APIError):
            self.call(self.other, 'awo_assignment_claim', self.args(
                task_id='T-A', expected_revision=queued['revision'], agent_id=self.f.agents['executor']))
        candidate = self.call(self.worker, 'awo_task_next')
        self.assertEqual(candidate['candidate']['task_id'], 'T-A')
        args = self.args(task_id='T-A', expected_revision=queued['revision'])
        result = self.call(self.worker, 'awo_assignment_claim', args)
        self.assertEqual(result['status'], 'RESERVED', result)
        self.assertTrue(self.call(self.worker, 'awo_assignment_claim', args)['replayed'])
        self.assertEqual(len(self.f.rows('SELECT * FROM v2_assignment')), 1)
        self.assertFalse((self.root / 'worker-executor' / 'sends.jsonl').exists())
        with self.assertRaises(api.APIError):
            self.call(self.other, 'awo_assignment_get', {'assignment_id': result['assignment_id']})

    def test_all_lifecycle_tools_preserve_independent_review_and_closure(self):
        aid, request, evidence, completed = self.delivery()
        self.assertEqual(completed['status'], 'REVIEW_READY')
        self.assertFalse(completed['independently_accepted'])
        self.assertEqual(self.f.rows('SELECT task_state FROM v2_task')[0]['task_state'], 'review_ready')
        reviewer = self.issue('reviewer', 'planner', task_id='T-A')
        note = Path(request['cwd']) / 'REVIEW.md'
        note.write_text(request['task_id'] + ' ' + request['nonce'] + chr(10) + 'Verdict: accept' + chr(10),
                        encoding='utf-8')
        args = self.args(assignment_id=aid, task_id='T-A', event_id=completed['event_id'],
                         expected_revision=self.f.get_assignment(aid)['revision'], verdict='accept',
                         evidence_ids=[evidence['evidence_id']], review_note_path=note.relative_to(self.root).as_posix(),
                         review_note_sha256=service._file_sha(note))
        with self.assertRaises(api.APIError):
            self.call(self.worker, 'awo_review_submit', args)
        reviewed = self.call(reviewer, 'awo_review_submit', args)
        self.assertEqual(reviewed['status'], 'REVIEW_RECORDED')
        self.assertTrue(reviewed['claims_retained'])
        self.assertTrue(self.call(reviewer, 'awo_review_submit', args)['replayed'])
        self.f.close(aid, completed)
        self.assertEqual(self.f.rows('SELECT task_state FROM v2_task')[0]['task_state'], 'accepted')
        self.assertEqual(self.f.get_assignment(aid)['holds_slot'], 0)

    def test_evidence_hash_path_mutation_and_receipt_forgery_rejected(self):
        aid, request, evidence, completed = self.delivery()
        assignment = self.f.get_assignment(aid)
        for path in ('../outside', '/absolute', 'http://host/x', 'missing'):
            with self.assertRaises(api.APIError):
                self.call(self.worker, 'awo_evidence_record', self.args(
                    assignment_id=aid, expected_revision=assignment['revision'], kind='artifact',
                    path=path, sha256='0' * 64, bytes=0))
        (Path(request['cwd']) / 'RESULT.md').write_text('changed after sealing', encoding='utf-8')
        with self.assertRaises(api.APIError):
            self.call(self.worker, 'awo_assignment_complete', self.args(
                assignment_id=aid, generation=assignment['generation'], expected_revision=assignment['revision'],
                outcome='completed', evidence_ids=[evidence['evidence_id']], summary='not valid'))
        self.assertEqual(self.f.rows('SELECT count(*) AS n FROM reviews')[0]['n'], 0)

    def test_resource_tools_never_expand_or_release_live_claim(self):
        aid, request = self.reserve({'gpu': 1})
        args = self.args(assignment_id=aid, expected_revision=1, resources={'gpu': 1})
        self.assertEqual(self.call(self.worker, 'awo_resource_claim', args)['status'], 'CLAIM_CONFIRMED')
        with self.assertRaises(api.APIError):
            self.call(self.worker, 'awo_resource_claim', self.args(
                assignment_id=aid, expected_revision=1, resources={'gpu': 2}))
        with self.assertRaises(api.APIError):
            self.call(self.worker, 'awo_resource_release', self.args(assignment_id=aid, expected_revision=1))
        self.assertEqual(self.f.get_assignment(aid)['holds_slot'], 1)

    def test_heartbeat_does_not_close_assignment_or_release_resources(self):
        aid, _ = self.reserve()
        revision = self.f.rows('SELECT revision FROM v2_agent WHERE agent_id=?',
                               (self.f.agents['executor'],))[0]['revision']
        args = self.args(expected_revision=revision, status='offline')
        heartbeat = self.call(self.worker, 'awo_agent_heartbeat', args)
        self.assertEqual(heartbeat['revision'], revision + 1)
        self.assertTrue(self.call(self.worker, 'awo_agent_heartbeat', args)['replayed'])
        self.assertEqual(self.f.get_assignment(aid)['holds_slot'], 1)

    def test_root_bound_grant_registration_replay_and_secrets_not_logged(self):
        path = self.root / 'enrollment-secret'
        service._enroll_agent(self.state, *self.f.actor, self.f.envelope('agent.enroll', {
            'role': 'worker', 'allowed_capabilities': ['status'], 'project_root': str(self.root)}), str(path))
        grant = path.read_text().strip()
        with self.assertRaises(api.APIError):
            self.call(grant, 'awo_task_list')
        args = self.args(handle='new-worker', harness='local', harness_version='1',
                         host='local', os='windows', capabilities=['status'])
        result = self.call(grant, 'awo_agent_register', args)
        self.assertEqual(result['role'], 'worker')
        self.assertEqual(self.call(result['access_token'], 'awo_task_list')['status'], 'TASKS')
        replay = self.call(grant, 'awo_agent_register', args)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['access_token'], result['access_token'])
        with self.assertRaises(api.APIError):
            self.call(grant, 'awo_agent_register', dict(args, command_id='new-command-123'))
        with self.f.db(readonly=True) as con:
            dump = chr(10).join(con.iterdump())
        self.assertNotIn(grant, dump)
        self.assertNotIn(result['access_token'], dump)

    def message_pair(self):
        self.f.create('T-A')
        self.f.create('T-B')
        for task in ('T-A', 'T-B'):
            self.call(self.controller, 'awo_message_send', self.args(
                to=[self.f.agents['second']], kind='notice', body=task, task_id=task))
        scoped = self.issue('worker', 'second', task_id='T-A')
        return scoped, self.call(self.other, 'awo_message_read')['messages']

    def test_task_scoped_message_inbox_filters_context(self):
        scoped, all_messages = self.message_pair()
        self.assertEqual(len(all_messages), 2)
        messages = self.call(scoped, 'awo_message_read')['messages']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['task_id'], 'T-A')

    def test_message_ack_scope_is_checked_before_replay(self):
        scoped, messages = self.message_pair()
        foreign = next(m for m in messages if m['task_id'] == 'T-B')
        args = self.args(delivery_id=foreign['delivery_id'], expected_revision=foreign['revision'],
                         state='acknowledged')
        with self.assertRaises(api.APIError):
            self.call(scoped, 'awo_message_ack', args)
        self.call(self.other, 'awo_message_ack', args)
        with self.assertRaises(api.APIError):
            self.call(scoped, 'awo_message_ack', args)

    def test_scoped_reply_cannot_inherit_foreign_context(self):
        scoped, messages = self.message_pair()
        foreign = next(m for m in messages if m['task_id'] == 'T-B')
        with self.assertRaises(api.APIError):
            self.call(scoped, 'awo_message_send', self.args(to=[self.f.agents['executor']],
                kind='notice', body='reply', reply_to=foreign['message_id']))

    def run_message_pair(self):
        aid, request = self.reserve()
        for scope in ({}, {'assignment_id': aid, 'generation': self.f.get_assignment(aid)['generation']}):
            self.call(self.controller, 'awo_message_send', self.args(
                to=[self.f.agents['executor']], kind='notice', body='Assignment context', task_id='T-A', **scope))
        scoped = self.issue('worker', 'executor', assignment_id=aid)
        messages = self.call(self.worker, 'awo_message_read')['messages']
        return scoped, aid, messages

    def test_run_scoped_message_ack_and_replay_reject_task_only_delivery(self):
        scoped, aid, messages = self.run_message_pair()
        null_context = next(m for m in messages if m['assignment_id'] is None)
        exact = self.call(scoped, 'awo_message_read')['messages']
        self.assertEqual([m['assignment_id'] for m in exact], [aid])
        args = self.args(delivery_id=null_context['delivery_id'],
                         expected_revision=null_context['revision'], state='acknowledged')
        with self.assertRaises(api.APIError):
            self.call(scoped, 'awo_message_ack', args)
        self.call(self.worker, 'awo_message_ack', args)
        with self.assertRaises(api.APIError):
            self.call(scoped, 'awo_message_ack', args)
        self.call(scoped, 'awo_message_ack', self.args(delivery_id=exact[0]['delivery_id'],
                  expected_revision=exact[0]['revision'], state='acknowledged'))

    def test_run_scoped_message_send_and_reply_require_exact_assignment(self):
        scoped, aid, messages = self.run_message_pair()
        null_context = next(m for m in messages if m['assignment_id'] is None)
        for scope in ({'task_id': 'T-A'}, {'reply_to': null_context['message_id']}):
            with self.assertRaises(api.APIError):
                self.call(scoped, 'awo_message_send', self.args(to=[self.f.agents['planner']],
                    kind='notice', body='Request context', **scope))
        self.call(scoped, 'awo_message_send', self.args(to=[self.f.agents['planner']],
            kind='notice', body='Exact context', task_id='T-A', assignment_id=aid,
            generation=self.f.get_assignment(aid)['generation']))

    def test_message_service_uses_same_authenticated_boundary(self):
        args = self.args(to=[self.f.agents['second']], kind='notice', body='Please inspect the log.')
        sent = self.call(self.worker, 'awo_message_send', args)
        self.assertTrue(self.call(self.worker, 'awo_message_send', args)['replayed'])
        received = self.call(self.other, 'awo_message_read')['messages']
        self.assertEqual(len(received), 1)
        item = received[0]
        ack = self.call(self.other, 'awo_message_ack', self.args(
            delivery_id=item['delivery_id'], expected_revision=item['revision'], state='acknowledged'))
        self.assertEqual(self.f.rows('SELECT count(*) AS n FROM v2_task')[0]['n'], 0)


if __name__ == '__main__':
    unittest.main()
