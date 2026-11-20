"""Real local Owner bootstrap and credential checks for supervision decisions.

The project content-root binding and the local terminal are fixtures. No live
Agent route is exercised: the CLI refuses every non-interactive invocation.
"""
import io
import json
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import awo_service as service
import owner_access as owner
import service_access as access
import test_service_access as fixtures


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class OwnerAccessTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceAccessTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.base = self.f.f
        self.state, self.root = self.base.state, self.base.root
        service.set_enabled(self.state, *self.base.actor, True)
        self.base.epoch = service.service_status(self.state)['controller_epoch']
        self.epoch = service.service_status(self.state)['controller_epoch']
        self.project = service.service_status(self.state)['project_id']
        self.tokens = self.root / 'owner-tokens'
        self.tokens.mkdir()
        with self.base.db() as con:
            con.execute('INSERT INTO v2_project_content_root VALUES (?,?,?,?,?,?)',
                        (self.project, str(self.root), access.root_hash(self.root), 1,
                         'fixture-local-admin', service._now()))

    def token_file(self, name='owner.txt'):
        return self.tokens / (name + '-' + uuid.uuid4().hex + '.txt')

    def create(self, owner_id='human-one', **options):
        path = self.token_file(owner_id)
        result = owner.setup(self.state, *self.base.actor, str(self.root), owner_id, str(path), **options)
        return result, path, path.read_text(encoding='ascii').strip()

    def rows(self, sql, args=()):
        with self.base.db() as con:
            return [dict(row) for row in con.execute(sql, args)]

    def assert_code(self, code, call, *args, **kwargs):
        """Errors carry a machine code separately from their human message."""
        with self.assertRaises(ValueError) as caught:
            call(*args, **kwargs)
        self.assertEqual(getattr(caught.exception, 'code', None), code, str(caught.exception))

    def owner_authenticate(self, token, root=None):
        con = service._connect(service._db_path(self.state), readonly=True)
        try:
            con.execute('BEGIN')
            return owner.authenticate(con, str(root or self.root), token)
        finally:
            con.close()

    def test_interactive_setup_requires_terminal_and_exact_phrase(self):
        path = self.token_file()
        argv = [sys.executable, '-B', str(Path(owner.__file__)), '--state', str(self.state),
                '--project-root', str(self.root), 'setup', '--actor', self.base.actor[0],
                '--version', str(self.base.actor[1]), '--owner-id', 'human-cli',
                '--token-out', str(path)]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn('forbidden_role', proc.stdout)
        self.assertFalse(path.exists())
        with mock.patch.object(sys, 'stdin', FakeTerminal()), \
                mock.patch('builtins.input', return_value='not the phrase'):
            self.assertEqual(owner.main(argv[3:]), 2)
        self.assertFalse(path.exists())
        with mock.patch.object(sys, 'stdin', FakeTerminal()), \
                mock.patch('builtins.input', return_value='AUTHORIZE OWNER human-cli'):
            self.assertEqual(owner.main(argv[3:]), 0)
        self.assertTrue(path.exists())
        principal = self.owner_authenticate(path.read_text(encoding='ascii').strip())
        self.assertEqual(principal['role'], 'owner')
        self.assertEqual(principal['principal_id'], 'owner:human-cli')
        self.assertEqual(principal['auth_generation'], 1)

    def test_root_mismatch_linked_agent_and_generation_conflict(self):
        self.assert_code('project_mismatch', owner.setup, self.state, *self.base.actor,
                         str(self.base.secrets), 'human-one', str(self.token_file()),
                         linked_agent_id=self.f.worker_id)
        with self.assertRaisesRegex(ValueError, 'Linked Agent'):
            self.create(linked_agent_id='agent-not-in-project')
        result, path, token = self.create(linked_agent_id=self.f.worker_id)
        self.assertEqual(result['auth_generation'], 1)
        linked = self.rows('SELECT linked_agent_id FROM v2_supervision_owner_identity '
                           'WHERE principal_id=?', ('owner:human-one',))
        self.assertEqual(linked, [{'linked_agent_id': self.f.worker_id}])
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            self.create(expected_generation=0)
        self.assertEqual(self.owner_authenticate(token)['auth_generation'], 1)

    def test_rotation_preserves_linked_agent_when_flag_omitted(self):
        self.create(linked_agent_id=self.f.worker_id)
        rotated, path, token = self.create(expected_generation=1)
        self.assertEqual(rotated['auth_generation'], 2)
        linked = self.rows('SELECT linked_agent_id FROM v2_supervision_owner_identity '
                           'WHERE principal_id=?', ('owner:human-one',))
        self.assertEqual(linked, [{'linked_agent_id': self.f.worker_id}])

    def test_owner_identity_expiry_alone_revokes_authority(self):
        _, path, token = self.create()
        with self.base.db() as con:
            con.execute('UPDATE v2_supervision_owner_identity SET expires_at=? WHERE principal_id=?',
                        (int(time.time()) - 1, 'owner:human-one'))
        # The credential is still unexpired; only the supervision identity is stale.
        self.assert_code('invalid_principal', self.owner_authenticate, token)

    def test_rotation_revoke_expiry_and_credential_generation(self):
        result, path, token = self.create()
        rotated, next_path, next_token = self.create(expected_generation=1)
        self.assertEqual(rotated['auth_generation'], 2)
        self.assert_code('unauthenticated', self.owner_authenticate, token)
        self.assertEqual(self.owner_authenticate(next_token)['auth_generation'], 2)
        self.assert_code('project_mismatch', self.owner_authenticate, next_token,
                         root=self.base.secrets)
        with self.base.db() as con:
            con.execute('UPDATE v2_owner_credential SET expires_at=? WHERE revoked_at IS NULL',
                        (int(time.time()) - 1,))
            con.execute('UPDATE v2_supervision_owner_identity SET expires_at=? WHERE principal_id=?',
                        (int(time.time()) - 1, 'owner:human-one'))
        self.assert_code('unauthenticated', self.owner_authenticate, next_token)
        with self.base.db() as con:
            con.execute('UPDATE v2_owner_credential SET expires_at=? WHERE revoked_at IS NULL',
                        (int(time.time()) + 3600,))
            con.execute('UPDATE v2_supervision_owner_identity SET expires_at=? WHERE principal_id=?',
                        (int(time.time()) + 3600, 'owner:human-one'))
            con.execute('UPDATE v2_supervision_owner_identity SET auth_generation=3 WHERE principal_id=?',
                        ('owner:human-one',))
        self.assert_code('invalid_principal', self.owner_authenticate, next_token)

    def command(self, token, tool, payload, command_id=None):
        env = {'command_id': command_id or ('owner-' + uuid.uuid4().hex), 'tool': tool,
               'payload': payload, 'expected_epoch': self.epoch}
        return owner.command(self.state, str(self.root), token, env)

    def test_revoke_and_denied_commands_are_checked_before_replay(self):
        _, _, token = self.create()
        issued, controller_token, _ = self.f.issue('controller')
        controller = access.principal(self.state, str(self.root), controller_token)
        payload = {'agent_id': controller['agent_id'], 'binding_id': controller['binding_id'],
                   'binding_version': controller['binding_version'],
                   'expected_epoch': self.epoch, 'expires_at': int(time.time()) + 3600}
        command_id = 'owner-' + uuid.uuid4().hex
        first = self.command(token, 'supervision_bind', payload, command_id)
        self.assertFalse(first['replayed'])
        self.assertTrue(self.command(token, 'supervision_bind', payload, command_id)['replayed'])
        with self.assertRaises(ValueError):
            self.command(token, 'supervision_approve', {})
        rejected = self.rows("SELECT count(*) AS n FROM v2_event WHERE event_type='owner.command_rejected'")
        self.assertEqual(rejected[0]['n'], 1)
        with self.base.db() as con:
            con.execute('UPDATE v2_owner_credential SET revoked_at=?', (int(time.time()),))
        self.assert_code('unauthenticated', self.command, token, 'supervision_bind', payload, command_id)
        # A fresh generation still has to name its own current generation.
        _, _, next_token = self.create(expected_generation=1)
        with self.assertRaisesRegex(ValueError, 'current Owner generation'):
            self.command(next_token, 'owner.revoke', {'auth_generation': 1})
        result = self.command(next_token, 'owner.revoke', {'auth_generation': 2})
        self.assertEqual(result['status'], 'OWNER_REVOKED')
        self.assert_code('invalid_principal', self.command, next_token, 'owner.revoke',
                         {'auth_generation': 2})


if __name__ == '__main__':
    unittest.main()
