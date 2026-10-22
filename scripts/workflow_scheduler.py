"""Durable, single-attempt dispatch, workspace claims, and reviewed DAG scheduling."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid

import workflow_control as control
from monitor_task import Transport, identity_matches, norm_path, read_json, sequence, validate_argv


OPERATIONS = ("spawn", "attach_resume", "send", "status", "cancel", "collect", "heartbeat")
DDL = (
    "CREATE TABLE IF NOT EXISTS dispatches (task TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS dag_runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS task_observations (task TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS scheduling_blocks (task TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS task_journal (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, "
    "task TEXT NOT NULL, generation INTEGER NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL, "
    "kind TEXT NOT NULL, data TEXT NOT NULL)",
)


def create_schema(con):
    # execute(), rather than executescript(), preserves the caller's transaction.
    for statement in DDL:
        con.execute(statement)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('scheduler_schema','1')")


def has_schema(con):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='dispatches'").fetchone())


def journal(con, task, generation, actor, kind, data, event_id=None):
    event_id = event_id or uuid.uuid4().hex
    con.execute("INSERT INTO task_journal(id,task,generation,actor,at,kind,data) VALUES (?,?,?,?,?,?,?)",
                (event_id, task, generation, actor, control.stamp(), kind, json.dumps(data)))


def migrate(state, actor, version, context):
    with control.database(state) as con:
        control.guard(con, actor, version, context, "dispatch")
        create_schema(con)
        control.audit(con, "scheduler_migrated", {"actor": actor, "version": version, "schema": 1})
    return {"status": "SCHEDULER_SCHEMA_READY", "schema": 1}


def overlap(first, second):
    first, second = norm_path(Path(first).resolve()), norm_path(Path(second).resolve())
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def claims(con):
    if not has_schema(con):
        return []
    # Failed processes and uncertain sends keep their claims until inspected.
    result = []
    for row in con.execute("SELECT d.task,d.data,t.status FROM dispatches d JOIN tasks t ON t.id=d.task"):
        value = json.loads(row["data"])
        if row["status"] != "resolved":
            result.append((row["task"], value))
    return result


def check_claim(con, request, reservation=None):
    """Legacy registration also respects outstanding scheduler workspace claims."""
    if has_schema(con):
        for row in con.execute("SELECT id,data FROM dag_runs"):
            graph = json.loads(row["data"])["graph"]
            for node in graph["nodes"]:
                if node["request"]["task_id"] == request["task_id"]:
                    if not reservation or reservation.get("run") != row["id"] or reservation.get("node") != node["id"]:
                        raise ValueError("frozen DAG tasks must be dispatched through their run")
                    assert_ready(con, row["id"], node["id"], request)
                    if reservation["message"] != node["message"]:
                        raise ValueError("message differs from the frozen DAG")
                    if reservation["resources"] != node.get("resources", {}):
                        raise ValueError("resources differ from the frozen DAG")
    requested_paths = [request["cwd"], request["worker"]["expected_identity"]["launch_cwd"]]
    for task, value in claims(con):
        if task != request["task_id"] and any(overlap(first, second)
                for first in value["workspaces"] for second in requested_paths):
            raise ValueError("workspace is claimed by task " + task)
    if reservation is None:
        return
    resources = reservation["resources"]
    cfg = control.config_of(con).get("scheduler", {})
    capacities = cfg.get("resources", {})
    for key, amount in resources.items():
        if key not in capacities or type(capacities[key]) is not int or capacities[key] < 1:
            raise ValueError("resource has no positive configured capacity: " + key)
        used = sum(item["resources"].get(key, 0) for _, item in claims(con))
        if used + amount > capacities[key]:
            raise ValueError("resource capacity exhausted: " + key)
    # Include work registered through the old API in both isolation and concurrency checks.
    running = con.execute("SELECT id,data FROM tasks WHERE status='running'").fetchall()
    limit = cfg.get("max_concurrency", 1)
    if type(limit) is not int or limit < 1:
        raise ValueError("max_concurrency must be a positive integer")
    if len(running) >= limit:
        raise ValueError("max_concurrency reached")
    for row in running:
        old = json.loads(row["data"])["request"]
        old_paths = [old["cwd"], old["worker"]["expected_identity"]["launch_cwd"]]
        if any(overlap(first, second) for first in old_paths for second in requested_paths):
            raise ValueError("workspace overlaps a running task: " + row["id"])


def reserve(con, request, who, value):
    if not has_schema(con):
        raise ValueError("migrate the state before scheduler dispatch")
    value = dict(value, task_id=request["task_id"], nonce=request["nonce"], cwd=request["cwd"],
                 workspaces=[str(Path(request["cwd"]).resolve()),
                             str(Path(request["worker"]["expected_identity"]["launch_cwd"]).resolve())],
                 controller=who["instance_id"], controller_version=who["version"],
                 request_sha256=control.digest(request), reserved_at=control.stamp())
    con.execute("INSERT INTO dispatches VALUES (?,?,?)", (request["task_id"], "reserved", json.dumps(value)))
    journal(con, request["task_id"], value["generation"], who["instance_id"], "dispatch_reserved", value)


def dispatch(state, request, message, actor, version, context, *, resources=None, run=None, node=None,
             transport=None):
    """Persist a reservation before the sole side effect. Never retry a reserved ID."""
    request = copy.deepcopy(request)
    if request.get("dispatch_confirmed") is not False:
        raise ValueError("dispatch requires dispatch_confirmed=false")
    auth = request.get("authorization", {})
    if auth.get("dispatch_worker") is not True or not auth.get("source"):
        raise ValueError("explicit dispatch_worker authorization and source required")
    if request["task_id"] not in message or request["nonce"] not in message:
        raise ValueError("message must bind task ID and nonce")
    if not request.get("workflow_state") or Path(request["workflow_state"]).resolve() != Path(state).resolve():
        raise ValueError("request workflow_state must match scheduler state")
    generation = request.get("generation", 1)
    if type(generation) is not int or generation < 1:
        raise ValueError("generation must be a positive integer")
    resources = {} if resources is None else resources
    if not isinstance(resources, dict) or any(not isinstance(k, str) or not k or type(v) is not int or v < 1
                                              for k, v in resources.items()):
        raise ValueError("resources must map names to positive integer units")
    spec = request["worker"]
    if spec["adapter"] == "command":
        validate_argv(spec.get("prompt_argv"))
        if spec["prompt_argv"].count("{message}") != 1:
            raise ValueError("prompt_argv needs one standalone {message}")
    elif spec["adapter"] != "herdr":
        raise ValueError("unsupported worker send operation; use a verified command or Herdr adapter")
    # Queries can be repeated. Query failure never reserves or sends.
    transport = transport or Transport(request)
    observed = transport.status(spec)
    if not identity_matches(observed.get("identity", {}), spec["expected_identity"]):
        raise ValueError("worker identity changed before dispatch")
    if run:
        # A downstream long-lived worker may have completed its upstream task.
        # Freeze the actual baseline only when this node becomes dispatchable.
        with control.database(state) as con:
            control.guard(con, actor, version, context, "dispatch")
            row = con.execute("SELECT data FROM dag_runs WHERE id=?", (run,)).fetchone()
            if not row:
                raise ValueError("unknown DAG run")
            graph = json.loads(row[0])["graph"]
            template = next((item for item in graph["nodes"] if item["id"] == node), None)
            if not template or control.digest(template["request"]) != control.digest(request):
                raise ValueError("request differs from the frozen DAG template")
        request["dag_binding"] = {"run": run, "node": node, "template_sha256": control.digest(request)}
        spec["baseline_completion_seq"] = sequence(observed.get("completion_seq"))
    if observed.get("status") != "idle" or sequence(observed.get("completion_seq")) != spec["baseline_completion_seq"]:
        raise ValueError("worker is not idle at the frozen completion baseline")
    if spec["adapter"] == "herdr":
        Transport.check_prompt_available(observed)
    value = {"generation": generation, "message": message, "message_sha256": control.digest(message),
             "resources": resources, "run": run, "node": node}
    # Same ID/hash returns ALREADY_REGISTERED; it must never proceed to sending.
    result = control.register_task(state, request, actor, version, context, reservation=value)
    if result["status"] == "ALREADY_REGISTERED":
        return {"status": "ALREADY_ATTEMPTED", "task_id": request["task_id"]}
    try:
        # Hold the write transaction across the bounded send so handoff cannot
        # retire this controller between the final check and the external call.
        with control.database(state) as con:
            control.guard(con, actor, version, context, "dispatch")
            if run:
                assert_ready(con, run, node, request)
            current = control.active(con, spec["role"])
            target = {k: v for k, v in spec.items() if k != "baseline_completion_seq"}
            if current["spec"] != target:
                raise ValueError("worker binding changed after reservation")
            live = transport.status(spec)
            if (not identity_matches(live.get("identity", {}), spec["expected_identity"])
                    or live.get("status") != "idle"
                    or sequence(live.get("completion_seq")) != spec["baseline_completion_seq"]):
                raise ValueError("worker identity or availability changed before send")
            response = transport.send(spec, message)
            data = json.loads(con.execute("SELECT data FROM dispatches WHERE task=?", (request["task_id"],)).fetchone()[0])
            data.update(response=str(response)[:2000], submitted_at=control.stamp())
            con.execute("UPDATE dispatches SET status='submitted',data=? WHERE task=?", (json.dumps(data), request["task_id"]))
            journal(con, request["task_id"], generation, actor, "dispatch_submitted", {"response": data["response"]})
        return {"status": "SUBMITTED", "task_id": request["task_id"], "next": "confirm execution with reconcile"}
    except Exception as exc:
        with control.database(state) as con:
            # A crash (BaseException/process death) leaves 'reserved', also uncertain.
            con.execute("UPDATE dispatches SET status='uncertain' WHERE task=? AND status='reserved'", (request["task_id"],))
            journal(con, request["task_id"], generation, actor, "dispatch_uncertain", {"error": str(exc)[:500]})
        return {"status": "DISPATCH_UNCERTAIN", "task_id": request["task_id"], "error": str(exc)[:500]}


def reconcile(state, task, outcome, evidence, actor, version, context):
    if outcome not in ("running", "not_sent", "terminated"):
        raise ValueError("invalid reconciliation outcome")
    path = Path(evidence).resolve()
    raw = path.read_bytes()
    note = raw.decode("utf-8-sig")
    with control.database(state) as con:
        control.guard(con, actor, version, context, "dispatch")
        row = con.execute("SELECT * FROM dispatches WHERE task=?", (task,)).fetchone()
        if not row:
            raise ValueError("unknown dispatch")
        value = json.loads(row["data"])
        if task not in note or value["nonce"] not in note or ("Outcome: " + outcome) not in note.splitlines():
            raise ValueError("evidence must bind task, nonce, and exact Outcome line")
        if row["status"] in ("not_sent", "terminated"):
            raise ValueError("dispatch was already closed")
        if outcome == "not_sent" and row["status"] == "running":
            raise ValueError("a confirmed execution cannot become not_sent")
        task_row = con.execute("SELECT status,data FROM tasks WHERE id=?", (task,)).fetchone()
        data = json.loads(task_row["data"])
        if task_row["status"] == "resolved" and not (outcome == "running" and row["status"] == "running"):
            raise ValueError("a closed task attempt cannot be reconciled again")
        if outcome == "running":
            if row["status"] == "running":
                return {"status": "ALREADY_CONFIRMED", "request": data["request"]}
            data["request"]["dispatch_confirmed"] = True
            data["request_sha256"] = control.digest(data["request"])
            con.execute("UPDATE tasks SET request_hash=?,data=? WHERE id=?",
                        (data["request_sha256"], json.dumps(data), task))
        else:
            con.execute("UPDATE tasks SET status='resolved' WHERE id=?", (task,))
        record = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "outcome": outcome}
        value["reconciliation"] = record
        con.execute("UPDATE dispatches SET status=?,data=? WHERE task=?", (outcome, json.dumps(value), task))
        journal(con, task, value["generation"], actor, "dispatch_reconciled", record)
        return {"status": outcome.upper(), "request": data["request"]}


def validate_capabilities(profile, root):
    """A verified claim needs versioned, hash-bound reproducible evidence."""
    for key in ("harness", "harness_version", "transport", "transport_version", "os", "skill_commit"):
        if not isinstance(profile.get(key), str) or not profile[key].strip():
            raise ValueError("capability profile needs " + key)
    if set(profile.get("operations", {})) != set(OPERATIONS):
        raise ValueError("capability profile must list all seven operations")
    for operation, claim in profile["operations"].items():
        if claim.get("status") not in ("verified", "unsupported", "unverified"):
            raise ValueError("invalid capability status for " + operation)
        if claim["status"] == "verified":
            if any(profile[key].strip().lower() in ("unknown", "unverified")
                   for key in ("harness", "harness_version", "transport_version", "os", "skill_commit")):
                raise ValueError("verified capability needs actual versions and provenance")
            if not claim.get("commands") or not claim.get("evidence"):
                raise ValueError("verified capability needs reproduction commands and evidence")
            for argv in claim["commands"]:
                validate_argv(argv)
            for item in claim["evidence"]:
                path = (Path(root) / item["path"]).resolve()
                path.relative_to(Path(root).resolve())
                raw = path.read_bytes()
                if not raw or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                    raise ValueError("capability evidence hash mismatch")
    return profile


def record_observation(state, request, report):
    with control.database(state) as con:
        if not has_schema(con):
            return
        row = con.execute("SELECT request_hash FROM tasks WHERE id=? AND nonce=?",
                          (request["task_id"], request["nonce"])).fetchone()
        if not row or row[0] != control.digest(request):
            raise ValueError("observation differs from registered request")
        con.execute("INSERT OR REPLACE INTO task_observations VALUES (?,?)", (request["task_id"], json.dumps(report)))


def validate_dag(graph):
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not graph["nodes"]:
        raise ValueError("DAG needs a nonempty nodes list")
    nodes = graph["nodes"]
    names = [node["id"] for node in nodes]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("DAG node IDs must be unique nonempty strings")
    tasks = [node["request"]["task_id"] for node in nodes]
    if len(set(tasks)) != len(tasks):
        raise ValueError("DAG task IDs must be unique")
    for node in nodes:
        dependencies = node.get("needs", [])
        if not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies):
            raise ValueError("needs must list unique node IDs")
        if any(dep not in names or dep == node["id"] for dep in dependencies):
            raise ValueError("unknown or self dependency")
        if node["request"].get("dispatch_confirmed") is not False:
            raise ValueError("DAG requests must be unconfirmed")
        if node["request"]["task_id"] not in node.get("message", "") or node["request"]["nonce"] not in node.get("message", ""):
            raise ValueError("DAG messages must bind task ID and nonce")
    pending, visited = {node["id"]: set(node.get("needs", [])) for node in nodes}, set()
    while pending:
        ready = {key for key, deps in pending.items() if deps <= visited}
        if not ready:
            raise ValueError("DAG contains a cycle")
        visited.update(ready)
        pending = {key: deps for key, deps in pending.items() if key not in ready}
    return graph


def load_dag(state, run, graph, actor, version, context):
    from monitor_task import validate_request
    validate_dag(graph)
    for node in graph["nodes"]:
        validate_request(node["request"], require_dispatch=False)
        if Path(node["request"].get("workflow_state", "")).resolve() != Path(state).resolve():
            raise ValueError("every DAG request must target this state")
    with control.database(state) as con:
        control.guard(con, actor, version, context, "dispatch")
        if not has_schema(con):
            raise ValueError("migrate the state before loading a DAG")
        if con.execute("SELECT 1 FROM dag_runs WHERE id=?", (run,)).fetchone():
            raise ValueError("run ID already exists; never overwrite a frozen DAG")
        for node in graph["nodes"]:
            if con.execute("SELECT 1 FROM tasks WHERE id=?", (node["request"]["task_id"],)).fetchone():
                raise ValueError("DAG task ID already exists")
            for old in con.execute("SELECT data FROM dag_runs"):
                if any(item["request"]["task_id"] == node["request"]["task_id"]
                       for item in json.loads(old[0])["graph"]["nodes"]):
                    raise ValueError("task ID already belongs to another DAG")
        value = {"graph": graph, "sha256": control.digest(graph), "created_at": control.stamp(), "actor": actor}
        con.execute("INSERT INTO dag_runs VALUES (?,?)", (run, json.dumps(value)))
        control.audit(con, "dag_loaded", {"run": run, "sha256": value["sha256"], "actor": actor})
    return {"status": "DAG_LOADED", "run": run}


def acceptance(con, task):
    """Revalidate review and delivered bytes every time a dependency is consumed."""
    rows = con.execute("SELECT e.data AS event,r.data AS review FROM events e "
                       "JOIN reviews r ON r.event=e.id WHERE e.task=?", (task,)).fetchall()
    for row in rows:
        review, event = json.loads(row["review"]), json.loads(row["event"])
        if review["verdict"] != "accept" or event["report"]["status"] != "review_ready":
            continue
        receipt = event.get("receipt")
        if not receipt or receipt.get("task_id") != review["task_id"] or receipt.get("nonce") != review["nonce"]:
            continue
        files = receipt.get("artifacts", []) + [receipt.get("binding_artifact", {})]
        try:
            if hashlib.sha256(Path(review["path"]).read_bytes()).hexdigest() != review["sha256"]:
                continue
            if not all(item.get("path") and hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
                       for item in files):
                continue
        except (OSError, KeyError):
            continue
        return review
    return None


def node_state(con, node):
    task = node["request"]["task_id"]
    row = con.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
    if not row:
        return "waiting"
    if acceptance(con, task):
        return "accepted"
    events = [json.loads(row[0]) for row in con.execute("SELECT data FROM events WHERE task=?", (task,))]
    reviews = [json.loads(row[0]) for row in con.execute("SELECT r.data FROM reviews r JOIN events e ON r.event=e.id WHERE e.task=?", (task,))]
    if row["status"] == "resolved" or any(review["verdict"] != "accept" for review in reviews):
        return "blocked"
    if any(item["report"]["status"] in ("failed", "exited") for item in events):
        return "blocked"
    if reviews:
        return "evidence_changed"
    if any(item["report"]["status"] == "review_ready" for item in events):
        return "under_review"
    dispatch_row = con.execute("SELECT status FROM dispatches WHERE task=?", (task,)).fetchone()
    return dispatch_row[0] if dispatch_row else "running"


def dag_view(con, run):
    row = con.execute("SELECT data FROM dag_runs WHERE id=?", (run,)).fetchone()
    if not row:
        raise ValueError("unknown DAG run")
    graph = json.loads(row[0])["graph"]
    states = {node["id"]: node_state(con, node) for node in graph["nodes"]}
    for _ in graph["nodes"]:
        for node in graph["nodes"]:
            if states[node["id"]] == "waiting":
                deps = [states[key] for key in node.get("needs", [])]
                if all(value == "accepted" for value in deps):
                    states[node["id"]] = "ready"
                elif any(value in ("blocked", "evidence_changed", "blocked_dependency") for value in deps):
                    states[node["id"]] = "blocked_dependency"
    return graph, states


def assert_ready(con, run, node_id, request):
    graph, states = dag_view(con, run)
    node = next((item for item in graph["nodes"] if item["id"] == node_id), None)
    normalized = copy.deepcopy(request)
    binding = normalized.pop("dag_binding", None)
    if node and binding:
        expected = {"run": run, "node": node_id, "template_sha256": control.digest(node["request"])}
        if binding != expected:
            raise ValueError("DAG template binding differs")
        normalized["worker"]["baseline_completion_seq"] = node["request"]["worker"]["baseline_completion_seq"]
    if not node or control.digest(node["request"]) != control.digest(normalized):
        raise ValueError("dispatch differs from the frozen DAG node")
    if any(not acceptance(con, next(item for item in graph["nodes"] if item["id"] == dep)["request"]["task_id"])
           for dep in node.get("needs", [])):
        raise ValueError("DAG dependency has no valid accepted evidence")
    return node


def tick(state, run, actor, version, context, *, transport=None):
    # One bounded pass. Polling/restarts are safe: reserved task IDs cannot resend.
    with control.database(state) as con:
        control.guard(con, actor, version, context, "dispatch")
        graph, states = dag_view(con, run)
        for node in graph["nodes"]:
            if states[node["id"]] == "accepted":
                task = node["request"]["task_id"]
                updated = con.execute("UPDATE tasks SET status='resolved' WHERE id=? AND status!='resolved'", (task,))
                if updated.rowcount:
                    journal(con, task, node["request"].get("generation", 1), actor,
                            "claim_released", {"actor_version": version})
    results = []
    for node in graph["nodes"]:
        if states[node["id"]] != "ready":
            continue
        try:
            result = dispatch(state, node["request"], node["message"], actor, version, context,
                              resources=node.get("resources", {}), run=run, node=node["id"], transport=transport)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            result = {"status": "DEFERRED", "reason": str(exc)}
        with control.database(state) as con:
            if result["status"] == "DEFERRED":
                con.execute("INSERT OR REPLACE INTO scheduling_blocks VALUES (?,?)",
                            (node["request"]["task_id"], json.dumps(dict(result, run=run, node=node["id"], at=control.stamp()))))
            else:
                con.execute("DELETE FROM scheduling_blocks WHERE task=?", (node["request"]["task_id"],))
        results.append(dict(result, node=node["id"]))
    return {"run": run, "results": results}


def release(state, task, actor, version, context):
    """Release a completed claim after review; never infer termination from silence."""
    with control.database(state) as con:
        control.guard(con, actor, version, context, "dispatch")
        row = con.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
        if not row or (row[0] != "resolved" and not acceptance(con, task)):
            raise ValueError("claim requires valid acceptance or inspected termination")
        con.execute("UPDATE tasks SET status='resolved' WHERE id=?", (task,))
        dispatch_row = con.execute("SELECT data FROM dispatches WHERE task=?", (task,)).fetchone()
        generation = json.loads(dispatch_row[0])["generation"] if dispatch_row else 1
        journal(con, task, generation, actor, "claim_released", {"actor_version": version})
    return {"status": "CLAIM_RELEASED", "task_id": task}


def snapshot(state, task=None):
    """Read-only, single-transaction view; never migrates, polls, or contacts clients."""
    path = Path(state).resolve() / "workflow.sqlite3"
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        tasks = []
        query, params = ("SELECT * FROM tasks WHERE id=?", (task,)) if task else ("SELECT * FROM tasks ORDER BY id", ())
        for row in con.execute(query, params):
            data = json.loads(row["data"])
            value = {"task_id": row["id"], "nonce": row["nonce"], "execution": row["status"],
                     "worker": data["worker_binding"], "registered_at": data["registered_at"],
                     "request_sha256": row["request_hash"], "cwd": data["request"]["cwd"]}
            value["events"] = [dict(item, data=json.loads(item["data"])) for item in con.execute("SELECT * FROM events WHERE task=?", (row["id"],))]
            value["reviews"] = [json.loads(item[0]) for item in con.execute("SELECT r.data FROM reviews r JOIN events e ON r.event=e.id WHERE e.task=?", (row["id"],))]
            value["notifications"] = [json.loads(item[0]) for item in con.execute("SELECT n.data FROM notifications n JOIN events e ON n.event=e.id WHERE e.task=?", (row["id"],))]
            if has_schema(con):
                dispatch_row = con.execute("SELECT * FROM dispatches WHERE task=?", (row["id"],)).fetchone()
                value["dispatch"] = dict(dispatch_row, data=json.loads(dispatch_row["data"])) if dispatch_row else None
                value["journal"] = [dict(item, data=json.loads(item["data"])) for item in con.execute("SELECT * FROM task_journal WHERE task=? ORDER BY seq", (row["id"],))]
                value["claim_held"] = bool(dispatch_row and row["status"] != "resolved")
                observation = con.execute("SELECT data FROM task_observations WHERE task=?", (row["id"],)).fetchone()
                value["last_observation"] = json.loads(observation[0]) if observation else None
            tasks.append(value)
        if task and not tasks:
            raise ValueError("unknown task")
        dags, blocks = {}, {}
        if has_schema(con):
            blocks = {row["task"]: json.loads(row["data"]) for row in con.execute("SELECT * FROM scheduling_blocks")}
            for row in con.execute("SELECT id FROM dag_runs ORDER BY id"):
                graph, states = dag_view(con, row[0])
                dags[row[0]] = {"nodes": states, "dependencies": {node["id"]: node.get("needs", []) for node in graph["nodes"]}}
        return {"tasks": tasks, "dags": dags, "scheduling_blocks": blocks,
                "resources": control.config_of(con).get("scheduler", {}).get("resources", {}),
                "claims": [{"task_id": task_id, "workspaces": value["workspaces"], "resources": value["resources"]}
                           for task_id, value in claims(con)], "scheduler_schema": 1 if has_schema(con) else None}
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status"); status.add_argument("--task")
    caps = sub.add_parser("capabilities"); caps.add_argument("--file", required=True)
    for name in ("migrate", "dispatch", "reconcile", "dag-load", "tick", "release"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--actor", required=True); cmd.add_argument("--version", type=int, required=True)
        cmd.add_argument("--context", choices=("main", "side", "unknown"), default="unknown")
        if name == "dispatch":
            cmd.add_argument("--request", required=True); cmd.add_argument("--message-file", required=True)
            cmd.add_argument("--resources", default="{}", help="JSON object of resource units")
        if name == "reconcile":
            cmd.add_argument("--task", required=True); cmd.add_argument("--outcome", choices=("running", "not_sent", "terminated"), required=True)
            cmd.add_argument("--evidence", required=True); cmd.add_argument("--request-out", required=True)
        if name in ("dag-load", "tick"):
            cmd.add_argument("--run", required=True)
        if name == "dag-load":
            cmd.add_argument("--file", required=True)
        if name == "release":
            cmd.add_argument("--task", required=True)
    args = parser.parse_args()
    if args.command == "capabilities":
        result = validate_capabilities(read_json(args.file), Path(args.file).resolve().parent)
    else:
        if not args.state:
            parser.error("--state required")
        if args.command == "status":
            result = snapshot(args.state, args.task)
        else:
            who = (args.actor, args.version, args.context)
            if args.command == "migrate": result = migrate(args.state, *who)
            elif args.command == "dispatch":
                result = dispatch(args.state, read_json(args.request), Path(args.message_file).read_text(encoding="utf-8-sig"),
                                  *who, resources=json.loads(args.resources))
            elif args.command == "reconcile":
                result = reconcile(args.state, args.task, args.outcome, args.evidence, *who)
                # Exclusive creation; a crash here can be recovered with ALREADY_CONFIRMED.
                with Path(args.request_out).open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(result["request"], ensure_ascii=True, indent=2) + "\n")
            elif args.command == "dag-load": result = load_dag(args.state, args.run, read_json(args.file), *who)
            elif args.command == "tick": result = tick(args.state, args.run, *who)
            else: result = release(args.state, args.task, *who)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
