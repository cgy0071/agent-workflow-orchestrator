"""Opt-in, per-request B03 plugin adapter for the authenticated MCP service."""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sys
import sysconfig

import awo_service as service
import service_access as access
from service_api import APIError


READ_FIELDS = {
    'kb.packet': {}, 'kb.pull': {'record_id': {'type': 'string'}},
    'kb.check': {'delivery': {'type': 'object', 'additionalProperties': True}},
    'kb.maintenance.report': {}, 'kb.inventory': {'variants': {'type': 'array', 'items': {'type': 'string'}}},
    'kb.evidence.text': {'record_id': {'type': 'string'}}, 'kb.evidence.pdf': {'record_id': {'type': 'string'}},
}
STRING = {'type': 'string', 'minLength': 1}
WRITE_FIELDS = {
    'kb.candidate.list': {}, 'kb.candidate.todo': {},
    'kb.candidate.propose': {'record': {'type': 'object', 'additionalProperties': True}},
    'kb.candidate.approve': {'candidate_id': STRING, 'approved_sha256': STRING},
    'kb.candidate.reject': {'candidate_id': STRING, 'approved_sha256': STRING, 'reason': STRING},
    'kb.maintenance.decide': {'record_id': STRING, 'action': STRING, 'reason': STRING},
}
WRITE_OPERATIONS = frozenset(WRITE_FIELDS)


def _config(state):
    path = Path(state) / 'kb-host.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def deployment(state):
    cfg = _config(state)
    if cfg.get('enabled') is not True:
        return None
    from awo_core.clock import SystemClock
    from awo_core.host import CoreHost
    from awo_core.service import CoreService
    from awo_core.local_host import (AllowListTrust, LocalDirectoryArtifactReader, LocalDistributionMetadataSource,
                                     LocalTrustedModuleLoader, MappingConfigurationProvider, MappingSecretResolver,
                                     ScopeAuthorizationGate)

    class Loader(LocalTrustedModuleLoader):
        instance = None

        def load(self, context):
            self.instance = super().load(context)
            return self.instance

    source = LocalDistributionMetadataSource(sysconfig.get_path('purelib'))
    records = [r for r in source.discover() if r.distribution_name == 'awo-kb']
    if len(records) != 1:
        return None
    manifest = json.loads(records[0].manifest_bytes)
    effects = {row['operation']: row['effect'] for row in manifest['capabilities']}
    if (manifest['plugin_id'] != 'awo.kb' or manifest['version'] not in ('0.2.0', '0.3.0', '0.4.0', '0.4.1')
            or records[0].distribution_version != manifest['version']
            or cfg.get('artifact_digest') != manifest.get('artifact_digest')
            or {k for k, v in effects.items() if v == 'read'} != READ_FIELDS.keys()
            or {k for k, v in effects.items() if v == 'write'} != WRITE_OPERATIONS):
        return None
    class Source:
        source_id = source.source_id

        def discover(self):
            return records

    profile = 'awo-kb-local-v1'
    loader = Loader([profile])
    host = CoreHost(clock=SystemClock(), trust=AllowListTrust({'awo.kb': [cfg['artifact_digest']]}),
                    artifacts=LocalDirectoryArtifactReader(), authorization=ScopeAuthorizationGate(set(), set()),
                    loader=loader, secrets=MappingSecretResolver({}), configuration=MappingConfigurationProvider({}),
                    platform=sys.platform, isolation=frozenset({('trusted_process', profile)}))
    active = CoreService(host, [Source()]).start(load_approved=True).active
    if active != [records[0].deployment_id] or loader.instance is None or not loader.instance.health_check():
        return None
    operations = set(cfg['read_operations'])
    if not operations <= READ_FIELDS.keys():
        raise ValueError('unsupported KB operations')
    writes = set(cfg.get('write_operations', []))
    if not writes <= WRITE_OPERATIONS:
        raise ValueError('unsupported KB write operations')
    return loader.instance, operations | writes, cfg['artifact_digest']


def specs(deployed):
    if deployed is None:
        return []
    result = []
    for op in sorted(deployed[1]):
        write = op in WRITE_OPERATIONS
        fields = {'task_id': STRING, **(WRITE_FIELDS[op] if write else READ_FIELDS[op])}
        if write:
            fields.update(command_id=STRING, expected_epoch={'type': 'integer', 'minimum': 0})
        result.append({'name': op, 'description': 'Query the registered task knowledge base: ' + op,
                       'inputSchema': {'type': 'object', 'properties': fields, 'required': list(fields),
                                       'additionalProperties': False}, 'annotations': {'readOnlyHint': not write}})
    return result


def _binding(con, state, project_root, token, operation, task_id, deployed):
    from awo_kb.service import Reader
    principal = access.authenticate(con, project_root, token)
    if principal.get('knowledge_id'):
        raise PermissionError('entity-scoped credential')
    access.require_scope(con, principal, task_id, principal.get('assignment_id'),
                         own=principal['role'] == 'worker')
    if operation in WRITE_OPERATIONS:
        if principal['role'] == 'observer':
            raise PermissionError('observer credential')
        if operation in ('kb.candidate.approve', 'kb.candidate.reject', 'kb.maintenance.decide') and principal['role'] not in ('reviewer', 'controller'):
            raise PermissionError('review credential required')
    project = principal['project_id']
    actor = principal['principal_id']
    cfg = _config(state)
    if (cfg.get('enabled') is not True or cfg['artifact_digest'] != deployed[2]
            or operation not in cfg.get('write_operations' if operation in WRITE_OPERATIONS else 'read_operations', [])):
        raise PermissionError('deployment changed')
    project_cfg = cfg['projects'][project]
    row = con.execute('SELECT body FROM v2_task WHERE task_id=? AND project_id=?',
                      (task_id, project)).fetchone()
    binding = project_cfg['tasks'][task_id]
    if hashlib.sha256(row['body'].encode()).hexdigest() != binding['body_sha256']:
        raise PermissionError('task binding changed')
    task = json.loads(row['body'])['kb_task']
    if task['project'] != project:
        raise PermissionError('project mismatch')
    vault = Path(project_cfg['vault'])
    if not vault.is_absolute():
        raise PermissionError('invalid host vault')
    from kb_session_source import SOURCE, snapshot
    provider = None
    if project_cfg.get('candidate_session_source') == SOURCE:
        def provider(who, pinned, key):
            if who != actor or pinned != task:
                return None
            return snapshot(con, project, key, str(vault.resolve()))
    reader = Reader(vault, lambda: _config(state)['projects'][project]['policy'], candidate_sessions=provider)
    def inventory_grant(who, pinned, op, key):
        config = _config(state)
        fresh = config['projects'][project]
        bound = fresh['tasks'][task_id]
        if (who != actor or pinned != task or op != 'kb.inventory' or key is not None
                or config.get('enabled') is not True or config['artifact_digest'] != deployed[2]
                or 'kb.inventory' not in config['read_operations']
                or bound['body_sha256'] != binding['body_sha256']
                or Path(fresh['vault']).resolve() != vault.resolve()):
            raise PermissionError('inventory grant changed')
        reader._scope(who, pinned['project'], pinned['profiles'])
        scoped = bound['source_grants'][who]['kb.inventory']
        def material(spec, maintenance):
            base = Path(fresh['source_root'])
            if not base.is_absolute() or Path(spec['path']).is_absolute():
                raise PermissionError('invalid source root or path')
            base = base.resolve(strict=True)
            path = (base / spec['path']).resolve(strict=True)
            path.relative_to(base)
            if maintenance and spec['project'] != project:
                raise PermissionError('maintenance material outside project')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != spec['sha256']:
                raise PermissionError('inventory source changed')
            fields = ('id','project','kind') if maintenance else ('id','title','kind')
            return {**{k: spec[k] for k in fields}, 'bytes':raw}
        if isinstance(scoped, list):
            return [material(s, False) for s in scoped]
        if (not isinstance(scoped, dict) or set(scoped) != {'notes','maintenance'}
                or not all(isinstance(v, list) for v in scoped.values())):
            raise ValueError('invalid inventory grant')
        return {k: [material(s, k == 'maintenance') for s in rows] for k, rows in scoped.items()}
    reader.inventory_grant = inventory_grant
    if hasattr(reader, 'note_refs'):
        from kb_note_grants import note_provider
        reader.note_refs = note_provider(lambda: _config(state), project=project, task_id=task_id,
                                         actor=actor, task=task, body_sha256=binding['body_sha256'],
                                         artifact_digest=deployed[2], vault=project_cfg['vault'], operation=operation)
    return project, actor, task, reader


def source_revision(state, project_root, token, task_id, operation, record_id):
    """Return the immutable KB history and source file digests under the current grant."""
    deployed = deployment(state)
    if not deployed or operation not in deployed[1]:
        raise PermissionError("source verification unavailable")
    with closing(service._connect(service._db_path(state), readonly=True)) as con:
        con.execute("BEGIN")
        project, actor, task, reader = _binding(con, state, project_root, token,
                                                operation, task_id, deployed)
        reader._scope(actor, project, task["profiles"])
        grants = _config(state)["projects"][project]["tasks"][task_id]["source_grants"]
        return task["kb_revision"], grants[actor][operation][record_id]["sha256"]


def material_grants(state, project_root, token, task_id):
    """Discover only this task's exact grants that are visible SRCs in its project."""
    deployed = deployment(state)
    if not deployed or 'kb.pull' not in deployed[1]:
        raise PermissionError('source discovery unavailable')
    with closing(service._connect(service._db_path(state), readonly=True)) as con:
        con.execute('BEGIN')
        project, actor, task, reader = _binding(con, state, project_root, token,
                                                'kb.pull', task_id, deployed)
        reader._scope(actor, project, task['profiles'])
        grants = _config(state)['projects'][project]['tasks'][task_id]['source_grants'].get(actor, {})
        refs = []
        for kind, op in [('note', 'kb.evidence.text'), ('pdf', 'kb.evidence.pdf')]:
            if op not in deployed[1]:
                continue
            for key in sorted(grants.get(op, {})):
                try:
                    row = reader.pull(actor, task, key)
                except KeyError:
                    continue
                if row['type'] == 'SRC' and row['active'] and row['project'] == project:
                    refs.append((kind, key))
        return refs


def call(state, project_root, token, operation, fields, deployed):
    if not deployed or operation not in deployed[1]:
        raise APIError('forbidden_role', 'KB operation unavailable')
    fields = dict(fields)
    task_id = fields.pop('task_id')
    try:
        if operation in WRITE_OPERATIONS:
            import kb_command_recovery as recovery
            env = {'command_id': fields.pop('command_id'), 'expected_epoch': fields.pop('expected_epoch'),
                   'tool': operation, 'payload': {'task_id': task_id, 'fields': fields}}
            def bind(con, envelope):
                values = _binding(con, state, project_root, token, envelope['tool'],
                                  envelope['payload']['task_id'], deployed)
                return recovery.Binding(*values, deployed[0], deployed[2])
            return recovery.execute(state, env, bind)
        with closing(service._connect(service._db_path(state), readonly=True)) as con:
            con.execute('BEGIN')
            project, actor, task, reader = _binding(con, state, project_root, token, operation, task_id, deployed)
            if operation in ('kb.evidence.text', 'kb.evidence.pdf', 'kb.inventory'):
                if operation == 'kb.inventory':
                    return deployed[0].invoke_granted(operation, reader, actor, task,
                                                      reader.inventory_grant, **fields)
                def grant(who, pinned, op, key):
                    fresh = _config(state)['projects'][project]
                    scoped = fresh['tasks'][task_id]['source_grants'][who][op]
                    if who != actor or pinned != task or op != operation:
                        raise PermissionError('source grant mismatch')
                    def read(spec):
                        base = Path(fresh['source_root']).resolve(strict=True)
                        path = (base / spec['path']).resolve(strict=True)
                        path.relative_to(base)
                        raw = path.read_bytes()
                        if hashlib.sha256(raw).hexdigest() != spec['sha256']:
                            raise PermissionError('approved source changed')
                        return raw
                    raw = read(scoped[key])
                    if op == 'kb.evidence.pdf':
                        from pypdf import PdfReader
                        def pages(data):
                            return (p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
                        return raw, pages
                    return raw
                return deployed[0].invoke_granted(operation, reader, actor, task, grant, **fields)
            return deployed[0].invoke(operation, reader, actor, task, **fields)
    except (KeyError, OSError, TypeError, ValueError, service.ServiceError) as exc:
        raise APIError('forbidden_role', 'KB task or source scope denied') from exc
