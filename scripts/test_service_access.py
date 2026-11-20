"""Real SQLite authorization checks for the shared application transport boundary."""
import json
from pathlib import Path
import unittest
import uuid

import awo_service as service
import service_access as access
import test_awo_registry as fixtures


class ServiceAccessTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RegistryTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.state, self.actor = self.f.state, self.f.actor
        self.worker_id = service._agent_id_for_binding(self.f.worker['instance_id'])
        self.controller_id = service._agent_id_for_binding(self.actor[0])

    def issue(self, role='worker', **overrides):
        payload = {'project_root': str(self.f.root), 'agent_id': self.worker_id, 'role': role}
        if role == 'controller':
            payload.update(agent_id=self.controller_id, binding_id=self.actor[0],
                           binding_version=self.actor[1])
        payload.update(overrides)
        path = self.f.secrets / ('access-' + uuid.uuid4().hex + '.txt')
        env = self.f.envelope('access.issue', payload)
        result = access.issue_token(self.state, *self.actor, env, str(path))
        return result, path.read_text(encoding='utf-8').strip(), env

    def test_root_identity_and_role_derive_from_stored_credential(self):
        result, token, env = self.issue()
        principal = access.principal(self.state, str(self.f.root), token)
        self.assertEqual(principal['agent_id'], self.worker_id)
        self.assertEqual(principal['role'], 'worker')
        self.assertNotIn(token, json.dumps(result))
        with self.assertRaisesRegex(ValueError, 'project_mismatch'):
            access.principal(self.state, str(self.f.secrets), token)
        with self.assertRaises(ValueError):
            access.principal(self.state, str(self.f.root), 'forged-bearer-secret')

    def test_worker_cannot_issue_or_escalate_credentials(self):
        with self.assertRaises(ValueError):
            access.issue_token(self.state, self.f.worker['instance_id'], self.f.worker['version'],
                               'main', self.f.envelope('access.issue', {
                                   'project_root': str(self.f.root), 'agent_id': self.worker_id}),
                               str(self.f.secrets / 'forged.txt'))
        with self.assertRaises(ValueError):
            self.issue(role='reviewer', binding_id=self.actor[0], binding_version=self.actor[1])

    def test_token_expiry_and_revocation_are_enforced(self):
        result, token, env = self.issue()
        revoke = self.f.envelope('access.revoke', {'token_id': result['token_id'], 'expected_revision': 1})
        access.revoke_token(self.state, *self.actor, revoke)
        self.assertTrue(access.revoke_token(self.state, *self.actor, revoke)['replayed'])
        with self.assertRaises(ValueError):
            access.principal(self.state, str(self.f.root), token)
        result, token, env = self.issue()
        with self.f.db() as con:
            con.execute("UPDATE v2_access_token SET expires_at='2000-01-01T00:00:00+00:00' WHERE token_id=?",
                        (result['token_id'],))
        with self.assertRaises(ValueError):
            access.principal(self.state, str(self.f.root), token)

    def test_privileged_token_rechecks_current_binding(self):
        result, token, env = self.issue('controller')
        self.assertEqual(access.principal(self.state, str(self.f.root), token)['role'], 'controller')
        with self.f.db() as con:
            con.execute("UPDATE bindings SET status='retired' WHERE id=?", (self.actor[0],))
        with self.assertRaises(ValueError):
            access.principal(self.state, str(self.f.root), token)

    def test_agent_revocation_blocks_all_credentials(self):
        result, token, env = self.issue()
        with self.f.db() as con:
            con.execute("UPDATE v2_agent SET lifecycle='revoked' WHERE agent_id=?", (self.worker_id,))
        with self.assertRaises(ValueError):
            access.principal(self.state, str(self.f.root), token)

    def test_reviewer_requires_task_scope(self):
        with self.assertRaisesRegex(ValueError, 'scoped to a task'):
            self.issue('reviewer', agent_id=self.controller_id, binding_id=self.actor[0],
                       binding_version=self.actor[1])

    def test_secret_is_exclusive_and_replay_does_not_reissue(self):
        result, token, env = self.issue()
        replay = access.issue_token(self.state, *self.actor, env, str(self.f.secrets / 'other.txt'))
        self.assertTrue(replay['replayed'])
        self.assertFalse((self.f.secrets / 'other.txt').exists())
        self.assertEqual(Path(result['token_file']).read_text().strip(), token)
        with self.assertRaises(FileExistsError):
            access.issue_token(self.state, *self.actor,
                               self.f.envelope('access.issue', env['payload']), result['token_file'])
        with self.f.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM v2_access_token').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
