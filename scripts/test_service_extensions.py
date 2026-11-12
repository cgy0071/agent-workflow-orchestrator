"""Application integration for knowledge/docs using actual SQLite and IPC."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

import awo_service as service
import service_access as access
import service_extensions as extensions
from service_api import APIError
import test_service_api as fixtures
from test_service_http import ServerFixture, client_frame, server_frame


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceAPITests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root, self.state = self.f.root, self.f.state
        self.bind = self.f.f.envelope('project.bind_root',
                    {'project_root': str(self.root), 'expected_revision': 0})
        service.execute_command(self.state, *self.f.f.actor, self.bind)

    def call(self, name, payload, token=None, arguments=None):
        args = arguments or ({'payload': payload} if name in extensions.knowledge.QUERIES
                             else self.f.args(payload=payload))
        return extensions.call(self.state, str(self.root), token or self.f.worker, name, args)

    def doc(self, token=None):
        return self.call('doc_import', {'title': 'Operating instructions',
                                       'scope': 'project', 'content': 'Verified local content'}, token)

    def review(self, item, token):
        view = self.call('doc_get', {'doc_id': item['doc_id']}, token)
        return self.call('doc_review', {key: view[key] for key in ('doc_id', 'revision_sha256', 'evidence_sha256')} |
                         {'expected_revision': view['revision'], 'verdict': 'accept',
                          'evidence_reviewed': True, 'comment': 'Reviewed exact content'}, token)

    def test_explicit_immutable_root_and_replay(self):
        self.assertTrue(service.execute_command(self.state, *self.f.f.actor, self.bind)['replayed'])
        with self.assertRaises(APIError):
            service.execute_command(self.state, *self.f.f.actor, self.f.f.envelope(
                'project.bind_root', {'project_root': str(self.state), 'expected_revision': 1}))
        self.assertEqual(str(self.state.resolve()),
                         self.f.f.rows('SELECT root_path FROM v2_project')[0]['root_path'])

    def test_document_review_and_explicit_publish_same_agent_identity_preserved(self):
        item = self.doc()
        reviewer = self.f.issue('reviewer', 'planner', knowledge_id=item['doc_id'])
        reviewed = self.review(item, reviewer)
        self.assertEqual(reviewed['state'], 'reviewed')
        result = self.call('doc_publish', {k: reviewed[k] for k in
                           ('doc_id', 'review_id', 'revision_sha256', 'content_sha256')} |
                           {'expected_revision': reviewed['revision']}, self.f.controller)
        self.assertEqual(result['state'], 'published')
        self.assertTrue(self.call('doc_get', {'doc_id': item['doc_id']})['usable'])
        row = self.f.f.rows('SELECT * FROM v2_knowledge_review')[0]
        self.assertEqual(row['reviewer_agent_id'], self.f.f.agents['planner'])

    def test_author_controller_cannot_review_via_narrowed_reviewer_credential(self):
        item = self.doc(self.f.controller)
        reviewer = self.f.issue('reviewer', 'planner', knowledge_id=item['doc_id'])
        with self.assertRaises(APIError):
            self.review(item, reviewer)
        self.assertEqual(self.f.f.rows('SELECT * FROM v2_knowledge_review'), [])

    def test_entity_scope_and_task_scope_do_not_cross(self):
        first, second = self.doc(), self.doc()
        scoped = self.f.issue('reviewer', 'planner', knowledge_id=first['doc_id'])
        for tool, payload in (('doc_get', {'doc_id': second['doc_id']}), ('doc_list', {})):
            with self.assertRaises(APIError):
                self.call(tool, payload, scoped)
        with self.assertRaises(APIError):
            self.f.call(scoped, 'awo_task_list')
        self.f.f.create()
        task_token = self.f.issue('worker', 'executor', task_id='T-A')
        with self.assertRaises(APIError):
            self.call('doc_get', {'doc_id': first['doc_id']}, task_token)

    def test_auth_role_and_revocation_before_durable_replay(self):
        payload = {'title': 'One document', 'scope': 'project', 'content': 'Only one'}
        args = self.f.args(payload=payload)
        first = self.call('doc_import', payload, arguments=args)
        self.assertTrue(self.call('doc_import', payload, arguments=args)['replayed'])
        observer = self.f.issue('observer', 'executor')
        with self.assertRaises(APIError):
            self.call('doc_import', payload, observer, args)
        p = access.principal(self.state, str(self.root), self.f.worker)
        access.revoke_token(self.state, *self.f.f.actor, self.f.f.envelope('access.revoke',
                           {'token_id': p['token_id'], 'expected_revision': 1}))
        with self.assertRaises(APIError):
            self.call('doc_import', payload, arguments=args)
        self.assertEqual(len(self.f.f.rows('SELECT * FROM v2_knowledge')), 1)

    def test_project_file_approval_uses_content_root_and_blocks_mutated_proof(self):
        proof = self.root / 'proof.txt'
        proof.write_text('Original proof', encoding='utf-8')
        sha = hashlib.sha256(proof.read_bytes()).hexdigest()
        source = self.call('knowledge_source_approve', {'relative_path': 'proof.txt', 'sha256': sha}, self.f.controller)
        doc = self.call('doc_import', {'title': 'Local source', 'scope': 'project',
                                     'source_id': source['source_id'], 'content_sha256': sha})
        proof.write_text('Replaced proof', encoding='utf-8')
        reviewer = self.f.issue('reviewer', 'planner', knowledge_id=doc['doc_id'])
        with self.assertRaises(APIError):
            self.review(doc, reviewer)

    def test_extension_cli_and_actual_gateway_subprocess_share_command(self):
        from mcp_gateway import ServiceBackend, GatewayError
        arguments = self.f.args(payload={'title': 'Cross-entry', 'scope': 'project', 'content': 'Same bytes'})
        token_file, argument_file = self.root / 'entry-secret', self.root / 'entry-arguments.json'
        token_file.write_text(self.f.worker, encoding='utf-8')
        argument_file.write_text(json.dumps(arguments), encoding='utf-8')
        proc = subprocess.run([sys.executable, '-B', str(Path(service.__file__)), '--state', str(self.state),
                               'extension', '--project-root', str(self.root), '--token-file', str(token_file),
                               '--tool', 'doc_import', '--arguments', str(argument_file)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(json.loads(proc.stdout)['replayed'])
        backend = ServiceBackend(self.state, self.root, extensions_enabled=True)
        self.assertTrue(backend.call(self.f.worker, 'doc_import', arguments)['replayed'])
        self.assertEqual(len(backend.specs(self.f.worker)),
                         len(service.gateway_tool_specs()) + len(extensions.tool_specs()))
        with self.assertRaises(GatewayError):
            ServiceBackend(self.state, self.root).call(self.f.worker, 'doc_import', arguments)
        self.assertEqual(len(self.f.f.rows('SELECT * FROM v2_knowledge')), 1)
        self.assertEqual(len(service.gateway_tool_specs()), 13)

    def test_extension_epoch_freeze_and_invalid_fields(self):
        for args in ({'payload': {}}, self.f.args(payload={'title': 'x', 'injected': True})):
            with self.assertRaises(APIError):
                self.call('doc_import', {}, arguments=args)
        service.set_freeze(self.state, *self.f.f.actor, True, 'inspection')
        with self.assertRaises(APIError):
            self.doc()
        self.assertEqual(self.call('doc_list', {})['items'], [])


    def approved_proof(self, name='fact-proof.txt', text='Independent measurement: 4\n'):
        (self.root / name).write_text(text, encoding='utf-8')
        digest = hashlib.sha256((self.root / name).read_bytes()).hexdigest()
        existing = self.f.f.rows('SELECT source_id,sha256 FROM v2_knowledge_source '
                                 'WHERE relative_path=? AND sha256=?', (name, digest))
        if existing:
            return {'kind': 'file', 'source_id': existing[0]['source_id'], 'sha256': digest}
        source = self.call('knowledge_source_approve',
                           {'relative_path': name, 'sha256': digest}, self.f.controller)
        return {'kind': 'file', 'source_id': source['source_id'], 'sha256': digest}

    def fact(self, refs, source='human', **changes):
        payload = {'kind': 'fact', 'title': 'Measured sum', 'scope': 'project',
                   'body': 'The measured sum is four.', 'source': source,
                   'confidence': 'high', 'source_refs': refs}
        payload.update(changes)
        return payload

    def test_source_label_cannot_bypass_fact_evidence_through_service_or_cli(self):
        inline = 'Owner said two plus two is four.'
        payload = self.fact([{'kind': 'inline', 'content': inline, 'label': 'chat transcript',
                              'sha256': hashlib.sha256(inline.encode()).hexdigest()}])
        with self.assertRaises(APIError) as caught:
            self.call('knowledge_propose', payload)
        self.assertEqual('evidence_required', caught.exception.code)
        # A caller-supplied author/role is rejected, never trusted as provenance.
        with self.assertRaises(APIError) as caught:
            self.call('knowledge_propose', dict(payload, author_agent_id='planner', role='controller'))
        self.assertEqual('invalid_payload', caught.exception.code)
        # The same rejection covers the supported CLI/extension subprocess.
        token_file, argument_file = self.root / 'label-secret', self.root / 'label-arguments.json'
        token_file.write_text(self.f.worker, encoding='utf-8')
        arguments = self.f.args(payload=payload)
        argument_file.write_text(json.dumps(arguments), encoding='utf-8')
        proc = subprocess.run([sys.executable, '-B', str(Path(service.__file__)), '--state', str(self.state),
                               'extension', '--project-root', str(self.root),
                               '--token-file', str(token_file), '--tool', 'knowledge_propose',
                               '--arguments', str(argument_file)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(2, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn('replayable local evidence', proc.stderr)
        # No knowledge, revision or review record was created by any attempt.
        for table in ('v2_knowledge', 'v2_knowledge_revision', 'v2_knowledge_review',
                      'v2_knowledge_sync'):
            self.assertEqual([], self.f.f.rows('SELECT * FROM ' + table), table)
        rejected = {json.loads(row['payload_json'])['code'] for row in self.f.f.rows(
            "SELECT payload_json FROM v2_event WHERE event_type='command.rejected'")}
        self.assertEqual({'evidence_required', 'invalid_payload'}, rejected)

    def test_fact_provenance_persists_and_replay_cannot_change_it(self):
        payload = self.fact([self.approved_proof()])
        args = self.f.args(payload=payload)
        proposed = self.call('knowledge_propose', payload, arguments=args)
        replay = self.call('knowledge_propose', payload, arguments=args)
        self.assertTrue(replay['replayed'])
        self.assertEqual(proposed['revision_sha256'], replay['revision_sha256'])
        reviewer = self.f.issue('reviewer', 'planner', knowledge_id=proposed['knowledge_id'])
        view = self.call('knowledge_get', {'knowledge_id': proposed['knowledge_id']}, reviewer)
        reviewed = self.call('knowledge_review', {k: view[k] for k in
                             ('knowledge_id', 'revision_sha256', 'evidence_sha256')} |
                             {'expected_revision': view['revision'], 'verdict': 'accept',
                              'evidence_reviewed': True,
                              'comment': 'Verified the approved local measurement file.'}, reviewer)
        promoted = self.call('knowledge_promote', {k: reviewed[k] for k in
                             ('knowledge_id', 'review_id', 'revision_sha256')} |
                             {'expected_revision': reviewed['revision']}, self.f.controller)
        final = self.call('knowledge_get', {'knowledge_id': promoted['knowledge_id']})
        self.assertTrue(final['usable'])
        self.assertEqual('active', final['state'])
        self.assertFalse(final['source_authenticated'])
        self.assertEqual('authenticated-submission', final['provenance_class'])
        self.assertEqual({'declared': 'human', 'authenticated': False},
                         final['source_assertion'])
        self.assertEqual(self.f.f.agents['executor'], final['submitted_by']['agent_id'])
        self.assertEqual(self.f.f.agents['planner'], final['review']['reviewer_agent_id'])
        self.assertEqual(1, len(self.f.f.rows('SELECT * FROM v2_knowledge_revision')))
        # Reusing a command id with different bytes is an idempotency conflict;
        # the recorded revision and its provenance stay untouched.
        with self.assertRaises(APIError) as caught:
            self.call('knowledge_propose', dict(payload, body='changed bytes'),
                      arguments=dict(args, payload=dict(payload, body='changed bytes')))
        self.assertEqual('idempotency_conflict', caught.exception.code)
        self.assertEqual(1, len(self.f.f.rows('SELECT * FROM v2_knowledge_revision')))
        self.assertEqual(final['revision_sha256'],
                         self.call('knowledge_get', {'knowledge_id': promoted['knowledge_id']})['revision_sha256'])

    def test_author_cannot_relabel_via_revision_or_review_own_fact(self):
        convention = self.call('knowledge_propose', {
            'kind': 'convention', 'title': 'Team convention', 'scope': 'project',
            'body': 'Use the bounded path.', 'source': 'human', 'confidence': 'unknown',
            'source_refs': [self.approved_proof()]})
        inline = 'The author now claims this is a fact.'
        with self.assertRaises(APIError) as caught:
            self.call('knowledge_revise', {
                'knowledge_id': convention['knowledge_id'],
                'expected_revision': convention['revision'], 'kind': 'fact',
                'title': 'Relabeled', 'scope': 'project', 'body': inline,
                'source': 'human', 'confidence': 'high',
                'source_refs': [{'kind': 'inline', 'content': inline, 'label': 'note',
                                 'sha256': hashlib.sha256(inline.encode()).hexdigest()}]})
        self.assertEqual('evidence_required', caught.exception.code)
        self.assertEqual(1, len(self.f.f.rows('SELECT * FROM v2_knowledge_revision')))
        self.assertEqual('proposed', self.call('knowledge_get', {
            'knowledge_id': convention['knowledge_id']})['state'])
        # The author's own narrowed reviewer credential cannot review the Fact.
        owned = self.call('knowledge_propose', self.fact([self.approved_proof()]), self.f.controller)
        reviewer = self.f.issue('reviewer', 'planner', knowledge_id=owned['knowledge_id'])
        review = self.call('knowledge_get', {'knowledge_id': owned['knowledge_id']}, reviewer)
        with self.assertRaises(APIError) as caught:
            self.call('knowledge_review', {k: review[k] for k in
                      ('knowledge_id', 'revision_sha256', 'evidence_sha256')} |
                      {'expected_revision': review['revision'], 'verdict': 'accept',
                       'evidence_reviewed': True, 'comment': 'Self-approval attempt.'}, reviewer)
        self.assertEqual('independent_review_required', caught.exception.code)
        with self.assertRaises(APIError) as caught:
            self.call('knowledge_promote', {'knowledge_id': owned['knowledge_id'],
                      'expected_revision': review['revision'], 'review_id': 'kr-absent',
                      'revision_sha256': review['revision_sha256']}, reviewer)
        self.assertEqual('independent_review_required', caught.exception.code)
        self.assertEqual([], self.f.f.rows('SELECT * FROM v2_knowledge_review'))
        self.assertEqual('proposed', self.call('knowledge_get', {
            'knowledge_id': owned['knowledge_id']})['state'])

    def test_revoked_credential_is_rejected_before_knowledge_replay(self):
        payload = {'kind': 'convention', 'title': 'Team convention', 'scope': 'project',
                   'body': 'Bounded replay check.', 'source': 'human',
                   'confidence': 'unknown', 'source_refs': [self.approved_proof()]}
        args = self.f.args(payload=payload)
        self.call('knowledge_propose', payload, arguments=args)
        self.assertTrue(self.call('knowledge_propose', payload, arguments=args)['replayed'])
        p = access.principal(self.state, str(self.root), self.f.worker)
        access.revoke_token(self.state, *self.f.f.actor, self.f.f.envelope(
            'access.revoke', {'token_id': p['token_id'], 'expected_revision': 1}))
        with self.assertRaises(APIError) as caught:
            self.call('knowledge_propose', payload, arguments=args)
        self.assertEqual('unauthenticated', caught.exception.code)
        self.assertEqual(1, len(self.f.f.rows('SELECT * FROM v2_knowledge')))

    def test_expired_credential_is_rejected_before_knowledge_replay(self):
        path = self.root / 'expiring-knowledge-token'
        access.issue_token(self.state, *self.f.f.actor, self.f.f.envelope('access.issue', {
            'project_root': str(self.root), 'role': 'worker',
            'agent_id': self.f.f.agents['executor'], 'expires_minutes': 1}), str(path))
        token = path.read_text().strip()
        payload = {'kind': 'convention', 'title': 'Expiring credential',
                   'scope': 'project', 'body': 'Bounded expiry check.', 'source': 'human',
                   'confidence': 'unknown', 'source_refs': [self.approved_proof()]}
        args = self.f.args(payload=payload)
        self.call('knowledge_propose', payload, token, arguments=args)
        self.assertTrue(self.call('knowledge_propose', payload, token, arguments=args)['replayed'])
        con = service._connect(service._db_path(self.state))
        try:
            con.execute("UPDATE v2_access_token SET expires_at='2000-01-01T00:00:00+00:00' "
                        "WHERE revoked_at IS NULL AND agent_id=?",
                        (self.f.f.agents['executor'],))
            con.commit()
        finally:
            con.close()
        with self.assertRaises(APIError) as caught:
            self.call('knowledge_propose', payload, token, arguments=args)
        self.assertEqual('unauthenticated', caught.exception.code)
        self.assertEqual(1, len(self.f.f.rows('SELECT * FROM v2_knowledge')))

    def test_scoped_and_cross_project_claims_are_denied_precisely(self):
        first = self.call('knowledge_propose', {'kind': 'convention', 'title': 'First',
                           'scope': 'project', 'body': 'First.', 'source': 'human',
                           'confidence': 'unknown', 'source_refs': [self.approved_proof()]})
        second = self.call('knowledge_propose', {'kind': 'convention', 'title': 'Second',
                            'scope': 'project', 'body': 'Second.', 'source': 'human',
                            'confidence': 'unknown', 'source_refs': [self.approved_proof()]})
        scoped = self.f.issue('reviewer', 'planner', knowledge_id=first['knowledge_id'])
        for tool, payload in (('knowledge_get', {'knowledge_id': second['knowledge_id']}),
                              ('knowledge_review', {'knowledge_id': second['knowledge_id'],
                               'expected_revision': 1, 'verdict': 'accept',
                               'evidence_reviewed': True, 'comment': 'Out of scope.',
                               'revision_sha256': 'a' * 64, 'evidence_sha256': 'b' * 64}),
                              ('knowledge_list', {})):
            with self.subTest(tool=tool):
                with self.assertRaises(APIError) as caught:
                    self.call(tool, payload, scoped)
                self.assertEqual('forbidden_role', caught.exception.code)
        with self.assertRaises(APIError) as caught:
            extensions.call(self.state, str(self.state), self.f.worker, 'knowledge_propose',
                            self.f.args(payload={'kind': 'convention', 'title': 'Wrong root',
                                                 'scope': 'project', 'body': 'Wrong.',
                                                 'source': 'human', 'confidence': 'unknown',
                                                 'source_refs': [self.approved_proof()]}))
        self.assertEqual('project_mismatch', caught.exception.code)
        self.assertEqual([], self.f.f.rows('SELECT * FROM v2_knowledge_review'))
        self.assertEqual(['proposed', 'proposed'],
                         [row['state'] for row in self.f.f.rows(
                             'SELECT state FROM v2_knowledge ORDER BY entity_id')])


class ExtensionWireTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        from mcp_gateway import ServiceBackend
        self.f = ExtensionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.token = self.f.f.worker
        self.start_server(ServiceBackend(self.f.state, self.f.root, extensions_enabled=True))

    def test_http_websocket_and_cli_boundary_share_extension_replay(self):
        value = {'id': 1, 'tool': 'doc_import', 'arguments': self.f.f.args(
            payload={'title': 'Wire document', 'scope': 'project', 'content': 'Protocol verified'})}
        status, response, unused = self.request(value)
        self.assertEqual(status, 200, response)
        self.assertFalse(response['result']['replayed'])
        client, source, status, unused = self.websocket()
        self.assertEqual(status, 101)
        client.sendall(client_frame(1, json.dumps(dict(value, id=2)).encode()))
        replay = json.loads(server_frame(source)[1])
        self.assertTrue(replay['result']['replayed'], replay)
        self.assertTrue(extensions.call(self.f.state, str(self.f.root), self.token,
                                       value['tool'], value['arguments'])['replayed'])
        self.assertEqual(len(self.f.f.f.rows('SELECT * FROM v2_knowledge')), 1)


if __name__ == '__main__':
    unittest.main()
