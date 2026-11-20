'''Disposable real Git/SQLite fixtures; no network or source-repository writes.'''
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import receipt_export as receipts
from test_runtime_views import NOW, SECRET, database, insert


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class ReceiptBuildTests(unittest.TestCase):
    def setUp(self):
        self.con = database()
        self.addCleanup(self.con.close)

    def test_sanitization_preserves_claims_and_exact_source_payload_hash(self):
        raw = json.dumps({'token': SECRET, 'environment': {'SECRET': SECRET},
                          'path': SECRET, 'command': SECRET, 'message': SECRET,
                          'state': 'completed', 'evidence_sha256': 'c' * 64,
                          'inventory': [{'sealed_sha256': 'D' * 64, 'path': SECRET}],
                          'credential': {'sha256': 'e' * 64}})
        self.con.execute("UPDATE v2_event SET payload_json=? WHERE event_id='event-1'", (raw,))
        receipt = receipts.build_receipt(self.con, 'project')
        encoded = receipts._canonical(receipt)
        for private in (SECRET, 'FOREIGN-TITLE', 'foreign-task', 'fixture-host'):
            self.assertNotIn(private.encode(), encoded)
        self.assertNotIn(('e' * 64).encode(), encoded)
        payload = receipt['events'][0]['payload']
        self.assertEqual(payload['source_payload_utf8_sha256'], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual({r['value'] for r in payload['stated_hashes']}, {'c' * 64, 'D' * 64})
        self.assertGreater(payload['omitted_top_level_fields'], 0)
        self.assertEqual(payload['fields']['state'], 'completed')
        self.assertEqual(receipt['events'][0]['exported_payload_sha256'],
                         receipts._sha(receipts._canonical(payload)))

    def test_ids_relationships_and_event_chain_are_deterministic(self):
        receipt = receipts.build_receipt(self.con, 'project')
        again = receipts.build_receipt(self.con, 'project')
        self.assertEqual(receipts._canonical(receipt), receipts._canonical(again))
        self.assertEqual([e['global_seq'] for e in receipt['events']], [1, 3])
        self.assertEqual(receipt['events'][0]['previous_sha256'], receipts.ZERO_HASH)
        self.assertEqual(receipt['events'][1]['previous_sha256'],
                         receipts._sha(receipts._canonical(receipt['events'][0])))
        task = next(r['data'] for r in receipt['records']
                    if r['table'] == 'v2_task' and r['data']['task_state'] == 'review_ready')
        evidence = next(r['data'] for r in receipt['records'] if r['table'] == 'v2_evidence')
        self.assertEqual(task['task_id'], evidence['task_id'])
        self.assertEqual(evidence['sealed_sha256'], 'a' * 64)
        self.assertEqual(evidence['current_sha256'], 'a' * 64)

    def test_read_only_queries_and_owned_transaction(self):
        queries, changes = [], self.con.total_changes
        self.con.set_trace_callback(queries.append)
        receipts.build_receipt(self.con, 'project')
        self.assertTrue(all(q.upper().startswith(('SELECT', '-- PRAGMA TABLE_INFO=')) for q in queries))
        self.assertTrue(self.con.in_transaction)
        self.assertEqual(changes, self.con.total_changes)
        self.con.commit()
        with self.assertRaises(receipts.ReceiptError):
            receipts.build_receipt(self.con, 'project')

    def test_row_bound_fails_instead_of_incomplete_archive(self):
        with self.assertRaisesRegex(receipts.ReceiptError, 'bound'):
            receipts.build_receipt(self.con, 'project', max_rows=1)

    def test_unavailable_optional_sources_remain_explicit(self):
        self.con.execute('DROP TABLE v2_review')
        receipt = receipts.build_receipt(self.con, 'project')
        self.assertEqual(receipt['sources']['v2_review'], {'available': False, 'count': 0})
        self.con.execute('DROP TABLE v2_event')
        with self.assertRaises(receipts.ReceiptError):
            receipts.build_receipt(self.con, 'project')

    def test_invalid_event_json_and_nonfinite_numbers_rejected(self):
        for raw in ('{bad', '{"amount": NaN}', '[]', '{"state":"completed","state":"failed"}'):
            self.con.execute("UPDATE v2_event SET payload_json=?", (raw,))
            with self.subTest(raw=raw), self.assertRaises(receipts.ReceiptError):
                receipts.build_receipt(self.con, 'project')

    def test_unknown_enums_and_private_timestamps_are_not_exported(self):
        raw = json.dumps({'state': SECRET, 'at': SECRET, 'sealed_sha256': SECRET,
                          'task_id': SECRET, 'unapproved_name': SECRET})
        self.con.execute("UPDATE v2_event SET payload_json=?", (raw,))
        receipt = receipts.build_receipt(self.con, 'project')
        self.assertNotIn(SECRET, json.dumps(receipt))
        fields = receipt['events'][0]['payload']['fields']
        self.assertIsNone(fields['state'])
        self.assertIsNone(fields['at'])
        self.assertTrue(fields['task_id'].startswith('id:'))

    def test_payload_depth_is_bounded(self):
        value = {}
        for _ in range(40):
            value = {'nested': value}
        self.con.execute('UPDATE v2_event SET payload_json=?', (json.dumps(value),))
        with self.assertRaises(receipts.ReceiptError):
            receipts.build_receipt(self.con, 'project')

    def test_workspace_snapshot_claims_retained_without_paths(self):
        insert(self.con, 'v2_workspace', workspace_id='workspace', task_id='t-run', assignment_id='a-run',
               host='fixture', path=SECRET, kind='git_worktree', spec_json='{}', spec_sha256='a' * 64,
               status='frozen', created_at=NOW, snapshot_json=json.dumps({
                   'manifest_sha256': 'b' * 64, 'inventory_sha256': 'c' * 64,
                   'scope_ok': True, 'dirty': False, 'manifest_path': SECRET}))
        receipt = receipts.build_receipt(self.con, 'project')
        row = next(r['data'] for r in receipt['records'] if r['table'] == 'v2_workspace')
        self.assertEqual(row['snapshot']['inventory_sha256'], 'c' * 64)
        self.assertEqual(row['kind'], 'git_worktree')
        self.assertNotIn(SECRET, json.dumps(row))


@unittest.skipUnless(shutil.which('git'), 'Git CLI is required for disposable receipt fixtures')
class ReceiptGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get('AWO_VISIBILITY_TEST_ROOT'))
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.protected = self.root / 'protected'
        self.protected.mkdir()
        self.con = database()
        self.addCleanup(self.con.close)
        self.receipt = receipts.build_receipt(self.con, 'project')

    def export(self, name='receipt'):
        repository = self.root / name
        result = receipts.export_receipt(self.receipt, repository, enabled=True,
                                         protected_roots=[self.protected])
        return repository, result

    def test_real_git_export_verify_and_readonly_verification(self):
        repository, result = self.export()
        self.assertTrue(result['verified'])
        self.assertEqual(result['event_count'], 2)
        before = tree_bytes(repository)
        unpinned = receipts.verify_receipt(repository)
        self.assertEqual(unpinned['trust'], 'internal consistency only')
        pinned = receipts.verify_receipt(repository, expected_manifest_sha256=result['manifest_sha256'],
                                          expected_commit=result['commit'])
        self.assertEqual(pinned['trust'], 'externally pinned')
        self.assertEqual(before, tree_bytes(repository))
        committed = receipts._git(repository, ['ls-tree', '-r', '--name-only', 'HEAD']).decode()
        self.assertIn('manifest.json', committed)
        self.assertNotIn('.env', committed)

    def test_rebuild_exact_sanitized_audit_projection_and_rollback(self):
        repository, result = self.export()
        target = sqlite3.connect(':memory:')
        try:
            target.execute('BEGIN')
            rebuilt = receipts.rebuild_receipt(target, repository, expected_commit=result['commit'])
            self.assertTrue(target.in_transaction)
            self.assertFalse(rebuilt['runtime_restore'])
            rows = target.execute('SELECT data_json FROM receipt_record ORDER BY ordinal').fetchall()
            self.assertEqual([json.loads(row[0]) for row in rows], self.receipt['records'])
            events = target.execute('SELECT data_json FROM receipt_event ORDER BY global_seq').fetchall()
            self.assertEqual([json.loads(row[0]) for row in events], self.receipt['events'])
            target.rollback()
            self.assertEqual(target.execute('SELECT name FROM sqlite_master').fetchall(), [])
        finally:
            target.close()

    def test_rebuild_refuses_runtime_db_and_missing_transaction(self):
        repository, _ = self.export()
        target = sqlite3.connect(':memory:')
        try:
            with self.assertRaises(receipts.ReceiptError):
                receipts.rebuild_receipt(target, repository)
            target.execute('BEGIN')
            target.execute('CREATE TABLE v2_task(task_id)')
            with self.assertRaises(receipts.ReceiptError):
                receipts.rebuild_receipt(target, repository)
            self.assertEqual(target.execute('SELECT name FROM sqlite_master').fetchall(), [('v2_task',)])
        finally:
            target.close()

    def test_rebuild_verifies_before_any_write(self):
        repository, _ = self.export()
        (repository / 'manifest.json').write_bytes(b'{}')
        target = sqlite3.connect(':memory:')
        try:
            target.execute('BEGIN')
            with self.assertRaises(receipts.ReceiptError):
                receipts.rebuild_receipt(target, repository)
            self.assertEqual(target.execute('SELECT name FROM sqlite_master').fetchall(), [])
        finally:
            target.close()

    def test_opt_in_required_before_destination_creation(self):
        for enabled in (False, None, 1, 'true'):
            destination = self.root / 'disabled'
            with self.subTest(enabled=enabled), self.assertRaises(receipts.ReceiptError):
                receipts.export_receipt(self.receipt, destination, enabled=enabled,
                                         protected_roots=[self.protected])
            self.assertFalse(destination.exists())

    def test_empty_existing_and_protected_destinations_refused(self):
        existing = self.root / 'existing'
        existing.mkdir()
        marker = existing / 'owned-by-someone-else'
        marker.write_text('fixture', encoding='utf-8')
        for path in (existing, self.protected / 'nested', self.root, self.root / '..' / 'escape'):
            with self.subTest(path=path), self.assertRaises(receipts.ReceiptError):
                receipts.export_receipt(self.receipt, path, enabled=True, protected_roots=[self.protected])
        self.assertEqual(marker.read_text(encoding='utf-8'), 'fixture')
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_receipt(self.receipt, self.root / 'new', enabled=True, protected_roots=[])

    def test_nested_git_destination_refused(self):
        repository, _ = self.export()
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_receipt(self.receipt, repository / 'nested', enabled=True,
                                     protected_roots=[self.protected])
        self.assertFalse((repository / 'nested').exists())

    def test_detached_receipt_cannot_add_private_fields(self):
        for mutate in (
                lambda r: r.update(secret=SECRET),
                lambda r: r['records'][0]['data'].update(token=SECRET),
                lambda r: r['events'][0]['payload']['fields'].update(state=SECRET),
                lambda r: r['limitations'].append(SECRET)):
            forged = copy.deepcopy(self.receipt)
            mutate(forged)
            destination = self.root / 'forged'
            with self.assertRaises(receipts.ReceiptError):
                receipts.export_receipt(forged, destination, enabled=True, protected_roots=[self.protected])
            self.assertFalse(destination.exists())

    def test_content_hash_chain_manifest_and_trust_anchor_tampering(self):
        repository, result = self.export()
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository, expected_manifest_sha256='0' * 64)
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository, expected_commit='0' * 40)
        obj = next((repository / 'objects').glob('*.json'))
        original = obj.read_bytes()
        obj.write_bytes(original + b' ')
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository)
        obj.write_bytes(original)
        self.assertTrue(receipts.verify_receipt(repository, expected_commit=result['commit'])['verified'])
        forged = copy.deepcopy(self.receipt)
        forged['events'][1]['previous_sha256'] = receipts.ZERO_HASH
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_receipt(forged, self.root / 'forged', enabled=True,
                                     protected_roots=[self.protected])

    def test_extra_missing_and_uncommitted_files_fail_verification(self):
        repository, _ = self.export()
        extra = repository / 'private.txt'
        extra.write_text('fixture', encoding='utf-8')
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository)
        extra.unlink()
        path = next((repository / 'objects').glob('*.json'))
        content = path.read_bytes()
        path.unlink()
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository)
        path.write_bytes(content)
        receipts._git(repository, ['update-index', '--force-remove', '--', path.relative_to(repository).as_posix()])
        receipts._git(repository, ['commit', '--amend', '--quiet', '--no-verify', '--no-edit'])
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository)

    def test_rehashed_event_still_requires_valid_chain(self):
        repository, _ = self.export()
        manifest_file = repository / 'manifest.json'
        manifest = json.loads(manifest_file.read_text(encoding='ascii'))
        old_hash = manifest['event_sha256'][1]
        old_file = repository / 'objects' / (old_hash + '.json')
        event = json.loads(old_file.read_text(encoding='ascii'))
        event['previous_sha256'] = receipts.ZERO_HASH
        content = receipts._canonical(event)
        new_hash = receipts._sha(content)
        (repository / 'objects' / (new_hash + '.json')).write_bytes(content)
        old_file.unlink()
        manifest['event_sha256'][1] = new_hash
        manifest['files'].pop(old_hash)
        manifest['files'][new_hash] = len(content)
        manifest_file.write_bytes(receipts._canonical(manifest))
        with self.assertRaisesRegex(receipts.ReceiptError, 'chain'):
            receipts.verify_receipt(repository)

    def test_local_repo_config_and_alternates_are_rejected(self):
        repository, _ = self.export()
        config = repository / '.git' / 'config'
        original = config.read_bytes()
        config.write_bytes(original + b'\n[include]\npath = private-config\n')
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository)
        config.write_bytes(original)
        alternate = repository / '.git' / 'objects' / 'info' / 'alternates'
        alternate.write_text(str(self.protected), encoding='utf-8')
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository)

    def test_inherited_git_config_and_git_dir_are_not_used(self):
        global_config = self.root / 'global-config'
        global_config.write_text('[core]\n hooksPath = BAD-FIXTURE-PATH\n[commit]\n gpgsign = true\n',
                                 encoding='utf-8')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(global_config),
                                     'GIT_DIR': str(self.protected), 'GIT_WORK_TREE': str(self.protected),
                                     'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.bare',
                                     'GIT_CONFIG_VALUE_0': 'true'}):
            repository, result = self.export()
            self.assertTrue(result['verified'])
        self.assertEqual(list(self.protected.iterdir()), [])
        self.assertTrue(receipts.verify_receipt(repository)['verified'])

    def test_identical_receipts_have_identical_manifests_and_commits(self):
        _, first = self.export('one')
        _, second = self.export('two')
        self.assertEqual(first['manifest_sha256'], second['manifest_sha256'])
        self.assertEqual(first['commit'], second['commit'])

    def test_export_failure_preserves_partial_destination(self):
        destination = self.root / 'partial'
        with patch('receipt_export.shutil.which', return_value=None):
            with self.assertRaises(receipts.ReceiptError):
                receipts.export_receipt(self.receipt, destination, enabled=True,
                                         protected_roots=[self.protected])
        self.assertTrue((destination / 'manifest.json').exists())
        before = tree_bytes(destination)
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_receipt(self.receipt, destination, enabled=True,
                                     protected_roots=[self.protected])
        self.assertEqual(before, tree_bytes(destination))

    def test_bounded_git_output_uses_actual_subprocess(self):
        repository, _ = self.export()
        with self.assertRaisesRegex(receipts.ReceiptError, 'bound'):
            receipts._git(repository, ['log', '-1', '--format=fuller'], max_output=1)

    def test_hardlinks_are_refused(self):
        repository, _ = self.export()
        path = repository / 'manifest.json'
        try:
            os.link(path, self.root / 'alias')
        except OSError:
            self.skipTest('Hardlink creation unavailable on this fixture filesystem')
        with self.assertRaises(receipts.ReceiptError):
            receipts.verify_receipt(repository)

    def test_symlink_ancestor_is_refused(self):
        link = self.root / 'symlink'
        try:
            link.symlink_to(self.protected, target_is_directory=True)
        except OSError:
            self.skipTest('Directory symlink creation requires unavailable platform privileges')
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_receipt(self.receipt, link / 'receipt', enabled=True,
                                     protected_roots=[self.root / 'unrelated'])
        self.assertEqual(list(self.protected.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction fixture')
    def test_windows_junction_ancestor_is_refused(self):
        link = self.root / 'junction'
        process = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(self.protected)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, shell=False)
        if process.returncode:
            self.skipTest('Junction creation unavailable on this fixture filesystem')
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_receipt(self.receipt, link / 'receipt', enabled=True,
                                     protected_roots=[self.root / 'unrelated'])
        self.assertEqual(list(self.protected.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
