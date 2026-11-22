"""Project application roles for native scheduler contracts (not service credentials)."""
import argparse
import json
import re
import sqlite3
import sys
import uuid

import awo_service as service
import service_access as access


def create_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS v2_project_role_grant (
        project_id TEXT NOT NULL REFERENCES v2_project(project_id),
        agent_id TEXT NOT NULL REFERENCES v2_agent(agent_id),
        role_key TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
        revision INTEGER NOT NULL CHECK(revision > 0),
        PRIMARY KEY(project_id,agent_id,role_key))""")


def _role(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", value):
        raise service.ServiceError("Invalid project role key")
    return value


def _record(con, project, agent, role):
    if not service._table_exists(con, "v2_project_role_grant"):
        raise service.ServiceError("Project role schema missing; run schema-upgrade")
    row = con.execute("SELECT active,revision FROM v2_project_role_grant "
                      "WHERE project_id=? AND agent_id=? AND role_key=?", (project, agent, _role(role))).fetchone()
    return (bool(row[0]), row[1]) if row else (False, 0)


def require(con, project, agent, role, revision=None):
    active, current = _record(con, project, agent, role)
    if not active or (revision is not None and current != revision):
        raise service.ServiceError("Required project role is absent, revoked or its grant revision changed")
    return current


def check_contract(con, project, contract):
    if "required_project_role" in contract:
        if type(contract.get("project_role_revision")) is not int:
            raise service.ServiceError("Queued project role lacks a pinned grant revision")
        require(con, project, contract["agent_id"], contract["required_project_role"], contract["project_role_revision"])


def operate(state, root, token, action, agent, role, *, epoch=None, expected_revision=None, command_id=None):
    if action not in ("grant", "revoke", "check"):
        raise service.ServiceError("Unknown role action")
    write = action != "check"
    con = service._connect(service._db_path(state), readonly=not write)
    try:
        con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        principal = access.authenticate(con, root, token)
        if principal["role"] != "controller" or any(principal.get(k) for k in ("task_id", "assignment_id", "knowledge_id")):
            raise service.ServiceError("Project-wide Controller credential required")
        project = principal["project_id"]
        target = con.execute("SELECT role,lifecycle FROM v2_agent WHERE agent_id=? AND project_id=?",
                             (agent, project)).fetchone()
        if not target or target["role"] != "worker":
            raise service.ServiceError("Target must be a registered worker in this project")
        active, rev = _record(con, project, agent, role)
        result = dict(project_id=project, agent_id=agent, role=role, active=active, revision=rev)
        if not write:
            result["eligible"] = active and target["lifecycle"] == "active"
            return result
        env = service._command_envelope(dict(command_id=command_id or "role-" + uuid.uuid4().hex,
            tool="project.role." + action, expected_epoch=epoch,
            payload=dict(agent_id=agent, role=role, expected_revision=expected_revision)),
            ("project.role.grant", "project.role.revoke"))
        current = service._state_row(con)
        if current["frozen"] or not current["v2_enabled"] or current["controller_epoch"] != epoch:
            raise service.ServiceError("Controller epoch stale or service disabled/frozen")
        sha = service._request_sha(env)
        actor = principal["principal_id"]
        replay = service._replay(con, project, actor, env, sha)
        if replay is not None:
            return replay
        if type(expected_revision) is not int or expected_revision != rev:
            raise service.ServiceError("Project role revision changed; check it first")
        if action == "grant" and target["lifecycle"] != "active":
            raise service.ServiceError("Target worker is not active")
        if active == (action == "grant"):
            raise service.ServiceError("Role already in requested state")
        result.update(active=(action == "grant"), revision=rev + 1)
        con.execute("INSERT INTO v2_project_role_grant(project_id,agent_id,role_key,active,revision) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(project_id,agent_id,role_key) DO UPDATE SET "
                    "active=excluded.active,revision=excluded.revision", (project, agent, role, result["active"], rev + 1))
        result["event_id"] = service._event(con, project, "agent", agent, env["tool"], principal["binding_id"],
                                            "v2", None, dict(role=role, revision=rev + 1, epoch=epoch))
        result.update(replayed=False, command_id=env["command_id"])
        service._record(con, project, actor, env, sha, result)
        con.commit()
        return result
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "grant", "revoke"))
    for name in ("state", "project-root", "token-file", "agent", "role"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--epoch", type=int)
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--command-id")
    args = parser.parse_args(argv)
    try:
        result = operate(args.state, args.project_root, service._read_secret_file(args.token_file, "Controller token"),
                         args.action, args.agent, args.role, epoch=args.epoch, expected_revision=args.expected_revision,
                         command_id=args.command_id)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
