"""Assignment workspaces behind the application service (stdlib only).

The service owns authorization, migrations, transactions and Task lifecycle.
This module never dispatches workers or establishes quiescence from ownership.
External operations consume committed one-shot tickets; ambiguous work is kept.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import threading
import uuid


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 10000
MAX_COMMAND_BYTES = 32 * 1024 * 1024
COMMAND_TIMEOUT = 30
MARKER = 'awo-workspace.json'
DIRECTORY_MARKER = '.awo-workspace.json'
STATES = ('requested', 'provisioning', 'ready', 'active', 'frozen', 'released',
          'orphaned', 'quarantined', 'workspace_lost', 'failed')


class WorkspaceError(ValueError):
    """A closed workspace gate; caller must preserve claims on uncertainty."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    return _sha(_json(value).encode('ascii'))


def _rows(con, sql, args=()):
    cur = con.execute(sql, args)
    names = [item[0] for item in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _exists(con, table):
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _transaction(con):
    if not con.in_transaction:
        raise WorkspaceError('caller must begin a transaction (BEGIN IMMEDIATE for writes)')


def create_workspace_schema(con):
    """Explicit additive migration. No executescript, commit, or implicit BEGIN."""
    _transaction(con)
    if _exists(con, 'v2_workspace_meta'):
        row = con.execute("SELECT value FROM v2_workspace_meta WHERE key='version'").fetchone()
        if row and int(row[0]) > SCHEMA_VERSION:
            raise WorkspaceError('workspace schema is newer than this build')
    statements = (
        "CREATE TABLE IF NOT EXISTS v2_workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS v2_workspace (
          workspace_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES v2_task(task_id),
          assignment_id TEXT NOT NULL UNIQUE REFERENCES v2_assignment(assignment_id),
          host TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
          spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('requested','provisioning','ready','active',
            'frozen','released','orphaned','quarantined','workspace_lost','failed')),
          operation_token TEXT, operation_kind TEXT,
          operation_started INTEGER NOT NULL DEFAULT 0 CHECK(operation_started IN (0,1)),
          revision INTEGER NOT NULL DEFAULT 1, fingerprint_json TEXT, error TEXT,
          created_at TEXT NOT NULL, ready_at TEXT, released_at TEXT,
          quiescence_json TEXT, snapshot_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS v2_workspace_host ON v2_workspace(host,status)",
        """CREATE TRIGGER IF NOT EXISTS v2_workspace_immutable BEFORE UPDATE OF
          workspace_id,task_id,assignment_id,host,path,kind,spec_json,spec_sha256,created_at
          ON v2_workspace BEGIN SELECT RAISE(ABORT,'immutable workspace reservation'); END""",
        """CREATE TABLE IF NOT EXISTS v2_workspace_journal (
          seq INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES v2_workspace(workspace_id),
          revision INTEGER NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL, data_json TEXT NOT NULL)""",
    )
    for sql in statements:
        con.execute(sql)
    con.execute("INSERT OR IGNORE INTO v2_workspace_meta VALUES ('version',?)", (str(SCHEMA_VERSION),))


def _journal(con, workspace, kind, data):
    con.execute('INSERT INTO v2_workspace_journal(workspace_id,revision,kind,at,data_json) '
                'VALUES (?,?,?,?,?)',
                (workspace['workspace_id'], workspace['revision'], kind, _now(), _json(data)))


def get_workspace(con, workspace_id):
    """Read only, including on an unmigrated database. Returns None if absent."""
    if not _exists(con, 'v2_workspace'):
        return None
    rows = _rows(con, 'SELECT * FROM v2_workspace WHERE workspace_id=?', (workspace_id,))
    if not rows:
        return None
    row = rows[0]
    spec = json.loads(row['spec_json'])
    if _digest(spec) != row['spec_sha256']:
        raise WorkspaceError('workspace specification hash mismatch')
    return dict(spec, **row)


def _required(con, workspace_id):
    value = get_workspace(con, workspace_id)
    if value is None:
        raise WorkspaceError('workspace does not exist; explicit migration/reservation required')
    return value


def _canon(path):
    return os.path.normcase(str(Path(path).resolve()))


def _inside(path, root):
    try:
        return os.path.commonpath([_canon(path), _canon(root)]) == _canon(root)
    except ValueError:
        return False


def paths_overlap(first, second):
    return _inside(first, second) or _inside(second, first)


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _no_links(path):
    """Reject symlinks and Windows junction/reparse components, even in ancestors."""
    path = Path(os.path.abspath(path))
    for part in reversed((path,) + tuple(path.parents)):
        if os.path.lexists(part) and _is_link(part.lstat()):
            raise WorkspaceError('symlink/junction/reparse path: ' + str(part))
    return path


def _absolute(path):
    if not isinstance(path, (str, os.PathLike)) or not Path(path).is_absolute():
        raise WorkspaceError('absolute path required')
    raw = str(path)
    if '\x00' in raw or (os.name == 'nt' and (raw.startswith(('\\', '//')) or ':' in raw[2:])):
        raise WorkspaceError('UNC/device/alternate-stream paths are not supported')
    return _canon(_no_links(path))


def _identity(path):
    info = _no_links(path).stat()
    return [info.st_dev, info.st_ino]


def _read_bytes(path, limit=MAX_FILE_BYTES):
    path = _no_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise WorkspaceError('nonregular or hardlinked file: ' + str(path))
    if before.st_size > limit:
        raise WorkspaceError('file exceeds byte limit: ' + str(path))
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise WorkspaceError('file changed during open')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    final = path.lstat()
    # Windows lstat and fstat expose different ctime semantics. Compare matching
    # observations for ctime, and compare identity/size/mtime across both APIs.
    keys = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if (len(data) > limit or keys(before) != keys(after) or keys(after) != keys(final) or
            before.st_ctime_ns != final.st_ctime_ns or opened.st_ctime_ns != after.st_ctime_ns):
        raise WorkspaceError('file changed during read: ' + str(path))
    return data


def run_argv(argv, *, cwd, timeout=COMMAND_TIMEOUT, max_bytes=MAX_COMMAND_BYTES):
    """Bounded argv execution, no shell. Timeout/overflow never proves quiescence."""
    if (not isinstance(argv, (list, tuple)) or not argv or
            any(not isinstance(arg, str) or '\x00' in arg for arg in argv)):
        raise WorkspaceError('argv must contain non-NUL strings')
    if not 0 < timeout <= 300 or not 0 < max_bytes <= MAX_TOTAL_BYTES:
        raise WorkspaceError('invalid subprocess bounds')
    env = {key: val for key, val in os.environ.items() if not key.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_LFS_SKIP_SMUDGE='1')
    proc = subprocess.Popen(list(argv), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    output = [bytearray(), bytearray()]
    overflow = threading.Event()
    def drain(stream, target):
        try:
            while True:
                block = stream.read(65536)
                if not block:
                    break
                if len(target) + len(block) > max_bytes:
                    overflow.set()
                    proc.kill()
                    break
                target.extend(block)
        finally:
            stream.close()
    readers = [threading.Thread(target=drain, args=(stream, target), daemon=True)
               for stream, target in zip((proc.stdout, proc.stderr), output)]
    for thread in readers:
        thread.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait(timeout=5)
        raise WorkspaceError('external operation timed out; outcome/writers uncertain') from exc
    finally:
        for thread in readers:
            thread.join(timeout=1)
    if any(thread.is_alive() for thread in readers):
        raise WorkspaceError('external output still open; descendants may remain')
    if overflow.is_set():
        raise WorkspaceError('external output exceeded limit; outcome uncertain')
    return {'returncode': proc.returncode, 'stdout': bytes(output[0]), 'stderr': bytes(output[1])}


def _git(cwd, *args, ok=(0,)):
    result = run_argv(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'protocol.allow=never',
                       '-c', 'submodule.recurse=false', '-C', str(cwd), *args], cwd=cwd)
    if result['returncode'] not in ok:
        raise WorkspaceError('git ' + args[0] + ' failed (' + str(result['returncode']) + '): ' +
                             result['stderr'].decode('utf-8', 'replace')[:1000])
    return result['stdout']


def _git_text(cwd, *args):
    return os.fsdecode(_git(cwd, *args)).strip()


def _check_repo(repo):
    # Checkout filters can execute arbitrary commands. Disable hooks and reject filters.
    config = _git(repo, 'config', '--includes', '--get-regexp', r'^filter[.]', ok=(0, 1))
    if config:
        raise WorkspaceError('repositories with local checkout filters are unsupported')


def _patterns(values):
    if not isinstance(values, (list, tuple)) or not 0 < len(values) <= 128:
        raise WorkspaceError('allowed_paths must be an explicit nonempty sequence')
    for value in values:
        if (not isinstance(value, str) or not value or len(value) > 1024 or value.count('/') > 64 or value.startswith('/') or
                '\\' in value or ':' in value or '\x00' in value or
                any(part in ('', '.', '..') for part in value.split('/'))):
            raise WorkspaceError('invalid relative allowed_paths glob')
    return sorted(set(values))


def prepare_workspace(*, task_id, assignment_id, host, kind, root, path, allowed_paths,
                      repository_id=None, repo_root=None, base_commit=None, branch=None,
                      artifact_paths=(), harness='', resource_claims=()):
    """Read-only external preflight, before entering the reservation transaction."""
    for value in (task_id, assignment_id):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value) or value in ('.', '..'):
            raise WorkspaceError('invalid Task/Assignment identity')
    if not isinstance(host, str) or not host.strip() or len(host) > 255:
        raise WorkspaceError('explicit local host identity required')
    if kind not in ('git_worktree', 'directory'):
        raise WorkspaceError('kind must be git_worktree or directory')
    root, path = _absolute(root), _absolute(path)
    if not Path(root).is_dir() or not Path(path).parent.is_dir():
        raise WorkspaceError('workspace root and immediate parent must already exist')
    if path == root or not _inside(path, root) or os.path.lexists(path):
        raise WorkspaceError('workspace path must be new and strictly within root')
    spec = dict(workspace_id='ws-' + uuid.uuid4().hex, task_id=task_id, assignment_id=assignment_id,
                host=host, kind=kind, root=root, path=path, launch_cwd=path,
                root_identity=_identity(root), parent_identity=_identity(Path(path).parent),
                allowed_paths=_patterns(allowed_paths), artifact_paths=sorted({_absolute(p) for p in artifact_paths}),
                harness=harness, resource_claims=list(resource_claims), ownership_token=secrets.token_hex(32),
                created_at=_now(), repository_id=repository_id, repo_root=None, common_git_dir=None,
                checkout_id=None, base_commit=None, branch=None)
    if kind == 'git_worktree':
        if not isinstance(repository_id, str) or not repository_id.strip():
            raise WorkspaceError('stable configured repository_id required (not a checkout path hash)')
        repo = _absolute(repo_root)
        _check_repo(repo)
        top = _absolute(_git_text(repo, 'rev-parse', '--show-toplevel'))
        common = _absolute(_git_text(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        if repo != top or paths_overlap(path, repo) or paths_overlap(path, common):
            raise WorkspaceError('workspace must be outside source checkout and Git metadata')
        if not isinstance(base_commit, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', base_commit):
            raise WorkspaceError('base_commit must be a full immutable commit object ID')
        base = _git_text(repo, 'rev-parse', '--verify', base_commit + '^{commit}')
        branch = branch or ('awo/' + task_id + '/' + assignment_id)
        if not isinstance(branch, str) or not branch.startswith('awo/') or branch.startswith('-'):
            raise WorkspaceError('workspace branch must be in awo/ namespace')
        _git(repo, 'check-ref-format', 'refs/heads/' + branch)
        if _git(repo, 'show-ref', '--verify', '--hash', 'refs/heads/' + branch, ok=(0, 1, 128)):
            raise WorkspaceError('branch already exists; refusing adoption')
        spec.update(repo_root=repo, common_git_dir=common, base_commit=base, branch=branch,
                    checkout_id=_digest([host, repo, common]), repo_identity=_identity(repo),
                    common_identity=_identity(common))
    elif any(value is not None for value in (repository_id, repo_root, base_commit, branch)):
        raise WorkspaceError('directory mode must not carry Git identity')
    # JSON serialization also rejects nonportable metadata/NaN before writes.
    _json(spec)
    return spec


def _paths_valid(workspace, *, absent=False):
    for field in ('root', 'path'):
        if _absolute(workspace[field]) != workspace[field]:
            raise WorkspaceError('canonical path drift: ' + field)
    if (not _inside(workspace['path'], workspace['root']) or workspace['path'] == workspace['root'] or
            _identity(workspace['root']) != workspace['root_identity'] or
            _identity(Path(workspace['path']).parent) != workspace['parent_identity']):
        raise WorkspaceError('workspace root/parent changed')
    if absent and os.path.lexists(workspace['path']):
        raise WorkspaceError('path exists; never adopt a preexisting directory')
    if workspace['kind'] == 'git_worktree':
        for field, identity in (('repo_root', 'repo_identity'), ('common_git_dir', 'common_identity')):
            if _absolute(workspace[field]) != workspace[field] or _identity(workspace[field]) != workspace[identity]:
                raise WorkspaceError('repository identity changed')
        if paths_overlap(workspace['path'], workspace['repo_root']) or paths_overlap(workspace['path'], workspace['common_git_dir']):
            raise WorkspaceError('workspace overlaps repository')
    for path in workspace['artifact_paths']:
        if _absolute(path) != path:
            raise WorkspaceError('artifact path changed')


def workspace_claims(con, host=None):
    """Physical claims only. Failed/lost/orphaned work retains its claims."""
    if not _exists(con, 'v2_workspace'):
        return []
    result = []
    for row in _rows(con, "SELECT workspace_id FROM v2_workspace WHERE status!='released'"):
        ws = _required(con, row['workspace_id'])
        if host is None or ws['host'] == host:
            result.append(dict(workspace_id=ws['workspace_id'], assignment_id=ws['assignment_id'],
                               task_id=ws['task_id'], host=ws['host'], status=ws['status'],
                               workspaces=sorted(set([ws['path']] + ws['artifact_paths'])),
                               resources=ws['resource_claims']))
    return result


def _claim_conflicts(con, spec):
    wanted = [spec['path']] + spec['artifact_paths']
    own_attempt = None
    if _exists(con, 'v2_assignment') and spec.get('assignment_id'):
        row = con.execute('SELECT legacy_task_id FROM v2_assignment WHERE assignment_id=?',
                          (spec['assignment_id'],)).fetchone()
        own_attempt = row[0] if row else None
    for claim in workspace_claims(con, spec['host']):
        if claim['workspace_id'] != spec['workspace_id'] and any(
                paths_overlap(a, b) for a in wanted for b in claim['workspaces']):
            raise WorkspaceError('physical workspace/artifact overlap: ' + claim['workspace_id'])
    if _exists(con, 'dispatches') and _exists(con, 'tasks'):
        for row in _rows(con, "SELECT d.task,d.data FROM dispatches d JOIN tasks t ON t.id=d.task "
                         "WHERE t.status!='resolved'"):
            if own_attempt is not None and row['task'] == own_attempt:
                # The scheduler reserves its own attempt before workspace.create
                # provisions the frozen launch directory; that is not a conflict.
                continue
            value = json.loads(row['data'])
            # Old ledgers have no host; conservatively treat their paths as local.
            if value.get('host', spec['host']) == spec['host'] and any(
                    paths_overlap(a, b) for a in wanted for b in value.get('workspaces', [])):
                raise WorkspaceError('legacy dispatch holds workspace: ' + row['task'])
    if _exists(con, 'tasks'):
        from workflow_evidence import delivery_paths
        for row in _rows(con, 'SELECT id,data,status FROM tasks'):
            if own_attempt is not None and row['id'] == own_attempt:
                continue
            request = json.loads(row['data']).get('request', {})
            old = list(delivery_paths(request)) if request.get('completion') else []
            if row['status'] != 'resolved':
                old += [p for p in (request.get('cwd'), request.get('worker', {}).get(
                    'expected_identity', {}).get('launch_cwd')) if p]
            if any(paths_overlap(a, b) for a in wanted for b in old):
                raise WorkspaceError('legacy task/evidence holds path: ' + row['id'])
    # Reviews/receipts remain evidence claims even after task closure.
    for table, column in (('reviews', 'data'), ('events', 'data')):
        if not _exists(con, table):
            continue
        for row in _rows(con, 'SELECT ' + column + ' FROM ' + table):
            value = json.loads(row[column])
            if table == 'reviews':
                old = [value['path']] if value.get('path') else []
            else:
                receipt = value.get('receipt') or {}
                items = receipt.get('artifacts', []) + [receipt.get('binding_artifact', {})]
                old = [item['path'] for item in items if isinstance(item, dict) and item.get('path')]
            if any(paths_overlap(a, b) for a in wanted for b in old):
                raise WorkspaceError('retained legacy review/receipt owns requested path')


def reserve_workspace(con, spec):
    """Reserve in service BEGIN IMMEDIATE; no Git/external execution or commit."""
    _transaction(con)
    if not _exists(con, 'v2_workspace'):
        raise WorkspaceError('explicit workspace migration required')
    spec = json.loads(_json(spec))
    if (not re.fullmatch(r'ws-[0-9a-f]{32}', spec.get('workspace_id', '')) or
            not re.fullmatch(r'[0-9a-f]{64}', spec.get('ownership_token', '')) or
            spec.get('kind') not in ('git_worktree', 'directory') or
            spec.get('allowed_paths') != _patterns(spec.get('allowed_paths'))):
        raise WorkspaceError('invalid prepared workspace specification')
    prior = get_workspace(con, spec['workspace_id'])
    if prior:
        if prior['spec_sha256'] != _digest(spec):
            raise WorkspaceError('immutable workspace replay differs')
        return prior
    rows = _rows(con, 'SELECT * FROM v2_assignment WHERE assignment_id=?', (spec['assignment_id'],))
    if not rows or rows[0]['task_id'] != spec['task_id']:
        raise WorkspaceError('Assignment/Task mismatch')
    assignment = rows[0]
    if assignment['workspace_id'] or assignment['state'] not in ('reserved', 'blocked'):
        raise WorkspaceError('Assignment already bound or not reservable')
    if assignment.get('agent_id') and _exists(con, 'v2_agent'):
        agent = _rows(con, 'SELECT host FROM v2_agent WHERE agent_id=?', (assignment['agent_id'],))
        if not agent or agent[0]['host'] != spec['host']:
            raise WorkspaceError('Assignment agent host differs')
    _paths_valid(spec, absent=True)
    _claim_conflicts(con, spec)
    # Same checkout/branch cannot be reserved concurrently even with distinct paths.
    for row in _rows(con, "SELECT workspace_id FROM v2_workspace WHERE status!='released'"):
        other = _required(con, row['workspace_id'])
        if spec['kind'] == 'git_worktree' and (other['checkout_id'], other['branch']) == (spec['checkout_id'], spec['branch']):
            raise WorkspaceError('branch is already reserved')
    con.execute('INSERT INTO v2_workspace(workspace_id,task_id,assignment_id,host,path,kind,'
                'spec_json,spec_sha256,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (spec['workspace_id'], spec['task_id'], spec['assignment_id'], spec['host'], spec['path'],
                 spec['kind'], _json(spec), _digest(spec), 'requested', spec['created_at']))
    con.execute('UPDATE v2_assignment SET workspace_id=?,revision=revision+1,updated_at=? WHERE assignment_id=?',
                (spec['workspace_id'], _now(), spec['assignment_id']))
    ws = _required(con, spec['workspace_id'])
    _journal(con, ws, 'reserved', {'spec_sha256': ws['spec_sha256']})
    return ws


def begin_provision(con, workspace_id):
    _transaction(con)
    ws = _required(con, workspace_id)
    if ws['status'] != 'requested':
        raise WorkspaceError('provision may begin exactly once; inspect uncertain work')
    _paths_valid(ws, absent=True)
    _claim_conflicts(con, ws)
    con.execute("UPDATE v2_workspace SET status='provisioning',operation_kind='create',operation_token=?,"
                'operation_started=0,revision=revision+1 WHERE workspace_id=?',
                (secrets.token_hex(24), workspace_id))
    ws = _required(con, workspace_id)
    _journal(con, ws, 'provision_reserved', {})
    return ws


def _consume_ticket(database, workspace_id, operation_token, kind):
    # A separate connection cannot see uncommitted reservations. Do not create DBs.
    path = Path(database).resolve()
    con = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=1)
    try:
        con.execute('BEGIN IMMEDIATE')
        ws = _required(con, workspace_id)
        expected = 'provisioning' if kind == 'create' else 'frozen'
        if (ws['status'] != expected or ws['operation_kind'] != kind or
                ws['operation_token'] != operation_token or ws['operation_started']):
            raise WorkspaceError('operation ticket is absent, stale or already consumed')
        con.execute('UPDATE v2_workspace SET operation_started=1,revision=revision+1 WHERE workspace_id=?',
                    (workspace_id,))
        ws = _required(con, workspace_id)
        _journal(con, ws, kind + '_started', {})
        con.commit()
        return ws
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _sync_dir(directory):
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _exclusive_json(path, value):
    _no_links(path)
    with Path(path).open('xb') as stream:
        stream.write(_json(value).encode('ascii'))
        stream.flush()
        os.fsync(stream.fileno())
    _sync_dir(Path(path).parent)


def _private_dir(ws):
    git_file = Path(ws['path']) / '.git'
    raw = _read_bytes(git_file, 16384).decode('utf-8').strip()
    if not raw.startswith('gitdir: '):
        raise WorkspaceError('linked worktree .git pointer required')
    private = _absolute(git_file.parent / raw[8:])
    common = Path(ws['common_git_dir'])
    if not _inside(private, common / 'worktrees') or Path(private).parent != common / 'worktrees':
        raise WorkspaceError('Git private directory is outside registered common dir')
    common_ref = _read_bytes(Path(private) / 'commondir', 16384).decode('utf-8').strip()
    back_ref = _read_bytes(Path(private) / 'gitdir', 16384).decode('utf-8').strip()
    if _canon(Path(private) / common_ref) != _canon(common) or _canon(back_ref) != _canon(git_file):
        raise WorkspaceError('Git worktree backlink/common directory mismatch')
    return Path(private)


def _marker_path(ws):
    return (_private_dir(ws) / MARKER if ws['kind'] == 'git_worktree' else
            Path(ws['path']) / DIRECTORY_MARKER)


def _marker_value(ws):
    return dict(workspace_id=ws['workspace_id'], assignment_id=ws['assignment_id'], task_id=ws['task_id'],
                ownership_token=ws['ownership_token'], spec_sha256=ws['spec_sha256'],
                repository_id=ws['repository_id'], created_at=ws['created_at'],
                directory_identity=_identity(ws['path']))


def _ownership(ws):
    _paths_valid(ws)
    if not Path(ws['path']).is_dir():
        raise WorkspaceError('workspace_lost')
    marker = _marker_path(ws)
    value = json.loads(_read_bytes(marker, 65536))
    if value != _marker_value(ws):
        raise WorkspaceError('ownership marker mismatch; foreign workspace')
    return marker


def provision_workspace(database, workspace_id, operation_token):
    """After service commit: one external create, then read-only inspection."""
    ws = _consume_ticket(database, workspace_id, operation_token, 'create')
    try:
        _paths_valid(ws, absent=True)
        # Exclusive mkdir closes the preflight-to-Git empty-directory adoption race.
        Path(ws['path']).mkdir(exist_ok=False)
        created_identity = _identity(ws['path'])
        if ws['kind'] == 'git_worktree':
            _check_repo(ws['repo_root'])
            _git(ws['repo_root'], 'worktree', 'add', '--no-track', '-b', ws['branch'],
                 '--', ws['path'], ws['base_commit'])
            private = _private_dir(ws)
            actual = _absolute(_git_text(ws['path'], 'rev-parse', '--path-format=absolute', '--git-path', MARKER))
            if actual != _canon(private / MARKER):
                raise WorkspaceError('marker resolution is not worktree-private')
        if _identity(ws['path']) != created_identity:
            raise WorkspaceError('created directory was replaced before ownership publication')
        _exclusive_json(_marker_path(ws), _marker_value(ws))
        report = inspect_workspace(ws)
        if report['ok'] and (report['dirty'] or report.get('head') != ws['base_commit']):
            raise WorkspaceError('new materialization differs from immutable base')
        return dict(report, creation_completed=True)
    except (OSError, ValueError) as exc:
        return dict(workspace_id=workspace_id, operation_token=operation_token, revision=ws['revision'],
                    spec_sha256=ws['spec_sha256'], ok=False, error=str(exc), status='quarantined')


def _relative(name):
    if (not name or name.startswith('/') or '\\' in name or ':' in name or
            any(part in ('', '.', '..') for part in name.split('/'))):
        raise WorkspaceError('unsafe inventory relative path: ' + repr(name))
    return name


def _scan(ws):
    """Whole filesystem inventory, including ignored files; never follows links."""
    root = Path(ws['path'])
    entries, problems, total, seen = [], [], 0, set()
    pending = [root]
    while pending:
        directory = pending.pop()
        _no_links(directory)
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda child: child.name)
        for child in children:
            path = Path(child.path)
            rel = path.relative_to(root).as_posix()
            if rel.count('/') > 128:
                raise WorkspaceError('inventory directory depth limit exceeded')
            if directory == root and child.name == ('.git' if ws['kind'] == 'git_worktree' else DIRECTORY_MARKER):
                continue
            _relative(rel)
            key = rel.casefold() if os.name == 'nt' else rel
            if key in seen:
                raise WorkspaceError('case-colliding inventory path')
            seen.add(key)
            if len(seen) > MAX_FILES:
                raise WorkspaceError('inventory path count limit exceeded; incomplete inventory')
            # Full lstat supplies inode/link count (Windows DirEntry caches omit these).
            info = path.lstat()
            row = dict(path=rel, mode=stat.S_IMODE(info.st_mode), bytes=info.st_size, sha256=None)
            if _is_link(info):
                row.update(kind='link', target=os.readlink(path), unknown='link_not_followed')
                row['sha256'] = _sha(os.fsencode(row['target']))
                row['escapes'] = not _inside(path, root)
                problems.append(rel + (': path_escape' if row['escapes'] else ': link_requires_review'))
            elif stat.S_ISDIR(info.st_mode):
                row.update(kind='directory', bytes=0)
                pending.append(path)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                row.update(kind='special', unknown='special_or_hardlinked_file')
                problems.append(rel + ': special_or_hardlinked_file')
            elif info.st_size > MAX_FILE_BYTES or total + info.st_size > MAX_TOTAL_BYTES:
                row.update(kind='file', unknown='large_file_policy')
                problems.append(rel + ': content_not_archived_byte_limit')
            else:
                data = _read_bytes(path)
                total += len(data)
                row.update(kind='file', bytes=len(data), sha256=_sha(data), binary=b'\x00' in data,
                           mime=mimetypes.guess_type(rel)[0] or 'application/octet-stream')
            entries.append(row)
    return sorted(entries, key=lambda item: item['path']), sorted(problems)


def _git_metadata(ws):
    private = _private_dir(ws)
    common = Path(ws['common_git_dir'])
    head = _read_bytes(private / 'HEAD', 16384).decode('ascii').strip()
    if head != 'ref: refs/heads/' + ws['branch']:
        raise WorkspaceError('workspace branch changed or HEAD detached')
    paths = [Path(ws['path']) / '.git', private / 'HEAD', private / 'index', private / 'commondir',
             private / 'gitdir', common / ('refs/heads/' + ws['branch']), common / 'packed-refs',
             common / 'config', private / 'config.worktree', private / MARKER]
    # Unfinished operations cannot be dispatched, even when the tree looks clean.
    for name in ('index.lock', 'HEAD.lock', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                 'rebase-apply', 'rebase-merge', 'sequencer'):
        if os.path.lexists(private / name):
            raise WorkspaceError('Git operation in progress: ' + name)
    result = {}
    for path in paths:
        _no_links(path)
        result[str(path)] = _sha(_read_bytes(path)) if path.exists() else None
    return result


def _fingerprint(ws):
    marker = _ownership(ws)
    entries, problems = _scan(ws)
    return dict(directory_identity=_identity(ws['path']), marker_sha256=_sha(_read_bytes(marker, 65536)),
                tree_sha256=_digest(entries), problems=problems,
                git=_git_metadata(ws) if ws['kind'] == 'git_worktree' else {})


def _tree_records(data, index=False):
    result = {}
    for raw in data.split(b'\x00'):
        if not raw:
            continue
        metadata, name = raw.split(b'\t', 1)
        fields = metadata.decode('ascii').split()
        path = _relative(os.fsdecode(name))
        if index:
            item = dict(mode=fields[0], oid=fields[1], stage=int(fields[2]))
            result.setdefault(path, []).append(item)
        else:
            result[path] = dict(mode=fields[0], type=fields[1], oid=fields[2])
    return result


def _name_changes(data):
    fields = data.split(b'\x00')
    changes, position = [], 0
    while position < len(fields) and fields[position]:
        code = fields[position].decode('ascii')
        position += 1
        path = _relative(os.fsdecode(fields[position]))
        position += 1
        item = {'status': code, 'path': path}
        if code[0] in ('R', 'C'):
            item['old_path'] = path
            item['path'] = _relative(os.fsdecode(fields[position]))
            position += 1
        changes.append(item)
    return changes


def _status_records(data):
    fields = data.split(b'\x00')
    result, position = [], 0
    while position < len(fields) and fields[position]:
        raw = fields[position]
        position += 1
        code, path = raw[:2].decode('ascii'), _relative(os.fsdecode(raw[3:]))
        item = {'status': code, 'path': path}
        if 'R' in code or 'C' in code:
            item['old_path'] = _relative(os.fsdecode(fields[position]))
            position += 1
        result.append(item)
    return result


def _allowed(path, patterns):
    # Segment-aware glob: '*' does not consume '/', '**' does. Match both sides of renames.
    parts = path.split('/')
    from functools import lru_cache
    @lru_cache(maxsize=16384)
    def match(names, glob):
        if not glob:
            return not names
        if glob[0] == '**':
            return match(names, glob[1:]) or bool(names and match(names[1:], glob))
        return bool(names and fnmatch.fnmatchcase(names[0], glob[0]) and match(names[1:], glob[1:]))
    return any(match(tuple(parts), tuple(pattern.split('/'))) for pattern in patterns)


def collect_inventory(ws):
    """Read-only full review inventory. Unknown content blocks the scope gate."""
    _ownership(ws)
    before = _fingerprint(ws)
    entries, problems = _scan(ws)
    by_path = {item['path']: item for item in entries}
    baseline, index, changes, status_rows, head = {}, {}, [], [], None
    if ws['kind'] == 'git_worktree':
        cwd = ws['path']
        head = _git_text(cwd, 'rev-parse', '--verify', 'HEAD')
        baseline = _tree_records(_git(cwd, 'ls-tree', '-r', '-z', '--full-tree', ws['base_commit']))
        index = _tree_records(_git(cwd, 'ls-files', '--stage', '-z'), index=True)
        changes = _name_changes(_git(cwd, 'diff', '--no-ext-diff', '--no-textconv', '--name-status',
                                    '-z', '--find-renames', ws['base_commit'], '--'))
        status_rows = _status_records(_git(cwd, 'status', '--porcelain=v1', '-z',
                                          '--untracked-files=all', '--ignore-submodules=none'))
    changed = {item['path'] for item in changes + status_rows}
    changed.update(item['old_path'] for item in changes + status_rows if 'old_path' in item)
    for path in sorted(set(by_path) | set(baseline) | set(index)):
        row = by_path.get(path)
        if row is None:
            row = dict(path=path, kind='deleted', bytes=0, sha256=None)
            entries.append(row)
        if path in baseline:
            row['base'] = baseline[path]
        if path in index:
            row['index'] = index[path]
        # With core.symlinks=false Git checks a symlink out as an ordinary text
        # file on Windows. Its target still needs review; it is not a safe blob.
        if row['kind'] == 'file' and (baseline.get(path, {}).get('mode') == '120000' or
                any(item['mode'] == '120000' for item in index.get(path, []))):
            row['unknown'] = 'git_symlink_requires_review'
            problems.append(path + ': git_symlink_requires_review')
        row['tracked'] = path in baseline or path in index
        if row['kind'] != 'directory' and (not row['tracked'] or ws['kind'] == 'directory'):
            changed.add(path)
        if (baseline.get(path, {}).get('mode') == '160000' or
                any(item['mode'] == '160000' for item in index.get(path, []))):
            row['unknown'] = 'submodule_requires_separate_inventory'
            problems.append(path + ': submodule_requires_separate_inventory')
        if any(item['stage'] != 0 for item in index.get(path, [])):
            problems.append(path + ': unmerged_index')
        row['changed'] = path in changed
    after = _fingerprint(ws)
    if before != after:
        raise WorkspaceError('workspace changed during inventory; snapshot unavailable')
    undeclared = sorted(path for path in changed if not _allowed(path, ws['allowed_paths']))
    return dict(format=1, workspace_id=ws['workspace_id'], assignment_id=ws['assignment_id'],
                task_id=ws['task_id'], host=ws['host'], kind=ws['kind'], repository_id=ws['repository_id'],
                base_commit=ws['base_commit'], head=head, allowed_paths=ws['allowed_paths'],
                entries=sorted(entries, key=lambda row: row['path']), changes=changes, status=status_rows,
                changed_paths=sorted(changed), undeclared_paths=undeclared, problems=sorted(set(problems)),
                scope_ok=not undeclared and not problems,
                dirty=bool(status_rows or any(not row['tracked'] and row['kind'] != 'directory'
                                              for row in entries)), fingerprint=after)


def inspect_workspace(ws):
    """External read-only inspection; absence/ownership never implies old writer exit."""
    report = dict(workspace_id=ws['workspace_id'], spec_sha256=ws['spec_sha256'], revision=ws['revision'],
                  operation_token=ws['operation_token'], ok=False)
    try:
        if not os.path.lexists(ws['path']):
            return dict(report, status='workspace_lost', error='workspace path missing')
        inventory = collect_inventory(ws)
        if ws['kind'] == 'git_worktree':
            common = _absolute(_git_text(ws['path'], 'rev-parse', '--path-format=absolute', '--git-common-dir'))
            if common != ws['common_git_dir']:
                raise WorkspaceError('common Git directory differs')
            # Registration and ancestry are independent of marker content.
            listing = _git(ws['repo_root'], 'worktree', 'list', '--porcelain', '-z')
            listed = [os.fsdecode(item[9:]) for item in listing.split(b'\x00') if item.startswith(b'worktree ')]
            if not any(_canon(path) == ws['path'] for path in listed):
                raise WorkspaceError('workspace is not registered with Git')
            _git(ws['path'], 'merge-base', '--is-ancestor', ws['base_commit'], 'HEAD')
        return dict(report, ok=True, status='inspected', dirty=inventory['dirty'], head=inventory['head'],
                    inventory=inventory, fingerprint=inventory['fingerprint'])
    except (OSError, ValueError) as exc:
        return dict(report, status='quarantined', error=str(exc))


def _validate_report(ws, report):
    if any(report.get(key) != ws[key] for key in ('workspace_id', 'spec_sha256', 'operation_token', 'revision')):
        raise WorkspaceError('stale or mismatched inspection report')
    if report.get('ok') is not True or report.get('fingerprint') != _fingerprint(ws):
        raise WorkspaceError('inspection failed or materialization changed')


def finalize_workspace(con, workspace_id, report):
    """Caller transaction; no subprocess. Inspect again externally if this rejects."""
    _transaction(con)
    ws = _required(con, workspace_id)
    if ws['status'] != 'provisioning' or ws['operation_kind'] != 'create' or not ws['operation_started']:
        raise WorkspaceError('no consumed provisioning operation')
    if any(report.get(key) != ws[key] for key in ('workspace_id', 'spec_sha256', 'operation_token', 'revision')):
        raise WorkspaceError('stale/mismatched provisioning report')
    error = report.get('error')
    try:
        if report.get('creation_completed') is not True:
            raise WorkspaceError('recovered inspection requires explicit quiescence, not create finalization')
        _validate_report(ws, report)
        if (report.get('dirty') is not False or report.get('head') != ws['base_commit'] or
                report['inventory']['problems']):
            raise WorkspaceError('materialization is dirty/unknown or differs from base')
    except (OSError, ValueError) as exc:
        error = error or str(exc)
    status = 'quarantined' if error else 'ready'
    con.execute('UPDATE v2_workspace SET status=?,fingerprint_json=?,error=?,ready_at=?,revision=revision+1 '
                'WHERE workspace_id=?', (status, _json(report['fingerprint']) if not error else None,
                                         error, _now() if not error else None, workspace_id))
    ws = _required(con, workspace_id)
    _journal(con, ws, 'provision_' + status, {'error': error})
    return ws


def check_workspace_ready(con, workspace_id, *, assignment_id=None, host=None, allow_active=False):
    """Read-only final scheduler gate, usable INSIDE its reservation transaction.

    No Git subprocess, migration, BEGIN, commit or state mutation. Bounded local
    content/metadata fingerprint revalidation detects missing/dirty/replaced trees.
    The scheduler still owns authorization, dependency materialization and resources.
    Only explicit allow_active=True also permits active while Assignment remains
    reserved/blocked. The caller must verify its persisted attempted send intent
    before opting in; this is not retry authorization or proof of quiescence.
    """
    result = dict(ready=False, workspace_id=workspace_id, reasons=[])
    try:
        if type(allow_active) is not bool:
            raise WorkspaceError('allow_active must be a boolean')
        ws = _required(con, workspace_id)
        if ws['status'] != 'ready' and not (allow_active and ws['status'] == 'active'):
            raise WorkspaceError('workspace_not_ready:' + ws['status'])
        if assignment_id is not None and assignment_id != ws['assignment_id']:
            raise WorkspaceError('workspace Assignment mismatch')
        if host is not None and host != ws['host']:
            raise WorkspaceError('workspace host mismatch')
        rows = _rows(con, 'SELECT workspace_id,state FROM v2_assignment WHERE assignment_id=?',
                     (ws['assignment_id'],))
        if not rows or rows[0]['workspace_id'] != workspace_id or rows[0]['state'] not in ('reserved', 'blocked'):
            raise WorkspaceError('Assignment no longer reservable or binding changed')
        _claim_conflicts(con, ws)
        fingerprint = _fingerprint(ws)
        if fingerprint != json.loads(ws['fingerprint_json'] or 'null') or fingerprint['problems']:
            raise WorkspaceError('workspace materialization changed since ready')
        result.update(ready=True, path=ws['path'], assignment_id=ws['assignment_id'], host=ws['host'],
                      revision=ws['revision'], base_commit=ws['base_commit'])
    except (OSError, ValueError, sqlite3.Error) as exc:
        result['reasons'].append(str(exc))
    return result


def mark_workspace_active(con, workspace_id):
    _transaction(con)
    check = check_workspace_ready(con, workspace_id)
    if not check['ready']:
        raise WorkspaceError('; '.join(check['reasons']))
    con.execute("UPDATE v2_workspace SET status='active',quiescence_json=NULL,revision=revision+1 "
                'WHERE workspace_id=?', (workspace_id,))
    ws = _required(con, workspace_id)
    _journal(con, ws, 'active', {'writer_quiescence': 'unknown'})
    return ws


def _quiescence(ws, evidence):
    # This is a service-attested contract, not authentication. Service must verify
    # controller binding/epoch and process/host fencing before passing the evidence.
    if not isinstance(evidence, dict) or evidence.get('verified') is not True:
        raise WorkspaceError('controller-verified quiescence evidence required')
    for key in ('workspace_id', 'assignment_id', 'host', 'revision'):
        if evidence.get(key) != ws[key]:
            raise WorkspaceError('stale/mismatched quiescence: ' + key)
    if not evidence.get('controller') or not evidence.get('reason'):
        raise WorkspaceError('quiescence must identify controller and observation')
    path = _absolute(evidence.get('evidence_path'))
    if _inside(path, ws['path']) or _sha(_read_bytes(path)) != evidence.get('evidence_sha256'):
        raise WorkspaceError('quiescence evidence missing, changed, or inside workspace')
    return evidence


def recover_workspace(con, workspace_id, report, *, quiescence=None):
    """Apply explicit inspection, retain claims; never retry or delete external work."""
    _transaction(con)
    ws = _required(con, workspace_id)
    if ws['status'] == 'released':
        raise WorkspaceError('released workspace history is immutable')
    if any(report.get(key) != ws[key] for key in ('workspace_id', 'spec_sha256', 'operation_token', 'revision')):
        raise WorkspaceError('stale recovery report')
    status, fingerprint = 'quarantined', None
    error = report.get('error')
    if not os.path.lexists(ws['path']):
        status, error = 'workspace_lost', 'workspace missing; writer quiescence unknown'
    elif report.get('ok'):
        _validate_report(ws, report)
        assignment = _rows(con, 'SELECT state FROM v2_assignment WHERE assignment_id=?', (ws['assignment_id'],))
        if quiescence is None:
            status = 'active' if ws['status'] == 'active' else 'orphaned'
            error = 'ownership does not prove writer quiescence'
        else:
            _quiescence(ws, quiescence)
            status = 'frozen'
            if (assignment and assignment[0]['state'] in ('reserved', 'blocked') and
                    not report['dirty'] and not report['inventory']['problems'] and
                    report['head'] == ws['base_commit']):
                status = 'ready'
                fingerprint = _json(report['fingerprint'])
            error = None
    con.execute('UPDATE v2_workspace SET status=?,error=?,fingerprint_json=?,quiescence_json=?,'
                'revision=revision+1 WHERE workspace_id=?',
                (status, error, fingerprint, _json(quiescence) if quiescence else None, workspace_id))
    ws = _required(con, workspace_id)
    _journal(con, ws, 'recovered', {'status': status, 'error': error})
    return ws


def _cas(root, data):
    digest = _sha(data)
    directory = Path(root) / 'sha256' / digest[:2]
    _no_links(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _no_links(directory)
    destination = directory / digest
    if destination.exists():
        if _sha(_read_bytes(destination, MAX_TOTAL_BYTES)) != digest:
            raise WorkspaceError('existing CAS blob is corrupt')
        return digest
    temporary = directory / ('.pending-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Atomic publication without replacing concurrent/foreign existing content.
            os.link(temporary, destination)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    _sync_dir(directory)
    if _sha(_read_bytes(destination, MAX_TOTAL_BYTES)) != digest:
        raise WorkspaceError('CAS publication verification failed')
    return digest


def snapshot_workspace(ws, evidence_root):
    """External immutable CAS snapshot. Unknown/large content blocks sealing."""
    root = _absolute(evidence_root)
    protected = [ws['path']] + ([ws['repo_root'], ws['common_git_dir']] if ws['kind'] == 'git_worktree' else [])
    if any(paths_overlap(root, value) for value in protected):
        raise WorkspaceError('evidence store must be outside workspace and repository')
    inventory = collect_inventory(ws)
    if inventory['problems']:
        raise WorkspaceError('snapshot_unavailable: ' + '; '.join(inventory['problems']))
    Path(root).mkdir(parents=True, exist_ok=True)
    for row in inventory['entries']:
        if row['kind'] == 'file':
            data = _read_bytes(Path(ws['path']) / row['path'])
            if _sha(data) != row['sha256']:
                raise WorkspaceError('file changed before archival')
            _cas(root, data)
    patches = {}
    if ws['kind'] == 'git_worktree':
        commands = {
            'base_to_worktree': [ws['base_commit']],
            'staged': ['--cached', ws['base_commit']],
            'unstaged': [],
            'committed': [ws['base_commit'], 'HEAD'],
        }
        for name, args in commands.items():
            data = _git(ws['path'], 'diff', '--no-ext-diff', '--no-textconv', '--binary', '--full-index',
                        '--find-renames', *args, '--')
            patches[name] = _cas(root, data)
    after = collect_inventory(ws)
    if after != inventory:
        raise WorkspaceError('snapshot_unavailable: workspace changed during snapshot')
    manifest = dict(format=1, inventory=inventory, patches=patches,
                    resource_claims=ws['resource_claims'], harness=ws['harness'])
    digest = _cas(root, _json(manifest).encode('ascii'))
    return dict(workspace_id=ws['workspace_id'], evidence_root=root, manifest_sha256=digest,
                manifest_path=str(Path(root) / 'sha256' / digest[:2] / digest),
                scope_ok=inventory['scope_ok'], dirty=inventory['dirty'],
                inventory_sha256=_digest(inventory))


def verify_snapshot(snapshot):
    """Read-only CAS integrity check; does not accept evidence or migrate a DB."""
    root = _absolute(snapshot['evidence_root'])
    digest = snapshot['manifest_sha256']
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise WorkspaceError('invalid manifest digest')
    path = Path(root) / 'sha256' / digest[:2] / digest
    raw = _read_bytes(path, MAX_TOTAL_BYTES)
    if _sha(raw) != digest:
        raise WorkspaceError('snapshot_unavailable: manifest hash mismatch')
    manifest = json.loads(raw)
    hashes = list(manifest['patches'].values())
    hashes += [row['sha256'] for row in manifest['inventory']['entries'] if row['kind'] == 'file']
    for item in hashes:
        if not isinstance(item, str) or not re.fullmatch('[0-9a-f]{64}', item):
            raise WorkspaceError('invalid content digest')
        if _sha(_read_bytes(Path(root) / 'sha256' / item[:2] / item, MAX_TOTAL_BYTES)) != item:
            raise WorkspaceError('snapshot_unavailable: content hash mismatch')
    return manifest


def begin_release(con, workspace_id, report, snapshot, *, quiescence):
    """Caller transaction: reserve cleanup only for owned, clean, snapshotted work."""
    _transaction(con)
    ws = _required(con, workspace_id)
    if ws['status'] in ('requested', 'provisioning', 'released'):
        raise WorkspaceError('workspace is not releasable')
    _validate_report(ws, report)
    _quiescence(ws, quiescence)
    manifest = verify_snapshot(snapshot)
    if (snapshot.get('workspace_id') != workspace_id or manifest['inventory'] != report['inventory'] or
            not manifest['inventory']['scope_ok']):
        raise WorkspaceError('snapshot identity/content/scope differs')
    if report['dirty']:
        raise WorkspaceError('dirty workspace retained; no forced cleanup')
    # Directory mode can only remove an empty owned directory. All noncode outputs
    # are retained; explicit archival/review does not authorize arbitrary deletion.
    if ws['kind'] == 'directory' and report['inventory']['entries']:
        raise WorkspaceError('nonempty directory retained')
    con.execute("UPDATE v2_workspace SET status='frozen',operation_kind='release',operation_token=?,"
                'operation_started=0,quiescence_json=?,snapshot_json=?,revision=revision+1 WHERE workspace_id=?',
                (secrets.token_hex(24), _json(quiescence), _json(snapshot), workspace_id))
    ws = _required(con, workspace_id)
    _journal(con, ws, 'release_reserved', {'snapshot': snapshot['manifest_sha256']})
    return ws


def release_workspace(database, workspace_id, operation_token):
    """After commit; no --force, branch deletion, prune or recursive directory erase."""
    ws = _consume_ticket(database, workspace_id, operation_token, 'release')
    result = dict(workspace_id=workspace_id, operation_token=operation_token, revision=ws['revision'],
                  spec_sha256=ws['spec_sha256'], ok=False)
    try:
        snapshot = json.loads(ws['snapshot_json'])
        manifest = verify_snapshot(snapshot)
        report = inspect_workspace(ws)
        _validate_report(ws, report)
        if report['dirty'] or manifest['inventory'] != report['inventory']:
            raise WorkspaceError('workspace changed since release reservation')
        proof = json.loads(ws['quiescence_json'])
        if _sha(_read_bytes(proof['evidence_path'])) != proof['evidence_sha256']:
            raise WorkspaceError('quiescence evidence changed')
        if ws['kind'] == 'git_worktree':
            _git(ws['repo_root'], 'worktree', 'remove', '--', ws['path'])
            listing = _git(ws['repo_root'], 'worktree', 'list', '--porcelain', '-z')
            listed = [os.fsdecode(item[9:]) for item in listing.split(b'\x00') if item.startswith(b'worktree ')]
            if any(_canon(path) == ws['path'] for path in listed):
                raise WorkspaceError('worktree remains registered after remove')
        else:
            if report['inventory']['entries']:
                raise WorkspaceError('directory is nonempty')
            marker = _marker_path(ws)
            payload = _read_bytes(marker, 65536)
            marker.unlink()
            try:
                Path(ws['path']).rmdir()
            except OSError:
                # Do not restore ownership on a replaced directory.
                expected = json.loads(payload)['directory_identity']
                if _identity(ws['path']) == expected and not os.path.lexists(marker):
                    _exclusive_json(marker, json.loads(payload))
                raise
        if os.path.lexists(ws['path']):
            raise WorkspaceError('path still exists after removal')
        return dict(result, ok=True)
    except (OSError, ValueError) as exc:
        return dict(result, error=str(exc))


def finalize_release(con, workspace_id, report):
    _transaction(con)
    ws = _required(con, workspace_id)
    if (ws['status'] != 'frozen' or ws['operation_kind'] != 'release' or not ws['operation_started'] or
            any(report.get(key) != ws[key] for key in ('workspace_id', 'spec_sha256', 'operation_token', 'revision'))):
        raise WorkspaceError('stale/mismatched release result')
    ok = report.get('ok') is True and not os.path.lexists(ws['path'])
    status = 'released' if ok else 'quarantined'
    con.execute('UPDATE v2_workspace SET status=?,released_at=?,error=?,revision=revision+1 WHERE workspace_id=?',
                (status, _now() if ok else None, None if ok else report.get('error', 'release uncertain'), workspace_id))
    ws = _required(con, workspace_id)
    _journal(con, ws, 'release_' + status, {'branch_retained': ws['branch']})
    return ws
