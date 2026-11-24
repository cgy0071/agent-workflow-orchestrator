"""Schedule multiple tasks using an existing project controller and ledger."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

import awo_service as service
from mcp_gateway import read_token_file
import priority_scheduler as scheduler
import project_domain


def _controller(con, state, project_root, token, expected_project_id):
    if not expected_project_id:
        raise service.ServiceError("Expected project ID is required")
    principal, _ = project_domain.bound(con, state, project_root, token, expected_project_id)
    if principal["role"] != "controller":
        raise service.ServiceError("A project-wide Controller credential is required")
    return principal["binding_id"], principal["binding_version"], "main"


def _authority(state, project_root, token, expected_project_id):
    def authorize(con):
        return _controller(con, state, project_root, token, expected_project_id)
    con = service._connect(service._db_path(state), readonly=True)
    try:
        con.execute("BEGIN")
        actor = authorize(con)
    finally:
        con.close()
    # Recheck the same bearer, binding, and project under each actual writer lock.
    return actor, authorize


def command(state, project_root, token, expected_project_id, envelope):
    env = service._command_envelope(envelope, ("task.create", "scheduler.enqueue", "scheduler.claim"))
    payload = env["payload"]
    if not service.control.valid_task_id(payload.get("task_id")):
        raise service.ServiceError("A stable task ID is required")
    if env["tool"] == "scheduler.enqueue":
        request = payload.get("request")
        cwd = request.get("cwd") if isinstance(request, dict) else None
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).resolve().is_relative_to(
                Path(project_root).resolve()):
            raise service.ServiceError("Task working directory must be inside this project")
    actor, authorize = _authority(state, project_root, token, expected_project_id)
    return service.execute_command(state, *actor, env, authorize=authorize)


def dispatch(state, project_root, token, expected_project_id, assignment_id, epoch):
    actor, authorize = _authority(state, project_root, token, expected_project_id)
    return scheduler.dispatch_assignment(state, assignment_id, *actor, expected_epoch=epoch, authorize=authorize)


def reconcile(state, project_root, token, expected_project_id, assignment_id, epoch):
    actor, authorize = _authority(state, project_root, token, expected_project_id)
    return scheduler.reconcile_assignment(state, assignment_id, *actor, expected_epoch=epoch, authorize=authorize)


def status(state, project_root, token, expected_project_id, task_id):
    con = service._connect(service._db_path(state), readonly=True)
    try:
        con.execute("BEGIN")
        _controller(con, state, project_root, token, expected_project_id)
        task = scheduler._task(con, expected_project_id, task_id)
        assignments = [dict(row) for row in con.execute(
            """SELECT a.assignment_id,a.generation,a.slot_id,a.state,a.review_status,
                      a.holds_slot,i.send_state,i.run_id
               FROM v2_assignment a LEFT JOIN v2_scheduler_intent i USING(assignment_id)
               WHERE a.task_id=? ORDER BY a.generation,a.slot_id""", (task_id,))]
        return {"project_id": expected_project_id, "task_id": task_id, "revision": task["revision"],
                "task_state": task["task_state"], "accepted_evidence_id": task["accepted_evidence_id"],
                "assignments": assignments}
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "project-root", "token-file", "expected-project-id"):
        parser.add_argument("--" + name, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("command").add_argument("--file", type=Path, required=True)
    actions.add_parser("status").add_argument("--task", required=True)
    for name in ("dispatch", "reconcile"):
        action = actions.add_parser(name)
        action.add_argument("--assignment", required=True)
        action.add_argument("--epoch", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        binding = (args.state, args.project_root, read_token_file(args.token_file), args.expected_project_id)
        if args.action == "command":
            result = command(*binding, json.loads(args.file.read_text(encoding="utf-8-sig")))
        elif args.action == "dispatch":
            result = dispatch(*binding, args.assignment, args.epoch)
        elif args.action == "reconcile":
            result = reconcile(*binding, args.assignment, args.epoch)
        else:
            result = status(*binding, args.task)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
