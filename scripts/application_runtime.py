"""Authenticated launch boundaries for previously committed external-effect intents."""
import json
import sqlite3

import awo_service as service
import service_access as access
from service_api import APIError


def controller(con, project_root, token, expected_epoch):
    principal = access.authenticate(con, project_root, token)
    if principal['role'] != 'controller' or any(principal.get(k) is not None for k in
            ('task_id', 'assignment_id', 'generation', 'knowledge_id')):
        raise APIError('forbidden_role', 'A project Controller credential is required')
    state = service._state_row(con)
    if type(expected_epoch) is not int or expected_epoch != state['controller_epoch']:
        raise APIError('revision_conflict', 'Controller epoch changed')
    if state['frozen'] or not state['v2_enabled']:
        raise APIError('state_conflict', 'Application writes are frozen or disabled')
    return principal


def merge_run(state, project_root, token, intent_id, expected_epoch, *, recover=False):
    """Read the committed ticket only after current authority checks; never expose it."""
    import merge_queue
    con = service._connect(service._db_path(state))
    try:
        con.execute('BEGIN IMMEDIATE')
        principal = controller(con, project_root, token, expected_epoch)
        row, bundle = merge_queue._intent(con, principal['project_id'], intent_id)
        if bundle['service_state']['controller_epoch'] != expected_epoch and not recover:
            raise APIError('revision_conflict', 'Intent belongs to an earlier Controller epoch')
        secret = row['operation_token']
        con.rollback()
    finally:
        con.close()
    # Domain runner consumes its durable ticket before any process launch. It
    # rechecks project/epoch/policy/evidence and owns no long-running transaction.
    runner = merge_queue.recover_intent if recover else merge_queue.execute_intent
    return runner(service._db_path(state), intent_id, secret)


def collaboration_run(state, project_root, token, verification_id, expected_epoch):
    import collaboration_runtime
    con = service._connect(service._db_path(state))
    try:
        con.execute('PRAGMA synchronous=FULL')
        con.execute('BEGIN IMMEDIATE')
        principal = controller(con, project_root, token, expected_epoch)
        con.rollback()
        return collaboration_runtime.run_verification(con, principal['project_id'], principal, verification_id)
    finally:
        con.close()
