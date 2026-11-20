"""Managed workspace service, scheduler and review integration with actual Git."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

import awo_service as service
import priority_scheduler as scheduler
import workspace_manager as workspace
import workspace_runtime as runtime
import test_priority_scheduler as fixtures


class WorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        # The executor identity is frozen to a source directory that does not
        # exist yet; workspace.create must provision exactly that directory.
        self.f = FrozenExecutorFixture()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root, self.state = self.f.root, self.f.state
        self.workspaces = self.root / 'workspaces'
        self.workspaces.mkdir()

    def git(self, directory, *args):
        proc = subprocess.run(['git', '-C', str(directory), *args],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def reserve(self, kind='artifact', role='executor', **extra):
        queued, request = self.f.enqueue(task_kind=kind, role=role, **extra)
        claim = self.f.claim(task_id='T-A', expected_revision=queued['revision'])
        self.assertEqual(claim['status'], 'RESERVED', claim)
        return claim['assignment_id'], request

    def repository(self, name='repo'):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, 'init')
        self.git(repo, 'config', 'user.name', 'Fixture Author')
        self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
        (repo / 'allowed.txt').write_text('baseline', encoding='utf-8')
        self.git(repo, 'add', 'allowed.txt')
        self.git(repo, 'commit', '-m', 'fixture baseline')
        return repo

    def managed(self, kind='directory', role='executor'):
        extra, repo, base = {}, None, None
        if kind == 'git_worktree':
            repo = self.repository()
            base = self.git(repo, 'rev-parse', 'HEAD')
            extra = {'workspace_mode': 'git_worktree', 'workspace_base_commit': base}
            path = self.f.specs[role]['expected_identity']['launch_cwd']
        else:
            path = str(self.workspaces / 'assignment-a')
        aid, request = self.reserve('code' if kind == 'git_worktree' else 'artifact', role, **extra)
        p = {'assignment_id': aid, 'expected_revision': 1, 'kind': kind,
             'root': str(self.workspaces), 'path': path,
             'allowed_paths': ['allowed.txt']}
        if kind == 'git_worktree':
            p.update(repository_id='fixture-repo', repo_root=str(repo), base_commit=base)
        env = self.f.envelope('workspace.create', p)
        result = runtime.create(self.state, *self.f.actor, env)
        self.assertEqual(result['status'], 'WORKSPACE_READY', result)
        return aid, request, result['workspace'], env

    def finish_review(self, aid, request, verdict='accept'):
        self.assertEqual(self.f.dispatch(aid)['status'], 'SUBMITTED')
        completion = self.f.reconcile(aid)
        self.assertEqual(completion['status'], 'REVIEW_READY')
        self.f.review(completion, request, verdict, same_connection=True)
        return completion

    def test_directory_create_replay_dispatch_review_and_clean_release(self):
        aid, request, ws, env = self.managed()
        self.assertTrue(runtime.create(self.state, *self.f.actor, env)['replayed'])
        completion = self.finish_review(aid, request)
        with self.f.db(readonly=True) as con:
            self.assertIsNotNone(runtime.verify_reviewed_workspace(con, aid))
        self.f.close(aid, completion)
        with self.f.db(readonly=True) as con:
            current = workspace.get_workspace(con, ws['workspace_id'])
        release = self.f.envelope('workspace.release', {'workspace_id': ws['workspace_id'],
            'expected_revision': current['revision'], 'evidence_root': str(self.root / 'archive')})
        result = runtime.release(self.state, *self.f.actor, release)
        self.assertEqual(result['status'], 'WORKSPACE_RELEASED', result)
        self.assertFalse(Path(ws['path']).exists())
        self.assertTrue(runtime.release(self.state, *self.f.actor, release)['replayed'])

    def test_freeze_keeps_accepted_workspace_with_reobserved_quiescence(self):
        aid, request, ws, env = self.managed('git_worktree')
        with self.assertRaises(ValueError):
            runtime.freeze(self.state, *self.f.actor, self.f.envelope('workspace.freeze',
                {'workspace_id': ws['workspace_id'], 'expected_revision': ws['revision'],
                 'evidence_root': str(self.root / 'freeze-proof')}))
        completion = self.finish_review(aid, request)
        self.f.close(aid, completion)
        with self.f.db(readonly=True) as con:
            current = workspace.get_workspace(con, ws['workspace_id'])
        env = self.f.envelope('workspace.freeze', {'workspace_id': ws['workspace_id'],
            'expected_revision': current['revision'], 'evidence_root': str(self.root / 'freeze-proof')})
        result = runtime.freeze(self.state, *self.f.actor, env)
        self.assertEqual(result['status'], 'WORKSPACE_FROZEN')
        self.assertTrue(runtime.freeze(self.state, *self.f.actor, env)['replayed'])
        with self.f.db(readonly=True) as con:
            frozen = workspace.get_workspace(con, ws['workspace_id'])
            self.assertIsNotNone(runtime.verify_reviewed_workspace(con, aid))
        proof = json.loads(frozen['quiescence_json'])
        self.assertTrue(proof['verified'])
        self.assertEqual(proof['evidence_sha256'], service._file_sha(proof['evidence_path']))
        self.assertTrue(Path(ws['path']).exists())

    def test_real_worktree_inventory_blocks_mutation_after_review(self):
        aid, request, ws, env = self.managed('git_worktree')
        self.assertTrue((Path(ws['path']) / '.git').is_file())
        completion = self.finish_review(aid, request)
        (Path(ws['path']) / 'allowed.txt').write_text('changed after review', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_CHANGED'):
            self.f.close(aid, completion)
        self.assertEqual(self.f.get_assignment(aid)['holds_slot'], 1)
        self.assertEqual(self.f.rows('SELECT task_state FROM v2_task')[0]['task_state'], 'review_ready')

    def test_unknown_write_blocks_accept_but_can_be_recorded_for_repair(self):
        aid, request, ws, env = self.managed('git_worktree')
        self.assertEqual(self.f.dispatch(aid)['status'], 'SUBMITTED')
        (Path(ws['path']) / 'outside-scope.txt').write_text('not permitted', encoding='utf-8')
        completion = self.f.reconcile(aid)
        with self.assertRaisesRegex(ValueError, 'scope violations'):
            self.f.review(completion, request, 'accept', same_connection=True)
        self.assertEqual(self.f.rows('SELECT count(*) AS n FROM reviews')[0]['n'], 0)
        self.f.review(completion, request, 'repair', same_connection=True)
        self.f.close(aid, completion, 'repair_required')
        self.assertEqual(self.f.rows('SELECT task_state FROM v2_task')[0]['task_state'], 'repair_required')
        self.assertTrue(Path(ws['path']).exists())

    def test_missing_workspace_recovery_retains_slot_and_blocks_send(self):
        aid, request, ws, env = self.managed()
        # Relocate only this disposable fixture directory; no deletion of live work.
        Path(ws['path']).rename(self.workspaces / 'retained-missing-workspace')
        result = runtime.recover(self.state, *self.f.actor, self.f.envelope('workspace.recover',
            {'workspace_id': ws['workspace_id'], 'expected_revision': ws['revision']}))
        self.assertEqual(result['status'], 'WORKSPACE_WORKSPACE_LOST')
        self.assertEqual(self.f.get_assignment(aid)['state'], 'blocked')
        self.assertEqual(self.f.get_assignment(aid)['holds_slot'], 1)
        with self.assertRaises(ValueError):
            self.f.dispatch(aid)

    def test_changed_workspace_before_send_is_rejected(self):
        aid, request, ws, env = self.managed()
        (Path(ws['path']) / 'allowed.txt').write_text('unexpected pre-run change', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.f.dispatch(aid)
        self.assertFalse((self.root / 'worker-executor' / 'sends.jsonl').exists())
        self.assertEqual(self.f.get_assignment(aid)['holds_slot'], 1)

    def test_code_dispatch_requires_a_bound_execution_contract(self):
        aid, request, ws, env = self.managed('git_worktree')
        original = self.f.rows('SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?',
                               (aid,))[0]['contract_json']
        cases = []
        for mutate, message in (
                (lambda c, m: m.update(execution_cwd=str(self.root)), 'execution_cwd'),
                (lambda c, m: m.update(sandbox='read-only'), 'sandbox'),
                (lambda c, m: (c['request'].update(cwd=str(ws['path'])),
                               m.update(cwd=str(ws['path']))), 'Delivery cwd')):
            altered = json.loads(original)
            body = json.loads(altered['message'])
            mutate(altered, body)
            altered['message'] = json.dumps(body)
            cases.append((json.dumps(altered), message))
        for contract, message in cases:
            self.f.mutate('UPDATE v2_scheduler_intent SET contract_json=? WHERE assignment_id=?',
                          (contract, aid))
            with self.assertRaisesRegex(ValueError, message):
                self.f.dispatch(aid)
        self.f.mutate('UPDATE v2_scheduler_intent SET contract_json=? WHERE assignment_id=?',
                      (original, aid))
        self.assertFalse((self.root / 'worker-executor' / 'sends.jsonl').exists())
        self.assertEqual(self.f.dispatch(aid)['status'], 'SUBMITTED')

    def test_workspace_cli_inspection_does_not_disclose_operation_secrets(self):
        aid, request, ws, env = self.managed()
        proc = subprocess.run([sys.executable, '-B', str(Path(service.__file__)),
            '--state', str(self.state), 'workspace-inspect', '--actor', self.f.actor[0],
            '--version', str(self.f.actor[1]), '--workspace', ws['workspace_id']],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertTrue(value['inspection']['ok'])
        self.assertNotIn('operation_token', proc.stdout)
        self.assertNotIn('ownership_token', proc.stdout)

    def test_inspect_requires_the_controller_binding(self):
        aid, request, ws, env = self.managed()
        worker = self.f.bindings['executor']
        with self.assertRaises(ValueError):
            runtime.inspect(self.state, worker['instance_id'], worker['version'], 'main',
                            ws['workspace_id'])

    def test_directory_mode_task_cannot_bind_git_source_workspace(self):
        repo = self.repository()
        base = self.git(repo, 'rev-parse', 'HEAD')
        aid, request = self.reserve('code')
        env = self.f.envelope('workspace.create', {
            'assignment_id': aid, 'expected_revision': 1, 'kind': 'git_worktree',
            'root': str(self.workspaces), 'path': str(self.workspaces / 'adopted-source'),
            'allowed_paths': ['allowed.txt'], 'repository_id': 'fixture-repo',
            'repo_root': str(repo), 'base_commit': base})
        with self.assertRaisesRegex(ValueError, 'directory-mode Task'):
            runtime.create(self.state, *self.f.actor, env)

    def test_freeze_rejects_changed_completion_sequence(self):
        aid, request, ws, env = self.managed()
        completion = self.finish_review(aid, request)
        self.f.close(aid, completion)
        status = self.root / 'worker-executor' / 'status.json'
        value = json.loads(status.read_text(encoding='utf-8'))
        value['completion_seq'] = value['completion_seq'] + 1
        status.write_text(json.dumps(value), encoding='utf-8')
        with self.f.db(readonly=True) as con:
            current = workspace.get_workspace(con, ws['workspace_id'])
        freeze = self.f.envelope('workspace.freeze', {
            'workspace_id': ws['workspace_id'], 'expected_revision': current['revision'],
            'evidence_root': str(self.root / 'freeze-proof')})
        with self.assertRaisesRegex(ValueError, 'quiescence'):
            runtime.freeze(self.state, *self.f.actor, freeze)

    def _closed_freeze_env(self, mutation):
        aid, request, ws, env = self.managed()
        completion = self.finish_review(aid, request)
        self.f.close(aid, completion)
        status = self.root / 'worker-executor' / 'status.json'
        value = json.loads(status.read_text(encoding='utf-8'))
        mutation(value, request)
        status.write_text(json.dumps(value), encoding='utf-8')
        with self.f.db(readonly=True) as con:
            current = workspace.get_workspace(con, ws['workspace_id'])
        return self.f.envelope('workspace.freeze', {
            'workspace_id': ws['workspace_id'], 'expected_revision': current['revision'],
            'evidence_root': str(self.root / 'freeze-proof')})

    def test_freeze_rejects_changed_worker_identity(self):
        freeze = self._closed_freeze_env(
            lambda value, request: value.update(identity=dict(value['identity'],
                                                              launch_cwd=request['cwd'])))
        with self.assertRaisesRegex(ValueError, 'quiescence'):
            runtime.freeze(self.state, *self.f.actor, freeze)

    def test_freeze_rejects_a_closed_but_active_worker(self):
        freeze = self._closed_freeze_env(lambda value, request: value.update(status='working'))
        with self.assertRaisesRegex(ValueError, 'quiescence'):
            runtime.freeze(self.state, *self.f.actor, freeze)


class FrozenExecutorFixture(fixtures.PrioritySchedulerTests):
    """Bind the executor to a source workspace that is provisioned later.

    The frozen identity is what a real harness must report after the Controller
    creates the worktree. Binding it first proves the queue freezes the exact
    directory instead of adopting whatever tree happens to exist.

    This class only supplies the frozen fixture to FrozenWorkspaceRuntimeTests;
    its inherited directory-mode scheduler tests require an existing launch
    directory and are not collected here.
    """

    __test__ = False

    def enqueue(self, task='T-A', role='executor', priority='P3', request=None, create=True, **extra):
        # Directory-mode artifact work still reports the planned launch
        # directory, so materialize it there; Git Tasks freeze it as new.
        if extra.get('workspace_mode', 'directory') != 'git_worktree':
            Path(self.specs[role]['expected_identity']['launch_cwd']).mkdir(parents=True, exist_ok=True)
            return super().enqueue(task, role, priority, request, create, **extra)
        # A real code Task freezes the execution directory and write sandbox in
        # its message; the send gate rechecks both against the reserved workspace.
        request = request or self.request('ATTEMPT-' + task, role)
        extra['message'] = json.dumps({
            'task_id': request['task_id'], 'nonce': request['nonce'], 'cwd': request['cwd'],
            'execution_cwd': request['worker']['expected_identity']['launch_cwd'],
            'sandbox': 'workspace-write'})
        return super().enqueue(task, role, priority, request, create, **extra)

    def spec(self, role):
        spec = super().spec(role)
        if role in ('executor', 'second'):
            name = 'frozen-source' if role == 'executor' else 'frozen-source-' + role
            planned = str((self.root / 'workspaces' / name).resolve())
            identity = dict(spec['expected_identity'], launch_cwd=planned)
            (self.root / ('worker-' + role) / 'status.json').write_text(
                json.dumps({'identity': identity, 'status': 'idle', 'completion_seq': 10}),
                encoding='utf-8')
            spec['expected_identity'] = identity
        return spec


class FrozenWorkspaceRuntimeTests(unittest.TestCase):
    """Reuse the workspace helpers without inheriting directory-mode tests."""

    git = WorkspaceRuntimeTests.git
    reserve = WorkspaceRuntimeTests.reserve
    repository = WorkspaceRuntimeTests.repository

    def setUp(self):
        self.f = FrozenExecutorFixture()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root, self.state = self.f.root, self.f.state
        self.workspaces = self.root / 'workspaces'
        self.workspaces.mkdir()

    def managed(self, kind='git_worktree', role='executor'):
        """Provision the exact frozen launch directory for a Git source Task."""
        if kind != 'git_worktree':
            raise AssertionError('the frozen executor only provisions Git source workspaces')
        repo = self.repository()
        base = self.git(repo, 'rev-parse', 'HEAD')
        queued, request = self.f.enqueue(task_kind='code', workspace_mode='git_worktree',
                                         workspace_base_commit=base, role=role)
        claim = self.f.claim(task_id='T-A', expected_revision=queued['revision'])
        self.assertEqual(claim['status'], 'RESERVED', claim)
        aid = claim['assignment_id']
        path = self.f.specs[role]['expected_identity']['launch_cwd']
        env = self.f.envelope('workspace.create', {
            'assignment_id': aid, 'expected_revision': 1, 'kind': 'git_worktree',
            'root': str(self.workspaces), 'path': path, 'allowed_paths': ['allowed.txt'],
            'repository_id': 'fixture-repo', 'repo_root': str(repo), 'base_commit': base})
        result = runtime.create(self.state, *self.f.actor, env)
        self.assertEqual(result['status'], 'WORKSPACE_READY', result)
        return aid, request, result['workspace'], env

    def frozen_reserve(self, base):
        queued, request = self.f.enqueue(task_kind='code', workspace_mode='git_worktree',
                                         workspace_base_commit=base)
        claim = self.f.claim(task_id='T-A', expected_revision=queued['revision'])
        self.assertEqual(claim['status'], 'RESERVED', claim)
        return claim['assignment_id'], request

    def test_frozen_git_task_requires_exact_source_workspace(self):
        repo = self.repository()
        base = self.git(repo, 'rev-parse', 'HEAD')
        aid, request = self.frozen_reserve(base)
        frozen = str((self.workspaces / 'frozen-source').resolve())
        common = {'assignment_id': aid, 'expected_revision': 1, 'kind': 'git_worktree',
                  'root': str(self.workspaces), 'allowed_paths': ['allowed.txt'],
                  'repository_id': 'fixture-repo', 'repo_root': str(repo)}
        with self.assertRaisesRegex(ValueError, 'frozen worker launch directory'):
            runtime.create(self.state, *self.f.actor, self.f.envelope('workspace.create',
                dict(common, path=str(self.workspaces / 'other-source'), base_commit=base)))
        with self.assertRaisesRegex(ValueError, 'frozen materialized base commit'):
            runtime.create(self.state, *self.f.actor, self.f.envelope('workspace.create',
                dict(common, path=frozen, base_commit='b' * 40)))
        with self.assertRaisesRegex(ValueError, 'Git source workspace'):
            runtime.create(self.state, *self.f.actor, self.f.envelope('workspace.create',
                dict(common, kind='directory', path=frozen, base_commit=base)))
        created = runtime.create(self.state, *self.f.actor, self.f.envelope('workspace.create',
            dict(common, path=frozen, base_commit=base)))
        self.assertEqual(created['status'], 'WORKSPACE_READY', created)
        self.assertEqual(Path(created['workspace']['path']).resolve(), Path(frozen).resolve())
        self.assertEqual(created['workspace']['base_commit'], base)
        self.assertEqual(self.f.dispatch(aid)['status'], 'SUBMITTED')
        contract = json.loads(self.f.rows(
            'SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?', (aid,))[0]['contract_json'])
        self.assertEqual(contract['workspace_path'], frozen)
        self.assertEqual(contract['request']['worker']['expected_identity']['launch_cwd'], frozen)
        self.assertNotEqual(contract['request']['cwd'], frozen)
        sent = [json.loads(line) for line in (self.root / 'worker-executor' / 'sends.jsonl')
                .read_text(encoding='utf-8').splitlines()]
        self.assertEqual(sent[-1]['cwd'], contract['request']['cwd'])
        completion = self.f.reconcile(aid)
        self.assertEqual(completion['status'], 'REVIEW_READY', completion)
        self.f.review(completion, request, same_connection=True)
        self.f.close(aid, completion)
        self.assertEqual(self.f.rows("SELECT task_state FROM v2_task")[0]['task_state'], 'accepted')

    def test_uppercase_frozen_base_is_normalized_end_to_end(self):
        repo = self.repository()
        base = self.git(repo, 'rev-parse', 'HEAD')
        upper = base.upper()
        aid, request = self.frozen_reserve(upper)
        contract = json.loads(self.f.rows(
            'SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?', (aid,))[0]['contract_json'])
        self.assertEqual(contract['workspace_base_commit'], base)
        frozen = str((self.workspaces / 'frozen-source').resolve())
        created = runtime.create(self.state, *self.f.actor, self.f.envelope('workspace.create', {
            'assignment_id': aid, 'expected_revision': 1, 'kind': 'git_worktree',
            'root': str(self.workspaces), 'path': frozen, 'allowed_paths': ['allowed.txt'],
            'repository_id': 'fixture-repo', 'repo_root': str(repo), 'base_commit': upper}))
        self.assertEqual(created['status'], 'WORKSPACE_READY', created)
        self.assertEqual(created['workspace']['base_commit'], base)

    def test_frozen_git_task_without_source_workspace_never_sends(self):
        repo = self.repository()
        base = self.git(repo, 'rev-parse', 'HEAD')
        aid, request = self.frozen_reserve(base)
        with self.assertRaisesRegex(ValueError, 'reserved managed workspace'):
            self.f.dispatch(aid)
        self.assertFalse((self.root / 'worker-executor' / 'sends.jsonl').exists())
        self.assertEqual(self.f.get_assignment(aid)['holds_slot'], 1)

    def test_worker_reporting_delivery_cwd_is_not_accepted(self):
        aid, request, ws, env = self.managed()
        self.assertEqual(self.f.dispatch(aid)['status'], 'SUBMITTED')
        status = self.root / 'worker-executor' / 'status.json'
        value = json.loads(status.read_text(encoding='utf-8'))
        value['identity'] = dict(value['identity'], launch_cwd=request['cwd'])
        status.write_text(json.dumps(value), encoding='utf-8')
        result = self.f.reconcile(aid)
        self.assertEqual(result['status'], 'IDENTITY_CHANGED', result)
        self.assertEqual(self.f.get_assignment(aid)['holds_slot'], 1)
        self.assertEqual(self.f.rows('SELECT task_state FROM v2_task')[0]['task_state'], 'active')


def load_tests(loader, standard_tests, pattern):
    """Collect the runtime suites only; FrozenExecutorFixture is a fixture provider."""
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(WorkspaceRuntimeTests))
    suite.addTests(loader.loadTestsFromTestCase(FrozenWorkspaceRuntimeTests))
    return suite


if __name__ == '__main__':
    unittest.main()
