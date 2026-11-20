"""Authenticated domains on real SQLite, native fixtures and subprocess IPC.

Synthetic local workers belong to existing offline fixtures. Negative process
tripwires are labeled mocks; domain calls, credentials and replay are real.
No provider/network or live compatibility claims.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import uuid

import awo_service as service
import collaboration_runtime as collaboration
import merge_queue as merge
import service_access as access
import service_extensions as extensions
from service_api import APIError
import test_service_api as fixtures
import test_collaboration_runtime as collaboration_fixtures
import test_merge_queue as merge_fixtures


class BoundaryHelpers:
    def issue(self, role='controller', agent='planner', root=None, **scope):
        payload = dict(project_root=str(root or self.root), role=role,
                       agent_id=self.f.agents[agent], **scope)
        if role in ('controller', 'reviewer'):
            payload.update(binding_id=self.f.actor[0], binding_version=self.f.actor[1])
        path = self.f.root / ('extension-secret-' + uuid.uuid4().hex)
        access.issue_token(self.state, *self.f.actor, self.f.envelope('access.issue', payload), str(path))
        return path.read_text(encoding='utf-8').strip()

    def args(self, payload):
        return dict(payload=payload, command_id='domain-' + uuid.uuid4().hex, expected_epoch=self.f.epoch)

    def call(self, name, payload, token=None, arguments=None):
        args = arguments if arguments is not None else (
            {'payload': payload} if name in extensions.QUERIES else self.args(payload))
        return extensions.call(self.state, str(self.root), token or self.controller, name, args)

    def narrow(self, token, **changes):
        # Real credential attenuation in SQLite simulates operator scope changes.
        principal = access.principal(self.state, str(self.root), token)
        with self.f.db() as con:
            for key, value in changes.items():
                self.assertIn(key, ('task_id', 'assignment_id', 'knowledge_id'))
                con.execute('UPDATE v2_access_token SET ' + key + '=? WHERE token_id=?',
                            (value, principal['token_id']))


class DomainExtensionTests(BoundaryHelpers, unittest.TestCase):
    def setUp(self):
        self.api = fixtures.ServiceAPITests()
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)
        self.f = self.api.f
        self.root, self.state = self.f.root, self.f.state
        self.controller, self.worker = self.api.controller, self.api.worker
        self.f.create('T-A')
        self.f.create('T-B')

    def config(self, task='T-A'):
        return dict(task_id=task, expected_revision=1, mode='fanout',
                    slots=[{'slot_id': 1, 'agent_id': self.f.agents['executor']},
                           {'slot_id': 2, 'agent_id': self.f.agents['second']}],
                    max_attempts=4, max_repairs=1, verifier={
                        'argv': [sys.executable, '-B', '-c', 'print(42)'],
                        'timeout_seconds': 3, 'max_output_bytes': 4096,
                        'max_runs_per_assignment': 2})

    def policy(self):
        repo = self.root / 'repo'
        operations, archive = self.root / 'operations', self.root / 'archive'
        for path in (repo, operations, archive):
            path.mkdir(exist_ok=True)
        executable = str(Path(sys.executable).resolve())
        return dict(repository_id='boundary-repo', repo_root=str(repo),
                    operation_root=str(operations), archive_root=str(archive),
                    verify_argv=[executable, '-B', '-c', 'print(42)'],
                    pins={executable: service._file_sha(executable)})

    def test_exact_extension_catalog_and_unchanged_core(self):
        names = {s['name'] for s in extensions.tool_specs()}
        self.assertEqual(names, extensions.COMMANDS | extensions.QUERIES)
        self.assertEqual(len(names), 39)
        self.assertEqual(len(service.gateway_tool_specs()), 13)
        self.assertEqual(len(service.gateway_tool_specs(True)), 16)
        self.assertNotIn('run_verification', names)
        self.assertNotIn('execute_intent', names)
        for tool in ('merge.execute', 'collaboration_run', 'awo_collaboration_configure'):
            with self.assertRaises(APIError):
                self.call(tool, {})

    def test_configure_query_and_persistent_replay(self):
        payload = self.config()
        args = self.args(payload)
        first = self.call('collaboration_configure', payload, arguments=args)
        self.assertFalse(first['replayed'])
        self.assertTrue(self.call('collaboration_configure', payload, arguments=args)['replayed'])
        self.assertEqual(len(self.f.rows('SELECT * FROM v2_collaboration_policy')), 1)
        self.assertEqual(len(self.call('collaboration_history', {'task_id': 'T-A'})['events']), 1)
        self.assertEqual(self.call('collaboration_get', {'task_id': 'T-A'})['config']['mode'], 'fanout')
        with self.assertRaises(APIError) as caught:
            self.call('collaboration_configure', payload, arguments=dict(args, payload=self.config('T-B')))
        self.assertEqual(caught.exception.code, 'idempotency_conflict')

    def test_controller_scope_and_role_checks_precede_replay(self):
        args = self.args(self.config())
        self.call('collaboration_configure', {}, arguments=args)
        for token in (self.worker, self.issue('observer', 'planner'),
                      self.issue('reviewer', task_id='T-A'), self.issue(task_id='T-A')):
            with self.subTest(token_role=access.principal(self.state, str(self.root), token)['role']):
                with self.assertRaises(APIError) as caught:
                    self.call('collaboration_configure', {}, token, args)
                self.assertEqual(caught.exception.code, 'forbidden_role')
        self.narrow(self.controller, task_id='T-A')
        with self.assertRaises(APIError):
            self.call('collaboration_configure', {}, arguments=args)

    def test_revocation_and_epoch_precede_replay(self):
        args = self.args(self.config())
        self.call('collaboration_configure', {}, arguments=args)
        with self.assertRaises(APIError) as caught:
            self.call('collaboration_configure', {}, arguments=dict(args, expected_epoch=self.f.epoch + 1))
        self.assertEqual(caught.exception.code, 'revision_conflict')
        principal = access.principal(self.state, str(self.root), self.controller)
        access.revoke_token(self.state, *self.f.actor, self.f.envelope('access.revoke',
                            {'token_id': principal['token_id'], 'expected_revision': 1}))
        with self.assertRaises(APIError):
            self.call('collaboration_configure', {}, arguments=args)

    def test_freeze_precedes_replay_but_queries_remain_read_only(self):
        args = self.args(self.config())
        self.call('collaboration_configure', {}, arguments=args)
        service.set_freeze(self.state, *self.f.actor, True, 'focused fixture')
        with self.assertRaises(APIError):
            self.call('collaboration_configure', {}, arguments=args)
        before = self.f.snapshot()
        self.call('collaboration_get', {'task_id': 'T-A'})
        self.call('collaboration_history', {'task_id': 'T-A'})
        self.call('merge_queue', {})
        self.call('merge_intents', {'task_id': 'T-A'})
        self.assertEqual(self.f.snapshot(), before)

    def test_wrong_root_and_cross_project_task_rejected(self):
        with self.assertRaises(APIError):
            extensions.call(self.state, str(self.state), self.controller,
                            'collaboration_configure', self.args(self.config()))
        with self.f.db() as con:
            project = dict(con.execute('SELECT * FROM v2_project WHERE project_id=?', (self.f.project,)).fetchone())
            project['project_id'] = 'foreign-project'
            columns = ','.join(project)
            con.execute('INSERT INTO v2_project (' + columns + ') VALUES (' +
                        ','.join('?' for _ in project) + ')', tuple(project.values()))
            con.execute("UPDATE v2_task SET project_id='foreign-project' WHERE task_id='T-B'")
        for name, payload in (('collaboration_configure', self.config('T-B')),
                              ('merge_intents', {'task_id': 'T-B'}),
                              ('merge_queue', {'task_id': 'T-B'})):
            with self.subTest(name=name), self.assertRaises(APIError):
                self.call(name, payload)

    def test_knowledge_entity_credential_never_grants_domain_control_or_reads(self):
        service.execute_command(self.state, *self.f.actor, self.f.envelope('project.bind_root',
            {'project_root': str(self.root), 'expected_revision': 0}))
        doc = self.call('doc_import', {'title': 'Scoped document', 'scope': 'project', 'content': 'bytes'})
        token = self.issue(knowledge_id=doc['doc_id'])
        for name, payload in (('collaboration_configure', self.config()), ('merge.policy', {}),
                              ('merge.review', {}), ('merge_queue', {}),
                              ('merge_intents', {'task_id': 'T-A'}),
                              ('collaboration_history', {'task_id': 'T-A'})):
            with self.subTest(name=name), self.assertRaises(APIError) as caught:
                self.call(name, payload, token)
            self.assertEqual(caught.exception.code, 'forbidden_role')
        self.assertEqual(self.call('doc_get', {'doc_id': doc['doc_id']}, token)['doc_id'], doc['doc_id'])

    def test_task_scope_queries_cannot_inspect_another_task(self):
        for task in ('T-A', 'T-B'):
            self.call('collaboration_configure', self.config(task))
        token = self.issue('worker', 'executor', task_id='T-A')
        self.assertEqual(self.call('collaboration_get', {'task_id': 'T-A'}, token)['task']['task_id'], 'T-A')
        for name in ('collaboration_get', 'collaboration_history', 'merge_queue', 'merge_intents'):
            with self.subTest(name=name), self.assertRaises(APIError):
                self.call(name, {'task_id': 'T-B'}, token)

    def test_envelope_and_domain_payload_validation(self):
        for args in ({'payload': self.config()}, dict(self.args(self.config()), expected_epoch=True),
                     dict(self.args(self.config()), actor='forged'), self.args({'task_id': []}),
                     self.args(dict(self.config(), principal={'role': 'controller'}))):
            with self.subTest(args=args.keys()), self.assertRaises(APIError):
                self.call('collaboration_configure', {}, arguments=args)
        with self.assertRaises(APIError):
            self.call('merge_intents', {'task_id': 'T-A', 'operation_token': 'injected'})
        self.assertEqual(self.f.rows('SELECT * FROM v2_collaboration_policy'), [])

    def test_merge_policy_root_binding_and_external_roots(self):
        payload = self.policy()
        with self.assertRaises(APIError) as caught:
            self.call('merge.policy', payload)
        self.assertEqual(caught.exception.code, 'project_mismatch')
        self.root = Path(payload['repo_root'])
        self.controller = self.issue(root=self.root)
        args = self.args(payload)
        # Negative mock: fail if a command tries to launch any external process.
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('external process in command')):
            result = self.call('merge.policy', payload, arguments=args)
            self.assertTrue(self.call('merge.policy', payload, arguments=args)['replayed'])
        self.assertEqual(result['status'], 'APPROVED')
        self.assertEqual(list(Path(payload['operation_root']).iterdir()), [])
        self.assertEqual(list(Path(payload['archive_root']).iterdir()), [])
        with self.assertRaises(APIError):
            self.call('merge.policy', payload, self.issue(task_id='T-A'), args)

    def test_actual_backend_subprocess_routes_extensions_without_expanding_core(self):
        from mcp_gateway import GatewayError, ServiceBackend
        backend = ServiceBackend(self.state, self.root, extensions_enabled=True)
        args = self.args(self.config())
        self.assertFalse(backend.call(self.controller, 'collaboration_configure', args)['replayed'])
        self.assertTrue(self.call('collaboration_configure', {}, arguments=args)['replayed'])
        self.assertEqual(backend.call(self.controller, 'merge_queue', {'payload': {}}), {'items': []})
        self.assertEqual(backend.call(self.controller, 'merge_intents', {'payload': {'task_id': 'T-A'}}), {'items': []})
        self.assertEqual(len(backend.specs(self.controller)), 52)
        self.assertEqual(len(ServiceBackend(self.state, self.root).specs(self.controller)), 13)
        with self.assertRaises(GatewayError):
            ServiceBackend(self.state, self.root).call(self.controller, 'collaboration_get', {'payload': {'task_id': 'T-A'}})


class VerifierBoundaryTests(BoundaryHelpers, unittest.TestCase):
    def setUp(self):
        self.f = collaboration_fixtures.CollaborationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.state, self.root = self.f.state, self.f.root
        self.controller = self.issue()

    def test_verify_commits_ready_intent_and_never_runs_in_service_transaction(self):
        marker = self.root / 'verifier-effect'
        self.f.configure(code='from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("ran")')
        admission, event = self.f.ready()
        payload = dict(task_id='T-A', expected_revision=self.f.revision(),
                       assignment_id=admission['assignment_id'], event_id=event)
        args = self.args(payload)
        # Negative mock only: the sidecar must not reach Popen through this route.
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('verifier ran in envelope')):
            result = self.call('collaboration_verify', payload, arguments=args)
            self.assertTrue(self.call('collaboration_verify', payload, arguments=args)['replayed'])
        self.assertFalse(marker.exists())
        row = self.f.rows('SELECT * FROM v2_collaboration_verify_intent')[0]
        self.assertEqual(row['state'], 'ready')
        principal = access.principal(self.state, str(self.root), self.controller)
        con = service._connect(service._db_path(self.state))
        try:
            receipt = collaboration.run_verification(con, self.f.project, principal, result['verification_id'])
        finally:
            con.close()
        self.assertTrue(marker.exists())
        self.assertEqual(receipt['status'], 'RECORDED')
        self.assertEqual(len(self.f.rows('SELECT * FROM v2_collaboration_verification')), 1)

    def test_assignment_token_cannot_read_task_wide_collaboration(self):
        self.f.configure()
        admission = self.f.admit(self.f.prepare())
        scoped = self.issue('observer', 'executor', assignment_id=admission['assignment_id'])
        for name in ('collaboration_get', 'collaboration_history'):
            with self.assertRaises(APIError) as caught:
                self.call(name, {'task_id': 'T-A'}, scoped)
            self.assertEqual(caught.exception.code, 'forbidden_role')


class NativeMergeBoundaryTests(BoundaryHelpers, unittest.TestCase):
    def setUp(self):
        self.native = merge_fixtures.NativeMergeTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.item = self.native.accepted()
        self.f = self.native.f
        self.root, self.state = self.item['repo'], self.f.state
        self.controller = self.issue()
        self.reviewer = self.issue('reviewer', task_id=self.item['task_id'], assignment_id=self.item['assignment_id'])
        # Reuse native fixture helpers, but send every subsequent command through
        # the real authenticated boundary. Explicit execution stays post-commit.
        self.native.command = lambda name, payload, role='controller': self.call(
            name, payload, self.reviewer if role == 'reviewer' else self.controller)

    def test_native_merge_intent_review_identity_and_replay_scope(self):
        identity = self.native.propose()
        self.native.action(identity, 'merge.enqueue')
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('Git ran in command')):
            ticket = self.native.action(identity, 'merge.begin', kind='build')
        self.assertEqual(self.f.rows('SELECT state FROM v2_merge_intent')[0]['state'], 'ready')
        self.native.finish(ticket)
        row = self.native.row(identity)
        candidate = json.loads(row['candidate_json'])
        note = {key: candidate[key] for key in ('task_id', 'assignment_id', 'generation')}
        note.update(candidate_sha256=merge._digest(candidate), verdict='accept',
                    reviewer_agent_id=self.f.agents['planner'])
        path = self.f.root / 'extension-review.json'
        path.write_text(json.dumps(note), encoding='utf-8')
        payload = dict(merge_request_id=identity, expected_revision=row['revision'],
                       note_path=str(path), note_sha256=service._file_sha(path))
        args = self.args(payload)
        result = self.call('merge.review', payload, self.reviewer, args)
        self.assertFalse(result['replayed'])
        from mcp_gateway import ServiceBackend
        self.assertTrue(ServiceBackend(self.state, self.root, extensions_enabled=True).call(
            self.reviewer, 'merge.review', args)['replayed'])
        saved = json.loads(self.native.row(identity)['review_json'])
        self.assertEqual(saved['reviewer_agent_id'], self.f.agents['planner'])
        self.assertEqual(saved['binding_id'], self.f.actor[0])
        self.assertEqual(saved['binding_version'], self.f.actor[1])
        with self.assertRaises(APIError):
            self.call('merge.begin', {'merge_request_id': identity,
                      'expected_revision': self.native.row(identity)['revision']}, self.reviewer)
        self.f.create('UNRELATED')
        self.narrow(self.reviewer, task_id='UNRELATED', assignment_id=None)
        with self.assertRaises(APIError):
            self.call('merge.review', payload, self.reviewer, args)

    def test_native_merge_queries_scope_no_tokens_or_foreign_task_leak(self):
        identity = self.native.propose()
        self.native.action(identity, 'merge.enqueue')
        ticket = self.native.action(identity, 'merge.begin', kind='build')
        query = self.call('merge_queue', {}, self.reviewer)
        self.assertEqual([x['merge_request_id'] for x in query['items']], [identity])
        intents = self.call('merge_intents', {'task_id': self.item['task_id']}, self.reviewer)
        self.assertEqual([x['intent_id'] for x in intents['items']], [ticket['intent_id']])
        self.assertNotIn(ticket['operation_token'], json.dumps((query, intents)))
        self.f.create('UNRELATED')
        other = self.issue('reviewer', task_id='UNRELATED')
        self.assertEqual(self.call('merge_queue', {}, other), {'items': []})
        for name, payload in (('merge_queue', {'merge_request_id': identity}),
                              ('merge_intents', {'task_id': self.item['task_id']})):
            with self.assertRaises(APIError):
                self.call(name, payload, other)
        outsider = self.issue('worker', 'second')
        self.assertEqual(self.call('merge_queue', {}, outsider), {'items': []})
        with self.assertRaises(APIError):
            self.call('merge_intents', {'task_id': self.item['task_id']}, outsider)

    def test_reviewer_assignment_scope_and_policy_root_rechecked(self):
        identity = self.native.propose()
        queued, _ = self.f.enqueue(task='OTHER', role='second')
        other = self.f.claim(task_id='OTHER', expected_revision=queued['revision'])
        token = self.issue('reviewer', assignment_id=other['assignment_id'])
        with self.assertRaises(APIError):
            self.call('merge.review', {'merge_request_id': identity, 'expected_revision': 1}, token)
        wrong_root = self.f.root / 'other-root'
        wrong_root.mkdir()
        wrong = self.issue(root=wrong_root)
        args = self.args({'merge_request_id': identity, 'expected_revision': 1})
        self.call('merge.enqueue', {}, arguments=args)
        with self.assertRaises(APIError) as caught:
            extensions.call(self.state, str(wrong_root), wrong, 'merge.enqueue', args)
        self.assertEqual(caught.exception.code, 'project_mismatch')
        self.assertEqual(extensions.call(self.state, str(wrong_root), wrong,
                         'merge_queue', {'payload': {}}), {'items': []})


if __name__ == '__main__':
    unittest.main()
