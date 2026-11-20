"""Authorization and transaction boundaries for committed application intents.

Covers the caller-facing application runtime only. The merge and collaboration
domains own the external effects and are tested in their own modules.
"""
import sqlite3
import unittest
from unittest import mock

import awo_service as service
import application_runtime as runtime
import collaboration_runtime
import merge_queue
import test_awo_registry as fixtures


class ApplicationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RegistryTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.state, self.root = self.f.state, self.f.root
        self.project = service.service_status(self.state)['project_id']
        self.epoch = service.service_status(self.state)['controller_epoch']

    def principal(self, role='controller', **scope):
        value = {'role': role, 'agent_id': 'agent-fixture', 'principal_id': 'principal:' + role,
                 'project_id': self.project}
        value.update(scope)
        return value

    def assert_code(self, code, call, *args, **kwargs):
        with self.assertRaises(ValueError) as caught:
            call(*args, **kwargs)
        self.assertEqual(getattr(caught.exception, 'code', None), code, str(caught.exception))

    def runner_spy(self, store):
        def runner(db, intent_id, token):
            # The runtime holds no transaction or lock across the external runner.
            con = sqlite3.connect(db)
            try:
                con.execute('BEGIN IMMEDIATE')
                con.rollback()
            finally:
                con.close()
            store['token'] = token
            return {'outcome': 'verified', 'intent_id': intent_id}
        return runner

    def merge(self, principal, *, recover=False, epoch=None, bundle_epoch=None, intent=True):
        row = {'operation_token': 'operation-secret'}
        bundle = {'service_state': {'controller_epoch':
                                    self.epoch if bundle_epoch is None else bundle_epoch}}
        store = {}
        runner = self.runner_spy(store)
        with mock.patch.object(runtime.access, 'authenticate', return_value=principal), \
                mock.patch.object(merge_queue, '_intent',
                                  return_value=(row, bundle) if intent else ({}, {})), \
                mock.patch.object(merge_queue, 'execute_intent', side_effect=runner), \
                mock.patch.object(merge_queue, 'recover_intent', side_effect=runner):
            result = runtime.merge_run(self.state, str(self.root), 'bearer', 'intent-1',
                                       self.epoch if epoch is None else epoch, recover=recover)
        return result, store

    def test_controller_role_and_unscoped_principal_are_required(self):
        for principal in (self.principal('worker'), self.principal('observer'),
                          self.principal('controller', task_id='T-A'),
                          self.principal('controller', assignment_id='A-1'),
                          self.principal('controller', generation=1),
                          self.principal('controller', knowledge_id='K-1')):
            with self.subTest(principal=principal):
                with mock.patch.object(runtime.access, 'authenticate', return_value=principal), \
                        mock.patch.object(merge_queue, '_intent', side_effect=AssertionError('read before auth')):
                    self.assert_code('forbidden_role', runtime.merge_run, self.state, str(self.root),
                                     'bearer', 'intent-1', self.epoch)

    def test_epoch_freeze_and_disable_are_checked_before_the_ticket(self):
        principal = self.principal()
        with mock.patch.object(runtime.access, 'authenticate', return_value=principal), \
                mock.patch.object(merge_queue, '_intent', side_effect=AssertionError('read before epoch')):
            self.assert_code('revision_conflict', runtime.merge_run, self.state, str(self.root),
                             'bearer', 'intent-1', self.epoch + 1)
        service.set_freeze(self.state, *self.f.actor, True, "fixture freeze")
        frozen_epoch = service.service_status(self.state)['controller_epoch']
        with mock.patch.object(runtime.access, 'authenticate', return_value=principal), \
                mock.patch.object(merge_queue, '_intent', side_effect=AssertionError('read while frozen')):
            self.assert_code('state_conflict', runtime.merge_run, self.state, str(self.root),
                             'bearer', 'intent-1', frozen_epoch)
        service.set_freeze(self.state, *self.f.actor, False, "fixture unfreeze")
        service.set_enabled(self.state, *self.f.actor, False)
        disabled_epoch = service.service_status(self.state)['controller_epoch']
        with mock.patch.object(runtime.access, 'authenticate', return_value=principal), \
                mock.patch.object(merge_queue, '_intent', side_effect=AssertionError('read while disabled')):
            self.assert_code('state_conflict', runtime.merge_run, self.state, str(self.root),
                             'bearer', 'intent-1', disabled_epoch)

    def test_committed_ticket_is_consumed_only_after_the_transaction_closes(self):
        service.set_enabled(self.state, *self.f.actor, True)
        self.epoch = service.service_status(self.state)['controller_epoch']
        result, store = self.merge(self.principal())
        self.assertEqual(result, {'outcome': 'verified', 'intent_id': 'intent-1'})
        self.assertEqual(store['token'], 'operation-secret')

    def test_earlier_epoch_ticket_needs_explicit_recovery(self):
        service.set_enabled(self.state, *self.f.actor, True)
        self.epoch = service.service_status(self.state)['controller_epoch']
        store = {}
        def execute(db, intent_id, token):
            raise AssertionError('blind rerun of an earlier-epoch intent')
        def recover(db, intent_id, token):
            con = sqlite3.connect(db)
            try:
                con.execute('BEGIN IMMEDIATE')
                con.rollback()
            finally:
                con.close()
            store['token'] = token
            return {'outcome': 'verified', 'intent_id': intent_id}
        with mock.patch.object(runtime.access, 'authenticate', return_value=self.principal()), \
                mock.patch.object(merge_queue, '_intent', return_value=(
                    {'operation_token': 'operation-secret'},
                    {'service_state': {'controller_epoch': self.epoch - 1}})), \
                mock.patch.object(merge_queue, 'execute_intent', side_effect=execute), \
                mock.patch.object(merge_queue, 'recover_intent', side_effect=recover):
            self.assert_code('revision_conflict', runtime.merge_run, self.state, str(self.root),
                             'bearer', 'intent-1', self.epoch)
            result = runtime.merge_run(self.state, str(self.root), 'bearer', 'intent-1',
                                       self.epoch, recover=True)
        self.assertEqual(result['intent_id'], 'intent-1')
        self.assertEqual(store['token'], 'operation-secret')

    def test_collaboration_runner_starts_after_rollback_on_the_same_connection(self):
        service.set_enabled(self.state, *self.f.actor, True)
        self.epoch = service.service_status(self.state)['controller_epoch']
        observed = {}
        def verify(con, project_id, principal, verification_id):
            observed.update(in_transaction=con.in_transaction, project=project_id,
                            principal=principal['principal_id'], verification=verification_id,
                            synchronous=con.execute('PRAGMA synchronous').fetchone()[0])
            return {'status': 'VERIFIED', 'verification_id': verification_id}
        with mock.patch.object(runtime.access, 'authenticate', return_value=self.principal()), \
                mock.patch.object(collaboration_runtime, 'run_verification', side_effect=verify):
            result = runtime.collaboration_run(self.state, str(self.root), 'bearer', 'verify-1', self.epoch)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertFalse(observed['in_transaction'])
        self.assertEqual(observed['synchronous'], 2)
        self.assertEqual(observed['project'], self.project)
        self.assertEqual(observed['verification'], 'verify-1')


if __name__ == '__main__':
    unittest.main()
