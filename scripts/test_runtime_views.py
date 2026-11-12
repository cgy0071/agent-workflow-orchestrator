'''Actual SQLite projection and passive HTML regression tests.'''
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import awo_service
import runtime_views as views

NOW = '2026-11-10T12:00:00+00:00'
SECRET = 'fixture-private-token-and-prompt'


def insert(con, table, **values):
    con.execute('INSERT INTO ' + table + '(' + ','.join(values) + ') VALUES (' +
                ','.join('?' for _ in values) + ')', tuple(values.values()))


def database():
    con = sqlite3.connect(':memory:')
    con.execute('BEGIN')
    awo_service.create_schema(con)
    for project in ('project', 'other'):
        insert(con, 'v2_project', project_id=project, root_path=SECRET, created_at=NOW)
    insert(con, 'v2_service_state', id=1, project_id='project', v2_enabled=1,
           frozen=0, controller_epoch=1, updated_at=NOW)
    for name, state in (('ready', 'ready'), ('run', 'active'), ('review', 'review_ready'),
                        ('accepted', 'accepted'), ('landed', 'landed'), ('final', 'finalized'),
                        ('blocked', 'blocked'), ('draft', 'draft'), ('queue', 'queued')):
        insert(con, 'v2_task', task_id='t-' + name, project_id='project', title='Task ' + name,
               body=SECRET, task_state=state, created_at=NOW, updated_at=NOW)
    insert(con, 'v2_task', task_id='foreign-task', project_id='other', title='FOREIGN-TITLE',
           task_state='accepted', created_at=NOW, updated_at=NOW)
    insert(con, 'v2_agent', agent_id='agent', project_id='project', handle='Fixture worker',
           role='worker', harness='fixture', harness_version='1', host='fixture-host', os='fixture',
           lifecycle='active', token_hash=SECRET, source='fixture', created_at=NOW)
    insert(con, 'v2_agent_capability', agent_id='agent', capability_key='artifact',
           level='supported', harness_version='1', os='fixture', host='fixture-host',
           source='fixture', declared_by='fixture')
    for name, state in (('run', 'running'), ('review', 'completed')):
        insert(con, 'v2_assignment', assignment_id='a-' + name, task_id='t-' + name,
               agent_id='agent', generation=1, holds_slot=1, state=state, nonce=SECRET,
               created_at=NOW, updated_at=NOW)
        insert(con, 'v2_run_link', run_id='r-' + name, assignment_id='a-' + name,
               generation=1, created_at=NOW)
        insert(con, 'v2_scheduler_intent', assignment_id='a-' + name, run_id='r-' + name,
               attempt_id='attempt-' + name, contract_json=json.dumps({
                   'request': {'token': SECRET}, 'message': SECRET, 'resources': {'gpu': 1}}),
               controller_id='fixture', controller_version=1, controller_epoch=1,
               send_state=state if state == 'running' else 'review_ready', created_at=NOW)
    insert(con, 'v2_scheduler_queue', task_id='t-queue', project_id='project', attempt_id='attempt-q',
           enqueued_at=NOW, task_revision=1, contract_json=json.dumps({
               'dependencies': ['t-accepted'], 'dag_run': 'dag', 'dag_node': 'node',
               'request': {'secret': SECRET}, 'message': SECRET}))
    insert(con, 'v2_evidence', evidence_id='ev', task_id='t-review', assignment_id='a-review',
           kind='transcript', path=SECRET, sealed_sha256='a' * 64, current_sha256='a' * 64,
           bytes=10, sealed=1, state='sealed', source='fixture', created_at=NOW)
    insert(con, 'v2_budget', budget_id='budget', project_id='project', scope_type='workflow',
           scope_id='project', limits_json='{"cost_usd": 20}', policy_json=json.dumps({'secret': SECRET}),
           state='ok', created_at=NOW, updated_at=NOW)
    insert(con, 'v2_usage_close', run_id='r-review', assignment_id='a-review', generation=1,
           state='actual', usage_json='{"cost_usd":{"state":"actual","value":2}}', closed_at=NOW)
    insert(con, 'v2_budget_reservation', reservation_id='hold', budget_id='budget', assignment_id='a-run',
           generation=1, run_id='r-run', dimension='cost_usd', amount=5, state='held',
           basis='fixture', created_at=NOW, updated_at=NOW)
    for seq, project in ((1, 'project'), (2, 'other'), (3, 'project')):
        insert(con, 'v2_event', event_id='event-' + str(seq), project_id=project,
               entity_type='task', entity_id='t-review', event_type='task.updated', actor=SECRET,
               global_seq=seq, source='fixture', source_seq=seq, at=NOW,
               payload_json=json.dumps({'task_id': 't-review', 'state': 'completed',
                                        'evidence_sha256': 'b' * 64, 'token': SECRET,
                                        'request': {'prompt': SECRET}}))
    return con


class Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class RuntimeViewsTests(unittest.TestCase):
    def setUp(self):
        self.con = database()
        self.addCleanup(self.con.close)

    def snapshot(self, **kwargs):
        return views.build_runtime_views(self.con, 'project', now=NOW, **kwargs)

    def test_exact_thirteen_views_and_five_buckets(self):
        snapshot = self.snapshot()
        self.assertEqual(tuple(snapshot['views']), views.VIEW_NAMES)
        # Pin the public bucket set literally; comparing with the same mutable
        # constant would not detect a changed bucket count or name.
        self.assertEqual(tuple(snapshot['buckets']),
                         ('Coming In', 'Going Out', 'Needs You', 'Blocked', 'Completed'))
        self.assertEqual(len(snapshot['views']), 13)
        self.assertEqual(snapshot['source_max_event_seq'], 3)
        for section in snapshot['views'].values():
            self.assertTrue({'title', 'available', 'rows', 'truncated', 'explanation'} <= section.keys())

    def test_review_ready_never_completed_and_accepted_not_landed(self):
        snapshot = self.snapshot()
        self.assertIn('t-review', [r['task_id'] for r in snapshot['buckets']['Needs You']])
        completed = {r['task_id']: r for r in snapshot['buckets']['Completed']}
        self.assertEqual(set(completed), {'t-accepted', 't-landed', 't-final'})
        self.assertIn('not yet landed', completed['t-accepted']['label'])
        self.assertNotEqual(completed['t-accepted']['integration'], completed['t-landed']['integration'])

    def test_completed_assignment_does_not_complete_task(self):
        self.con.execute("UPDATE v2_task SET task_state='active' WHERE task_id='t-review'")
        snapshot = self.snapshot()
        self.assertNotIn('t-review', [r['task_id'] for r in snapshot['buckets']['Completed']])
        self.assertIn('review pending', snapshot['views']['assignments']['rows'][0]['label'])

    def test_unknown_cost_and_held_reservations_are_separate(self):
        metric = self.snapshot()['views']['budget']['rows'][0]['dimensions']['cost_usd']
        self.assertEqual(metric['known_actual'], 2)
        self.assertEqual(metric['held_reservation'], 5)
        self.assertEqual(metric['unknown_assignments'], 1)
        self.assertIsNone(metric['total_actual'])
        self.assertIsNone(metric['known_estimated'])

    def test_estimates_do_not_become_actual(self):
        self.con.execute("UPDATE v2_usage_close SET usage_json=?",
                         ('{"cost_usd":{"state":"estimated","value":3}}',))
        metric = self.snapshot()['views']['budget']['rows'][0]['dimensions']['cost_usd']
        self.assertEqual(metric['known_estimated'], 3)
        self.assertIsNone(metric['known_actual'])
        self.assertIsNone(metric['total_actual'])

    def test_missing_metric_not_zero_but_explicit_actual_zero_is_kept(self):
        self.con.execute("UPDATE v2_usage_close SET usage_json='{}'")
        metric = self.snapshot()['views']['budget']['rows'][0]['dimensions']['cost_usd']
        self.assertIsNone(metric['known_actual'])
        self.assertEqual(metric['unknown_assignments'], 2)
        self.con.execute("UPDATE v2_usage_close SET usage_json=?",
                         ('{"cost_usd":{"state":"actual","value":0}}',))
        metric = self.snapshot()['views']['budget']['rows'][0]['dimensions']['cost_usd']
        self.assertEqual(metric['known_actual'], 0)

    def test_unavailable_section_does_not_read_as_an_empty_result(self):
        # A missing source must say it is unavailable; an empty list would read as
        # "nothing matched", which is a different and wrong claim.
        self.con.execute('DROP TABLE v2_workspace')
        snapshot = self.snapshot()
        self.assertFalse(snapshot['views']['workspaces']['available'])
        rendered = views.render_html(snapshot, view='workspaces')
        self.assertIn('Source unavailable or incomplete.', rendered)
        self.assertNotIn('No matching records in this snapshot.', rendered)

    def test_unrecognized_state_keeps_recorded_value_for_diagnosis(self):
        # A value without a verified interpretation is labelled for inspection and
        # kept verbatim beside it instead of being dropped or shown as healthy.
        row = views._friendly({'id': 'x', 'state': 'mystery'})
        self.assertEqual(row['label'], 'Status needs inspection')
        self.assertEqual(row['recorded_state'], 'mystery')
        self.assertNotIn('recorded_state', views._friendly({'id': 'x', 'state': 'accepted'}))

    def test_scope_isolated_and_private_context_not_returned(self):
        text = json.dumps(self.snapshot())
        for private in (SECRET, 'FOREIGN-TITLE', 'foreign-task', 'event-2', 'payload_json', 'contract_json'):
            self.assertNotIn(private, text)

    def test_projection_uses_readonly_connection_without_migration_or_commit(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('AWO_VISIBILITY_TEST_ROOT')) as folder:
            path = Path(folder) / 'database.sqlite'
            target = sqlite3.connect(path)
            self.con.commit()
            self.con.backup(target)
            target.close()
            before = path.read_bytes()
            readonly = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            try:
                readonly.execute('BEGIN')
                queries = []
                readonly.set_trace_callback(queries.append)
                snapshot = views.build_runtime_views(readonly, 'project', now=NOW)
                self.assertTrue(readonly.in_transaction)
                self.assertEqual(len(snapshot['views']), 13)
                self.assertTrue(all(q.lstrip().upper().startswith(('SELECT', '-- PRAGMA TABLE_INFO='))
                                    for q in queries))
            finally:
                readonly.close()
            self.assertEqual(path.read_bytes(), before)

    def test_caller_transaction_is_required(self):
        self.con.commit()
        with self.assertRaises(views.ViewError):
            self.snapshot()
        self.assertFalse(self.con.in_transaction)

    def test_missing_tables_are_unavailable_without_creation(self):
        self.con.execute('DROP TABLE v2_review')
        self.con.execute('DROP TABLE v2_workspace')
        before = self.con.total_changes
        snapshot = self.snapshot()
        self.assertFalse(snapshot['views']['review']['available'])
        self.assertFalse(snapshot['views']['workspaces']['available'])
        self.assertEqual(before, self.con.total_changes)
        self.assertIsNone(self.con.execute("SELECT name FROM sqlite_master WHERE name='v2_review'").fetchone())

    def test_partial_schema_fails_closed(self):
        con = sqlite3.connect(':memory:')
        try:
            con.execute('BEGIN')
            con.execute('CREATE TABLE v2_project(project_id,created_at)')
            con.execute("INSERT INTO v2_project VALUES ('p',NULL)")
            con.execute('CREATE TABLE v2_task(task_id)')
            con.execute('CREATE TABLE v2_assignment(assignment_id)')
            snapshot = views.build_runtime_views(con, 'p')
            self.assertFalse(snapshot['views']['task_board']['available'])
        finally:
            con.close()

    def test_truncation_is_visible_and_totals_incomplete(self):
        snapshot = self.snapshot(limit=1)
        self.assertTrue(snapshot['views']['task_board']['truncated'])
        self.assertIn('partial snapshot', views.render_html(snapshot))
        self.assertIsNone(snapshot['views']['budget']['rows'][0]['dimensions']['cost_usd']['total_actual'])

    def test_queue_comparator_uses_aging_and_does_not_mutate_override(self):
        self.con.execute("UPDATE v2_task SET priority='P4' WHERE task_id='t-queue'")
        self.con.execute("UPDATE v2_scheduler_queue SET enqueued_at='2026-11-01T12:00:00+00:00'")
        before = self.con.total_changes
        row = self.snapshot()['views']['queue']['rows'][0]
        self.assertEqual(row['effective_priority'], 'P1')
        self.assertEqual(before, self.con.total_changes)
        self.assertIn('fresh admission', row['suggested_action'])

    def test_dependencies_frozen_and_uncertain_attempts_block(self):
        self.con.execute("UPDATE v2_scheduler_queue SET contract_json=?", (
            json.dumps({'dependencies': ['missing']}),))
        self.con.execute("UPDATE v2_scheduler_intent SET send_state='attempted' WHERE assignment_id='a-run'")
        snapshot = self.snapshot()
        self.assertIn('t-queue', [r['task_id'] for r in snapshot['buckets']['Blocked']])
        self.assertIn('t-run', [r['task_id'] for r in snapshot['buckets']['Blocked']])
        recovery = snapshot['views']['recovery']['rows']
        self.assertTrue(any('Reconcile' in r['suggested_action'] for r in recovery))
        self.con.execute('UPDATE v2_service_state SET frozen=1')
        self.assertIn('t-ready', [r['task_id'] for r in self.snapshot()['buckets']['Blocked']])

    def test_question_requires_attention_until_answered(self):
        insert(self.con, 'v2_message', message_id='m', project_id='project', sender_agent_id='agent',
               from_principal='agent', kind='question', body=SECRET, thread_id='thread',
               task_id='t-ready', priority='P3', created_at=NOW)
        self.assertIn('t-ready', [r['task_id'] for r in self.snapshot()['buckets']['Needs You']])
        insert(self.con, 'v2_message', message_id='answer', project_id='project', sender_agent_id='agent',
               from_principal='agent', kind='answer', body=SECRET, thread_id='thread', reply_to='m',
               task_id='t-ready', priority='P3', created_at=NOW)
        self.assertIn('t-ready', [r['task_id'] for r in self.snapshot()['buckets']['Coming In']])

    def test_inspector_and_integration_do_not_invent_success(self):
        snapshot = self.snapshot()
        row = next(r for r in snapshot['views']['transcript']['rows'] if r['assignment_id'] == 'a-review')
        self.assertEqual(row['transcript_evidence'], ['ev'])
        self.assertEqual(row['verification_evidence'], [])
        self.assertIn('Not performed', row['inspection'])
        self.assertTrue(row['transcript_available'])
        self.assertFalse(row['diff_available'])
        # Missing transcript/diff evidence is explicit, not an empty success.
        section = snapshot['views']['transcript']
        self.assertFalse(section['transcript_metadata_available'])
        self.assertIn('No transcript or diff evidence', section['unavailable_reason'])
        self.assertFalse(snapshot['views']['workspaces']['integration_available'])
        self.assertFalse(snapshot['views']['resources']['capacity_available'])
        self.assertFalse(snapshot['views']['milestones']['complete_graph'])
        rendered = views.render_html(snapshot)
        self.assertIn('Not available in this snapshot: transcript metadata available.', rendered)
        self.assertIn('No transcript or diff evidence is recorded for: a-run', rendered)
        self.assertIn('Not available in this snapshot: integration available.', rendered)
        self.assertIn('Not available in this snapshot: capacity available.', rendered)
        self.assertIn('Not available in this snapshot: complete graph.', rendered)
        self.assertIn('diff unavailable for a-review', rendered)
        self.assertIn('transcript unavailable for a-run', rendered)

    def test_transcript_absence_is_distinguished_from_truncated_or_missing_source(self):
        # This assignment sorts first and therefore remains visible at limit=1,
        # while a second evidence row makes v2_evidence truncated.
        insert(self.con, 'v2_task', task_id='t-aaa', project_id='project', title='Task aaa',
               task_state='active', created_at=NOW, updated_at=NOW)
        insert(self.con, 'v2_assignment', assignment_id='a-aaa', task_id='t-aaa', agent_id='agent',
               generation=1, holds_slot=1, state='running', nonce=SECRET, created_at=NOW, updated_at=NOW)
        insert(self.con, 'v2_evidence', evidence_id='ev2', task_id='t-run', assignment_id='a-run',
               kind='diff', path=SECRET, sealed_sha256='a' * 64, current_sha256='a' * 64,
               bytes=10, sealed=1, state='sealed', source='fixture', created_at=NOW)
        section = self.snapshot(limit=1)['views']['transcript']
        self.assertTrue(section['truncated'])
        self.assertIn('truncated', section['unavailable_reason'])
        self.assertIn('a-aaa', section['unavailable_reason'])
        self.assertNotIn('is recorded for', section['unavailable_reason'])
        self.con.execute('DROP TABLE v2_evidence')
        section = self.snapshot()['views']['transcript']
        self.assertFalse(section['available'])
        self.assertIn('unavailable', section['unavailable_reason'])
        self.assertNotIn('is recorded for', section['unavailable_reason'])

    def test_html_escapes_every_dynamic_context_and_has_no_actions(self):
        malicious = '<script>alert("x")</script><img src=x onerror=evil>&\''
        self.con.execute('UPDATE v2_task SET title=?', (malicious,))
        snapshot = self.snapshot()
        snapshot['project_id'] = malicious
        rendered = views.render_html(snapshot)
        parser = Tags()
        parser.feed(rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn(malicious, rendered)
        for tag, attrs in parser.tags:
            self.assertNotIn(tag, ('script', 'form', 'button', 'input', 'img', 'iframe'))
            self.assertFalse(any(k.startswith('on') for k in attrs))
            if tag == 'a':
                self.assertIn(attrs['href'], ['#awo-' + n for n in views.VIEW_NAMES])

    def test_friendly_no_monitor_and_unknown_states(self):
        self.assertNotIn('no_monitor', json.dumps(views.explain_state('no_monitor')))
        self.assertIn('observation', views.explain_state('no_monitor')['label'])
        self.assertEqual(views.bucket_for_task({'task_state': 'unrecognized'}), 'Blocked')
        self.assertEqual(views.bucket_for_task({'task_state': 'review_ready'}), 'Needs You')
        self.assertIn('inspect', views.explain_state('unrecognized')['label'].lower())

    def test_invalid_json_limit_and_unknown_project(self):
        for limit in (0, True, 10001):
            with self.assertRaises(views.ViewError):
                self.snapshot(limit=limit)
        with self.assertRaises(views.ViewError):
            views.build_runtime_views(self.con, 'absent')
        self.con.execute("UPDATE v2_scheduler_queue SET contract_json='{bad'")
        with self.assertRaises(views.ViewError):
            self.snapshot()
        with self.assertRaises(views.ViewError):
            views.render_html({'views': {}}, view='javascript:bad')

    def test_budget_gate_and_unknown_capability_block_without_writes(self):
        self.con.execute("UPDATE v2_budget SET state='unknown_blocked'")
        self.assertIn('t-queue', [r['task_id'] for r in self.snapshot()['buckets']['Blocked']])
        self.con.execute("UPDATE v2_budget SET state='approval_required'")
        self.assertIn('t-queue', [r['task_id'] for r in self.snapshot()['buckets']['Needs You']])
        self.con.execute("UPDATE v2_budget SET state='ok'")
        self.con.execute('UPDATE v2_scheduler_queue SET contract_json=?',
                         (json.dumps({'agent_id': 'agent', 'required_capabilities': ['unavailable']}),))
        self.assertIn('t-queue', [r['task_id'] for r in self.snapshot()['buckets']['Blocked']])

    def test_duplicate_json_keys_and_structured_identifiers_rejected(self):
        for raw in ('{"dependencies":[],"dependencies":["hidden"]}', '{"dag_run":{"secret":"hidden"}}'):
            self.con.execute('UPDATE v2_scheduler_queue SET contract_json=?', (raw,))
            with self.subTest(raw=raw), self.assertRaises(views.ViewError):
                self.snapshot()

    def test_sqlite_row_factory_supported(self):
        self.con.row_factory = sqlite3.Row
        self.assertEqual(len(self.snapshot()['views']), 13)

    def test_persisted_dag_projects_only_existing_project_nodes(self):
        graph = {'nodes': [
            {'id': 'first', 'request': {'task_id': 't-ready', 'prompt': SECRET}},
            {'id': 'second', 'request': {'task_id': 't-run'}, 'needs': ['first', 'private-node']},
            {'id': 'private-node', 'request': {'task_id': 'foreign-task'}, 'message': SECRET}]}
        insert(self.con, 'dag_runs', id='stored-dag', data=json.dumps({'graph': graph, 'actor': SECRET}))
        section = self.snapshot()['views']['milestones']
        rows = [r for r in section['rows'] if r['milestone_run'] == 'stored-dag']
        self.assertEqual({r['task_id'] for r in rows}, {'t-ready', 't-run'})
        second = next(r for r in rows if r['node'] == 'second')
        self.assertEqual(second['dependencies'], ['t-ready'])
        self.assertEqual(second['unresolved_dependency_count'], 1)
        for hidden in (SECRET, 'private-node', 'foreign-task'):
            self.assertNotIn(hidden, json.dumps(section))
        self.assertFalse(section['milestone_metadata_available'])
        self.assertIn('No dedicated milestone', section['unavailable_reason'])

    def test_milestone_missing_and_malformed_sources_have_concrete_reasons(self):
        insert(self.con, 'dag_runs', id='bad', data='{bad')
        section = self.snapshot()['views']['milestones']
        self.assertIn('malformed', section['explanation'])
        self.con.execute('DROP TABLE dag_runs')
        self.con.execute('DROP TABLE v2_scheduler_queue')
        section = self.snapshot()['views']['milestones']
        self.assertFalse(section['available'])
        self.assertIn('unavailable', section['explanation'])
        self.assertEqual(section['rows'], [])

    def test_workspace_snapshot_hashes_without_paths(self):
        insert(self.con, 'v2_workspace', workspace_id='workspace', task_id='t-run', assignment_id='a-run',
               host='fixture', path=SECRET, kind='directory', spec_json='{}', spec_sha256='a' * 64,
               status='frozen', created_at=NOW, snapshot_json=json.dumps({
                   'manifest_sha256': 'b' * 64, 'inventory_sha256': 'c' * 64,
                   'scope_ok': True, 'dirty': False, 'manifest_path': SECRET}))
        row = self.snapshot()['views']['workspaces']['rows'][0]
        self.assertEqual(row['snapshot']['manifest_sha256'], 'b' * 64)
        self.assertNotIn(SECRET, json.dumps(row))

    def test_overview_inherits_attention_source_truncation(self):
        for index in range(11):
            insert(self.con, 'v2_message', message_id='question-' + str(index), project_id='project',
                   sender_agent_id='agent', from_principal='agent', kind='question', body=SECRET,
                   thread_id='thread-' + str(index), task_id='t-ready', priority='P3', created_at=NOW)
        snapshot = self.snapshot(limit=10)
        self.assertFalse(snapshot['sources']['v2_task']['truncated'])
        self.assertTrue(snapshot['sources']['v2_message']['truncated'])
        self.assertTrue(snapshot['views']['project']['truncated'])


if __name__ == '__main__':
    unittest.main()
