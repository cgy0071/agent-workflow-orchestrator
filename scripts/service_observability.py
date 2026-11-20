"""Authenticated read-only runtime views and audit receipts (P2-4 integration).

The Dashboard uses the same projection through its existing page token; this
module is the command-line/service boundary for callers that hold a scoped
application access token. Nothing here schedules, reviews, dispatches or writes
runtime state. Receipt export is explicitly opt-in, creates one new isolated
local Git repository, and never contacts a network.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import awo_service as service
import receipt_export
import runtime_views
import service_access as access

READ_ROLES = ('controller', 'observer')
MAX_LIMIT = 1000


def _fail(message):
    raise service.ServiceError(message)


def _reader(con, project_root, token):
    """Authenticate read-only; project-wide read scope only."""
    principal = access.authenticate(con, project_root, token)
    if principal['role'] not in READ_ROLES:
        _fail('Runtime views require a project controller or observer credential.')
    if (principal.get('task_id') or principal.get('assignment_id')
            or principal.get('knowledge_id')):
        _fail('A Task/run/knowledge-scoped credential cannot read project-wide views.')
    return principal


def _snapshot(state, project_root, token, limit):
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        _fail('limit must be an integer from 1 through ' + str(MAX_LIMIT))
    con = service._connect(service._db_path(state), readonly=True)
    try:
        con.execute('BEGIN')
        principal = _reader(con, project_root, token)
        if not service.schema_present(con):
            return {'available': False, 'reason': 'v2_ledger_unavailable', 'can_write': False}
        snapshot = runtime_views.build_runtime_views(con, principal['project_id'], limit=limit)
        snapshot['can_write'] = False
        return snapshot
    finally:
        con.close()


def views(state, project_root, token, *, limit=200):
    """Bounded read-only projection; never acquires a write transaction."""
    return _snapshot(state, project_root, token, limit)


def views_html(state, project_root, token, *, limit=200, view=None):
    """Passive escaped HTML fragment for embedding after Dashboard authentication."""
    snapshot = _snapshot(state, project_root, token, limit)
    if not snapshot.get('available', True):
        return snapshot
    snapshot['html'] = runtime_views.render_html(snapshot, view=view)
    return snapshot


def export(state, project_root, token, destination, *, enabled=False, protected_roots):
    """Opt-in export of one sanitized receipt into a brand-new isolated Git repo."""
    con = service._connect(service._db_path(state), readonly=True)
    try:
        con.execute('BEGIN')
        principal = _reader(con, project_root, token)
        if not service.schema_present(con):
            _fail('v2 ledger is unavailable; there is no receipt to export')
        receipt = receipt_export.build_receipt(con, principal['project_id'])
    finally:
        con.close()
    return receipt_export.export_receipt(receipt, destination, enabled=enabled is True,
                                        protected_roots=protected_roots)


def verify(state, project_root, token, repository, *, expected_manifest_sha256=None,
           expected_commit=None):
    """Authenticated read-only verification; performs no checkout or repair."""
    con = service._connect(service._db_path(state), readonly=True)
    try:
        con.execute('BEGIN')
        _reader(con, project_root, token)
    finally:
        con.close()
    return receipt_export.verify_receipt(repository, expected_manifest_sha256=expected_manifest_sha256,
                                         expected_commit=expected_commit)


def rebuild(state, project_root, token, repository, audit_db, *, expected_manifest_sha256=None,
            expected_commit=None):
    """Rebuild sanitized audit tables into a NEW empty SQLite file only."""
    con = service._connect(service._db_path(state), readonly=True)
    try:
        con.execute('BEGIN')
        _reader(con, project_root, token)
    finally:
        con.close()
    target = receipt_export.rebuild_target(repository, audit_db)
    if target.exists():
        _fail('Rebuild destination already exists; audit rebuild never replaces a database')
    if not target.parent.is_dir():
        _fail('Rebuild destination parent directory does not exist')
    # Verify before creating anything: a refused repository must not leave an
    # empty database file behind, especially inside the receipt repository.
    receipt_export.verify_receipt(repository, expected_manifest_sha256=expected_manifest_sha256,
                                  expected_commit=expected_commit)
    destination = sqlite3.connect(str(target), timeout=15, isolation_level=None)
    try:
        destination.row_factory = sqlite3.Row
        destination.execute('PRAGMA synchronous=FULL')
        destination.execute('BEGIN IMMEDIATE')
        try:
            result = receipt_export.rebuild_receipt(
                destination, repository, expected_manifest_sha256=expected_manifest_sha256,
                expected_commit=expected_commit)
            destination.commit()
        except BaseException:
            destination.rollback()
            raise
        return result
    finally:
        destination.close()


def _file(path, limit):
    with Path(path).open('rb') as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        _fail('Input file exceeds its byte limit')
    return value


def _secret(path, label):
    return service._read_secret_file(Path(path), label)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description='Authenticated read-only runtime views and sanitized audit receipts.')
    parser.add_argument('--state', required=True)
    sub = parser.add_subparsers(dest='command', required=True)

    for name in ('views', 'views-html'):
        cmd = sub.add_parser(name, help='bounded read-only runtime projection')
        cmd.add_argument('--project-root', required=True)
        cmd.add_argument('--token-file', required=True)
        cmd.add_argument('--limit', type=int, default=200)
        if name == 'views-html':
            cmd.add_argument('--view', choices=runtime_views.VIEW_NAMES)

    export_cmd = sub.add_parser('receipt-export', help='opt-in export to a new isolated local Git repo')
    export_cmd.add_argument('--project-root', required=True)
    export_cmd.add_argument('--token-file', required=True)
    export_cmd.add_argument('--destination', required=True)
    export_cmd.add_argument('--enable-export', action='store_true',
                            help='explicitly allow creating the new local receipt repository')
    export_cmd.add_argument('--protected-root', action='append', required=True,
                            help='source or runtime root the destination must not overlap')

    verify_cmd = sub.add_parser('receipt-verify', help='read-only receipt repository verification')
    verify_cmd.add_argument('--project-root', required=True)
    verify_cmd.add_argument('--token-file', required=True)
    verify_cmd.add_argument('--repository', required=True)
    verify_cmd.add_argument('--manifest-sha256')
    verify_cmd.add_argument('--commit')

    rebuild_cmd = sub.add_parser('receipt-rebuild', help='rebuild into a new empty audit database')
    rebuild_cmd.add_argument('--project-root', required=True)
    rebuild_cmd.add_argument('--token-file', required=True)
    rebuild_cmd.add_argument('--repository', required=True)
    rebuild_cmd.add_argument('--audit-db', required=True)
    rebuild_cmd.add_argument('--manifest-sha256')
    rebuild_cmd.add_argument('--commit')

    args = parser.parse_args(argv)
    try:
        token = _secret(args.token_file, 'access')
        if args.command == 'views':
            result = views(args.state, args.project_root, token, limit=args.limit)
        elif args.command == 'views-html':
            result = views_html(args.state, args.project_root, token, limit=args.limit, view=args.view)
        elif args.command == 'receipt-export':
            if not args.enable_export:
                _fail('Receipt export requires the explicit --enable-export opt-in.')
            result = export(args.state, args.project_root, token, args.destination,
                            enabled=True, protected_roots=args.protected_root)
        elif args.command == 'receipt-verify':
            result = verify(args.state, args.project_root, token, args.repository,
                            expected_manifest_sha256=args.manifest_sha256,
                            expected_commit=args.commit)
        else:
            result = rebuild(args.state, args.project_root, token, args.repository, args.audit_db,
                             expected_manifest_sha256=args.manifest_sha256, expected_commit=args.commit)
    except (service.ServiceError, OSError, sqlite3.Error, ValueError) as exc:
        print('service_observability: ' + str(exc))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
