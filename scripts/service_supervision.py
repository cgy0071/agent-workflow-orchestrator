"""Local signed-draft intake and Owner-approved conversion (P2-3 integration).

The protocol is ``awo.paired-draft.v1`` with local HMAC authentication only. A
received draft is an untrusted proposal that can never dispatch work; conversion
creates only a draft Task through the real service and requires an exact,
independent Owner approval. Advisor and controller_entry entry points live in
``supervision.py`` so this file stays a thin authenticated transaction wrapper.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import awo_service as service
import service_access as access
import supervision


def _principal(con, project_root, token):
    return access.authenticate(con, project_root, token)


def _load(path, limit):
    target = Path(path)
    with target.open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise service.ServiceError('Input file exceeds its byte limit')
    return raw


def import_draft(state, project_root, token, envelope, key):
    """Authenticated local import of one signed paired draft (write transaction)."""
    if not isinstance(key, bytes) or not 32 <= len(key) <= 128:
        raise service.ServiceError('Pair key must contain 32..128 secret bytes')
    con = service._connect(service._db_path(state))
    try:
        con.execute('BEGIN IMMEDIATE')
        principal = _principal(con, project_root, token)
        if not service.schema_present(con):
            raise service.ServiceError('Install the v2 ledger schema before importing drafts')
        result = supervision.receive_envelope(con, principal['project_id'], principal, envelope, key)
        con.commit()
        return result
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def intent(state, project_root, token, payload):
    """Pure exact conversion intent for the independent Owner; no writes."""
    con = service._connect(service._db_path(state), readonly=True)
    try:
        con.execute('BEGIN')
        principal = _principal(con, project_root, token)
        result = supervision.conversion_intent(con, principal['project_id'], principal, payload)
        con.rollback()
        return result
    finally:
        con.close()


def convert(state, project_root, token, payload):
    """Atomically create only a draft Task after an exact Owner approval."""
    if 'approval_id' not in payload:
        raise service.ServiceError('Conversion requires the Owner approval ID')
    con = service._connect(service._db_path(state))
    try:
        con.execute('BEGIN IMMEDIATE')
        principal = _principal(con, project_root, token)
        result = supervision.convert_draft(con, principal['project_id'], principal, payload)
        con.commit()
        return result
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description='awo.paired-draft.v1 local intake; drafts never authorize dispatch.')
    parser.add_argument('--state', required=True)
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--token-file', required=True)
    sub = parser.add_subparsers(dest='command', required=True)

    receive = sub.add_parser('draft-import', help='import one locally signed proposal')
    receive.add_argument('--envelope', required=True)
    receive.add_argument('--key-file', required=True)

    convert_intent = sub.add_parser('draft-intent', help='print the exact Owner approval intent')
    convert_intent.add_argument('--request', required=True)

    convert_cmd = sub.add_parser('draft-convert', help='create a draft Task after exact Owner approval')
    convert_cmd.add_argument('--request', required=True)

    args = parser.parse_args(argv)
    try:
        token = service._read_secret_file(Path(args.token_file), 'access')
        if args.command == 'draft-import':
            raw = _load(args.envelope, supervision.MAX_ENVELOPE_BYTES)
            envelope = supervision.decode_envelope(raw)
            key = _load(args.key_file, 128)
            result = import_draft(args.state, args.project_root, token, envelope, key)
        else:
            payload = json.loads(_load(args.request, 65536).decode('utf-8'))
            handler = intent if args.command == 'draft-intent' else convert
            result = handler(args.state, args.project_root, token, payload)
    except (service.ServiceError, OSError, sqlite3.Error, ValueError) as exc:
        print('service_supervision: ' + str(exc))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
