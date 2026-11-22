import hashlib
from importlib import metadata
from importlib.util import find_spec
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if find_spec("awo_kb") is None:
    try:
        metadata.distribution("awo-kb")
    except metadata.PackageNotFoundError:
        raise unittest.SkipTest("Optional awo-kb package is not installed") from None

import awo_service as service
import kb_host_bridge as bridge
import mcp_gateway as gateway
import service_access as access
import test_service_api as fixtures
from awo_kb import store
from awo_kb import editor


FIXTURE_PINS = {
    "0.4.1": "sha256:3edb4eba44c4b2a82c56e888812192d442094892c25667d0dcad42caa0e0e9a5",
    "0.4.0": "sha256:818b448bcb6c2884eb8c62090a6337d608226088ecf3f1ca654b4b4bbf367f34",
    "0.2.0": "sha256:de1591b7142e47aa8b18b204f4f27b44afd7c8e6bbe302fb2f25ea33138ca786",
    "0.3.0": "sha256:434567906f19d534eaff71163076de0b798d65e9d7396157a2423df9d6926539",
}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.ServiceAPITests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.api = gateway.ServiceBackend(f.state, f.root, extensions_enabled=True)
        principal = access.principal(f.state, str(f.root), f.worker)
        project = principal['project_id']
        self.actor = principal['principal_id']
        vault = f.root / 'vault'
        raw = b'Named evidence'
        source = f.root / 'source.txt'
        source.write_bytes(raw)
        spec = {'path': source.name, 'sha256': hashlib.sha256(raw).hexdigest()}
        pin = store._append(vault, {'id': 'S1', 'project': project, 'profiles': ['core'], 'type': 'SRC',
                            'author': 'Alice', 'locator': 'line 1', 'version': 'v1', 'quote': raw.decode(),
                            'byte_start': 0, 'byte_end': len(raw), 'file_sha256': spec['sha256']}, store.GENESIS)
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        pdf = f.root / 'sample.pdf'
        writer = PdfWriter()
        page = writer.add_blank_page(300, 300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 30 100 Td (PDF excerpt) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(pdf)
        pdf_spec = {'path': pdf.name, 'sha256': hashlib.sha256(pdf.read_bytes()).hexdigest()}
        pin = store._append(vault, {'id': 'S2', 'project': project, 'profiles': ['core'], 'type': 'SRC',
                            'author': 'Alice', 'locator': 'p.1', 'version': 'v1', 'quote': 'PDF excerpt',
                            'physical_page': 1, 'file_sha256': pdf_spec['sha256']}, pin)
        task = {'kb_revision': pin, 'project': project, 'profiles': []}
        body = json.dumps({'kb_task': task})
        service.execute_command(f.state, *f.f.actor, f.f.envelope('task.create',
                                {'task_id': 'T-A', 'title': 'fixture', 'body': body}))
        self.token = f.issue('worker', 'executor', task_id='T-A')
        operations = list(bridge.READ_FIELDS)
        self.policy = {'subjects': {}, 'actors': {self.actor: {'read': [project]}},
                       'projects': {project: {'profiles': [], 'sources': []}}}
        grants = {'kb.evidence.text': {'S1': spec}, 'kb.evidence.pdf': {'S2': pdf_spec},
                  'kb.inventory': [dict(spec, kind='concept', id='N1', title='Note')]}
        self.cfg = {'enabled': True, 'artifact_digest': FIXTURE_PINS[metadata.version('awo-kb')],
                    'read_operations': operations, 'projects': {project: {'policy': self.policy, 'vault': str(vault),
                    'source_root': str(f.root), 'tasks': {'T-A': {'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                    'source_grants': {self.actor: grants}}}}}}
        self.save()

    def save(self):
        (self.f.state / 'kb-host.json').write_text(json.dumps(self.cfg), encoding='utf-8')

    def call(self, op, **args):
        return self.api.call(self.token, op, dict(task_id='T-A', **args))

    def test_installed_wheel_and_current_grants(self):
        self.assertIn('kb.packet', [s['name'] for s in self.api.specs(self.token)])
        self.assertIn('packet_sha256', self.call('kb.packet'))
        self.assertEqual(self.call('kb.pull', record_id='S1')['id'], 'S1')
        self.assertTrue(self.call('kb.evidence.text', record_id='S1'))
        self.assertTrue(self.call('kb.evidence.pdf', record_id='S2'))
        self.assertFalse(self.call('kb.check', delivery={'refs': ['S1'], 'terms': [], 'judgments': [], 'coinages': []})['blocked'])
        self.assertIsInstance(self.call('kb.inventory', variants=['evidence']), dict)
        self.assertIsInstance(self.call('kb.maintenance.report'), dict)
        with self.assertRaises(gateway.GatewayError):
            self.call('kb.pull', record_id='S1', actor='other')
        self.policy['actors'][self.actor]['read'] = []
        self.save()
        with self.assertRaises(gateway.GatewayError):
            self.call('kb.packet')

    def test_disabled_wrong_digest_and_cross_task(self):
        with self.assertRaises(gateway.GatewayError):
            self.api.call(self.token, 'kb.packet', {'task_id': 'elsewhere'})
        previous = {'0.4.1': '0.4.0', '0.4.0': '0.3.0', '0.3.0': '0.2.0'}.get(metadata.version('awo-kb'))
        if previous:
            self.cfg['artifact_digest'] = FIXTURE_PINS[previous]
            self.save()
            self.assertFalse(any(s['name'].startswith('kb.') for s in self.api.specs(self.token)))
        self.cfg['artifact_digest'] = 'sha256:' + '0' * 64
        self.save()
        self.assertFalse(any(s['name'].startswith('kb.') for s in self.api.specs(self.token)))
        self.cfg['enabled'] = False
        self.save()
        with self.assertRaises(gateway.GatewayError):
            self.call('kb.packet')

    def test_cached_deployment_rechecks_access_on_each_read(self):
        project, = self.cfg['projects']
        deployed = bridge.deployment(self.f.state)

        def read():
            return bridge.call(self.f.state, self.f.root, self.token, 'kb.pull',
                               {'task_id': 'T-A', 'record_id': 'S1'}, deployed)

        def assert_denied():
            with self.assertRaises(bridge.APIError) as denied:
                read()
            self.assertEqual(denied.exception.code, 'forbidden_role')

        self.assertEqual(read()['quote'], 'Named evidence')
        self.policy['actors'][self.actor]['read'] = []
        self.save()
        assert_denied()
        self.policy['actors'][self.actor]['read'] = [project]
        self.save()
        self.assertEqual(read()['quote'], 'Named evidence')
        self.cfg['enabled'] = False
        self.save()
        assert_denied()

    def enable_writes(self):
        self.project = next(iter(self.cfg['projects']))
        self.human = access.principal(self.f.state, str(self.f.root), self.f.controller)['principal_id']
        self.policy['actors'][self.actor].update(kind='agent', write=[self.project])
        self.policy['actors'][self.human] = {'kind': 'human', 'read': [self.project], 'write': [self.project]}
        self.cfg['write_operations'] = list(bridge.WRITE_OPERATIONS)
        self.save()

    def write(self, op, command, *, token=None, **fields):
        return self.api.call(token or self.token, op, dict(task_id='T-A', command_id=command,
                             expected_epoch=self.f.f.epoch, **fields))

    def test_concurrent_write_and_queue_replay_with_current_authority(self):
        self.enable_writes()
        self.assertEqual({s['name'] for s in self.api.specs(self.token) if s['name'].startswith('kb.')},
                         bridge.READ_FIELDS.keys() | bridge.WRITE_OPERATIONS)
        record = {'id': 'T1', 'type': 'TERM', 'project': self.project, 'profiles': ['core'],
                  'canonical': 'sample', 'definition': 'example'}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.write('kb.candidate.propose', 'propose-concurrent', record=record),
                                    range(2)))
        self.assertEqual(sorted(r['replayed'] for r in results), [False, True])
        candidate = results[0]['result']
        decision = dict(candidate_id=candidate, approved_sha256=editor.digest(record))
        self.assertEqual(self.write('kb.candidate.list', 'list-candidates')['result'][0]['status'], 'pending')
        self.assertTrue(self.write('kb.candidate.todo', 'todo-candidates')['result']['todo'])
        with self.assertRaises(gateway.GatewayError):
            self.write('kb.candidate.reject', 'reject-as-worker', reason='close', **decision)
        self.write('kb.candidate.reject', 'reject-as-human', token=self.f.controller, reason='close', **decision)
        self.assertEqual(self.write('kb.candidate.todo', 'todo-candidates'),
                         {'result': {'project': self.project, 'session_limit': None, 'todo': [], 'expired': []},
                          'replayed': True})
        second = dict(record, id='T2')
        new_id = self.write('kb.candidate.propose', 'propose-another', record=second)['result']
        args = dict(candidate_id=new_id, approved_sha256=editor.digest(second))
        with ThreadPoolExecutor(2) as pool:
            approved = list(pool.map(lambda _: self.write('kb.candidate.approve', 'approve-concurrent',
                                                          token=self.f.controller, **args), range(2)))
        self.assertEqual(approved[0]['result'], approved[1]['result'])
        self.assertEqual(sorted(r['replayed'] for r in approved), [False, True])
        self.assertEqual(len(store.history(self.f.root / 'vault')), 3)
        self.policy['actors'][self.human]['write'] = []
        self.save()
        with self.assertRaises(gateway.GatewayError):
            self.write('kb.candidate.approve', 'approve-concurrent', token=self.f.controller, **args)
        rows = self.f.f.rows("SELECT response_json FROM v2_command_log WHERE command_id='todo-candidates'")
        self.assertEqual(len(rows), 1)
        self.assertNotIn('result', json.loads(rows[0]['response_json']))

    def test_maintenance_replay_and_command_collision(self):
        self.enable_writes()
        op = 'kb.maintenance.decide'
        args = dict(record_id='S1', action='retain', reason='checked excerpt')
        first = self.write(op, 'maintenance-command', token=self.f.controller, **args)
        retry = self.write(op, 'maintenance-command', token=self.f.controller, **args)
        self.assertEqual((first['result'], retry['replayed']), (retry['result'], True))
        with self.assertRaises(gateway.GatewayError):
            self.write(op, 'maintenance-command', token=self.f.controller, **dict(args, action='retire'))
        self.assertEqual(len(store.history(self.f.root / 'vault')), 3)

    def test_session_threshold_uses_real_stdio_completion_and_requires_complete_range(self):
        self.enable_writes()
        config = self.cfg['projects'][self.project]
        config['candidate_session_source'] = 'mcp-stdio-v1'
        self.policy['projects'][self.project]['candidate_session_limit'] = 2
        self.save()
        record = {'id': 'T4', 'type': 'TERM', 'project': self.project, 'profiles': ['core'],
                  'canonical': 'age', 'definition': 'session count'}
        candidate = self.write('kb.candidate.propose', 'propose-for-age', record=record)['result']
        token_file = self.f.root / 'session-test-token'
        token_file.write_text(self.token, encoding='ascii')
        initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'offline', 'version': '1'}}}
        initialized = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        def session(messages):
            wire = ''.join(json.dumps(m) + '\n' for m in messages).encode()
            proc = subprocess.run([sys.executable, '-B', str(Path(gateway.__file__)), '--extensions-enabled',
                                   '--project-root', str(self.f.root), '--state', str(self.f.state),
                                   '--token-file', str(token_file)], input=wire, capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        def todo():
            return self.write('kb.candidate.todo', 'read-aged-candidates')['result']
        session([initialize])
        self.assertEqual(todo()['todo'][0]['completed_count'], 0)
        session([initialize, initialized])
        self.assertEqual(todo()['todo'][0]['completed_count'], 1)
        session([initialize, initialized])
        expired = todo()['expired'][0]
        self.assertEqual((expired['candidate_id'], expired['completed_count']), (candidate, 2))
        from contextlib import closing
        with closing(service._connect(service._db_path(self.f.state))) as con, con:
            con.execute("DELETE FROM v2_event WHERE global_seq=(SELECT MIN(global_seq) FROM v2_event "
                        "WHERE source='mcp-stdio-v1')")
        retained = todo()['todo'][0]
        self.assertEqual((retained['candidate']['id'], retained['status']), (candidate, 'unmeasured'))

    def test_interrupted_mcp_approval_and_queue_recovery_share_command_log(self):
        self.enable_writes()
        record = {'id': 'T3', 'type': 'TERM', 'project': self.project, 'profiles': ['core'],
                  'canonical': 'sample', 'definition': 'published before interruption'}
        candidate = self.write('kb.candidate.propose', 'interrupted-proposal', record=record)['result']
        fields = dict(candidate_id=candidate, approved_sha256=editor.digest(record))
        op = 'kb.candidate.approve'
        command = 'interrupted-approval'
        request = dict(operation='call', state=str(self.f.state), project_root=str(self.f.root),
                       token=self.f.controller, messages_enabled=False, extensions_enabled=True, tool=op,
                       arguments=dict(task_id='T-A', command_id=command, expected_epoch=self.f.f.epoch, **fields))
        # Simulate process loss immediately after the real Markdown revision is published.
        program = '''
import os, mcp_gateway
from awo_kb import store
append = store._append
def cut(*args, **kwargs):
    append(*args, **kwargs)
    os._exit(86)
store._append = cut
mcp_gateway.service_worker()
'''
        child = subprocess.run([sys.executable, '-B', '-c', program], input=json.dumps(request).encode(),
                               cwd=Path(__file__).parent, capture_output=True, timeout=20)
        self.assertEqual(child.returncode, 86)
        sql = "SELECT response_json FROM v2_command_log WHERE command_id='interrupted-approval'"
        self.assertEqual(json.loads(self.f.f.rows(sql)[0]['response_json'])['kb_write_state'], 'intent')
        entries = self.write('kb.candidate.list', 'recover-the-queue')['result']
        self.assertEqual(entries[0]['status'], 'approved')
        confirmed = self.write(op, command, token=self.f.controller, **fields)
        self.assertEqual(confirmed['result'], entries[0]['pin'])
        self.assertEqual(confirmed['result'], store.head(self.f.root / 'vault'))
        self.assertEqual(len(store.history(self.f.root / 'vault')), 3)
        self.assertEqual(json.loads(self.f.f.rows(sql)[0]['response_json'])['kb_write_state'], 'done')
