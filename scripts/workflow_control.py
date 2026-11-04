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
    scheduler = value.get("scheduler", {})
    if not isinstance(scheduler, dict):
        raise ValueError("scheduler must be an object")
    concurrency = scheduler.get("max_concurrency", 1)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("scheduler.max_concurrency must be a positive integer")
    resources = scheduler.get("resources", {})
    if not isinstance(resources, dict) or any(not isinstance(k, str) or not k or type(v) is not int or v < 1
                                              for k, v in resources.items()):
        raise ValueError("scheduler.resources must map names to positive integer capacities")
    continues = scheduler.get("max_continue_prompts", 2)
    if type(continues) is not int or not 0 <= continues <= 10:
        raise ValueError("scheduler.max_continue_prompts must be an integer from 0 to 10")
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
        from workflow_scheduler import create_schema
        create_schema(con)
        con.commit()
    finally:
        con.close()
    return {"status": "INITIALIZED", "state": str(directory), "config_sha256": digest(config)}


# Operating limits only. Roles, permissions and review transitions stay as initialized, so an edit can
# never grant an actor new authority or change what a verdict means for recorded reviews.
EDITABLE_CONFIG = ("goal", "scheduler.max_concurrency", "scheduler.max_continue_prompts", "scheduler.resources")


def config_value(config, key):
    section, _, name = key.partition(".")
    return config.get(section) if not name else config.get(section, {}).get(name)


def apply_config_changes(config, changes):
    """Return a validated copy of config with the allowed changes applied; never writes."""
    if not isinstance(changes, dict) or not changes or any(key not in EDITABLE_CONFIG for key in changes):
        raise ValueError("only these settings can change: " + ", ".join(EDITABLE_CONFIG))
    updated = json.loads(json.dumps(config))
    for key, value in changes.items():
        if key == "goal":
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError("goal must be text of at most 2000 characters")
            updated["goal"] = value.strip()
        else:
            updated.setdefault("scheduler", {})[key.split(".", 1)[1]] = value
    validate_workflow(updated)
    return updated


def update_config(state, changes, actor, version, context):
    with database(state) as con:
        who = guard(con, actor, version, context, "dispatch")
        old = config_of(con)
        new = apply_config_changes(old, changes)
        if new == old:
            return {"status": "UNCHANGED", "config_sha256": digest(old)}
        con.execute("UPDATE meta SET value=? WHERE key='config'", (json.dumps(new),))
        audit(con, "config_updated", {"actor": actor, "version": version, "role": who["role"], "changes": changes,
                                      "old_sha256": digest(old), "new_sha256": digest(new)})
    return {"status": "CONFIG_UPDATED", "config_sha256": digest(new)}


def audit(con, action, data):
    con.execute("INSERT INTO audit(at,action,data) VALUES (?,?,?)", (stamp(), action, json.dumps(data)))


def config_of(con):
    return json.loads(con.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])


def active(con, role):
    row = con.execute("SELECT data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
    if not row:
        raise ValueError("no active binding for role: " + role)
    return json.loads(row[0])


def prepare_binding_spec(spec, mode, policy, reason):
    from monitor_task import validate_spec
    if mode != "worker":
        spec = dict(spec, context_policy=policy)
        if policy == "dedicated_window" and reason.strip():
            spec.setdefault("context_declaration", reason)
    validate_spec(spec, reviewer=(mode != "worker"))
    return spec


LABEL_PREFIX = "awo:"  # Herdr display names owned by this tool; never an identity field.


def pane_label(role):
    return LABEL_PREFIX + role


def label_source(name, role=None):
    if not name:
        return "none"
    if name.startswith(LABEL_PREFIX):
        return "awo_stale" if role and name != pane_label(role) else "awo"
    return "custom"


def live_pane(transport, spec):
    expected = spec["expected_identity"]
    return next((agent for agent in transport.agents(spec)
                 if agent.get("terminal_id") == expected.get("terminal")
                 and agent.get("pane_id") == spec.get("pane")), None)


def label_transport():
    from monitor_task import Transport
    return Transport({})


def apply_labels(new, old=None):
    """Name the newly active pane after its role; clear the retired pane only if it still carries our name.

    Best effort and outside any ledger transaction: a failed rename never blocks or undoes a binding.
    """
    result = {"label_actions": []}
    transport = label_transport()
    if new["spec"]["adapter"] == "herdr":
        spec = new["spec"]
        action = {"pane": spec["pane"], "original_label": None, "action": "failed"}
        result["label_actions"].append(action)
        try:
            agent = live_pane(transport, spec)
            if agent is None:
                raise ValueError("bound pane was not found in Herdr session")
            name = agent.get("name")
            action["original_label"] = name
            source = label_source(name, new["role"])
            if source == "custom":
                action["action"] = "kept"
            elif source == "awo":
                action["action"] = "unchanged"
                result["label"] = name
            else:
                transport.label(spec, pane_label(new["role"]))
                action["action"] = "set"
                result["label"] = pane_label(new["role"])
        except Exception as exc:
            action["error"] = str(exc)[:300]
            result["label_error"] = action["error"]
    if old and old["spec"]["adapter"] == "herdr" and \
            (old["spec"].get("session"), old["spec"].get("pane")) != \
            (new["spec"].get("session"), new["spec"].get("pane")):
        action = {"pane": old["spec"]["pane"], "original_label": None, "action": "failed"}
        result["label_actions"].append(action)
        try:
            agent = live_pane(transport, old["spec"])
            if agent is None:
                action["action"] = "unchanged"
            else:
                name = agent.get("name")
                action["original_label"] = name
                if name == pane_label(old["role"]):
                    transport.label(old["spec"], None)
                    action["action"] = "cleared"
                    result["cleared"] = old["spec"]["pane"]
                else:
                    action["action"] = "kept" if name else "unchanged"
        except Exception as exc:
            action["error"] = str(exc)[:300]
            result["label_error"] = action["error"]
    return result


def bind(state, role, spec, observed, mode="worker", policy="inbox_only", defer=False, reason=""):
    from monitor_task import Transport, identity_matches, validate_identity
    if mode not in ("controller", "reviewer", "worker", "advisor") or policy not in ("inbox_only", "exact_thread", "dedicated_window"):
        raise ValueError("invalid mode or context policy")
    spec = prepare_binding_spec(spec, mode, policy, reason)
    validate_identity(observed)
    if not identity_matches(observed, spec["expected_identity"]):
        raise ValueError("live identity does not match the selected binding")
    if mode != "worker" and policy != "inbox_only" and spec["adapter"] == "herdr":
        if not identity_matches(Transport({}).reviewer_identity(spec), spec["expected_identity"]):
            raise ValueError("live reviewer identity changed during binding")
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
        retired = con.execute("SELECT data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
        if not defer:
            con.execute("UPDATE bindings SET status='retired' WHERE role=? AND status='active'", (role,))
        con.execute("INSERT INTO bindings VALUES (?,?,?,?,?,?,?)",
                    (record["instance_id"], role, version, mode, policy, status, json.dumps(record)))
        audit(con, "binding_" + status, record)
    labels = {} if defer else apply_labels(record, json.loads(retired[0]) if retired else None)
    return dict(record, status=status, **labels)


def activate(state, role, observed):
    from monitor_task import Transport, identity_matches
    with database(state) as con:
        row = con.execute("SELECT data FROM bindings WHERE role=? AND status='pending'", (role,)).fetchone()
        if not row:
            raise ValueError("no pending binding")
        if con.execute("SELECT count(*) FROM tasks WHERE worker=? AND status='running'", (role,)).fetchone()[0]:
            raise ValueError("old tasks still running or uncertain")
        value = json.loads(row[0])
        if not identity_matches(observed, value["spec"]["expected_identity"]):
            raise ValueError("pending binding failed live recheck")
        if value["mode"] != "worker" and value["context_policy"] != "inbox_only" and value["spec"]["adapter"] == "herdr":
            spec = dict(value["spec"], context_policy=value["context_policy"])
            if not identity_matches(Transport({}).reviewer_identity(spec), spec["expected_identity"]):
                raise ValueError("pending reviewer identity changed during activation")
        retired = con.execute("SELECT data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
        con.execute("UPDATE bindings SET status='retired' WHERE role=? AND status='active'", (role,))
        con.execute("UPDATE bindings SET status='active' WHERE id=?", (value["instance_id"],))
        audit(con, "binding_activated", value)
    return dict(value, **apply_labels(value, json.loads(retired[0]) if retired else None))


def roles(state, transport=None):
    """Live map of every active role to its pane, plus unbound Herdr agents; read-only."""
    from workflow_scheduler import _read_database
    with _read_database(state) as con:
        bindings = [json.loads(row[0]) for row in con.execute(
            "SELECT data FROM bindings WHERE status='active' ORDER BY role")]
    from monitor_task import identity_matches
    transport = transport or label_transport()
    sessions, rows, bound = {}, [], set()
    for binding in bindings:
        spec = binding["spec"]
        row = {"role": binding["role"], "mode": binding["mode"], "version": binding["version"],
               "adapter": spec["adapter"], "pane": spec.get("pane"),
               "terminal": spec["expected_identity"].get("terminal"), "expected_label": None,
               "label_source": "none"}
        rows.append(row)
        if spec["adapter"] != "herdr":
            row["live"] = "not a Herdr binding"
            continue
        row["expected_label"] = pane_label(binding["role"])
        key = (spec["executable"], spec["session"])
        try:
            if key not in sessions:
                sessions[key] = (spec, transport.agents(spec))
            live = {a.get("terminal_id"): a for a in sessions[key][1]}
        except Exception as exc:
            row["live"] = "query failed: " + str(exc)[:200]
            continue
        bound.add((key, row["terminal"]))
        agent = live.get(row["terminal"])
        if agent and agent.get("pane_id") != row["pane"]:
            agent = None
        if not agent:
            row["live"] = "terminal not found; the agent was closed or restarted, rebind it"
            continue
        observed = {"kind": agent.get("agent"), "terminal": agent.get("terminal_id"),
                    "launch_cwd": agent.get("cwd"), "pane": agent.get("pane_id")}
        row.update(live="identity matches" if identity_matches(observed, spec["expected_identity"]) else
                   "identity differs; inspect before acting", status=agent.get("agent_status"),
                   label=agent.get("name"), label_source=label_source(agent.get("name"), binding["role"]),
                   title=agent.get("terminal_title_stripped"))
    unbound = []
    for key, (spec, agents) in sessions.items():
        for agent in agents:
            if (key, agent.get("terminal_id")) not in bound:
                unbound.append({"executable": key[0], "session": key[1],
                                "pane": agent.get("pane_id"), "agent": agent.get("agent"),
                                "status": agent.get("agent_status"), "label": agent.get("name"),
                                "label_source": label_source(agent.get("name")),
                                "title": agent.get("terminal_title_stripped")})
    return {"roles": rows, "unbound": unbound}


def label_panes(state, transport=None, force=False):
    """Re-apply role names to active Herdr panes and clear stale names this tool set on other panes."""
    transport = transport or label_transport()
    view = roles(state, transport)
    with database(state) as con:
        specs = {json.loads(row[0])["role"]: json.loads(row[0])["spec"] for row in con.execute(
            "SELECT data FROM bindings WHERE status='active'")}
    done, panes = [], []
    for row in view["roles"]:
        if row["adapter"] != "herdr":
            continue
        entry = {"pane": row["pane"], "original_label": row.get("label"), "action": "unchanged"}
        panes.append(entry)
        if row.get("live", "").startswith("query failed"):
            entry.update(action="failed", error=row["live"])
        elif row.get("live") != "identity matches":
            entry["action"] = "failed"
            entry["error"] = row.get("live", "identity could not be verified")
        elif row["label_source"] == "custom" and not force:
            entry["action"] = "kept"
        elif row.get("label") != row["expected_label"]:
            try:
                transport.label(specs[row["role"]], row["expected_label"])
                entry["action"] = "set"
                done.append({"pane": row["pane"], "label": row["expected_label"]})
            except Exception as exc:
                entry.update(action="failed", error=str(exc)[:300])
    session_specs = {(spec["executable"], spec["session"]): spec for spec in specs.values()
                     if spec["adapter"] == "herdr"}
    for agent in view["unbound"]:
        entry = {"pane": agent["pane"], "original_label": agent.get("label"), "action": "kept" if agent.get("label") else "unchanged"}
        panes.append(entry)
        if agent["label_source"] == "awo":
            target = dict(session_specs[(agent["executable"], agent["session"])], pane=agent["pane"])
            try:
                transport.label(target, None)
                entry["action"] = "cleared"
                done.append({"pane": agent["pane"], "label": None})
            except Exception as exc:
                entry.update(action="failed", error=str(exc)[:300])
    return {"status": "LABELS_SYNCED", "changes": done, "panes": panes}


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


def register_task(state, request, actor, version, context, *, reservation=None):
    from monitor_task import validate_request
    validate_request(request, require_dispatch=reservation is None)
    if reservation is not None and request["dispatch_confirmed"]:
        raise ValueError("a dispatch reservation needs an unconfirmed request")
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
        requested = {k: v for k, v in request["worker"].items() if k != "baseline_completion_seq"}
        differences = sorted(k for k in worker["spec"].keys() | requested.keys()
                             if k not in worker["spec"] or k not in requested or worker["spec"][k] != requested[k])
        if differences:
            raise ValueError("request worker does not match the active role binding; differs in: " + ", ".join(differences))
        old = con.execute("SELECT request_hash FROM tasks WHERE id=?", (request["task_id"],)).fetchone()
        if old:
            if old[0] != digest(request):
                raise ValueError("task ID reused with different inputs")
            return {"status": "ALREADY_REGISTERED"}
        if con.execute("SELECT count(*) FROM tasks WHERE worker=? AND status='running'", (worker_role,)).fetchone()[0]:
            raise ValueError("role already has an active or uncertain task")
        from workflow_scheduler import check_claim, reserve
        check_claim(con, request, reservation)
        data = {"request": request, "request_sha256": digest(request),
                "authorization_sha256": digest(request["authorization"]), "worker_binding": worker,
                "controller": who, "registered_at": stamp()}
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)", (request["task_id"], request["nonce"],
                    digest(request), worker_role, "running", json.dumps(data)))
        if reservation is not None:
            reserve(con, request, who, reservation)
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
    if not request.get("dispatch_confirmed"):
        raise ValueError("confirm dispatch before publishing monitor events")
    with database(state) as con:
        task = con.execute("SELECT request_hash FROM tasks WHERE id=? AND nonce=?",
                           (request["task_id"], request["nonce"])).fetchone()
        if not task or task[0] != digest(request):
            raise ValueError("monitor request is not the registered frozen request")
        # A later stall episode of the same attempt is a new event; the first keeps the original ID.
        identity = {"request": digest(request), "status": report["status"]}
        if report.get("stall_episode", 1) > 1:
            identity["stall_episode"] = report["stall_episode"]
        event_id = digest(identity)
        data = {"event_id": event_id, "request_sha256": digest(request), "report": report,
                "receipt": receipt, "created_at": stamp()}
        inserted = con.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", (event_id, request["task_id"],
                    request["nonce"], request["reviewer"]["role"], "pending", json.dumps(data)))
        from workflow_scheduler import has_schema, journal
        if inserted.rowcount and has_schema(con):
            journal(con, request["task_id"], request.get("generation", 1), "monitor:" + digest(request),
                    "delivery_" + report["status"], data, event_id)
        if report["status"] in ("review_ready", "failed", "exited"):
            con.execute("UPDATE tasks SET status='finished' WHERE id=? AND status='running'", (request["task_id"],))
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
        spec = dict(target["spec"], context_policy=target["context_policy"])
        if target["context_policy"] == "dedicated_window":
            spec.setdefault("context_declaration", target["reason"])
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
    from monitor_task import event_guidance
    report = json.loads(event["data"])["report"]
    delivery = report["status"]
    message = ("Automatic workflow event under existing authorization, not a new Owner instruction. "
               "Event " + event_id + ", task " + event["task"] + ", nonce " + event["nonce"] +
               ", delivery status " + delivery + ". State directory: " + str(Path(state).resolve()) +
               ". Target reviewer instance " + target["instance_id"] + ", binding version " + str(target["version"]) +
               ". Only the registered main context may acknowledge/review this event. "
               "Side conversations are advisory-only: do not acknowledge, review, or dispatch. "
               "Read the inbox and raw evidence. " + event_guidance(delivery, event["task"], report.get("cause")) +
               " Delivery is not acceptance; no blind resubmission.")
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
        task_status = con.execute("SELECT status FROM tasks WHERE id=?", (event["task"],)).fetchone()[0]
        delivery = json.loads(event["data"])["report"]["status"]
        from monitor_task import STALLED
        if delivery in STALLED and task_status == "running":
            # A verdict on a pause would outlive the attempt's real delivery; wait or close the attempt first.
            raise ValueError("delivery is still open (" + delivery + "); wait for review_ready, continue the "
                             "worker, or close the attempt before recording a review")
        task_data = json.loads(con.execute("SELECT data FROM tasks WHERE id=?", (event["task"],)).fetchone()[0])
        declared = task_data["request"].get("review_path")
        if declared and Path(declared).resolve() != path:
            raise ValueError("review file differs from the frozen review_path")
        from workflow_evidence import check_review_path
        check_review_path(con, event["task"], path)
        if not re.search(r"^Verdict:\s*" + re.escape(verdict) + r"\s*$", text, re.M):
            raise ValueError("REVIEW.md Verdict must match the declared verdict")
        cfg = config_of(con)
        if verdict not in cfg.get("workflow", {}).get("transitions", {}):
            raise ValueError("verdict is not configured in workflow.transitions")
        if verdict == "accept" and delivery != "review_ready":
            raise ValueError("only review_ready delivery can be accepted")
        if verdict == "accept" and task_status == "resolved":
            raise ValueError("a closed task attempt cannot be accepted")
        result = {"event_id": event_id, "task_id": event["task"], "nonce": event["nonce"],
                  "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "verdict": verdict,
                  "action": cfg["workflow"]["transitions"][verdict], "actor": who,
                  "workflow_config_sha256": digest(cfg), "reviewed_at": stamp()}
        con.execute("INSERT INTO reviews VALUES (?,?)", (event_id, json.dumps(result)))
        con.execute("UPDATE events SET status='reviewed' WHERE id=?", (event_id,))
        audit(con, "review_recorded", result)
        from workflow_scheduler import has_schema, journal
        if has_schema(con):
            task_data = json.loads(con.execute("SELECT data FROM tasks WHERE id=?", (event["task"],)).fetchone()[0])
            journal(con, event["task"], task_data["request"].get("generation", 1), actor, "review_" + verdict, result)
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


def settle(state, event_id, actor, version, context, file, verdict):
    """Resume the protected acknowledge, review, and claim-close sequence."""
    from workflow_scheduler import acceptance, has_schema, journal, release
    from monitor_task import STALLED
    path = Path(file).resolve()
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    completed = []
    with database(state) as con:
        reviewer = guard(con, actor, version, context, "review")
        guard(con, actor, version, context, "dispatch")
        event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["role"] != reviewer["role"]:
            raise ValueError("event belongs to another reviewer role")
        task_status = con.execute("SELECT status FROM tasks WHERE id=?", (event["task"],)).fetchone()[0]
        delivery = json.loads(event["data"])["report"]["status"]
        if delivery in STALLED and task_status == "running":
            raise ValueError("delivery is still open; wait, continue, or close the attempt before review")
        previous = con.execute("SELECT data FROM reviews WHERE event=?", (event_id,)).fetchone()
        if previous:
            review = json.loads(previous[0])
            if (review["path"], review["sha256"], review["verdict"]) != (str(path), sha, verdict):
                raise ValueError("existing review differs in path, file hash, or verdict")
        elif event["status"] not in ("pending", "acknowledged"):
            raise ValueError("event cannot be reviewed from status " + event["status"])
    if not previous:
        if event["status"] == "pending":
            acknowledge(state, event_id, actor, version, context)
            completed.append("acknowledged")
        try:
            review = record_review(state, event_id, actor, version, context, path, verdict)
            completed.append("reviewed")
        except Exception as exc:
            if completed:
                return {"status": "SETTLE_INCOMPLETE", "event_id": event_id, "completed": completed,
                        "error": str(exc)[:500]}
            raise
    else:
        completed.append("reviewed")
    try:
        with database(state) as con:
            row = con.execute("SELECT status,data FROM tasks WHERE id=?", (review["task_id"],)).fetchone()
            task_status = row["status"]
            accepted = bool(acceptance(con, review["task_id"])) if verdict == "accept" else False
        if verdict == "accept":
            if not accepted:
                raise ValueError("acceptance evidence is no longer valid")
            if task_status != "resolved":
                release(state, review["task_id"], actor, version, context)
            completed.append("released")
        else:
            if task_status == "finished":
                with database(state) as con:
                    guard(con, actor, version, context, "dispatch")
                    row = con.execute("SELECT status,data FROM tasks WHERE id=?", (review["task_id"],)).fetchone()
                    if row["status"] != "finished":
                        raise ValueError("task status changed before closing reviewed attempt")
                    con.execute("UPDATE tasks SET status='resolved' WHERE id=?", (review["task_id"],))
                    request = json.loads(row["data"])["request"]
                    evidence = {"event_id": event_id, "path": str(path), "sha256": sha, "verdict": verdict}
                    if has_schema(con):
                        journal(con, review["task_id"], request.get("generation", 1), actor,
                                "attempt_closed_after_review", evidence)
                    audit(con, "attempt_closed_after_review", evidence)
            elif task_status != "resolved":
                raise ValueError("reviewed attempt has not finished")
            completed.append("closed")
        with database(state) as con:
            allowed = config_of(con).get("workflow", {}).get("continuation_verdicts", ["accept", "repair"])
        result = {"status": "SETTLED", "event_id": event_id, "task_id": review["task_id"],
                  "verdict": verdict, "completed": completed}
        if verdict in allowed:
            result["parent_review"] = next_gate(state, event_id, actor, version, context)["parent_review"]
            completed.append("next_gate")
        return result
    except Exception as exc:
        return {"status": "SETTLE_INCOMPLETE", "event_id": event_id, "task_id": review["task_id"],
                "completed": completed, "error": str(exc)[:500]}


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
    for name in ("register-task", "ack", "review", "settle", "next-check", "resolve-task", "set-config"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--actor", required=True); cmd.add_argument("--version", type=int, required=True)
        cmd.add_argument("--context", choices=["main", "side", "unknown"], default="unknown")
        if name == "set-config":
            cmd.add_argument("--changes", required=True, help="JSON object, e.g. {\"scheduler.max_concurrency\": 2}")
        elif name == "resolve-task":
            cmd.add_argument("--task", required=True); cmd.add_argument("--evidence", required=True)
        elif name == "register-task": cmd.add_argument("--request", required=True)
        else: cmd.add_argument("--event", required=True)
        if name in ("review", "settle"):
            cmd.add_argument("--file", required=True); cmd.add_argument("--verdict", required=True)
    for name in ("bindings", "inbox", "roles", "label-panes"):
        sub.add_parser(name)
    sub.choices["label-panes"].add_argument("--force", action="store_true")
    status_cmd = sub.add_parser("status")
    inspect_cmd = sub.add_parser("inspect"); inspect_cmd.add_argument("--task", required=True)
    for cmd in (status_cmd, inspect_cmd):
        cmd.add_argument("--summary", action="store_true")
        cmd.add_argument("--format", choices=("json", "text"), default="json")
        cmd.add_argument("--stale-after", type=float, default=900)
    deliver = sub.add_parser("deliver"); deliver.add_argument("--event", required=True)
    args = parser.parse_args()
    from monitor_task import Transport, read_json
    if args.command == "init": result = initialize(args.state, read_json(args.config))
    elif args.command in ("bind", "switch"):
        spec = prepare_binding_spec(read_json(args.spec), args.mode, args.policy, args.reason)
        transport = Transport({})
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
    elif args.command == "set-config":
        result = update_config(args.state, json.loads(args.changes), args.actor, args.version, args.context)
    elif args.command == "resolve-task": result = resolve_task(args.state, args.task, args.actor, args.version, args.context, args.evidence)
    elif args.command == "ack": result = acknowledge(args.state, args.event, args.actor, args.version, args.context)
    elif args.command == "review": result = record_review(args.state, args.event, args.actor, args.version, args.context, args.file, args.verdict)
    elif args.command == "settle": result = settle(args.state, args.event, args.actor, args.version,
                                                 args.context, args.file, args.verdict)
    elif args.command == "next-check": result = next_gate(args.state, args.event, args.actor, args.version, args.context)
    elif args.command == "deliver": result = deliver_event(args.state, args.event, Transport({}))
    elif args.command == "roles": result = roles(args.state)
    elif args.command == "label-panes": result = label_panes(args.state, force=args.force)
    elif args.command in ("status", "inspect"):
        from workflow_scheduler import snapshot
        if args.summary or args.format == "text":
            from workflow_status import summarize
            result = summarize(args.state, getattr(args, "task", None), args.stale_after)
        else: result = snapshot(args.state, getattr(args, "task", None))
    else:
        with database(args.state) as con:
            table = "bindings" if args.command == "bindings" else "events"
            result = [dict(row) for row in con.execute("SELECT * FROM " + table)]
            for item in result:
                item["data"] = json.loads(item["data"])
    if args.command in ("status", "inspect") and args.format == "text":
        from workflow_status import format_text
        print(format_text(result))
    else: print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
