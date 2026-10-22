"""Persistent role bindings, controller guards, inbox events, and review records."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def validate_workflow(value):
    if not isinstance(value, dict):
        raise ValueError("workflow configuration must be an object")
    roles = value.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise ValueError("roles must be a nonempty object")
    flow = value.get("workflow", {})
    if not isinstance(flow, dict):
        raise ValueError("workflow must be an object")
    for key in ("dispatch_role", "execution_role", "reviewer_role", "paper_review_role"):
        if key in flow and flow[key] not in roles:
            raise ValueError(key + " references an undefined role")
    if not isinstance(flow.get("transitions", {}), dict):
        raise ValueError("workflow.transitions must be an object")
    if not all(isinstance(k, str) and isinstance(v, str) and k and v
               for k, v in flow.get("transitions", {}).items()):
        raise ValueError("transition decisions/actions must be nonempty strings")
    for role, spec in roles.items():
        if not isinstance(role, str) or not role or not isinstance(spec, dict):
            raise ValueError("invalid role definition")
        if "permissions" in spec and (not isinstance(spec["permissions"], list) or
                not all(isinstance(x, str) for x in spec["permissions"])):
            raise ValueError("role permissions must be a list of action names")
    allowed = flow.get("continuation_verdicts", ["accept", "repair"])
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        raise ValueError("continuation_verdicts must be a list")
    return value


@contextmanager
def database(state):
    path = Path(state).resolve() / "workflow.sqlite3"
    if not path.is_file():
        raise ValueError("workflow state is not initialized: " + str(path))
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            yield con
    finally:
        con.close()


def initialize(state, config):
    validate_workflow(config)
    directory = Path(state).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    con = sqlite3.connect(directory / "workflow.sqlite3")
    try:
        con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE bindings (id TEXT PRIMARY KEY, role TEXT, version INTEGER,
          mode TEXT, policy TEXT, status TEXT, data TEXT);
        CREATE UNIQUE INDEX active_role ON bindings(role) WHERE status='active';
        CREATE UNIQUE INDEX pending_role ON bindings(role) WHERE status='pending';
        CREATE UNIQUE INDEX controller ON bindings(mode) WHERE status='active' AND mode='controller';
        CREATE TABLE tasks (id TEXT PRIMARY KEY, nonce TEXT, request_hash TEXT,
          worker TEXT, status TEXT, data TEXT);
        CREATE TABLE events (id TEXT PRIMARY KEY, task TEXT, nonce TEXT, role TEXT, status TEXT, data TEXT);
        CREATE TABLE notifications (event TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE reviews (event TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE audit (seq INTEGER PRIMARY KEY, at TEXT, action TEXT, data TEXT);
        """)
        con.execute("INSERT INTO meta VALUES ('config',?)", (json.dumps(config),))
        con.execute("INSERT INTO meta VALUES ('workflow_id',?)", (uuid.uuid4().hex,))
        con.commit()
    finally:
        con.close()
    return {"status": "INITIALIZED", "state": str(directory), "config_sha256": digest(config)}


def audit(con, action, data):
    con.execute("INSERT INTO audit(at,action,data) VALUES (?,?,?)", (stamp(), action, json.dumps(data)))


def config_of(con):
    return json.loads(con.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])


def active(con, role):
    row = con.execute("SELECT data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
    if not row:
        raise ValueError("no active binding for role: " + role)
    return json.loads(row[0])


def bind(state, role, spec, observed, mode="worker", policy="inbox_only", defer=False, reason=""):
    from monitor_task import identity_matches, validate_identity, validate_spec
    if mode not in ("controller", "reviewer", "worker", "advisor") or policy not in ("inbox_only", "exact_thread", "dedicated_window"):
        raise ValueError("invalid mode or context policy")
    validate_spec(spec, reviewer=(mode != "worker"))
    validate_identity(observed)
    if not identity_matches(observed, spec["expected_identity"]):
        raise ValueError("live identity does not match the selected binding")
    if policy == "exact_thread" and not all(observed.get(k) and spec["expected_identity"].get(k)
                                            for k in ("conversation_id", "branch_id")):
        raise ValueError("exact_thread requires observed and pinned conversation_id AND branch_id")
    if policy == "dedicated_window" and not reason.strip():
        raise ValueError("dedicated_window requires an explicit exclusive-window declaration")
    with database(state) as con:
        if role not in config_of(con)["roles"]:
            raise ValueError("undefined role")
        old = con.execute("SELECT id FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
        if old and not reason.strip(): raise ValueError("a switch needs an explicit handoff reason")
        running = con.execute("SELECT count(*) FROM tasks WHERE worker=? AND status='running'", (role,)).fetchone()[0]
        if old and running and not defer:
            raise ValueError("worker still has running/uncertain tasks; use --defer and inspect the old work")
        version = con.execute("SELECT COALESCE(MAX(version),0)+1 FROM bindings WHERE role=?", (role,)).fetchone()[0]
        record = {"instance_id": uuid.uuid4().hex, "role": role, "version": version, "mode": mode,
                  "context_policy": policy, "spec": spec, "observed_identity": observed,
                  "reason": reason, "created_at": stamp()}
        status = "pending" if defer else "active"
        if not defer:
            con.execute("UPDATE bindings SET status='retired' WHERE role=? AND status='active'", (role,))
        con.execute("INSERT INTO bindings VALUES (?,?,?,?,?,?,?)",
                    (record["instance_id"], role, version, mode, policy, status, json.dumps(record)))
        audit(con, "binding_" + status, record)
        return dict(record, status=status)


def activate(state, role, observed):
    from monitor_task import identity_matches
    with database(state) as con:
        row = con.execute("SELECT data FROM bindings WHERE role=? AND status='pending'", (role,)).fetchone()
        if not row:
            raise ValueError("no pending binding")
        if con.execute("SELECT count(*) FROM tasks WHERE worker=? AND status='running'", (role,)).fetchone()[0]:
            raise ValueError("old tasks still running or uncertain")
        value = json.loads(row[0])
        if not identity_matches(observed, value["spec"]["expected_identity"]):
            raise ValueError("pending binding failed live recheck")
        con.execute("UPDATE bindings SET status='retired' WHERE role=? AND status='active'", (role,))
        con.execute("UPDATE bindings SET status='active' WHERE id=?", (value["instance_id"],))
        audit(con, "binding_activated", value)
        return value


def guard(con, actor, version, context, action):
    if context != "main":
        raise ValueError("side or unregistered context is advisory-only")
    row = con.execute("SELECT data FROM bindings WHERE id=? AND version=? AND status='active'",
                      (actor, version)).fetchone()
    if not row:
        raise ValueError("stale or unregistered actor binding")
    value = json.loads(row[0])
    permissions = config_of(con)["roles"][value["role"]].get("permissions")
    default = ["dispatch", "review"] if value["mode"] == "controller" else (["review"] if value["mode"] == "reviewer" else [])
    allowed = permissions if permissions is not None else default
    if action not in allowed or value["mode"] not in ("controller", "reviewer") or (action == "dispatch" and value["mode"] != "controller"):
        raise ValueError("actor does not hold controller permission for " + action)
    return value


def check_request_links(state, request):
    with database(state) as con:
        cfg = config_of(con)
        expected = cfg.get("workflow", {}).get("reviewer_role")
        role = request["reviewer"].get("role")
        if role not in cfg["roles"]:
            raise ValueError("request reviewer role is undefined")
        if expected and role != expected and request["authorization"].get("allow_reviewer_override") is not True:
            raise ValueError("reviewer role differs from workflow; explicit override authorization required")
        return {"workflow_config_sha256": digest(cfg), "reviewer_role": role}


def register_task(state, request, actor, version, context):
    from monitor_task import validate_request
    validate_request(request)
    check_request_links(state, request)
    if request.get("parent_review"):
        parent = next_gate(state, request["parent_review"]["event_id"], actor, version, context)["parent_review"]
        if any(parent.get(k) != request["parent_review"].get(k)
               for k in ("event_id", "task_id", "nonce", "path", "sha256", "verdict")):
            raise ValueError("follow-up parent review differs from the ledger")
    with database(state) as con:
        who = guard(con, actor, version, context, "dispatch")
        worker_role = request["worker"].get("role")
        worker = active(con, worker_role)
        if worker["mode"] != "worker":
            raise ValueError("execution target must hold worker mode")
        if worker["spec"] != {k: v for k, v in request["worker"].items() if k != "baseline_completion_seq"}:
            raise ValueError("request worker does not match the active role binding")
        old = con.execute("SELECT request_hash FROM tasks WHERE id=?", (request["task_id"],)).fetchone()
        if old:
            if old[0] != digest(request):
                raise ValueError("task ID reused with different inputs")
            return {"status": "ALREADY_REGISTERED"}
        if con.execute("SELECT count(*) FROM tasks WHERE worker=? AND status='running'", (worker_role,)).fetchone()[0]:
            raise ValueError("role already has an active or uncertain task")
        data = {"request": request, "request_sha256": digest(request),
                "authorization_sha256": digest(request["authorization"]), "worker_binding": worker,
                "controller": who, "registered_at": stamp()}
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)", (request["task_id"], request["nonce"],
                    digest(request), worker_role, "running", json.dumps(data)))
        audit(con, "task_registered", data)
        return {"status": "REGISTERED", "request_sha256": digest(request)}


def check_registered(state, request):
    with database(state) as con:
        row = con.execute("SELECT request_hash FROM tasks WHERE id=? AND nonce=?",
                          (request["task_id"], request["nonce"])).fetchone()
        if not row or row[0] != digest(request):
            raise ValueError("register the identical frozen request before starting the monitor")


def resolve_task(state, task_id, actor, version, context, evidence):
    """Record independently inspected termination; this never cancels a worker."""
    path = Path(evidence).resolve()
    raw = path.read_bytes()
    with database(state) as con:
        guard(con, actor, version, context, "dispatch")
        row = con.execute("SELECT nonce FROM tasks WHERE id=? AND status='running'", (task_id,)).fetchone()
        if not row or task_id not in raw.decode("utf-8-sig") or row[0] not in raw.decode("utf-8-sig"):
            raise ValueError("termination evidence must bind a running task ID and nonce")
        con.execute("UPDATE tasks SET status='resolved' WHERE id=?", (task_id,))
        result = {"task_id": task_id, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                  "actor": actor, "recorded_at": stamp(), "status": "TERMINATION_RECORDED"}
        audit(con, "task_termination_inspected", result)
        return result


def publish_event(state, request, report, receipt=None):
    with database(state) as con:
        task = con.execute("SELECT request_hash FROM tasks WHERE id=? AND nonce=?",
                           (request["task_id"], request["nonce"])).fetchone()
        if not task or task[0] != digest(request):
            raise ValueError("monitor request is not the registered frozen request")
        event_id = digest({"request": digest(request), "status": report["status"]})
        data = {"event_id": event_id, "request_sha256": digest(request), "report": report,
                "receipt": receipt, "created_at": stamp()}
        con.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", (event_id, request["task_id"],
                    request["nonce"], request["reviewer"]["role"], "pending", json.dumps(data)))
        if report["status"] in ("review_ready", "failed", "exited"):
            con.execute("UPDATE tasks SET status='finished' WHERE id=?", (request["task_id"],))
        return event_id


def deliver_event(state, event_id, transport):
    # Reserve before sending. A changed controller is an audited binding switch,
    # never an edit of the frozen task request.
    with database(state) as con:
        event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event:
            raise ValueError("unknown event")
        if event["status"] != "pending":
            return {"status": "ALREADY_ACKNOWLEDGED"}
        request = json.loads(con.execute("SELECT data FROM tasks WHERE id=?", (event["task"],)).fetchone()[0])["request"]
        if request["authorization"].get("notify_reviewer") is not True:
            return {"status": "INBOX_ONLY", "event_id": event_id}
        target = active(con, event["role"])
        if target["mode"] not in ("controller", "reviewer") or target["context_policy"] == "inbox_only":
            return {"status": "INBOX_ONLY", "event_id": event_id}
        spec = target["spec"]
        from monitor_task import identity_matches
        observed = transport.reviewer_identity(spec)
        if not identity_matches(observed, spec["expected_identity"]):
            return {"status": "IDENTITY_CHANGED", "event_id": event_id}
        if con.execute("SELECT 1 FROM notifications WHERE event=?", (event_id,)).fetchone():
            return {"status": "ALREADY_ATTEMPTED", "event_id": event_id}
        attempt = {"event_id": event_id, "target_binding": target, "request_sha256": digest(request),
                   "authorization_sha256": digest(request["authorization"]), "attempted_at": stamp(),
                   "status": "reserved_no_blind_retry"}
        con.execute("INSERT INTO notifications VALUES (?,?)", (event_id, json.dumps(attempt)))
    message = ("Automatic workflow event under existing authorization, not a new Owner instruction. "
               "Event " + event_id + ", task " + event["task"] + ", nonce " + event["nonce"] +
               ". State directory: " + str(Path(state).resolve()) + ". Target reviewer instance " +
               target["instance_id"] + ", binding version " + str(target["version"]) +
               ". Only the registered main context may acknowledge/review this event. "
               "Side conversations are advisory-only: do not acknowledge, review, or dispatch. "
               "Read inbox, independently review raw evidence, record REVIEW.md, then check the next gate. "
               "Delivery is not acceptance; no blind resubmission.")
    try:
        response = transport.send(spec, message)
        attempt.update(status="submitted", response=response[:2000])
    except Exception as exc:
        attempt.update(status="delivery_uncertain", error=str(exc)[:500])
    with database(state) as con:
        con.execute("UPDATE notifications SET data=? WHERE event=?", (json.dumps(attempt), event_id))
    return attempt


def acknowledge(state, event_id, actor, version, context):
    with database(state) as con:
        who = guard(con, actor, version, context, "review")
        event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["role"] != who["role"]:
            raise ValueError("event belongs to another reviewer role")
        if event["status"] == "pending":
            con.execute("UPDATE events SET status='acknowledged' WHERE id=?", (event_id,))
        audit(con, "event_acknowledged", {"event": event_id, "actor": actor, "version": version})
        return {"status": "ACKNOWLEDGED", "event_id": event_id}


def record_review(state, event_id, actor, version, context, file, verdict):
    path = Path(file).resolve()
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    with database(state) as con:
        who = guard(con, actor, version, context, "review")
        event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["role"] != who["role"] or event["status"] != "acknowledged":
            raise ValueError("acknowledge this role's event before recording a review")
        if event["task"] not in text or event["nonce"] not in text:
            raise ValueError("REVIEW.md must bind the task ID and nonce")
        if not re.search(r"^Verdict:\s*" + re.escape(verdict) + r"\s*$", text, re.M):
            raise ValueError("REVIEW.md Verdict must match the declared verdict")
        cfg = config_of(con)
        if verdict not in cfg.get("workflow", {}).get("transitions", {}):
            raise ValueError("verdict is not configured in workflow.transitions")
        result = {"event_id": event_id, "task_id": event["task"], "nonce": event["nonce"],
                  "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "verdict": verdict,
                  "action": cfg["workflow"]["transitions"][verdict], "actor": who,
                  "workflow_config_sha256": digest(cfg), "reviewed_at": stamp()}
        con.execute("INSERT INTO reviews VALUES (?,?)", (event_id, json.dumps(result)))
        con.execute("UPDATE events SET status='reviewed' WHERE id=?", (event_id,))
        audit(con, "review_recorded", result)
        return result


def next_gate(state, event_id, actor, version, context):
    with database(state) as con:
        who = guard(con, actor, version, context, "dispatch")
        row = con.execute("SELECT data FROM reviews WHERE event=?", (event_id,)).fetchone()
        if not row:
            raise ValueError("parent review missing")
        review = json.loads(row[0])
        if hashlib.sha256(Path(review["path"]).read_bytes()).hexdigest() != review["sha256"]:
            raise ValueError("parent review changed after acceptance")
        allowed = config_of(con).get("workflow", {}).get("continuation_verdicts", ["accept", "repair"])
        if review["verdict"] not in allowed:
            raise ValueError("review decision does not admit a follow-up")
        return {"status": "NEXT_GATE_PASS", "parent_review": review}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("--config", required=True)
    for name in ("bind", "switch"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--role", required=True); cmd.add_argument("--spec", required=True)
        cmd.add_argument("--mode", choices=["controller", "reviewer", "worker", "advisor"], required=True)
        cmd.add_argument("--policy", choices=["inbox_only", "exact_thread", "dedicated_window"], default="inbox_only")
        cmd.add_argument("--reason", default=""); cmd.add_argument("--defer", action="store_true")
    activate_cmd = sub.add_parser("activate"); activate_cmd.add_argument("--role", required=True)
    for name in ("register-task", "ack", "review", "next-check", "resolve-task"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--actor", required=True); cmd.add_argument("--version", type=int, required=True)
        cmd.add_argument("--context", choices=["main", "side", "unknown"], default="unknown")
        if name == "resolve-task":
            cmd.add_argument("--task", required=True); cmd.add_argument("--evidence", required=True)
        elif name == "register-task": cmd.add_argument("--request", required=True)
        else: cmd.add_argument("--event", required=True)
        if name == "review": cmd.add_argument("--file", required=True); cmd.add_argument("--verdict", required=True)
    for name in ("bindings", "inbox"):
        sub.add_parser(name)
    deliver = sub.add_parser("deliver"); deliver.add_argument("--event", required=True)
    args = parser.parse_args()
    from monitor_task import Transport, read_json
    if args.command == "init": result = initialize(args.state, read_json(args.config))
    elif args.command in ("bind", "switch"):
        spec = read_json(args.spec); transport = Transport({})
        observed = transport.status(spec)["identity"] if args.mode == "worker" else transport.reviewer_identity(spec)
        result = bind(args.state, args.role, spec, observed, args.mode, args.policy, args.defer, args.reason)
    elif args.command == "activate":
        with database(args.state) as con:
            row = con.execute("SELECT data FROM bindings WHERE role=? AND status='pending'", (args.role,)).fetchone()
            if not row: raise ValueError("no pending binding")
            binding = json.loads(row[0])
        spec = binding["spec"]; transport = Transport({})
        observed = transport.status(spec)["identity"] if binding["mode"] == "worker" else transport.reviewer_identity(spec)
        result = activate(args.state, args.role, observed)
    elif args.command == "register-task": result = register_task(args.state, read_json(args.request), args.actor, args.version, args.context)
    elif args.command == "resolve-task": result = resolve_task(args.state, args.task, args.actor, args.version, args.context, args.evidence)
    elif args.command == "ack": result = acknowledge(args.state, args.event, args.actor, args.version, args.context)
    elif args.command == "review": result = record_review(args.state, args.event, args.actor, args.version, args.context, args.file, args.verdict)
    elif args.command == "next-check": result = next_gate(args.state, args.event, args.actor, args.version, args.context)
    elif args.command == "deliver": result = deliver_event(args.state, args.event, Transport({}))
    else:
        with database(args.state) as con:
            table = "bindings" if args.command == "bindings" else "events"
            result = [dict(row) for row in con.execute("SELECT * FROM " + table)]
            for item in result:
                item["data"] = json.loads(item["data"])
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
