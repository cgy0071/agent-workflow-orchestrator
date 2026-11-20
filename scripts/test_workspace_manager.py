"""Real disposable Git/SQLite/process fixtures for the standalone workspace core."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import awo_service as service
import workflow_control as control
import workspace_manager as wm


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get('AWO_WORKSPACE_TEST_ROOT')
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='awo-workspace-fixture-', dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'fixture-repo'
        self.repo.mkdir()
        self.workspaces = self.root / 'workspaces'
        self.workspaces.mkdir()
        self.evidence = self.root / 'evidence'
        state = self.root / 'state'
        control.initialize(state, {'roles': {'fixture': {}}})
        self.db = state / 'workflow.sqlite3'
        self.git(self.repo, 'init', '-b', 'main')
        self.git(self.repo, 'config', 'user.name', 'Workspace Fixture')
        self.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.git(self.repo, 'config', 'core.autocrlf', 'false')
        (self.repo / 'alpha.txt').write_bytes(b'alpha\n')
        (self.repo / 'delete.txt').write_bytes(b'delete me\n')
        (self.repo / 'rename.txt').write_bytes(b'rename this fixture file\n' * 10)
        (self.repo / 'binary.bin').write_bytes(b'\x00\x01original\xff')
        (self.repo / '.gitignore').write_bytes(b'ignored/\n')
        self.commit('fixture initial content')
        self.base = self.git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        self.con = sqlite3.connect(self.db, timeout=2)
        self.addCleanup(self.con.close)
        self.con.execute('PRAGMA foreign_keys=ON')
        self.con.execute('BEGIN IMMEDIATE')
        service.create_schema(self.con)
        wm.create_workspace_schema(self.con)
        self.con.execute("INSERT INTO v2_project VALUES ('project',?,?)", (str(self.root), wm._now()))
        self.con.execute("INSERT INTO v2_task(task_id,project_id,task_state,created_at,updated_at) "
                         "VALUES ('T','project','ready',?,?)", (wm._now(), wm._now()))
        self.con.commit()
        self.counter = 0

    def tearDown(self):
        self.con.close()
        # TemporaryDirectory owns this newly created fixture only. Git marks objects read-only on Windows.
        def writable(function, path, _error):
            os.chmod(path, 0o700)
            function(path)
        target = self.root.resolve()
        self.assertTrue(target.name.startswith('awo-workspace-fixture-'))
        shutil.rmtree(target, onerror=writable)
        self.temporary.cleanup()

    def git(self, cwd, *args):
        result = wm.run_argv(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(cwd), *args], cwd=cwd)
        if result['returncode']:
            raise AssertionError(result['stderr'].decode('utf-8', 'replace'))
        return result['stdout']

    def commit(self, message, cwd=None):
        cwd = cwd or self.repo
        self.git(cwd, 'add', '--all')
        self.git(cwd, 'commit', '-m', message)

    def assignment(self, name=None):
        self.counter += 1
        name = name or ('A' + str(self.counter))
        self.con.execute('BEGIN IMMEDIATE')
        self.con.execute("INSERT INTO v2_assignment(assignment_id,task_id,generation,state,created_at,updated_at) "
                         "VALUES (?,'T',?,'reserved',?,?)", (name, self.counter, wm._now(), wm._now()))
        self.con.commit()
        return name

    def spec(self, *, kind='git_worktree', name=None, path=None, allowed=None, artifacts=(), branch=None):
        assignment = self.assignment(name)
        kw = dict(task_id='T', assignment_id=assignment, host='fixture-host', kind=kind,
                  root=str(self.workspaces), path=str(path or (self.workspaces / assignment)),
                  allowed_paths=allowed or ['**'], artifact_paths=artifacts)
        if kind == 'git_worktree':
            kw.update(repository_id='fixture/repo', repo_root=str(self.repo), base_commit=self.base, branch=branch)
        return wm.prepare_workspace(**kw)

    def reserve(self, spec):
        self.con.execute('BEGIN IMMEDIATE')
        ws = wm.reserve_workspace(self.con, spec)
        ticket = wm.begin_provision(self.con, ws['workspace_id'])
        self.con.commit()
        return ticket

    def create(self, **kwargs):
        ticket = self.reserve(self.spec(**kwargs))
        report = wm.provision_workspace(self.db, ticket['workspace_id'], ticket['operation_token'])
        self.assertTrue(report['ok'], report)
        self.con.execute('BEGIN IMMEDIATE')
        ws = wm.finalize_workspace(self.con, ticket['workspace_id'], report)
        self.con.commit()
        self.assertEqual(ws['status'], 'ready', ws['error'])
        return ws

    def current(self, ws):
        return wm.get_workspace(self.con, ws['workspace_id'])

    def proof(self, ws):
        path = self.root / (ws['workspace_id'] + '-quiescence.json')
        path.write_text(json.dumps({'process_fixture': 'controller observed no writer',
                                    'revision': ws['revision']}), encoding='utf-8')
        return dict(workspace_id=ws['workspace_id'], assignment_id=ws['assignment_id'], host=ws['host'],
                    revision=ws['revision'], verified=True, controller='fixture-controller',
                    reason='fixture subprocess exited and host reconciled', evidence_path=str(path),
                    evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def test_real_two_worktrees_same_and_different_relative_files(self):
        one, two = self.create(), self.create()
        self.assertNotEqual(one['path'], two['path'])
        self.assertEqual(one['base_commit'], self.base)
        self.assertTrue((Path(one['path']) / '.git').is_file())
        marker = wm._marker_path(one)
        self.assertTrue(wm._inside(marker, one['common_git_dir']))
        (Path(one['path']) / 'alpha.txt').write_bytes(b'first workspace\n')
        (Path(two['path']) / 'alpha.txt').write_bytes(b'second workspace\n')
        (Path(two['path']) / 'unique.txt').write_bytes(b'unique\n')
        self.assertEqual((self.repo / 'alpha.txt').read_bytes(), b'alpha\n')
        self.assertNotEqual(wm.collect_inventory(one)['entries'], wm.collect_inventory(two)['entries'])
        self.assertEqual(len(wm.workspace_claims(self.con)), 2)

    def test_explicit_migration_rolls_back_and_readonly_queries_do_not_create_schema(self):
        other = sqlite3.connect(self.root / 'empty.sqlite3')
        try:
            before = other.total_changes
            self.assertIsNone(wm.get_workspace(other, 'missing'))
            self.assertEqual(wm.workspace_claims(other), [])
            self.assertFalse(wm.check_workspace_ready(other, 'missing')['ready'])
            self.assertFalse(wm.check_workspace_ready(other, 'missing', allow_active=True)['ready'])
            self.assertEqual(other.total_changes, before)
            self.assertFalse(other.in_transaction)
            with self.assertRaises(wm.WorkspaceError):
                wm.create_workspace_schema(other)
            other.execute('BEGIN IMMEDIATE')
            wm.create_workspace_schema(other)
            self.assertTrue(other.in_transaction)
            other.rollback()
            self.assertFalse(wm._exists(other, 'v2_workspace'))
        finally:
            other.close()

    def test_reservation_and_assignment_binding_rollback_no_external_work(self):
        spec = self.spec()
        self.con.execute('BEGIN IMMEDIATE')
        with mock.patch.object(wm, 'run_argv', side_effect=AssertionError('external execution in transaction')):
            ws = wm.reserve_workspace(self.con, spec)
            wm.begin_provision(self.con, ws['workspace_id'])
        self.assertFalse(Path(ws['path']).exists())
        self.con.rollback()
        self.assertIsNone(wm.get_workspace(self.con, ws['workspace_id']))
        self.assertIsNone(self.con.execute('SELECT workspace_id FROM v2_assignment WHERE assignment_id=?',
                                         (spec['assignment_id'],)).fetchone()[0])

    def test_uncommitted_ticket_is_not_visible_to_external_provisioner(self):
        spec = self.spec()
        self.con.execute('BEGIN IMMEDIATE')
        ws = wm.reserve_workspace(self.con, spec)
        ticket = wm.begin_provision(self.con, ws['workspace_id'])
        with self.assertRaises(sqlite3.OperationalError):
            wm.provision_workspace(self.db, ws['workspace_id'], ticket['operation_token'])
        self.con.rollback()
        self.assertFalse(Path(spec['path']).exists())

    def test_immutable_reservation_and_no_assignment_reuse(self):
        spec = self.spec()
        ws = self.reserve(spec)
        self.con.execute('BEGIN IMMEDIATE')
        self.assertEqual(wm.reserve_workspace(self.con, spec)['workspace_id'], ws['workspace_id'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE v2_workspace SET host='other' WHERE workspace_id=?", (ws['workspace_id'],))
        with self.assertRaises(wm.WorkspaceError):
            wm.reserve_workspace(self.con, dict(spec, path=str(self.workspaces / 'different')))
        self.con.rollback()

    def test_final_scheduler_check_readonly_in_transaction_and_activation(self):
        ws = self.create()
        self.con.execute('BEGIN IMMEDIATE')
        changed = self.con.total_changes
        with mock.patch.object(wm, 'run_argv', side_effect=AssertionError('scheduler must not run Git')):
            self.assertTrue(wm.check_workspace_ready(self.con, ws['workspace_id'],
                assignment_id=ws['assignment_id'], host=ws['host'])['ready'])
            self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'], host='other')['ready'])
            self.assertEqual(self.con.total_changes, changed)
            active = wm.mark_workspace_active(self.con, ws['workspace_id'])
        self.assertEqual(active['status'], 'active')
        self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])
        self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=False)['ready'])
        changed = self.con.total_changes
        self.assertTrue(wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=True)['ready'])
        with self.assertRaises(wm.WorkspaceError):
            wm.mark_workspace_active(self.con, ws['workspace_id'])
        self.assertEqual(self.con.total_changes, changed)
        self.con.rollback()
        self.assertTrue(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])

    def test_allow_active_workspace_and_assignment_state_matrix(self):
        ws = self.create(kind='directory')
        self.con.execute('BEGIN IMMEDIATE')
        with mock.patch.object(wm, 'run_argv', side_effect=AssertionError('scheduler must not run Git')):
            for status in wm.STATES:
                self.con.execute('UPDATE v2_workspace SET status=? WHERE workspace_id=?',
                                 (status, ws['workspace_id']))
                for state in service.ASSIGNMENT_STATES:
                    with self.subTest(workspace=status, assignment=state):
                        self.con.execute('UPDATE v2_assignment SET state=? WHERE assignment_id=?',
                                         (state, ws['assignment_id']))
                        changed = self.con.total_changes
                        result = wm.check_workspace_ready(self.con, ws['workspace_id'],
                            assignment_id=ws['assignment_id'], host=ws['host'], allow_active=True)
                        expected = status in ('ready', 'active') and state in ('reserved', 'blocked')
                        self.assertEqual(result['ready'], expected, result)
                        self.assertEqual(bool(result['reasons']), not expected)
                        self.assertEqual(self.con.total_changes, changed)
                        self.assertTrue(self.con.in_transaction)
        self.con.rollback()
        self.assertEqual(self.current(ws)['status'], 'ready')

    def test_allow_active_requires_boolean_opt_in(self):
        ws = self.create(kind='directory')
        self.con.execute('BEGIN IMMEDIATE')
        for status in ('ready', 'active'):
            self.con.execute('UPDATE v2_workspace SET status=? WHERE workspace_id=?',
                             (status, ws['workspace_id']))
            for flag in ('true', 'false', 1, 0, None, [], {}):
                with self.subTest(status=status, flag=flag):
                    result = wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=flag)
                    self.assertFalse(result['ready'])
                    self.assertIn('allow_active must be a boolean', result['reasons'])
        self.con.rollback()

    def test_allow_active_readonly_connection_rechecks_without_mutation(self):
        ws = self.create()
        self.con.execute('BEGIN IMMEDIATE')
        active = wm.mark_workspace_active(self.con, ws['workspace_id'])
        self.con.commit()
        before = self.db.read_bytes()
        ro = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)
        statements = []
        try:
            ro.execute('PRAGMA query_only=ON')
            ro.execute('BEGIN')
            ro.set_trace_callback(statements.append)
            with mock.patch.object(wm, 'run_argv', side_effect=AssertionError('scheduler must not run Git')):
                result = wm.check_workspace_ready(ro, ws['workspace_id'],
                    assignment_id=ws['assignment_id'], host=ws['host'], allow_active=True)
            self.assertTrue(result['ready'], result)
            self.assertEqual(result['revision'], active['revision'])
            self.assertTrue(ro.in_transaction)
            self.assertEqual(ro.total_changes, 0)
            self.assertEqual(wm.get_workspace(ro, ws['workspace_id']), active)
            self.assertTrue(statements)
            self.assertTrue(all(sql.lstrip().upper().startswith('SELECT ') for sql in statements), statements)
        finally:
            ro.close()
        self.assertEqual(before, self.db.read_bytes())

    def test_allow_active_preserves_identity_binding_and_claim_checks(self):
        ws = self.create(kind='directory')
        self.con.execute('BEGIN IMMEDIATE')
        wm.mark_workspace_active(self.con, ws['workspace_id'])
        self.con.commit()
        for identity in ({'assignment_id': 'other'}, {'host': 'other'}):
            with self.subTest(identity=identity):
                self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'],
                    allow_active=True, **identity)['ready'])
        self.con.execute('BEGIN IMMEDIATE')
        self.con.execute('UPDATE v2_assignment SET workspace_id=NULL WHERE assignment_id=?',
                         (ws['assignment_id'],))
        self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=True)['ready'])
        self.con.rollback()
        self.con.execute('BEGIN IMMEDIATE')
        self.con.execute("INSERT INTO reviews(event,data) VALUES ('active-claim',?)",
                         (json.dumps({'path': ws['path']}),))
        result = wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=True)
        self.assertFalse(result['ready'])
        self.assertIn('retained legacy review/receipt owns requested path', result['reasons'])
        self.con.rollback()
        self.assertTrue(wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=True)['ready'])
        self.assertEqual(self.current(ws)['status'], 'active')
        self.assertEqual(len(wm.workspace_claims(self.con)), 1)

    def test_allow_active_rejects_git_content_and_metadata_drift(self):
        (self.repo / 'next.txt').write_bytes(b'fixture next commit\n')
        self.commit('fixture alternate ref target')
        tip = self.git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        for change in ('tracked', 'binary', 'untracked', 'delete', 'rename', 'index', 'marker', 'ref'):
            with self.subTest(change=change):
                ws = self.create()
                self.con.execute('BEGIN IMMEDIATE')
                active = wm.mark_workspace_active(self.con, ws['workspace_id'])
                self.con.commit()
                path = Path(ws['path'])
                if change == 'tracked':
                    (path / 'alpha.txt').write_bytes(b'late writer\n')
                elif change == 'binary':
                    (path / 'binary.bin').write_bytes(bytes([0, 255, 42]))
                elif change == 'untracked':
                    (path / 'new.txt').write_bytes(b'late output\n')
                elif change == 'delete':
                    (path / 'delete.txt').unlink()
                elif change == 'rename':
                    (path / 'rename.txt').rename(path / 'renamed.txt')
                elif change == 'index':
                    self.git(path, 'update-index', '--assume-unchanged', 'alpha.txt')
                elif change == 'marker':
                    marker = wm._marker_path(ws)
                    value = json.loads(marker.read_text(encoding='utf-8'))
                    value['ownership_token'] = 'foreign'
                    marker.write_text(json.dumps(value), encoding='utf-8')
                else:
                    self.git(self.repo, 'update-ref', 'refs/heads/' + ws['branch'], tip, ws['base_commit'])
                self.con.execute('BEGIN IMMEDIATE')
                changed = self.con.total_changes
                with mock.patch.object(wm, 'run_argv', side_effect=AssertionError('scheduler must not run Git')):
                    result = wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=True)
                self.assertFalse(result['ready'], result)
                self.assertTrue(result['reasons'])
                self.assertEqual(self.con.total_changes, changed)
                self.assertEqual(self.current(ws), active)
                self.con.rollback()
                self.assertTrue(path.is_dir())

    def test_allow_active_rejects_missing_or_replaced_directory(self):
        for change in ('missing', 'replaced'):
            with self.subTest(change=change):
                ws = self.create(kind='directory')
                self.con.execute('BEGIN IMMEDIATE')
                active = wm.mark_workspace_active(self.con, ws['workspace_id'])
                self.con.commit()
                path = Path(ws['path'])
                marker = wm._marker_path(ws).read_bytes()
                retained = self.root / ('retained-' + ws['workspace_id'])
                path.rename(retained)
                if change == 'replaced':
                    path.mkdir()
                    wm._marker_path(ws).write_bytes(marker)
                result = wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=True)
                self.assertFalse(result['ready'], result)
                self.assertEqual(self.current(ws), active)
                self.assertTrue(retained.is_dir())
                self.assertEqual(path.exists(), change == 'replaced')
        self.assertEqual(len(wm.workspace_claims(self.con)), 2)

    def test_allow_active_process_death_preserves_attempted_fixture_intent(self):
        ws = self.create(kind='directory')
        # This fixture models caller-owned intent; the workspace core never owns send state.
        self.con.execute('BEGIN IMMEDIATE')
        self.con.execute('CREATE TABLE fixture_send_intent(workspace_id TEXT PRIMARY KEY, send_state TEXT)')
        wm.mark_workspace_active(self.con, ws['workspace_id'])
        self.con.execute(
            "INSERT INTO fixture_send_intent VALUES (?,'attempted')", (ws['workspace_id'],))
        self.con.commit()
        code = """import os,sqlite3,sys
import workspace_manager as w
c=sqlite3.connect(sys.argv[1])
c.execute('BEGIN IMMEDIATE')
assert c.execute('SELECT send_state FROM fixture_send_intent WHERE workspace_id=?',
                 (sys.argv[2],)).fetchone()[0] == 'attempted'
result=w.check_workspace_ready(c,sys.argv[2],allow_active=True)
assert result['ready'], result
assert c.in_transaction and c.total_changes == 0
os._exit(78)
"""
        child = self.child(code, self.db, ws['workspace_id'])
        output = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 78, output)
        self.assertEqual(self.con.execute('SELECT send_state FROM fixture_send_intent').fetchone()[0],
                         'attempted')
        self.assertEqual(self.con.execute('SELECT state FROM v2_assignment WHERE assignment_id=?',
                                         (ws['assignment_id'],)).fetchone()[0], 'reserved')
        self.assertEqual(self.current(ws)['status'], 'active')
        self.assertEqual(len(wm.workspace_claims(self.con)), 1)
        self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])
        self.assertTrue(wm.check_workspace_ready(self.con, ws['workspace_id'], allow_active=True)['ready'])

    def test_readonly_connection_inventory_and_ready_do_not_modify_database(self):
        ws = self.create()
        before = self.db.read_bytes()
        ro = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)
        try:
            ro.execute('PRAGMA query_only=ON')
            self.assertTrue(wm.check_workspace_ready(ro, ws['workspace_id'])['ready'])
            self.assertTrue(wm.inspect_workspace(wm.get_workspace(ro, ws['workspace_id']))['ok'])
            self.assertEqual(len(wm.workspace_claims(ro)), 1)
            self.assertEqual(ro.total_changes, 0)
        finally:
            ro.close()
        self.assertEqual(before, self.db.read_bytes())

    def test_full_inventory_rename_delete_binary_untracked_ignored_and_scope(self):
        ws = self.create(allowed=['alpha.txt', 'renamed.txt', 'binary.bin'])
        path = Path(ws['path'])
        self.git(path, 'mv', 'rename.txt', 'renamed.txt')
        (path / 'delete.txt').unlink()
        (path / 'alpha.txt').write_bytes(b'modified\n')
        (path / 'binary.bin').write_bytes(b'\x00changed\xff')
        (path / 'untracked.bin').write_bytes(b'\x00untracked\xfe')
        (path / 'ignored').mkdir()
        (path / 'ignored' / 'secret.dat').write_bytes(b'ignored artifact')
        self.git(path, 'add', 'alpha.txt')
        inventory = wm.collect_inventory(ws)
        by_path = {row['path']: row for row in inventory['entries']}
        self.assertEqual(by_path['delete.txt']['kind'], 'deleted')
        self.assertEqual(by_path['rename.txt']['kind'], 'deleted')
        self.assertTrue(by_path['binary.bin']['binary'])
        self.assertEqual(by_path['untracked.bin']['sha256'], hashlib.sha256(b'\x00untracked\xfe').hexdigest())
        self.assertIn('ignored/secret.dat', inventory['changed_paths'])
        self.assertIn('rename.txt', inventory['undeclared_paths'])
        self.assertTrue(any(item.get('old_path') == 'rename.txt' for item in inventory['changes']))
        self.assertFalse(inventory['scope_ok'])
        self.assertTrue(inventory['dirty'])
        self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])
        snap = wm.snapshot_workspace(ws, str(self.evidence))
        manifest = wm.verify_snapshot(snap)
        self.assertEqual(manifest['inventory'], inventory)
        for name in ('base_to_worktree', 'staged', 'unstaged', 'committed'):
            self.assertIn(name, manifest['patches'])
        artifact = self.evidence / 'sha256' / by_path['untracked.bin']['sha256'][:2] / by_path['untracked.bin']['sha256']
        self.assertEqual(artifact.read_bytes(), b'\x00untracked\xfe')

    def test_committed_changes_remain_in_inventory_relative_to_frozen_base(self):
        ws = self.create(allowed=['alpha.txt'])
        path = Path(ws['path'])
        (path / 'alpha.txt').write_bytes(b'fixture committed update\n')
        self.commit('fixture workspace update', path)
        inventory = wm.collect_inventory(ws)
        self.assertFalse(inventory['dirty'])
        self.assertEqual(inventory['changed_paths'], ['alpha.txt'])
        self.assertNotEqual(inventory['head'], ws['base_commit'])

    def test_provision_collision_never_adopts_or_removes_foreign_path(self):
        ticket = self.reserve(self.spec())
        path = Path(ticket['path'])
        path.mkdir()
        (path / 'foreign').write_text('keep me', encoding='utf-8')
        report = wm.provision_workspace(self.db, ticket['workspace_id'], ticket['operation_token'])
        self.assertFalse(report['ok'])
        self.con.execute('BEGIN IMMEDIATE')
        ws = wm.finalize_workspace(self.con, ticket['workspace_id'], report)
        self.con.commit()
        self.assertEqual(ws['status'], 'quarantined')
        self.assertEqual((path / 'foreign').read_text(), 'keep me')
        self.assertEqual(len(wm.workspace_claims(self.con)), 1)
        with self.assertRaises(wm.WorkspaceError):
            wm.provision_workspace(self.db, ticket['workspace_id'], ticket['operation_token'])

    def test_branch_collision_after_reservation_does_not_delete_foreign_branch(self):
        ticket = self.reserve(self.spec())
        self.git(self.repo, 'branch', ticket['branch'], self.base)
        report = wm.provision_workspace(self.db, ticket['workspace_id'], ticket['operation_token'])
        self.assertFalse(report['ok'])
        self.assertEqual(self.git(self.repo, 'rev-parse', 'refs/heads/' + ticket['branch']).decode().strip(), self.base)

    def test_stale_marker_and_missing_tree_block_ready(self):
        ws = self.create()
        marker = wm._marker_path(ws)
        value = json.loads(marker.read_text())
        value['ownership_token'] = 'foreign-token'
        marker.write_text(json.dumps(value), encoding='utf-8')
        self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])
        self.assertFalse(wm.inspect_workspace(ws)['ok'])
        # Move fixture directory without deleting it; recovery must retain the claim.
        Path(ws['path']).rename(self.workspaces / 'moved-fixture')
        report = wm.inspect_workspace(ws)
        self.con.execute('BEGIN IMMEDIATE')
        recovered = wm.recover_workspace(self.con, ws['workspace_id'], report)
        self.con.commit()
        self.assertEqual(recovered['status'], 'workspace_lost')
        self.assertEqual(len(wm.workspace_claims(self.con)), 1)

    def test_finalize_detects_change_after_external_inspection(self):
        ticket = self.reserve(self.spec())
        report = wm.provision_workspace(self.db, ticket['workspace_id'], ticket['operation_token'])
        self.assertTrue(report['ok'])
        (Path(ticket['path']) / 'late.txt').write_text('late writer', encoding='utf-8')
        self.con.execute('BEGIN IMMEDIATE')
        ws = wm.finalize_workspace(self.con, ticket['workspace_id'], report)
        self.con.commit()
        self.assertEqual(ws['status'], 'quarantined')

    def test_recovery_ownership_is_not_quiescence_and_stale_proof_rejected(self):
        ws = self.create()
        report = wm.inspect_workspace(ws)
        proof = self.proof(ws)
        self.con.execute('BEGIN IMMEDIATE')
        recovered = wm.recover_workspace(self.con, ws['workspace_id'], report)
        self.con.commit()
        self.assertEqual(recovered['status'], 'orphaned')
        self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])
        self.con.execute('BEGIN IMMEDIATE')
        with self.assertRaises(wm.WorkspaceError):
            wm.recover_workspace(self.con, ws['workspace_id'], wm.inspect_workspace(recovered), quiescence=proof)
        self.con.rollback()
        self.con.execute('BEGIN IMMEDIATE')
        ready = wm.recover_workspace(self.con, ws['workspace_id'], wm.inspect_workspace(recovered),
                                     quiescence=self.proof(recovered))
        self.con.commit()
        self.assertEqual(ready['status'], 'ready')

    def test_active_recovery_keeps_claim_without_writer_exit_proof(self):
        ws = self.create()
        self.con.execute('BEGIN IMMEDIATE')
        active = wm.mark_workspace_active(self.con, ws['workspace_id'])
        self.con.commit()
        self.con.execute('BEGIN IMMEDIATE')
        recovered = wm.recover_workspace(self.con, ws['workspace_id'], wm.inspect_workspace(active))
        self.con.commit()
        self.assertEqual(recovered['status'], 'active')
        self.assertEqual(len(wm.workspace_claims(self.con)), 1)

    def test_physical_artifact_overlap_and_same_relative_paths_are_distinct(self):
        artifact = self.root / 'artifact-directory'
        first = self.reserve(self.spec(artifacts=[str(artifact)]))
        spec = self.spec(artifacts=[str(artifact / 'child')])
        self.con.execute('BEGIN IMMEDIATE')
        with self.assertRaises(wm.WorkspaceError):
            wm.reserve_workspace(self.con, spec)
        self.con.rollback()
        self.assertEqual(wm.workspace_claims(self.con, 'other-host'), [])
        self.assertEqual(wm.workspace_claims(self.con)[0]['assignment_id'], first['assignment_id'])

    def test_invalid_roots_refs_and_globs(self):
        with self.assertRaises(wm.WorkspaceError):
            self.spec(path=self.repo / 'inside')
        with self.assertRaises(wm.WorkspaceError):
            self.spec(allowed=['../escape'])
        with self.assertRaises(wm.WorkspaceError):
            self.spec(branch='awo/../bad')
        with self.assertRaises(wm.WorkspaceError):
            self.spec(branch='main')
        self.assertFalse(wm._allowed('src/nested/file.py', ['src/*']))
        self.assertTrue(wm._allowed('src/nested/file.py', ['src/**']))

    def test_directory_noncode_snapshot_and_scope_without_git(self):
        with mock.patch.object(wm, '_git', side_effect=AssertionError('noncode must not invoke Git')):
            ws = self.create(kind='directory', allowed=['outputs/**'])
            outputs = Path(ws['path']) / 'outputs'
            outputs.mkdir()
            (outputs / 'result.bin').write_bytes(b'\x00noncode-result')
            inventory = wm.collect_inventory(ws)
            self.assertTrue(inventory['scope_ok'])
            self.assertIsNone(inventory['head'])
            snapshot = wm.snapshot_workspace(ws, str(self.evidence))
            self.assertEqual(wm.verify_snapshot(snapshot)['patches'], {})
        self.assertEqual(self.con.execute("SELECT task_state FROM v2_task WHERE task_id='T'").fetchone()[0], 'ready')
        self.con.execute('BEGIN IMMEDIATE')
        with self.assertRaises(wm.WorkspaceError):
            wm.begin_release(self.con, ws['workspace_id'], wm.inspect_workspace(ws), snapshot,
                             quiescence=self.proof(ws))
        self.con.rollback()
        self.assertTrue((outputs / 'result.bin').exists())

    def test_clean_release_requires_quiescence_retains_branch_and_snapshot(self):
        ws = self.create()
        snapshot = wm.snapshot_workspace(ws, str(self.evidence))
        report = wm.inspect_workspace(ws)
        self.con.execute('BEGIN IMMEDIATE')
        with self.assertRaises(wm.WorkspaceError):
            wm.begin_release(self.con, ws['workspace_id'], report, snapshot, quiescence={'verified': True})
        self.con.rollback()
        self.con.execute('BEGIN IMMEDIATE')
        ticket = wm.begin_release(self.con, ws['workspace_id'], report, snapshot, quiescence=self.proof(ws))
        self.con.commit()
        outcome = wm.release_workspace(self.db, ws['workspace_id'], ticket['operation_token'])
        self.assertTrue(outcome['ok'], outcome)
        self.con.execute('BEGIN IMMEDIATE')
        released = wm.finalize_release(self.con, ws['workspace_id'], outcome)
        self.con.commit()
        self.assertEqual(released['status'], 'released')
        self.assertFalse(Path(ws['path']).exists())
        self.assertEqual(wm.workspace_claims(self.con), [])
        self.assertEqual(self.git(self.repo, 'rev-parse', ws['branch']).decode().strip(), self.base)
        self.assertTrue(wm.verify_snapshot(snapshot)['inventory']['scope_ok'])

    def test_dirty_ignored_content_prevents_release(self):
        ws = self.create()
        path = Path(ws['path']) / 'ignored'
        path.mkdir()
        (path / 'output.bin').write_bytes(b'\x00keep')
        snapshot = wm.snapshot_workspace(ws, str(self.evidence))
        self.con.execute('BEGIN IMMEDIATE')
        with self.assertRaises(wm.WorkspaceError):
            wm.begin_release(self.con, ws['workspace_id'], wm.inspect_workspace(ws), snapshot,
                             quiescence=self.proof(ws))
        self.con.rollback()
        self.assertTrue((path / 'output.bin').exists())

    def test_release_rechecks_new_writer_and_does_not_delete(self):
        ws = self.create()
        snapshot = wm.snapshot_workspace(ws, str(self.evidence))
        self.con.execute('BEGIN IMMEDIATE')
        ticket = wm.begin_release(self.con, ws['workspace_id'], wm.inspect_workspace(ws), snapshot,
                                  quiescence=self.proof(ws))
        self.con.commit()
        (Path(ws['path']) / 'late.bin').write_bytes(b'new writer')
        outcome = wm.release_workspace(self.db, ws['workspace_id'], ticket['operation_token'])
        self.assertFalse(outcome['ok'])
        self.con.execute('BEGIN IMMEDIATE')
        ws = wm.finalize_release(self.con, ws['workspace_id'], outcome)
        self.con.commit()
        self.assertEqual(ws['status'], 'quarantined')
        self.assertTrue((Path(ws['path']) / 'late.bin').exists())

    def test_large_file_unknown_and_corrupt_snapshot_fail_closed(self):
        ws = self.create(kind='directory')
        (Path(ws['path']) / 'big.dat').write_bytes(b'12345')
        with mock.patch.object(wm, 'MAX_FILE_BYTES', 4):
            inventory = wm.collect_inventory(ws)
            self.assertFalse(inventory['scope_ok'])
            self.assertTrue(inventory['problems'])
            with self.assertRaises(wm.WorkspaceError):
                wm.snapshot_workspace(ws, str(self.evidence))
        snap = wm.snapshot_workspace(ws, str(self.evidence))
        Path(snap['manifest_path']).write_bytes(b'corrupt')
        with self.assertRaises(wm.WorkspaceError):
            wm.verify_snapshot(snap)

    def test_symlink_escape_is_not_followed(self):
        ws = self.create(kind='directory')
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'outside must not be captured')
        link = Path(ws['path']) / 'escape'
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('OS symlink creation unavailable: ' + str(exc))
        inventory = wm.collect_inventory(ws)
        row = next(row for row in inventory['entries'] if row['path'] == 'escape')
        self.assertEqual(row['kind'], 'link')
        self.assertTrue(row['escapes'])
        self.assertNotEqual(row['sha256'], hashlib.sha256(outside.read_bytes()).hexdigest())
        with self.assertRaises(wm.WorkspaceError):
            wm.snapshot_workspace(ws, str(self.evidence))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction fixture')
    def test_windows_junction_escape_rejected(self):
        ws = self.create(kind='directory')
        outside = self.root / 'outside-dir'
        outside.mkdir()
        (outside / 'keep.txt').write_bytes(b'outside')
        junction = Path(ws['path']) / 'junction'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                                capture_output=True, timeout=10, shell=False)
        if result.returncode:
            self.skipTest('junction unavailable: ' + result.stderr.decode(errors='replace'))
        try:
            inventory = wm.collect_inventory(ws)
            self.assertFalse(inventory['scope_ok'])
            self.assertTrue(any('path_escape' in item for item in inventory['problems']))
            with self.assertRaises(wm.WorkspaceError):
                wm.prepare_workspace(task_id='T', assignment_id='AX', host='fixture-host', kind='directory',
                                     root=str(junction), path=str(junction / 'child'), allowed_paths=['**'])
            self.assertEqual((outside / 'keep.txt').read_bytes(), b'outside')
        finally:
            junction.rmdir()

    def test_timeout_and_output_limit_use_real_processes(self):
        with self.assertRaises(wm.WorkspaceError):
            wm.run_argv([sys.executable, '-B', '-c', 'import time; time.sleep(5)'], cwd=self.root, timeout=0.1)
        with self.assertRaises(wm.WorkspaceError):
            wm.run_argv([sys.executable, '-B', '-c', "print('x'*100000)"], cwd=self.root, max_bytes=100)

    def child(self, code, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(Path(__file__).parent.resolve()))
        return subprocess.Popen([sys.executable, '-B', '-c', code, *map(str, args)],
                                cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_cross_process_reservation_race_one_physical_claim(self):
        first = self.spec(kind='directory')
        second = self.spec(kind='directory', path=first['path'])
        files = []
        for i, spec in enumerate((first, second)):
            path = self.root / ('spec-' + str(i) + '.json')
            path.write_text(json.dumps(spec), encoding='utf-8')
            files.append(path)
        code = """import json,sqlite3,sys
import workspace_manager as w
c=sqlite3.connect(sys.argv[1],timeout=10)
try:
 c.execute('BEGIN IMMEDIATE')
 w.reserve_workspace(c,json.load(open(sys.argv[2])))
 c.commit(); print('reserved')
except w.WorkspaceError as e:
 c.rollback(); print('blocked:'+str(e)); sys.exit(3)
finally: c.close()
"""
        children = [self.child(code, self.db, path) for path in files]
        outputs = [child.communicate(timeout=15) for child in children]
        self.assertEqual(sorted(child.returncode for child in children), [0, 3], outputs)
        self.assertEqual(len(wm.workspace_claims(self.con)), 1)
        self.assertFalse(Path(first['path']).exists())

    def test_process_death_after_git_create_before_marker_does_not_adopt(self):
        ticket = self.reserve(self.spec())
        code = """import os,sys
import workspace_manager as w
ws=w._consume_ticket(sys.argv[1],sys.argv[2],sys.argv[3],'create')
w._git(ws['repo_root'],'worktree','add','--no-track','-b',ws['branch'],'--',ws['path'],ws['base_commit'])
os._exit(73)
"""
        child = self.child(code, self.db, ticket['workspace_id'], ticket['operation_token'])
        output = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 73, output)
        ws = self.current(ticket)
        self.assertTrue(Path(ws['path']).is_dir())
        self.assertFalse(wm._marker_path(ws).exists())
        report = wm.inspect_workspace(ws)
        self.assertFalse(report['ok'])
        self.con.execute('BEGIN IMMEDIATE')
        recovered = wm.recover_workspace(self.con, ws['workspace_id'], report)
        self.con.commit()
        self.assertEqual(recovered['status'], 'quarantined')
        with self.assertRaises(wm.WorkspaceError):
            wm.provision_workspace(self.db, ws['workspace_id'], ticket['operation_token'])
        self.assertEqual(len(wm.workspace_claims(self.con)), 1)

    def test_process_death_after_owned_create_requires_inspected_quiescence(self):
        ticket = self.reserve(self.spec(kind='directory'))
        code = """import os,sys
import workspace_manager as w
r=w.provision_workspace(sys.argv[1],sys.argv[2],sys.argv[3])
os._exit(74 if r['ok'] else 75)
"""
        child = self.child(code, self.db, ticket['workspace_id'], ticket['operation_token'])
        output = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 74, output)
        ws = self.current(ticket)
        report = wm.inspect_workspace(ws)
        self.assertTrue(report['ok'])
        self.con.execute('BEGIN IMMEDIATE')
        recovered = wm.recover_workspace(self.con, ws['workspace_id'], report)
        self.con.commit()
        self.assertEqual(recovered['status'], 'orphaned')
        self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])

    def test_inspection_alone_cannot_finalize_crashed_create(self):
        ticket = self.reserve(self.spec(kind='directory'))
        successful = wm.provision_workspace(self.db, ticket['workspace_id'], ticket['operation_token'])
        self.assertTrue(successful['ok'])
        ws = self.current(ticket)
        observation = wm.inspect_workspace(ws)
        self.con.execute('BEGIN IMMEDIATE')
        quarantined = wm.finalize_workspace(self.con, ws['workspace_id'], observation)
        self.con.commit()
        self.assertEqual(quarantined['status'], 'quarantined')
        self.assertTrue(Path(ws['path']).exists())

    def test_cross_process_provision_ticket_consumed_once(self):
        ticket = self.reserve(self.spec(kind='directory'))
        code = """import sys
import workspace_manager as w
try:
 r=w.provision_workspace(sys.argv[1],sys.argv[2],sys.argv[3])
 print(r['ok']); sys.exit(0 if r['ok'] else 4)
except w.WorkspaceError as e:
 print(str(e)); sys.exit(3)
"""
        children = [self.child(code, self.db, ticket['workspace_id'], ticket['operation_token']) for _ in range(2)]
        outputs = [child.communicate(timeout=15) for child in children]
        self.assertEqual(sorted(child.returncode for child in children), [0, 3], outputs)
        ws = self.current(ticket)
        self.assertTrue(wm.inspect_workspace(ws)['ok'])
        self.assertEqual(ws['status'], 'provisioning')
        self.assertEqual(self.con.execute("SELECT count(*) FROM v2_workspace_journal WHERE kind='create_started'").fetchone()[0], 1)

    def test_process_death_before_commit_leaves_no_reservation_or_path(self):
        spec = self.spec(kind='directory')
        path = self.root / 'uncommitted-spec.json'
        path.write_text(json.dumps(spec), encoding='utf-8')
        code = """import json,os,sqlite3,sys
import workspace_manager as w
c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE')
w.reserve_workspace(c,json.load(open(sys.argv[2])))
os._exit(76)
"""
        child = self.child(code, self.db, path)
        output = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 76, output)
        self.assertIsNone(wm.get_workspace(self.con, spec['workspace_id']))
        self.assertFalse(Path(spec['path']).exists())
        self.assertEqual(wm.workspace_claims(self.con), [])

    def test_live_writer_retains_active_claim_through_recovery(self):
        ws = self.create()
        self.con.execute('BEGIN IMMEDIATE')
        ws = wm.mark_workspace_active(self.con, ws['workspace_id'])
        self.con.commit()
        signal = self.root / 'writer-started'
        stop = self.root / 'writer-stop'
        code = """import sys,time
from pathlib import Path
Path(sys.argv[1]).write_bytes(b'live fixture writer')
Path(sys.argv[2]).write_bytes(b'ready')
end=time.monotonic()+15
while not Path(sys.argv[3]).exists() and time.monotonic()<end: time.sleep(.02)
"""
        child = self.child(code, Path(ws['path']) / 'alpha.txt', signal, stop)
        try:
            import time
            deadline = time.monotonic() + 10
            while not signal.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(signal.exists())
            self.assertIsNone(child.poll())
            report = wm.inspect_workspace(ws)
            self.con.execute('BEGIN IMMEDIATE')
            recovered = wm.recover_workspace(self.con, ws['workspace_id'], report)
            self.con.commit()
            self.assertEqual(recovered['status'], 'active')
            self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])
            self.assertEqual(len(wm.workspace_claims(self.con)), 1)
        finally:
            stop.write_bytes(b'stop')
            child.communicate(timeout=20)

    def test_staged_only_binary_content_is_in_patch_and_scope(self):
        ws = self.create(allowed=['alpha.txt'])
        path = Path(ws['path'])
        original = (path / 'binary.bin').read_bytes()
        (path / 'binary.bin').write_bytes(b'\x00staged-only-byte-content')
        self.git(path, 'add', 'binary.bin')
        (path / 'binary.bin').write_bytes(original)
        inventory = wm.collect_inventory(ws)
        self.assertIn('binary.bin', inventory['changed_paths'])
        self.assertFalse(inventory['scope_ok'])
        snap = wm.snapshot_workspace(ws, str(self.evidence))
        manifest = wm.verify_snapshot(snap)
        digest = manifest['patches']['staged']
        patch = (self.evidence / 'sha256' / digest[:2] / digest).read_bytes()
        self.assertIn(b'GIT binary patch', patch)
        self.assertNotEqual(manifest['patches']['staged'], manifest['patches']['base_to_worktree'])

    def test_snapshot_detects_content_change_during_archive(self):
        ws = self.create(kind='directory')
        artifact = Path(ws['path']) / 'output.txt'
        artifact.write_bytes(b'first')
        original = wm._cas
        def changed(root, data):
            result = original(root, data)
            artifact.write_bytes(b'second')
            return result
        with mock.patch.object(wm, '_cas', side_effect=changed):
            with self.assertRaises(wm.WorkspaceError):
                wm.snapshot_workspace(ws, str(self.evidence))

    def test_snapshot_detects_missing_or_corrupt_content_blobs(self):
        ws = self.create(kind='directory')
        artifact = Path(ws['path']) / 'output.txt'
        artifact.write_bytes(b'content-addressed')
        snap = wm.snapshot_workspace(ws, str(self.evidence))
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        blob = self.evidence / 'sha256' / digest[:2] / digest
        blob.write_bytes(b'different')
        with self.assertRaises(wm.WorkspaceError):
            wm.verify_snapshot(snap)
        blob.unlink()
        with self.assertRaises(OSError):
            wm.verify_snapshot(snap)

    def test_hardlink_cannot_exfiltrate_external_content(self):
        ws = self.create(kind='directory')
        source = self.root / 'outside-content'
        source.write_bytes(b'do not archive')
        try:
            os.link(source, Path(ws['path']) / 'hardlink')
        except OSError as exc:
            self.skipTest('hardlinks unavailable: ' + str(exc))
        inventory = wm.collect_inventory(ws)
        self.assertFalse(inventory['scope_ok'])
        self.assertIsNone(inventory['entries'][0]['sha256'])

    def test_git_symlink_as_text_on_windows_is_explicit_unknown(self):
        ws = self.create()
        path = Path(ws['path'])
        # Git index mode test does not require Windows symlink privileges.
        self.git(path, 'update-index', '--add', '--cacheinfo', '120000',
                 self.git(path, 'hash-object', '-w', '--stdin').decode().strip(), 'encoded-link')
        (path / 'encoded-link').write_bytes(b'../../outside')
        inventory = wm.collect_inventory(ws)
        self.assertTrue(any('git_symlink_requires_review' in value for value in inventory['problems']))
        self.assertFalse(inventory['scope_ok'])

    def test_submodule_gitlink_is_explicit_unknown(self):
        ws = self.create()
        self.git(ws['path'], 'update-index', '--add', '--cacheinfo', '160000', self.base, 'submodule-fixture')
        inventory = wm.collect_inventory(ws)
        self.assertTrue(any('submodule_requires_separate_inventory' in value for value in inventory['problems']))
        self.assertFalse(inventory['scope_ok'])

    def test_empty_directory_release_preserves_external_evidence(self):
        ws = self.create(kind='directory')
        snap = wm.snapshot_workspace(ws, str(self.evidence))
        self.con.execute('BEGIN IMMEDIATE')
        ticket = wm.begin_release(self.con, ws['workspace_id'], wm.inspect_workspace(ws), snap,
                                  quiescence=self.proof(ws))
        self.con.commit()
        result = wm.release_workspace(self.db, ws['workspace_id'], ticket['operation_token'])
        self.assertTrue(result['ok'], result)
        self.con.execute('BEGIN IMMEDIATE')
        released = wm.finalize_release(self.con, ws['workspace_id'], result)
        self.con.commit()
        self.assertEqual(released['status'], 'released')
        self.assertEqual(wm.verify_snapshot(snap)['inventory']['entries'], [])

    def test_legacy_claims_and_retained_evidence_are_respected(self):
        spec = self.spec(kind='directory')
        self.con.execute("INSERT INTO tasks(id,status,data) VALUES ('legacy','uncertain',?)",
                         (json.dumps({'request': {}}),))
        self.con.execute("INSERT INTO dispatches(task,status,data) VALUES ('legacy','uncertain',?)",
                         (json.dumps({'workspaces': [str(self.workspaces)]}),))
        self.con.commit()
        self.con.execute('BEGIN IMMEDIATE')
        with self.assertRaises(wm.WorkspaceError):
            wm.reserve_workspace(self.con, spec)
        self.con.rollback()
        self.con.execute("UPDATE tasks SET status='resolved'")
        self.con.execute("INSERT INTO reviews(event,data) VALUES ('retained-review',?)",
                         (json.dumps({'path': str(Path(spec['path']) / 'REVIEW.md')}),))
        self.con.commit()
        self.con.execute('BEGIN IMMEDIATE')
        with self.assertRaises(wm.WorkspaceError):
            wm.reserve_workspace(self.con, spec)
        self.con.rollback()

    def test_final_gate_detects_git_branch_ref_drift(self):
        ws = self.create()
        (self.repo / 'alpha.txt').write_bytes(b'new fixture source commit')
        self.commit('advance fixture source')
        tip = self.git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        self.git(self.repo, 'update-ref', 'refs/heads/' + ws['branch'], tip, ws['base_commit'])
        self.con.execute('BEGIN IMMEDIATE')
        with mock.patch.object(wm, '_git', side_effect=AssertionError('final gate ran Git')):
            self.assertFalse(wm.check_workspace_ready(self.con, ws['workspace_id'])['ready'])
        self.con.rollback()

    def test_prepare_rejects_checkout_filters_before_side_effects(self):
        self.git(self.repo, 'config', 'filter.fixture.smudge', 'some-command')
        with self.assertRaises(wm.WorkspaceError):
            self.spec()
        self.assertEqual(list(self.workspaces.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
