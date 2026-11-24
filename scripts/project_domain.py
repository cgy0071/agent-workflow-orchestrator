"""Resolve a project's stable identity from its existing AWO control state."""
import argparse
import json
import sqlite3
import sys

import awo_service as service
import service_access as access
from mcp_gateway import read_token_file


def lookup(state, project_root, token, expected_project_id=None):
    """Resolve one explicit binding. A successful lookup grants no new scope."""
    con = service._connect(service._db_path(state), readonly=True)
    try:
        con.execute("BEGIN")
        principal = access.authenticate(con, project_root, token)
        if (principal["role"] not in ("observer", "worker", "reviewer", "controller")
                or any(principal.get(k) for k in ("task_id", "assignment_id", "knowledge_id"))):
            raise service.ServiceError("Project-wide credential required")
        project_id = principal["project_id"]
        if project_id != service._project_id(con) or (
                expected_project_id is not None and project_id != expected_project_id):
            raise service.ServiceError("Project identity mismatch")
        row = con.execute("""SELECT p.root_path AS state_path, b.root_path, b.root_sha256, b.revision
                             FROM v2_project p JOIN v2_project_content_root b USING(project_id)
                             WHERE p.project_id=?""", (project_id,)).fetchone()
        if row is None:
            raise service.ServiceError("Controller must bind the project content root first")
        if (access.canonical_root(state) != access.canonical_root(row["state_path"])
                or access.root_hash(project_root) != row["root_sha256"]
                or access.root_hash(row["root_path"]) != row["root_sha256"]):
            raise service.ServiceError("Project root or state binding mismatch")
        return {"project_id": project_id, "root_sha256": row["root_sha256"],
                "binding_revision": row["revision"]}
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "project-root", "token-file"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--expected-project-id", help="Refuse a different ledger identity")
    args = parser.parse_args(argv)
    try:
        result = lookup(args.state, args.project_root, read_token_file(args.token_file),
                        args.expected_project_id)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
