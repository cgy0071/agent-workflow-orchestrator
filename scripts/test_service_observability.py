"""Authenticated read-only runtime views and isolated audit receipts.

Uses a real migrated ledger, real bearer credentials and real local Git. The
Dashboard UI check lives in test_dashboard; this file is the service boundary.
"""
import ctypes
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import awo_service as service
import receipt_export
import runtime_views
import service_observability as observability
import test_service_access as fixtures


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceAccessTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.base = self.f.f
        self.state, self.root = self.base.state, self.base.root
        service.set_enabled(self.state, *self.base.actor, True)
        self.base.epoch = service.service_status(self.state)['controller_epoch']
        _, self.controller_token, _ = self.f.issue('controller')
        _, self.observer_token, _ = self.f.issue('observer')
        _, self.worker_token, _ = self.f.issue('worker')

    def test_views_are_project_scoped_read_only_and_never_create_authority(self):
        result = observability.views(self.state, str(self.root), self.controller_token, limit=50)
        self.assertFalse(result['can_write'])
        self.assertEqual(set(result['views']), set(runtime_views.VIEW_NAMES))
        self.assertEqual(set(result['buckets']), set(runtime_views.BUCKETS))
        self.assertEqual(result['project_id'], service.service_status(self.state)['project_id'])
        html = observability.views_html(self.state, str(self.root), self.observer_token,
                                        limit=50, view='project')
        self.assertIn('<', html['html'])
        self.assertNotIn(self.observer_token, html['html'])
        with self.assertRaisesRegex(ValueError, 'controller or observer'):
            observability.views(self.state, str(self.root), self.worker_token)
        with self.assertRaisesRegex(ValueError, 'limit'):
            observability.views(self.state, str(self.root), self.controller_token, limit=0)

    def test_task_scoped_credential_cannot_read_project_views(self):
        project = service.service_status(self.state)['project_id']
        now = service._now()
        with self.base.db() as con:
            con.execute('INSERT INTO v2_task(task_id,project_id,task_state,created_at,updated_at) '
                        "VALUES ('T-OBS',?,'ready',?,?)", (project, now, now))
        _, scoped, _ = self.f.issue('controller', task_id='T-OBS')
        with self.assertRaisesRegex(ValueError, 'Task/run/knowledge-scoped'):
            observability.views(self.state, str(self.root), scoped)

    def test_service_cli_views_runs_against_the_real_ledger(self):
        token_file = self.root / 'obs-token.txt'
        token_file.write_text(self.controller_token, encoding='ascii')
        proc = subprocess.run([
            sys.executable, '-B', str(Path(service.__file__)), '--state', str(self.state),
            'views', '--project-root', str(self.root), '--token-file', str(token_file),
            '--limit', '20'], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertFalse(value['can_write'])
        self.assertEqual(set(value['views']), set(runtime_views.VIEW_NAMES))

    def test_service_cli_receipt_operations_run_against_the_real_ledger(self):
        token_file = self.root / 'obs-token.txt'
        token_file.write_text(self.controller_token, encoding='ascii')
        archive = tempfile.TemporaryDirectory(prefix='awo-receipt-cli-')
        self.addCleanup(archive.cleanup)
        destination = Path(archive.name) / ('cli-receipt-' + uuid.uuid4().hex)
        audit = Path(archive.name) / 'cli-audit.sqlite3'
        prefix = [sys.executable, '-B', str(Path(service.__file__)), '--state', str(self.state)]

        def run(*arguments):
            proc = subprocess.run(prefix + list(arguments) + [
                '--project-root', str(self.root), '--token-file', str(token_file)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        exported = run('receipt-export', '--destination', str(destination), '--enable-export',
                       '--protected-root', str(self.root), '--protected-root', str(self.state))
        self.assertTrue(exported['verified'])
        self.assertTrue(run('receipt-verify', '--repository', str(destination))['verified'])
        rebuilt = run('receipt-rebuild', '--repository', str(destination), '--audit-db', str(audit))
        self.assertTrue(rebuilt['verified'])
        self.assertTrue(audit.is_file())

    def test_receipt_export_verify_and_rebuild_stay_local_and_isolated(self):
        archive = tempfile.TemporaryDirectory(prefix='awo-receipt-')
        self.addCleanup(archive.cleanup)
        destination = Path(archive.name) / ('receipt-' + uuid.uuid4().hex)
        with self.assertRaisesRegex(ValueError, 'opt-in'):
            observability.export(self.state, str(self.root), self.controller_token,
                                 str(destination), enabled=False, protected_roots=[str(self.root)])
        self.assertFalse(destination.exists())
        exported = observability.export(self.state, str(self.root), self.controller_token,
                                        str(destination), enabled=True, protected_roots=[str(self.root)])
        self.assertTrue(exported['verified'])
        self.assertTrue((destination / '.git').exists())
        verified = observability.verify(self.state, str(self.root), self.observer_token, str(destination))
        self.assertTrue(verified['verified'])
        audit_db = Path(archive.name) / 'audit.sqlite3'
        rebuilt = observability.rebuild(self.state, str(self.root), self.controller_token,
                                        str(destination), str(audit_db))
        self.assertTrue(rebuilt['verified'])
        con = sqlite3.connect(str(audit_db))
        try:
            self.assertGreater(con.execute('SELECT count(*) FROM receipt_meta').fetchone()[0], 0)
        finally:
            con.close()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            observability.rebuild(self.state, str(self.root), self.controller_token,
                                  str(destination), str(audit_db))

    def test_rebuild_refuses_repository_overlap_and_unsafe_names(self):
        archive = tempfile.TemporaryDirectory(prefix='awo-receipt-')
        self.addCleanup(archive.cleanup)
        destination = Path(archive.name) / ('receipt-' + uuid.uuid4().hex)
        observability.export(self.state, str(self.root), self.controller_token,
                             str(destination), enabled=True, protected_roots=[str(self.root)])
        inside = destination / 'audit.sqlite3'
        with self.assertRaisesRegex(ValueError, 'outside the receipt repository'):
            observability.rebuild(self.state, str(self.root), self.controller_token,
                                  str(destination), str(inside))
        self.assertFalse(inside.exists())
        # A failed verification must not leave a newly created database file.
        bogus = Path(archive.name) / 'bogus-repository'
        bogus.mkdir()
        target = Path(archive.name) / 'never-created.sqlite3'
        with self.assertRaises(ValueError):
            observability.rebuild(self.state, str(self.root), self.controller_token,
                                  str(bogus), str(target))
        self.assertFalse(target.exists())
        # LIKE's '_' wildcard must not classify a user table as internal.
        con = sqlite3.connect(':memory:')
        try:
            con.execute('BEGIN IMMEDIATE')
            con.execute('CREATE TABLE sqliteNotes (x)')
            with self.assertRaisesRegex(ValueError, 'empty SQLite destination'):
                receipt_export.rebuild_receipt(con, str(destination))
        finally:
            con.close()

    @unittest.skipUnless(sys.platform == 'win32', 'NTFS short names are Windows-only')
    def test_rebuild_rejects_an_ntfs_short_name_alias_of_the_repository(self):
        # An 8.3 alias is not a reparse point, so lexical and symlink checks
        # alone would let rebuild write the audit database inside the receipt
        # repository it is meant to stay outside of.
        archive = tempfile.TemporaryDirectory(prefix='awo-receipt-')
        self.addCleanup(archive.cleanup)
        destination = Path(archive.name) / ('receipt-' + uuid.uuid4().hex)
        observability.export(self.state, str(self.root), self.controller_token,
                             str(destination), enabled=True, protected_roots=[str(self.root)])
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(destination), buffer, 32768)
        if not length or buffer.value == str(destination):
            self.skipTest('No 8.3 short-name alias exists for this directory.')
        self.assertEqual(Path(buffer.value).resolve(), destination.resolve())
        with self.assertRaisesRegex(ValueError, 'outside the receipt repository'):
            observability.rebuild(self.state, str(self.root), self.controller_token,
                                  str(destination), buffer.value)
        self.assertFalse(Path(buffer.value, 'audit.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
