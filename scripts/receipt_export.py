'''Opt-in, local-only sanitized audit receipts; never a runtime DB restore.'''
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading

from runtime_views import TABLES, ViewError, _Reader, _json, _number

FORMAT_VERSION = 1
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_OBJECTS = 100000
ZERO_HASH = '0' * 64
HASH = re.compile(r'[0-9a-fA-F]{64}\Z')
PSEUDONYM = re.compile(r'id:[0-9a-f]{64}\Z')
POLICY = 'allowlist-v1'
LIMITATIONS = [
    'Sanitized audit projection only; not a runtime backup or executable event replay.',
    'Original IDs are one-way pseudonyms; low-entropy IDs can still be guessed.',
    'Credentials, runtime configuration, free text, commands, paths and artifact bytes are omitted.',
    'Stated source hashes are claims; omitted source bytes cannot be independently verified or reconstructed.',
    'The event chain covers exported project events, not other projects or omitted history.',
    'Without an external manifest or commit pin, verification establishes consistency, not provenance.',
]
ID_FIELDS = frozenset(('project_id task_id agent_id assignment_id workspace_id evidence_id '
                       'accepted_evidence_id review_id event_id budget_id scope_id reservation_id run_id '
                       'message_id delivery_id thread_id reply_to entity_id capability_key dimension').split())
INT_FIELDS = frozenset(('generation slot_id holds_slot revision version bytes sealed global_seq '
                        'fanout_slots v2_enabled frozen controller_epoch').split())
NUMBER_FIELDS = frozenset(('amount', 'settled_amount'))
TIME_FIELDS = frozenset(('created_at updated_at last_seen_at checked_at ready_at released_at reviewed_at '
                         'enqueued_at due_at attempted_at submitted_at expires_at closed_at at').split())
HASH_FIELDS = frozenset(('sha256 sealed_sha256 current_sha256 note_sha256 evidence_sha256 '
                         'request_sha256 content_sha256 manifest_sha256 spec_sha256 diff_sha256 '
                         'source_sha256 result_sha256 snapshot_sha256 inventory_sha256 revision_sha256').split())
ENUMS = {
    'task_state': set('draft ready queued active review_ready accepted landed finalized rejected '
                      'repair_required cancelled blocked migration_uncertain'.split()),
    'state': set('reserved running completed failed blocked cancelled preempted rejected uncertain '
                'historical_registered sealed unknown missing changed held settled released actual partial '
                'ok warning pause_requested approval_required cancel_requested paused overrun_pending exceeded '
                'unknown_blocked closed sending pushed notified read acknowledged answered expired'.split()),
    'status': set('requested provisioning ready active frozen released orphaned quarantined workspace_lost failed'.split()),
    'review_status': set('none awaiting_review reviewing accepted repair rejected'.split()),
    'send_state': set('ready attempted submitted uncertain running review_ready closed'.split()),
    'lifecycle': {'pending', 'active', 'revoked'},
    'priority': {'P0', 'P1', 'P2', 'P3', 'P4'},
    'previous_priority': {'P0', 'P1', 'P2', 'P3', 'P4'},
    'execution_mode': {'single', 'fanout', 'ensemble'},
    'scope_type': {'run', 'task', 'workflow'},
    'role': {'worker', 'observer', 'reviewer', 'controller'},
    'level': {'supported', 'unsupported', 'unknown'},
    'verdict': {'accept', 'accepted', 'repair', 'reject', 'rejected', 'revise'},
    'kind': set('worktree git_worktree directory noncode artifact transcript diff test-report verification result '
                'handoff question answer notice consult state_request steer'.split()),
    'entity_type': set('task agent assignment workspace evidence review budget run message message_delivery '
                       'service enrollment_grant controller queue resource knowledge'.split()),
}
SENSITIVE = re.compile(r'token|secret|password|credential|authorization|nonce|config|environment|command|'
                       r'prompt|message|transcript|body|path|actor|binding', re.I)


class ReceiptError(ValueError):
    '''The receipt cannot be exported, verified or rebuilt safely.'''


def _canonical(value):
    try:
        return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                           allow_nan=False) + '\n').encode('ascii')
    except (ValueError, TypeError, RecursionError) as exc:
        raise ReceiptError('Receipt content is not canonical JSON data.') from exc


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ReceiptError('Invalid recorded identifier.')
    return 'id:' + _sha(value.encode('utf-8'))


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r'[0-9TtZz:+. -]+', value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.isoformat() if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _typed(key, value):
    if key in ID_FIELDS:
        return _id(value)
    if key in INT_FIELDS:
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None
    if key in NUMBER_FIELDS:
        return _number(value)
    if key in TIME_FIELDS:
        return _timestamp(value)
    if key in HASH_FIELDS:
        return value if isinstance(value, str) and HASH.fullmatch(value) else None
    if key in ENUMS:
        return value if isinstance(value, str) and value in ENUMS[key] else None
    return None


def _record(table, row):
    result = {}
    for key, value in row.items():
        if key in ID_FIELDS | INT_FIELDS | NUMBER_FIELDS | TIME_FIELDS | HASH_FIELDS | ENUMS.keys():
            result[key] = _typed(key, value)
    if table == 'v2_workspace':
        result['snapshot'] = None
        if row.get('snapshot_json'):
            snapshot = _json(row['snapshot_json'])
            if not isinstance(snapshot, dict):
                raise ReceiptError('Workspace snapshot must be a recorded object.')
            result['snapshot'] = {k: _typed(k, snapshot.get(k)) for k in
                                  ('manifest_sha256', 'inventory_sha256')}
            result['snapshot'].update({k: snapshot.get(k) if type(snapshot.get(k)) is bool else None
                                       for k in ('scope_ok', 'dirty')})
    if table == 'v2_usage_close':
        usage = _json(row['usage_json'])
        if not isinstance(usage, dict):
            raise ReceiptError('Usage must be a recorded object.')
        result['usage'] = [
            {'dimension': _id(dimension),
             'state': item.get('state') if isinstance(item, dict)
                      and item.get('state') in ('actual', 'estimated', 'unknown') else 'unknown',
             'value': _number(item.get('value')) if isinstance(item, dict)
                      and item.get('state') in ('actual', 'estimated') else None}
            for dimension, item in sorted(usage.items())]
    return {'table': table, 'data': result}


def _payload(raw):
    value = _json(raw)
    if not isinstance(value, dict):
        raise ReceiptError('Event payload must be a JSON object.')
    safe, claims = {}, []
    # Only these top-level typed fields are semantic audit data.
    for key, item in value.items():
        if key in ID_FIELDS | INT_FIELDS | NUMBER_FIELDS | TIME_FIELDS | HASH_FIELDS | ENUMS.keys():
            safe[key] = _typed(key, item)
    visited = 0

    def collect(item, route=(), depth=0):
        nonlocal visited
        visited += 1
        if visited > 20000 or depth > 32:
            raise ReceiptError('Event payload nesting or item count exceeds the limit.')
        if isinstance(item, dict):
            for key, child in sorted(item.items()):
                if key in HASH_FIELDS and isinstance(child, str) and HASH.fullmatch(child):
                    claims.append({'field': key, 'location_id': _id(json.dumps(route + (key,))),
                                   'value': child})
                elif not SENSITIVE.search(key):
                    collect(child, route + (key,), depth + 1)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                collect(child, route + (index,), depth + 1)
    collect(value)
    return {'fields': safe, 'stated_hashes': claims,
            'omitted_top_level_fields': len(set(value) - set(safe)),
            'source_payload_utf8_sha256': _sha(raw.encode('utf-8')),
            'exported_fields_sha256': _sha(_canonical(safe))}


def build_receipt(con, project_id, *, max_rows=10000):
    '''Read an authorized project snapshot; fail rather than truncate an archive.'''
    try:
        reader = _Reader(con, project_id, max_rows)
        data = {name: reader.read(name) for name in TABLES}
        if not data['v2_project'] or not reader.available['v2_event']:
            raise ReceiptError('Project and event schema are required.')
        if any(reader.truncated.values()):
            raise ReceiptError('Receipt row bound exceeded; no partial export was built.')
        records = [_record(table, row) for table, rows in data.items() if table != 'v2_event'
                   for row in rows]
        events, previous, last_seq = [], ZERO_HASH, 0
        for row in sorted(data['v2_event'], key=lambda item: item['global_seq']):
            seq = row['global_seq']
            if type(seq) is not int or seq <= last_seq:
                raise ReceiptError('Project event sequence is invalid.')
            payload = _payload(row['payload_json'])
            event = {k: _typed(k, row[k]) for k in
                     ('event_id', 'project_id', 'entity_type', 'entity_id', 'global_seq', 'at')}
            event.update(event_type_id=_id(row['event_type']), payload=payload,
                         exported_payload_sha256=_sha(_canonical(payload)), previous_sha256=previous)
            previous, last_seq = _sha(_canonical(event)), seq
            events.append(event)
        receipt = {'format_version': FORMAT_VERSION, 'policy': POLICY, 'project_id': _id(project_id),
                   'source_max_event_seq': last_seq if events else None,
                   'sources': {t: {'available': reader.available[t], 'count': len(data[t])} for t in TABLES},
                   'records': records, 'events': events, 'chain_tip': previous,
                   'limitations': list(LIMITATIONS)}
        _validate_receipt(receipt)
        return receipt
    except ViewError as exc:
        raise ReceiptError(str(exc)) from exc


def _validate_receipt(receipt):
    '''Validate detached input again: callers cannot smuggle extra fields to Git.'''
    expected = {'format_version', 'policy', 'project_id', 'source_max_event_seq', 'sources',
                'records', 'events', 'chain_tip', 'limitations'}
    if not isinstance(receipt, dict) or set(receipt) != expected:
        raise ReceiptError('Unexpected receipt envelope.')
    if (receipt['format_version'] != FORMAT_VERSION or receipt['policy'] != POLICY
            or receipt['limitations'] != LIMITATIONS
            or not isinstance(receipt['project_id'], str) or not PSEUDONYM.fullmatch(receipt['project_id'])):
        raise ReceiptError('Receipt policy or identity is invalid.')
    if not isinstance(receipt['sources'], dict) or set(receipt['sources']) != set(TABLES):
        raise ReceiptError('Receipt source coverage is invalid.')
    for source in receipt['sources'].values():
        if (not isinstance(source, dict) or set(source) != {'available', 'count'}
                or type(source['available']) is not bool or type(source['count']) is not int
                or not 0 <= source['count'] <= 10000
                or (not source['available'] and source['count'])):
            raise ReceiptError('Receipt source coverage is invalid.')
    if not isinstance(receipt['records'], list) or not isinstance(receipt['events'], list):
        raise ReceiptError('Receipt collections are invalid.')
    if len(receipt['records']) + len(receipt['events']) > MAX_OBJECTS:
        raise ReceiptError('Too many receipt objects.')

    def valid_fields(fields, allowed):
        if not isinstance(fields, dict) or not set(fields) <= set(allowed):
            raise ReceiptError('Unapproved receipt fields.')
        for key, value in fields.items():
            if value is None:
                continue
            if key in ID_FIELDS:
                valid = isinstance(value, str) and PSEUDONYM.fullmatch(value)
            else:
                valid = _typed(key, value) == value
            if not valid:
                raise ReceiptError('Unapproved receipt value.')

    counts = {table: 0 for table in TABLES}
    for record in receipt['records']:
        if not isinstance(record, dict) or set(record) != {'table', 'data'}:
            raise ReceiptError('Invalid record object.')
        table = record['table']
        if table not in TABLES or table == 'v2_event' or not isinstance(record['data'], dict):
            raise ReceiptError('Unapproved receipt table.')
        fields = dict(record['data'])
        if table == 'v2_workspace':
            if 'snapshot' not in fields:
                raise ReceiptError('Workspace snapshot projection is missing.')
            snapshot = fields.pop('snapshot')
            if snapshot is not None:
                if not isinstance(snapshot, dict) or set(snapshot) != {
                        'manifest_sha256', 'inventory_sha256', 'scope_ok', 'dirty'}:
                    raise ReceiptError('Workspace snapshot projection is invalid.')
                valid_fields({k: snapshot[k] for k in ('manifest_sha256', 'inventory_sha256')}, HASH_FIELDS)
                if any(snapshot[k] is not None and type(snapshot[k]) is not bool for k in ('scope_ok', 'dirty')):
                    raise ReceiptError('Workspace snapshot flags are invalid.')
        usage = fields.pop('usage', None) if table == 'v2_usage_close' else None
        allowed = set(TABLES[table][0].split()) & (ID_FIELDS | INT_FIELDS | NUMBER_FIELDS |
                                                TIME_FIELDS | HASH_FIELDS | ENUMS.keys())
        valid_fields(fields, allowed)
        if set(fields) != allowed:
            raise ReceiptError('Receipt fields are incomplete.')
        if table == 'v2_usage_close':
            if not isinstance(usage, list) or len(usage) > 10000:
                raise ReceiptError('Invalid usage projection.')
            for item in usage:
                if not isinstance(item, dict) or set(item) != {'dimension', 'state', 'value'}:
                    raise ReceiptError('Invalid usage metric.')
                if (not isinstance(item['dimension'], str) or not PSEUDONYM.fullmatch(item['dimension'])
                        or item['state'] not in ('actual', 'estimated', 'unknown')
                        or (item['value'] is not None and _number(item['value']) != item['value'])
                        or (item['state'] == 'unknown' and item['value'] is not None)):
                    raise ReceiptError('Invalid usage metric.')
        counts[table] += 1
    previous, last_seq, event_ids = ZERO_HASH, 0, set()
    for event in receipt['events']:
        fields = ('event_id', 'project_id', 'entity_type', 'entity_id', 'global_seq', 'at')
        if (not isinstance(event, dict) or set(event) != set(fields) |
                {'event_type_id', 'payload', 'exported_payload_sha256', 'previous_sha256'}):
            raise ReceiptError('Invalid event envelope.')
        valid_fields({k: event[k] for k in fields}, fields)
        seq = event['global_seq']
        if (type(seq) is not int or seq <= last_seq or event['event_id'] in event_ids
                or event['project_id'] != receipt['project_id']
                or not isinstance(event['event_type_id'], str) or not PSEUDONYM.fullmatch(event['event_type_id'])
                or event['previous_sha256'] != previous):
            raise ReceiptError('Event identity, order or chain is invalid.')
        payload = event['payload']
        if not isinstance(payload, dict) or set(payload) != {
                'fields', 'stated_hashes', 'omitted_top_level_fields',
                'source_payload_utf8_sha256', 'exported_fields_sha256'}:
            raise ReceiptError('Invalid sanitized payload.')
        valid_fields(payload['fields'], ID_FIELDS | INT_FIELDS | NUMBER_FIELDS |
                     TIME_FIELDS | HASH_FIELDS | ENUMS.keys())
        if (type(payload['omitted_top_level_fields']) is not int or payload['omitted_top_level_fields'] < 0
                or not isinstance(payload['source_payload_utf8_sha256'], str)
                or not HASH.fullmatch(payload['source_payload_utf8_sha256'])
                or payload['exported_fields_sha256'] != _sha(_canonical(payload['fields']))
                or event['exported_payload_sha256'] != _sha(_canonical(payload))
                or not isinstance(payload['stated_hashes'], list) or len(payload['stated_hashes']) > 20000):
            raise ReceiptError('Payload hash or omission metadata is invalid.')
        for claim in payload['stated_hashes']:
            if (not isinstance(claim, dict) or set(claim) != {'field', 'location_id', 'value'}
                    or claim['field'] not in HASH_FIELDS
                    or not isinstance(claim['location_id'], str) or not PSEUDONYM.fullmatch(claim['location_id'])
                    or not isinstance(claim['value'], str) or not HASH.fullmatch(claim['value'])):
                raise ReceiptError('Invalid stated hash claim.')
        previous, last_seq = _sha(_canonical(event)), seq
        event_ids.add(event['event_id'])
    counts['v2_event'] = len(receipt['events'])
    if (previous != receipt['chain_tip'] or receipt['source_max_event_seq'] != (last_seq or None)
            or any(counts[t] != receipt['sources'][t]['count'] for t in TABLES)):
        raise ReceiptError('Receipt counts, high-water mark or chain tip differ.')
    if len(_canonical(receipt)) > MAX_ARCHIVE_BYTES:
        raise ReceiptError('Receipt byte bound exceeded.')


def _safe_path(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or str(path).startswith(('\\\\', '//'))
            or any(':' in p or p.rstrip(' .') != p or
                   p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                              *('COM' + str(i) for i in range(1, 10)),
                                              *('LPT' + str(i) for i in range(1, 10))}
                   for p in path.parts[1:])):
        raise ReceiptError('An absolute, local, unambiguous path is required.')
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ReceiptError('Symlinks and junction/reparse paths are refused.')
    return path


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _read_file(path):
    _safe_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
        raise ReceiptError('Receipt file is not a bounded, private regular file.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ReceiptError('Receipt file changed during inspection.')
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ReceiptError('Receipt file exceeds its byte bound.')
    return data


def _write_file(path, content):
    _safe_path(path)
    if len(content) > MAX_FILE_BYTES:
        raise ReceiptError('Receipt object exceeds its byte bound.')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _git(repository, args, *, timeout=20, max_output=8 * 1024 * 1024):
    executable = shutil.which('git')
    if not executable:
        raise ReceiptError('Git CLI is unavailable.')
    env = {k: v for k, v in os.environ.items()
           if k.upper() in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT='0',
               GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1',
               GIT_ATTR_NOSYSTEM='1', LC_ALL='C',
               GIT_AUTHOR_NAME='AWO receipt', GIT_AUTHOR_EMAIL='receipt@localhost.invalid',
               GIT_COMMITTER_NAME='AWO receipt', GIT_COMMITTER_EMAIL='receipt@localhost.invalid',
               GIT_AUTHOR_DATE='2000-01-01T00:00:00+00:00',
               GIT_COMMITTER_DATE='2000-01-01T00:00:00+00:00')
    argv = [executable, '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.autocrlf=false',
            '-c', 'core.safecrlf=false', '-c', 'core.fsmonitor=false',
            '-c', 'core.attributesFile=' + os.devnull, '-c', 'commit.gpgsign=false',
            '-c', 'tag.gpgsign=false', '-c', 'maintenance.auto=false', '-c', 'gc.auto=0',
            '-c', 'protocol.allow=never', *args]
    process = subprocess.Popen(argv, cwd=repository, env=env, shell=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunks, count, lock, overflow = [[], []], [0], threading.Lock(), threading.Event()

    def drain(stream, index):
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                with lock:
                    count[0] += len(chunk)
                    if count[0] <= max_output:
                        chunks[index].append(chunk)
                    else:
                        overflow.set()
                        process.kill()
        finally:
            stream.close()
    threads = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=5)
        raise ReceiptError('Git operation exceeded its time bound; preserve the destination.') from exc
    finally:
        for thread in threads:
            thread.join(timeout=5)
    if overflow.is_set() or any(t.is_alive() for t in threads):
        raise ReceiptError('Git output exceeded its bound.')
    if process.returncode:
        # Do not reflect potentially private Git stderr/config into public errors.
        raise ReceiptError('Local Git operation failed; preserve the destination for inspection.')
    return b''.join(chunks[0])


def _inventory(repository):
    '''Reject reparse files, extra data and unsafe Git plumbing before invoking Git.'''
    paths, count = set(), 0
    stack = [repository]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                count += 1
                if count > MAX_OBJECTS * 4 + 1000:
                    raise ReceiptError('Repository inventory is too large.')
                path = _safe_path(entry.path)
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    stack.append(path)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ReceiptError('Repository has a non-private or special file.')
                else:
                    relative = path.relative_to(repository).as_posix()
                    if not relative.startswith('.git/'):
                        paths.add(relative)
    gitdir = repository / '.git'
    if not gitdir.is_dir() or (gitdir / 'commondir').exists():
        raise ReceiptError('A standalone repository with a private Git directory is required.')
    for relative in ('objects/info/alternates', 'objects/info/http-alternates',
                     'info/grafts', 'shallow'):
        if (gitdir / relative).exists():
            raise ReceiptError('External or rewritten Git object history is refused.')
    config = _read_file(gitdir / 'config').decode('ascii')
    section = None
    # Git init's own local core settings only; no includes, filters, hooks, remotes or worktree redirection.
    for raw in config.splitlines():
        line = raw.strip().lower()
        if not line:
            continue
        if line.startswith('['):
            if line != '[core]':
                raise ReceiptError('Repository config is outside the isolated export policy.')
            section = 'core'
        else:
            parts = line.split('=', 1)
            if section != 'core' or len(parts) != 2:
                raise ReceiptError('Repository config is malformed.')
            key, value = (p.strip() for p in parts)
            allowed = {'repositoryformatversion': {'0'}, 'filemode': {'true', 'false'},
                       'bare': {'false'}, 'logallrefupdates': {'true'},
                       'ignorecase': {'true', 'false'}, 'symlinks': {'true', 'false'}}
            if key not in allowed or value not in allowed[key]:
                raise ReceiptError('Repository config is outside the isolated export policy.')
    return paths


def _bundle(receipt):
    _validate_receipt(receipt)
    objects = {}

    def put(value):
        content = _canonical(value)
        if len(content) > MAX_FILE_BYTES:
            raise ReceiptError('Receipt object exceeds its byte bound.')
        digest = _sha(content)
        objects[digest] = content
        return digest
    snapshot = {k: v for k, v in receipt.items() if k not in ('records', 'events')}
    snapshot_hash = put(snapshot)
    records = [put(r) for r in receipt['records']]
    events = [put(e) for e in receipt['events']]
    manifest = {'format_version': FORMAT_VERSION, 'policy': POLICY,
                'snapshot_sha256': snapshot_hash, 'record_sha256': records,
                'event_sha256': events, 'chain_tip': receipt['chain_tip'],
                'object_count': len(objects),
                'files': {digest: len(content) for digest, content in sorted(objects.items())}}
    content = _canonical(manifest)
    if len(content) > MAX_FILE_BYTES or sum(map(len, objects.values())) + len(content) > MAX_ARCHIVE_BYTES:
        raise ReceiptError('Archive byte bound exceeded.')
    return objects, manifest, content


def export_receipt(receipt, destination, *, enabled=False, protected_roots):
    '''Create one new isolated local repo. Never adopt, append, remove or push.'''
    if enabled is not True:
        raise ReceiptError('Receipt export requires explicit Boolean opt-in.')
    if not isinstance(protected_roots, (list, tuple)) or not protected_roots:
        raise ReceiptError('Trusted source/runtime protected roots are required.')
    destination = _safe_path(destination)
    protected = [_safe_path(p) for p in protected_roots]
    if any(_overlap(destination, p) for p in protected):
        raise ReceiptError('Destination overlaps a protected source or runtime root.')
    if destination.exists() or not destination.parent.is_dir():
        raise ReceiptError('Destination must be absent in an existing private parent directory.')
    if any((p / '.git').exists() or
           ((p / 'HEAD').is_file() and (p / 'objects').is_dir() and (p / 'refs').is_dir())
           for p in destination.parents):
        raise ReceiptError('Destination is nested within another Git repository.')
    try:
        objects, manifest, content = _bundle(receipt)
    except (KeyError, TypeError, RecursionError, UnicodeError) as exc:
        raise ReceiptError('Detached receipt content is malformed.') from exc
    # All content/policy/path validation happens before the first external write.
    destination.mkdir(mode=0o700)
    (destination / 'objects').mkdir(mode=0o700)
    for digest, data in objects.items():
        _write_file(destination / 'objects' / (digest + '.json'), data)
    _write_file(destination / 'manifest.json', content)
    _git(destination, ['init', '--quiet', '--template=', '--initial-branch=receipts'])
    _inventory(destination)
    _git(destination, ['add', '--', 'objects', 'manifest.json'])
    _git(destination, ['commit', '--quiet', '--no-verify', '-m', 'Export sanitized AWO audit receipt'])
    return verify_receipt(destination, expected_manifest_sha256=_sha(content))


def _parse_json(data):
    try:
        if len(data) > MAX_FILE_BYTES:
            raise ValueError()
        value = json.loads(data.decode('ascii'),
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReceiptError('Receipt JSON is invalid.') from exc
    if _canonical(value) != data:
        raise ReceiptError('Receipt JSON is not canonical.')
    return value


def _verified(repository, expected_manifest_sha256=None, expected_commit=None):
    repository = _safe_path(repository)
    if not repository.is_dir():
        raise ReceiptError('Receipt repository is missing.')
    paths = _inventory(repository)
    manifest_bytes = _read_file(repository / 'manifest.json')
    digest = _sha(manifest_bytes)
    if expected_manifest_sha256 is not None and expected_manifest_sha256 != digest:
        raise ReceiptError('Manifest differs from the external trust anchor.')
    manifest = _parse_json(manifest_bytes)
    if not isinstance(manifest, dict) or set(manifest) != {
            'format_version', 'policy', 'snapshot_sha256', 'record_sha256', 'event_sha256',
            'chain_tip', 'object_count', 'files'}:
        raise ReceiptError('Unexpected manifest fields.')
    if (manifest['format_version'] != FORMAT_VERSION or manifest['policy'] != POLICY
            or not isinstance(manifest['files'], dict) or len(manifest['files']) > MAX_OBJECTS + 1
            or not isinstance(manifest['record_sha256'], list)
            or not isinstance(manifest['event_sha256'], list)):
        raise ReceiptError('Invalid manifest policy or collections.')
    hashes = [manifest['snapshot_sha256'], *manifest['record_sha256'], *manifest['event_sha256']]
    if any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in hashes):
        raise ReceiptError('Invalid content address.')
    if (set(hashes) != set(manifest['files']) or manifest['object_count'] != len(set(hashes))
            or paths != {'manifest.json', *('objects/' + h + '.json' for h in hashes)}):
        raise ReceiptError('Manifest and exact file inventory differ.')
    objects, total = {}, len(manifest_bytes)
    for address, size in manifest['files'].items():
        if type(size) is not int or not 0 < size <= MAX_FILE_BYTES:
            raise ReceiptError('Invalid manifest file size.')
        data = _read_file(repository / 'objects' / (address + '.json'))
        total += len(data)
        if total > MAX_ARCHIVE_BYTES or size != len(data) or _sha(data) != address:
            raise ReceiptError('Content hash, size or archive bound differs.')
        objects[address] = (data, _parse_json(data))
    receipt = objects[manifest['snapshot_sha256']][1]
    if not isinstance(receipt, dict) or 'records' in receipt or 'events' in receipt:
        raise ReceiptError('Invalid snapshot envelope.')
    receipt = dict(receipt, records=[objects[h][1] for h in manifest['record_sha256']],
                   events=[objects[h][1] for h in manifest['event_sha256']])
    _validate_receipt(receipt)
    if manifest['chain_tip'] != receipt['chain_tip']:
        raise ReceiptError('Manifest chain tip differs.')
    commit = _git(repository, ['rev-parse', '--verify', 'HEAD^{commit}']).decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ReceiptError('Unexpected Git object format.')
    if expected_commit is not None and expected_commit != commit:
        raise ReceiptError('Commit differs from the external trust anchor.')
    commit_data = _git(repository, ['cat-file', '-p', commit], max_output=65536)
    if b'\nparent ' in commit_data:
        raise ReceiptError('Receipt repository must contain one root export commit.')
    tree = _git(repository, ['ls-tree', '-r', '-z', '--full-tree', commit])
    committed = {}
    for item in tree.split(b'\0'):
        if not item:
            continue
        try:
            head, path = item.split(b'\t', 1)
            mode, kind, oid = head.decode('ascii').split(' ')
            path = path.decode('ascii')
        except (ValueError, UnicodeError) as exc:
            raise ReceiptError('Invalid Git tree entry.') from exc
        if mode != '100644' or kind != 'blob' or path in committed:
            raise ReceiptError('Unexpected Git tree mode or duplicate entry.')
        committed[path] = oid
    if set(committed) != paths:
        raise ReceiptError('Committed file inventory differs.')
    for path, oid in committed.items():
        data = manifest_bytes if path == 'manifest.json' else objects[Path(path).stem][0]
        expected_oid = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
        if oid != expected_oid:
            raise ReceiptError('Working receipt differs from its committed Git blob.')
    result = {'format_version': FORMAT_VERSION, 'manifest_sha256': digest, 'commit': commit,
              'chain_tip': receipt['chain_tip'], 'record_count': len(receipt['records']),
              'event_count': len(receipt['events']), 'verified': True,
              'trust': 'externally pinned' if expected_manifest_sha256 is not None or expected_commit is not None
                       else 'internal consistency only',
              'limitations': list(LIMITATIONS)}
    return result, receipt


def verify_receipt(repository, *, expected_manifest_sha256=None, expected_commit=None):
    '''Read only; no checkout, index refresh, DB changes, adoption or repair.'''
    try:
        return _verified(repository, expected_manifest_sha256, expected_commit)[0]
    except (OSError, UnicodeError, KeyError, TypeError, RecursionError) as exc:
        raise ReceiptError('Receipt structure is missing, malformed or unsafe.') from exc


def rebuild_target(repository, audit_db):
    """Return a safe audit path that is outside the verified receipt repository."""
    repository_path = _safe_path(repository)
    target = _safe_path(audit_db)
    # Compare final paths as well as lexical ones: an NTFS 8.3 alias is not a
    # reparse point, so _safe_path accepts it, yet it still names the repository.
    if (_overlap(target, repository_path)
            or _overlap(target.resolve(), repository_path.resolve())):
        raise ReceiptError('Rebuild destination must be outside the receipt repository.')
    return target


def rebuild_receipt(con, repository, *, expected_manifest_sha256=None, expected_commit=None):
    '''Rebuild only sanitized audit tables in an empty caller-owned transaction.'''
    if not con.in_transaction:
        raise ReceiptError('Caller must begin the destination transaction.')
    if con.execute("SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchone() or \
            con.execute("SELECT 1 FROM sqlite_temp_master WHERE name NOT GLOB 'sqlite_*'").fetchone():
        raise ReceiptError('Rebuild requires an empty SQLite destination, never a runtime DB.')
    try:
        result, receipt = _verified(repository, expected_manifest_sha256, expected_commit)
    except (OSError, UnicodeError, KeyError, TypeError, RecursionError) as exc:
        raise ReceiptError('Receipt structure is missing, malformed or unsafe.') from exc
    # The caller controls commit/rollback; a local savepoint prevents partial audit tables.
    con.execute('SAVEPOINT receipt_rebuild')
    try:
        con.execute('CREATE TABLE receipt_meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)')
        con.execute('CREATE TABLE receipt_record (ordinal INTEGER PRIMARY KEY, source_table TEXT NOT NULL, '
                    'object_sha256 TEXT NOT NULL, data_json TEXT NOT NULL)')
        con.execute('CREATE TABLE receipt_event (global_seq INTEGER PRIMARY KEY, event_sha256 TEXT NOT NULL, '
                    'data_json TEXT NOT NULL)')
        metadata = {k: v for k, v in receipt.items() if k not in ('records', 'events')}
        for key, value in metadata.items():
            con.execute('INSERT INTO receipt_meta VALUES (?,?)', (key, _canonical(value).decode('ascii')))
        for ordinal, record in enumerate(receipt['records']):
            content = _canonical(record)
            con.execute('INSERT INTO receipt_record VALUES (?,?,?,?)',
                        (ordinal, record['table'], _sha(content), content.decode('ascii')))
        for event in receipt['events']:
            content = _canonical(event)
            con.execute('INSERT INTO receipt_event VALUES (?,?,?)',
                        (event['global_seq'], _sha(content), content.decode('ascii')))
        con.execute('RELEASE receipt_rebuild')
    except Exception:
        con.execute('ROLLBACK TO receipt_rebuild')
        con.execute('RELEASE receipt_rebuild')
        raise
    return dict(result, rebuilt=True, runtime_restore=False)
