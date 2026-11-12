"""Native runtime ownership must survive historical projection and upgrades."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import awo_service as service
import test_awo_service as fixtures


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.state = self.fixture.state
        self.actor = self.fixture.actor

    def test_native_task_is_marked_at_creation(self):
        self.fixture.apply()
        epoch = service.set_enabled(self.state, *self.actor, True)['epoch']
        result = service.execute_command(self.state, *self.actor, {
            'command_id': 'create-native-test', 'tool': 'task.create',
            'expected_epoch': epoch, 'payload': {'task_id': 'T-NATIVE', 'title': 'Native'}})
        self.assertEqual(result['status'], 'CREATED')
        self.assertEqual(self.fixture.rows(
            'SELECT runtime_owner FROM v2_task WHERE task_id=?', ('T-NATIVE',))[0],
            {'runtime_owner': 'v2'})

    def test_legacy_reimport_cannot_overwrite_native_slot_or_evidence(self):
        self.fixture.insert_task()
        self.fixture.apply()
        with self.fixture.db() as con:
            con.execute("UPDATE v2_task SET runtime_owner='v2',task_state='active'")
            con.execute("UPDATE v2_assignment SET state='uncertain',holds_slot=1")
        tables = ('v2_task', 'v2_assignment', 'v2_evidence', 'v2_review', 'v2_legacy_map')
        before = {table: self.fixture.rows('SELECT * FROM ' + table) for table in tables}
        with self.assertRaisesRegex(service.ServiceError, 'cannot overwrite a V2-owned'):
            self.fixture.apply()
        after = {table: self.fixture.rows('SELECT * FROM ' + table) for table in tables}
        self.assertEqual(before, after)
        result = service.upgrade_schema(self.state, *self.actor)
        self.assertFalse(result['legacy_projection_replayed'])
        self.assertEqual(before, {table: self.fixture.rows('SELECT * FROM ' + table)
                                  for table in tables})

    def test_schema_upgrade_requires_initialization_and_controller(self):
        with self.assertRaisesRegex(service.ServiceError, 'initial migration'):
            service.upgrade_schema(self.state, *self.actor)
        self.fixture.apply()
        with self.assertRaises(ValueError):
            service.upgrade_schema(self.state, self.actor[0], self.actor[1], 'side')
        with self.assertRaises(ValueError):
            service.upgrade_schema(self.state, 'wrong-controller', self.actor[1], 'main')

    def test_enabled_runtime_requires_freeze_for_additive_upgrade(self):
        self.fixture.apply()
        service.set_enabled(self.state, *self.actor, True)
        with self.assertRaisesRegex(service.ServiceError, 'freeze'):
            service.upgrade_schema(self.state, *self.actor)
        service.set_freeze(self.state, *self.actor, True, 'schema maintenance')
        result = service.upgrade_schema(self.state, *self.actor)
        self.assertEqual(result['schema_version'], service.V2_SCHEMA_VERSION)
        self.assertTrue(service.service_status(self.state)['frozen'])

    def test_schema_upgrade_rolls_back_ddl_and_metadata(self):
        self.fixture.apply()
        before = self.fixture.rows('SELECT * FROM v2_meta')
        original = service.create_schema

        def broken(con):
            original(con)
            con.execute('CREATE TABLE regression_partial (id INTEGER)')
            con.execute("UPDATE v2_meta SET value='999' WHERE key='schema_version'")
            raise RuntimeError('injected interrupted migration')

        with patch.object(service, 'create_schema', side_effect=broken):
            with self.assertRaises(RuntimeError):
                service.upgrade_schema(self.state, *self.actor)
        self.assertEqual(before, self.fixture.rows('SELECT * FROM v2_meta'))
        self.assertEqual([], self.fixture.rows(
            "SELECT name FROM sqlite_master WHERE name='regression_partial'"))

    def test_cli_upgrade_uses_same_guarded_boundary(self):
        self.fixture.apply()
        proc = subprocess.run([
            sys.executable, '-B', str(Path(service.__file__)), '--state', str(self.state),
            'schema-upgrade', '--actor', self.actor[0], '--version', str(self.actor[1]),
            '--context', 'main'], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['status'], 'SCHEMA_UPGRADED')

    def old_native(self):
        self.fixture.apply()
        epoch = service.set_enabled(self.state, *self.actor, True)['epoch']
        service.execute_command(self.state, *self.actor, {
            'command_id': 'create-precolumn-native', 'tool': 'task.create',
            'expected_epoch': epoch, 'payload': {'task_id': 'T-OLDNATIVE', 'title': 'Keep my provenance'}})
        service.set_enabled(self.state, *self.actor, False)
        with self.fixture.db() as con:
            con.execute('ALTER TABLE v2_task DROP COLUMN runtime_owner')
            con.execute("UPDATE v2_meta SET value='2' WHERE key='schema_version'")

    def test_schema2_native_backfill_survives_repeat_upgrade(self):
        self.old_native()
        for unused in range(2):
            service.upgrade_schema(self.state, *self.actor)
            row = self.fixture.rows('SELECT * FROM v2_task WHERE task_id=?', ('T-OLDNATIVE',))[0]
            self.assertEqual((row['runtime_owner'], row['title'], row['task_state']),
                             ('v2', 'Keep my provenance', 'draft'))
            self.assertEqual([], self.fixture.rows('SELECT * FROM v2_legacy_map'))

    def test_schema2_native_collision_rejects_atomic_legacy_import(self):
        self.old_native()
        self.fixture.insert_task(task_id='T-OLDNATIVE')
        before = self.fixture.rows('SELECT * FROM v2_task')
        with self.assertRaisesRegex(service.ServiceError, 'cannot overwrite a V2-owned'):
            self.fixture.apply()
        self.assertEqual(before, self.fixture.rows('SELECT * FROM v2_task'))
        self.assertEqual([], self.fixture.rows('SELECT * FROM v2_legacy_map'))
        service.upgrade_schema(self.state, *self.actor)
        with self.assertRaisesRegex(service.ServiceError, 'cannot overwrite a V2-owned'):
            self.fixture.apply()

    def test_old_ambiguous_ownership_requires_inspection(self):
        self.old_native()
        with self.fixture.db() as con:
            con.execute("DELETE FROM v2_event WHERE entity_id='T-OLDNATIVE'")
        with self.assertRaisesRegex(service.ServiceError, 'ambiguous'):
            service.upgrade_schema(self.state, *self.actor)


if __name__ == '__main__':
    unittest.main()
