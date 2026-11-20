"""Separate local Owner credentials for explicit supervision decisions.

Bootstrap is an interactive local-administrator operation, never an Agent or
MCP route. Same-OS-user code is outside this bearer-credential security boundary.
"""
import argparse
import json
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
import uuid

import awo_service as service
import service_access as access
from service_api import APIError, _error_code


def create_owner_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS v2_owner_credential (
      credential_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL UNIQUE,
      project_id TEXT NOT NULL, principal_id TEXT NOT NULL, auth_generation INTEGER NOT NULL,
      root_path TEXT NOT NULL, root_sha256 TEXT NOT NULL, expires_at INTEGER NOT NULL,
      created_at INTEGER NOT NULL, revoked_at INTEGER,
      FOREIGN KEY(project_id,principal_id) REFERENCES
        v2_supervision_owner_identity(project_id,principal_id))''')


def setup(state, actor, version, context, project_root, owner_id, token_out, *,
          expected_generation=0, minutes=480, linked_agent_id=None):
    """Trusted local bootstrap; the CLI requires terminal confirmation first."""
    import supervision
    if (not isinstance(owner_id, str) or not re.fullmatch('[A-Za-z0-9._:-]{1,100}', owner_id)
            or type(expected_generation) is not int or expected_generation < 0
            or type(minutes) is not int or not 1 <= minutes <= 1440):
        raise APIError('invalid_arguments', 'Invalid Owner identity, generation or expiry')
    root = access.canonical_root(project_root)
    con = service._registry_open(state)
    target = None
    try:
        service._controller(con, actor, version, context)
        current = service._state_row(con)
        service._refuse_frozen(current)
        project, principal = current['project_id'], 'owner:' + owner_id
        bound = con.execute('SELECT * FROM v2_project_content_root WHERE project_id=?', (project,)).fetchone()
        if not bound or bound['root_sha256'] != access.root_hash(root):
            raise APIError('project_mismatch', 'Bind this project content root before Owner setup')
        row = con.execute('SELECT * FROM v2_supervision_owner_identity WHERE project_id=? AND principal_id=?',
                          (project, principal)).fetchone()
        if (row['auth_generation'] if row else 0) != expected_generation:
            raise APIError('revision_conflict', 'Owner generation changed')
        # Rotation is not a way to drop an independence constraint: omitting the
        # optional flag keeps the existing linked Agent rather than clearing it.
        if linked_agent_id is None and row is not None:
            linked_agent_id = row['linked_agent_id']
        if linked_agent_id is not None and not con.execute(
                'SELECT 1 FROM v2_agent WHERE agent_id=? AND project_id=?', (linked_agent_id, project)).fetchone():
            raise APIError('invalid_arguments', 'Linked Agent must belong to this project')
        generation, now = expected_generation + 1, int(time.time())
        expires = now + minutes * 60
        secret = 'awo_owner_' + secrets.token_urlsafe(32)
        target = service._secret_file(token_out, secret)
        con.execute('INSERT INTO v2_supervision_owner_identity VALUES (?,?,?,?,?,NULL) '
                    'ON CONFLICT(project_id,principal_id) DO UPDATE SET auth_generation=excluded.auth_generation,'
                    'linked_agent_id=excluded.linked_agent_id,expires_at=excluded.expires_at,revoked_at=NULL',
                    (project, principal, generation, linked_agent_id, expires))
        con.execute('UPDATE v2_owner_credential SET revoked_at=? WHERE project_id=? AND principal_id=?',
                    (now, project, principal))
        identity = 'owner-key-' + uuid.uuid4().hex
        con.execute('INSERT INTO v2_owner_credential VALUES (?,?,?,?,?,?,?,?,?,NULL)',
                    (identity, service._secret_hash(secret), project, principal, generation, root,
                     access.root_hash(root), expires, now))
        service._event(con, project, 'owner', principal, 'owner.credential_installed', actor, 'local-owner',
                       None, {'credential_id': identity, 'generation': generation, 'expires_at': expires})
        con.commit()
        return {'status': 'OWNER_CREDENTIAL_CREATED', 'credential_id': identity, 'principal_id': principal,
                'auth_generation': generation, 'expires_at': expires, 'token_file': target}
    except BaseException:
        con.rollback()
        if target:
            service._remove_secret_file(target)
        raise
    finally:
        con.close()


def authenticate(con, project_root, token):
    import supervision
    if not isinstance(token, str) or not token.startswith('awo_owner_') or not 30 <= len(token) <= 128:
        raise APIError('unauthenticated', 'Owner credential required')
    row = con.execute('SELECT * FROM v2_owner_credential WHERE secret_hash=?',
                      (service._secret_hash(token),)).fetchone()
    if (not row or row['revoked_at'] is not None or row['expires_at'] <= int(time.time())
            or row['project_id'] != service._project_id(con)):
        raise APIError('unauthenticated', 'Owner credential is unavailable')
    if access.root_hash(project_root) != row['root_sha256'] or access.root_hash(row['root_path']) != row['root_sha256']:
        raise APIError('project_mismatch', 'Owner credential does not authorize this project root')
    supervision._owner_row(con, row['project_id'], row['principal_id'], row['auth_generation'])
    return {'role': 'owner', 'agent_id': None, 'principal_id': row['principal_id'],
            'project_id': row['project_id'], 'auth_generation': row['auth_generation'],
            'project_root': row['root_path'], 'credential_id': row['credential_id']}


def command(state, project_root, token, envelope):
    import supervision
    env = service._command_envelope(envelope, supervision.COMMANDS | {'owner.revoke'})
    con = service._registry_open(state)
    principal = None
    try:
        principal = authenticate(con, project_root, token)
        state_row = service._state_row(con)
        if env['expected_epoch'] != state_row['controller_epoch']:
            raise APIError('revision_conflict', 'Controller epoch changed')
        if state_row['frozen'] or not state_row['v2_enabled']:
            raise APIError('state_conflict', 'Application writes are frozen or disabled')
        sha = service._request_sha(env)
        replay = service._replay(con, principal['project_id'], principal['principal_id'], env, sha)
        if replay is not None:
            con.rollback()
            return replay
        if env['tool'] == 'owner.revoke':
            if set(env['payload']) != {'auth_generation'} or env['payload']['auth_generation'] != principal['auth_generation']:
                raise APIError('revision_conflict', 'Revocation must name current Owner generation')
            con.execute('UPDATE v2_supervision_owner_identity SET revoked_at=? WHERE project_id=? AND principal_id=?',
                        (int(time.time()), principal['project_id'], principal['principal_id']))
            result = {'status': 'OWNER_REVOKED', 'principal_id': principal['principal_id']}
            service._event(con, principal['project_id'], 'owner', principal['principal_id'],
                           'owner.revoked', principal['principal_id'], 'local-owner', None,
                           {'generation': principal['auth_generation']})
        else:
            result = supervision.apply_command(con, principal['project_id'], principal, env['tool'], env['payload'])
        result['replayed'] = False
        service._record(con, principal['project_id'], principal['principal_id'], env, sha, result)
        con.commit()
        return result
    except (ValueError, OSError, sqlite3.Error) as exc:
        con.rollback()
        if principal:
            try:
                con.execute('BEGIN IMMEDIATE')
                service._event(con, principal['project_id'], 'command', env['command_id'], 'owner.command_rejected',
                               principal['principal_id'], 'local-owner', None, {'tool': env['tool'], 'code': _error_code(exc)})
                con.commit()
            except sqlite3.Error:
                con.rollback()
        raise
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--project-root', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    bootstrap = sub.add_parser('setup', help='interactive local Owner credential setup; never an Agent route')
    bootstrap.add_argument('--actor', required=True)
    bootstrap.add_argument('--version', type=int, required=True)
    bootstrap.add_argument('--owner-id', required=True)
    bootstrap.add_argument('--token-out', required=True)
    bootstrap.add_argument('--expected-generation', type=int, default=0)
    bootstrap.add_argument('--minutes', type=int, default=480)
    bootstrap.add_argument('--linked-agent')
    mutate = sub.add_parser('command')
    mutate.add_argument('--token-file', required=True)
    mutate.add_argument('--envelope', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'setup':
            if not sys.stdin.isatty():
                raise APIError('forbidden_role', 'Owner setup requires a local interactive terminal')
            phrase = 'AUTHORIZE OWNER ' + args.owner_id
            print('Create/rotate a separate Owner credential for ' + str(Path(args.project_root).resolve()))
            # Windows reports the NUL device as a character device, so isatty()
            # alone can pass for a detached stdin. A closed stream is still not
            # an interactive confirmation.
            try:
                confirmation = input('Type ' + phrase + ': ')
            except EOFError:
                raise APIError('forbidden_role', 'Owner setup requires a local interactive terminal')
            if confirmation.strip() != phrase:
                raise APIError('forbidden_role', 'Owner confirmation did not match')
            result = setup(args.state, args.actor, args.version, 'main', args.project_root, args.owner_id,
                           args.token_out, expected_generation=args.expected_generation, minutes=args.minutes,
                           linked_agent_id=args.linked_agent)
        else:
            result = command(args.state, args.project_root, service._read_secret_file(args.token_file, 'Owner'),
                             service._load_json(args.envelope))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'error': {'code': _error_code(exc)}}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
