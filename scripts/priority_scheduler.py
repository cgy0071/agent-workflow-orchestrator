"""Priority admission over the existing ledger; Task/Assignment stay authoritative.

SQL helpers never commit. Runtime helpers persist an intent before external send.
Queue entries are frozen inputs, not a second task lifecycle.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

import awo_service as service
import budget
import monitor_task as monitor
import workflow_control as control
import workflow_scheduler as legacy

TOOLS = ("scheduler.enqueue", "scheduler.claim", "scheduler.priority", "assignment.close")
DEFAULT_CAPABILITIES = ("send", "status", "collect")
AGING_MINUTES = {1: 480, 2: 240, 3: 120, 4: 60}
MAX_PROMOTIONS = {1: 1, 2: 2, 3: 2, 4: 2}


def create_scheduler_schema(con):
    """Add metadata in the caller's transaction; do not own a schema version."""
    service._ensure_column(con, "v2_task", "runtime_owner",
                           "TEXT NOT NULL DEFAULT 'legacy' CHECK (runtime_owner IN ('legacy','v2'))")
    if service._table_exists(con, "meta"):
        legacy.create_schema(con)
    else:
        # Standalone schema fixtures have no initialized legacy authority.
        # Create only the same additive tables; never manufacture meta/config.
        for statement in legacy.DDL:
            con.execute(statement)
    _create_queue_schema(con)
    con.execute("""CREATE TABLE IF NOT EXISTS v2_scheduler_intent (
      assignment_id TEXT PRIMARY KEY REFERENCES v2_assignment(assignment_id),
      run_id TEXT NOT NULL UNIQUE REFERENCES v2_run_link(run_id),
      attempt_id TEXT NOT NULL UNIQUE, contract_json TEXT NOT NULL,
      controller_id TEXT NOT NULL, controller_version INTEGER NOT NULL,
      controller_epoch INTEGER NOT NULL,
      send_state TEXT NOT NULL CHECK (send_state IN
        ('ready','attempted','submitted','uncertain','running','review_ready','closed')),
      attempted_at TEXT, submitted_at TEXT, response_json TEXT, closure_json TEXT,
      created_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS v2_priority_override (
      task_id TEXT PRIMARY KEY REFERENCES v2_task(task_id),
      previous_priority TEXT NOT NULL, expires_at TEXT NOT NULL,
      reason TEXT NOT NULL, actor TEXT NOT NULL
    )""")


def _create_queue_schema(con):
    """Preserve old single-slot approvals in a caller-owned schema upgrade."""
    columns = [r[1] for r in con.execute("PRAGMA table_info(v2_scheduler_queue)")]
    old = bool(columns) and "slot_id" not in columns
    name = "v2_scheduler_queue_slots" if old else "v2_scheduler_queue"
    if old:
        if not con.in_transaction:
            raise service.ServiceError("Queue schema upgrade requires a caller-owned transaction.")
        if service._table_exists(con, name):
            raise service.ServiceError("Unexpected queue migration table; inspect before upgrading.")
        extensions = con.execute("SELECT 1 FROM sqlite_master WHERE tbl_name='v2_scheduler_queue' "
                                 "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchone()
        if extensions:
            raise service.ServiceError("Custom queue indexes or triggers require an explicit migration.")
    con.execute("CREATE TABLE IF NOT EXISTS " + name + """ (
      task_id TEXT NOT NULL REFERENCES v2_task(task_id),
      slot_id INTEGER NOT NULL DEFAULT 0 CHECK (slot_id BETWEEN 0 AND 8),
      project_id TEXT NOT NULL REFERENCES v2_project(project_id),
      attempt_id TEXT NOT NULL UNIQUE, enqueued_at TEXT NOT NULL, due_at TEXT,
      task_revision INTEGER NOT NULL, contract_json TEXT NOT NULL,
      starved_notified INTEGER NOT NULL DEFAULT 0,
      deadline_notified INTEGER NOT NULL DEFAULT 0,
      collaboration_ticket_id TEXT UNIQUE,
      PRIMARY KEY(task_id,slot_id)
    )""")
    if old:
        names = ("task_id,project_id,attempt_id,enqueued_at,due_at,task_revision,contract_json,"
                 "starved_notified,deadline_notified")
        con.execute("INSERT INTO " + name + " (" + names + ") SELECT " + names + " FROM v2_scheduler_queue")
        con.execute("DROP TABLE v2_scheduler_queue")
        con.execute("ALTER TABLE " + name + " RENAME TO v2_scheduler_queue")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)


def _date(value, label="timestamp"):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise service.ServiceError(label + " must be an ISO-8601 timestamp with a timezone")


def _instant(now=None):
    return _date(service._now() if now is None else now)


def _fields(payload, allowed, required=()):
    if not isinstance(payload, dict):
        raise service.ServiceError("payload must be an object")
    extra, missing = set(payload) - set(allowed), set(required) - set(payload)
    if extra:
        raise service.ServiceError("unknown fields: " + ", ".join(sorted(extra)))
    if missing:
        raise service.ServiceError("missing fields: " + ", ".join(sorted(missing)))


def _revision(row, expected):
    if type(expected) is not int or expected < 1 or expected != row["revision"]:
        raise service.ServiceError("stale or invalid expected_revision; current is " + str(row["revision"]))


def _task(con, project_id, task_id):
    if not isinstance(task_id, str):
        raise service.ServiceError("task_id is required")
    row = con.execute("SELECT * FROM v2_task WHERE task_id=? AND project_id=?", (task_id, project_id)).fetchone()
    if row is None:
        raise service.ServiceError("Task does not belong to this project: " + task_id)
    return row


def _collaboration_policy(con, task_id):
    if not service._table_exists(con, "v2_collaboration_policy"):
        return None
    return con.execute("SELECT * FROM v2_collaboration_policy WHERE task_id=?", (task_id,)).fetchone()


def _principal(binding):
    """Derive sidecar identity only from a validated native binding."""
    return {"role": binding["mode"], "agent_id": service._agent_id_for_binding(binding["instance_id"]),
            "principal_id": binding["instance_id"]}


def _slot(payload):
    slot = payload.get("slot_id", 0)
    if type(slot) is not int or not 0 <= slot <= 8:
        raise service.ServiceError("slot_id must be an integer from 0 through 8.")
    return slot


def _task_progress(con, task, state, now, accepted_id=None):
    """Advance native Task and only still-current sibling approval fences."""
    revision = task["revision"] + 1
    changed = con.execute(
        "UPDATE v2_task SET task_state=?,runtime_owner='v2',revision=?,updated_at=?,"
        "accepted_evidence_id=CASE WHEN ? IS NOT NULL THEN ? ELSE accepted_evidence_id END "
        "WHERE task_id=? AND revision=?",
        (state, revision, now, accepted_id, accepted_id, task["task_id"], task["revision"]))
    if changed.rowcount != 1:
        raise service.ServiceError("Task changed during scheduler progression.")
    if _collaboration_policy(con, task["task_id"]):
        con.execute("UPDATE v2_scheduler_queue SET task_revision=? WHERE task_id=? AND task_revision=?",
                    (revision, task["task_id"], task["revision"]))
    return revision


def _guard(con, project_id, actor):
    if not con.in_transaction:
        raise service.ServiceError("scheduler writes require caller-owned BEGIN IMMEDIATE")
    con.execute("UPDATE v2_service_state SET id=id WHERE id=1")
    state = service._state_row(con)
    if state["project_id"] != project_id:
        raise service.ServiceError("project differs from the active workflow")
    if state["frozen"] or not state["v2_enabled"]:
        raise service.ServiceError("scheduler is frozen or disabled")
    row = con.execute("SELECT version FROM bindings WHERE id=? AND status='active'", (actor,)).fetchone()
    if row is None:
        raise service.ServiceError("controller binding is stale")
    return state, service._controller(con, actor, row["version"], "main")


def _event(con, project_id, entity, identity, kind, actor, data):
    return service._event(con, project_id, entity, identity, kind, actor, "v2-scheduler", None, data)


def _priority_details(row, now):
    base = row["priority"]
    if base == "P0" and row.get("override_expires_at") and _date(row["override_expires_at"]) <= now:
        base = row["previous_priority"]
    p, queued = int(base[1]), _date(row["enqueued_at"])
    minutes = max(0.0, (now - queued).total_seconds() / 60)
    due = _date(row["due_at"]) if row.get("due_at") else None
    critical = due is not None and due - now <= timedelta(hours=24)
    if p == 0:
        effective, promoted, starved = 0, False, False
    else:
        steps = min(MAX_PROMOTIONS[p], int(minutes // AGING_MINUTES[p]))
        effective = max(1, p - steps)
        if critical:
            effective = min(effective, 1)
        starved = minutes >= 7 * 24 * 60 and effective > 1
        if starved:
            effective = 1
        promoted = effective < p
    key = (effective, int(promoted), 0 if starved else 1, queued, row["task_id"], row.get("slot_id", 0))
    return {"effective_priority": "P" + str(effective), "base_priority": base,
            "promoted": promoted, "starved": starved, "deadline_critical": critical,
            "waiting_minutes": minutes, "key": key}


def admission_key(task, now=None):
    """Pure deterministic comparator, also used by the read projection."""
    return _priority_details(dict(task), _instant(now))["key"]


def _queue_rows(con, project_id):
    if not service._table_exists(con, "v2_scheduler_queue"):
        return []
    rows = [dict(row) for row in con.execute(
        "SELECT q.*,t.priority,t.task_state,t.revision,o.expires_at AS override_expires_at,"
        "o.previous_priority FROM v2_scheduler_queue q JOIN v2_task t ON t.task_id=q.task_id "
        "LEFT JOIN v2_priority_override o ON o.task_id=t.task_id "
        "WHERE t.project_id=? AND ((q.collaboration_ticket_id IS NULL AND t.task_state='queued') "
        "OR (q.collaboration_ticket_id IS NOT NULL AND t.task_state IN "
        "('queued','active','review_ready','repair_required','blocked'))) "
        "AND NOT EXISTS (SELECT 1 FROM v2_scheduler_intent i WHERE i.attempt_id=q.attempt_id)",
        (project_id,))]
    # Cancelled tickets remain immutable history, not pending queue candidates.
    return [r for r in rows if not r["collaboration_ticket_id"] or (
        service._table_exists(con, "v2_collaboration_ticket") and con.execute(
            "SELECT 1 FROM v2_collaboration_ticket WHERE ticket_id=? AND state='queued'",
            (r["collaboration_ticket_id"],)).fetchone())]


def queue_status(con, project_id, now=None):
    """Read-only: no harness polling, execution, release, or notifications."""
    instant, items = _instant(now), []
    state = service._state_row(con)
    if project_id != state["project_id"]:
        raise service.ServiceError("project differs from the active workflow")
    for row in sorted(_queue_rows(con, project_id), key=lambda r: admission_key(r, instant)):
        detail = _priority_details(row, instant)
        detail.pop("key")
        items.append({"task_id": row["task_id"], "slot_id": row["slot_id"], "revision": row["revision"],
                      "enqueued_at": row["enqueued_at"], "due_at": row["due_at"], **detail,
                      "reason": "Waiting for admission; priority does not bypass safety gates."})
    return {"status": "QUEUE", "enabled": bool(state["v2_enabled"]), "frozen": bool(state["frozen"]),
            "items": items, "steer_supported": False, "preempt_supported": False}


def assignment_get(con, project_id, assignment_id):
    row = con.execute("SELECT a.* FROM v2_assignment a JOIN v2_task t ON t.task_id=a.task_id "
                      "WHERE a.assignment_id=? AND t.project_id=?", (assignment_id, project_id)).fetchone()
    if row is None:
        raise service.ServiceError("Assignment does not belong to this project")
    result = dict(row)
    if service._table_exists(con, "v2_scheduler_intent"):
        intent = con.execute("SELECT * FROM v2_scheduler_intent WHERE assignment_id=?", (assignment_id,)).fetchone()
        if intent:
            result.update(run_id=intent["run_id"], send_state=intent["send_state"],
                          attempted_at=intent["attempted_at"], submitted_at=intent["submitted_at"],
                          closure=json.loads(intent["closure_json"]) if intent["closure_json"] else None)
    return result


def next_task(con, project_id, agent_id=None, now=None):
    """Evaluate durable gates read-only; live worker status waits for claim."""
    result = queue_status(con, project_id, now)
    if result["frozen"] or not result["enabled"]:
        return {"status": "WAITING", "candidate": None, "reserved": False,
                "reasons": [{"reason": "Scheduling is frozen or disabled."}]}
    rows = {(row["task_id"], row["slot_id"]): row for row in _queue_rows(con, project_id)}
    reasons = []
    for item in result["items"]:
        row = rows[item["task_id"], item["slot_id"]]
        target = json.loads(row["contract_json"])["agent_id"]
        if agent_id is not None and agent_id != target:
            continue
        try:
            task, contract, _, _, _ = _candidate_checks(con, project_id, row)
            _budget_eligibility(con, project_id, task["task_id"], contract["budget"])
        except (ValueError, OSError, RuntimeError) as exc:
            reasons.append({"task_id": row["task_id"], "slot_id": row["slot_id"], "reason": str(exc)})
            continue
        return {"status": "CANDIDATE", "candidate": dict(item, agent_id=target),
                "reserved": False, "reasons": reasons,
                "live_worker_check_pending": True}
    return {"status": "WAITING", "candidate": None, "reserved": False, "reasons": reasons}


def _budget_eligibility(con, project_id, task_id, dimensions):
    """Read projection only; final reservation still uses the budget authority."""
    # A new run has no pre-existing account. Never create one on a query.
    for kind, identity in (("task", task_id), ("workflow", project_id)):
        account = budget._budget(con, project_id, kind, identity, create=False)
        if account is None:
            continue
        if account["state"] in budget.BLOCKED_STATES:
            raise service.ServiceError("Budget state blocks admission: " + account["state"])
        limits = budget._limits(account)
        if set(limits) - set(dimensions):
            raise service.ServiceError("Bounded budget dimensions require estimates.")
        _, _, unknown, _ = budget._known_actual(con, account)
        for key, count in budget._coverage_unknown(con, account, limits).items():
            unknown[key] = unknown.get(key, 0) + count
        exposure = budget._exposure(con, account)
        for key, cap in limits.items():
            if key == "attempts":
                if budget._derived_attempts(con, account) + 1 > int(cap):
                    raise service.ServiceError("Task/workflow attempt cap reached.")
            elif key == "repairs":
                if budget._derived_repairs(con, account) >= int(cap):
                    raise service.ServiceError("Task/workflow repair cap reached.")
            elif unknown.get(key) and budget._policy(account)["on_unknown"] == "block":
                raise service.ServiceError("Unknown usage blocks admission for " + key)
            elif exposure.get(key, 0.0) + dimensions[key] > float(cap):
                raise service.ServiceError("Budget cap exceeded for " + kind + ":" + key)


def _worker_binding(con, request, frozen=None):
    worker = control.active(con, request["worker"].get("role"))
    requested = {k: v for k, v in request["worker"].items() if k != "baseline_completion_seq"}
    if worker["mode"] != "worker" or worker["spec"] != requested:
        raise service.ServiceError("Requested worker differs from its active verified binding.")
    if frozen and (worker["instance_id"], worker["version"]) != (frozen["instance_id"], frozen["version"]):
        raise service.ServiceError("Worker binding changed after enqueue; inspect the queued request.")
    reviewer = control.active(con, request["reviewer"].get("role"))
    if reviewer["mode"] not in ("controller", "reviewer") or control.same_binding_agent(worker, reviewer):
        raise service.ServiceError("An independent reviewer binding is required.")
    if reviewer["spec"] != request["reviewer"]:
        raise service.ServiceError("Reviewer differs from the active binding.")
    cfg, role = control.config_of(con), request["reviewer"]["role"]
    expected = cfg.get("workflow", {}).get("reviewer_role")
    if role not in cfg["roles"] or (expected and expected != role and request["authorization"].get("allow_reviewer_override") is not True):
        raise service.ServiceError("Reviewer role is not authorized by the workflow.")
    return worker


def _request_check(con, request, message, *, allow_missing_launch_cwd=False):
    monitor.validate_request(request, require_dispatch=False)
    if request.get("dispatch_confirmed") is not False:
        raise service.ServiceError("New attempt requires dispatch_confirmed=false.")
    if request["authorization"].get("dispatch_worker") is not True:
        raise service.ServiceError("Explicit dispatch_worker authorization is required.")
    db = next(row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main")
    if not request.get("workflow_state") or Path(request["workflow_state"]).resolve() != Path(db).resolve().parent:
        raise service.ServiceError("request.workflow_state must identify this ledger.")
    if not control.valid_task_id(request["task_id"]) or not control.valid_task_nonce(request["nonce"]):
        raise service.ServiceError("Invalid attempt task ID or nonce.")
    if not isinstance(message, str) or request["task_id"] not in message or request["nonce"] not in message:
        raise service.ServiceError("message must bind attempt task ID and nonce.")
    spec = request["worker"]
    if spec["adapter"] == "command":
        monitor.validate_argv(spec.get("prompt_argv"))
        if spec["prompt_argv"].count("{message}") != 1:
            raise service.ServiceError("prompt_argv requires one standalone {message}.")
    elif spec["adapter"] != "herdr":
        raise service.ServiceError("This adapter has no verified send operation.")
    if not Path(spec["expected_identity"]["launch_cwd"]).is_dir() and not allow_missing_launch_cwd:
        raise service.ServiceError("Existing worker directory is unavailable.")
    parent = request.get("parent_review")
    if parent:
        row = con.execute("SELECT data FROM reviews WHERE event=?", (parent["event_id"],)).fetchone()
        stored = json.loads(row[0]) if row else {}
        if any(parent.get(k) != stored.get(k) for k in ("event_id", "task_id", "nonce", "path", "sha256", "verdict")):
            raise service.ServiceError("Parent review differs from the sealed ledger record.")
        allowed = control.config_of(con).get("workflow", {}).get("continuation_verdicts", ["accept", "repair"])
        if stored.get("verdict") not in allowed:
            raise service.ServiceError("Parent review does not authorize continuation.")
    limit = control.config_of(con).get("scheduler", {}).get("max_repair_chain", 3)
    if type(limit) is not int or limit < 0:
        raise service.ServiceError("Invalid max_repair_chain configuration.")
    if limit and control.repair_chain(con, request.get("parent_task")) >= limit:
        if request["authorization"].get("allow_long_repair_chain") is not True:
            raise service.ServiceError("Repair limit reached; an Owner decision is required.")


def _capabilities(con, project_id, contract):
    _agent_binding(contract["agent_id"], contract["worker_binding"])
    agent = con.execute("SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?", (contract["agent_id"], project_id)).fetchone()
    if not agent or agent["lifecycle"] != "active" or agent["role"] != "worker":
        raise service.ServiceError("Selected Agent is not an active approved worker.")
    spec = contract["request"]["worker"]
    if (agent["harness"], agent["harness_version"]) != (spec.get("harness", "unknown"), spec.get("version", "unknown")):
        raise service.ServiceError("Agent harness/version differs from worker binding.")
    for key in contract["required_capabilities"]:
        row = con.execute("SELECT * FROM v2_agent_capability WHERE agent_id=? AND capability_key=?", (agent["agent_id"], key)).fetchone()
        if not row or row["level"] != "supported":
            raise service.ServiceError("Worker capability is unverified or unavailable: " + key)
        if any(row[k] != agent[k] for k in ("harness_version", "os", "host")):
            raise service.ServiceError("Capability evidence has a different version or host: " + key)
        try:
            if not row["checked_at"] or not row["evidence_path"] or not row["evidence_sha256"]:
                raise ValueError()
            _date(row["checked_at"])
            if service._file_sha(row["evidence_path"]) != row["evidence_sha256"]:
                raise ValueError()
        except (ValueError, OSError, TypeError):
            raise service.ServiceError("Capability evidence is missing or changed: " + key)
    return agent


def _agent_binding(agent_id, worker):
    # Enrollment alone does not prove ownership of a native session. Initially
    # use only the registry identity imported from this exact verified binding.
    if agent_id != service._agent_id_for_binding(worker["instance_id"]):
        raise service.ServiceError("Agent identity does not match the actual Worker binding; "
                                   "an enrollment identity requires a Controller-confirmed binding bridge.")


def _enqueue(con, project_id, actor, payload):
    _fields(payload, ("task_id", "expected_revision", "request", "message", "agent_id", "budget",
                      "required_capabilities", "resources", "dependencies", "workspace_mode",
                      "task_kind", "due_at", "dag_run", "dag_node", "slot_id", "expected_collaboration_revision",
                      "workspace_base_commit"),
            ("task_id", "expected_revision", "request", "message", "agent_id", "budget"))
    task = _task(con, project_id, payload["task_id"])
    _revision(task, payload["expected_revision"])
    slot, policy = _slot(payload), _collaboration_policy(con, task["task_id"])
    allowed = ("draft", "ready", "queued", "active", "review_ready", "repair_required", "blocked") if policy else (
        "draft", "ready", "repair_required")
    if task["task_state"] not in allowed:
        raise service.ServiceError("Task must be draft, ready, or independently marked repair_required.")
    if not policy and (task["execution_mode"] != "single" or task["fanout_slots"] != 1 or slot != 0):
        raise service.ServiceError("Fanout and ensemble require a Controller-approved collaboration policy.")
    if not policy and "expected_collaboration_revision" in payload:
        raise service.ServiceError("No collaboration policy exists for this Task.")
    if policy:
        _revision(policy, payload.get("expected_collaboration_revision"))
    if con.execute("SELECT 1 FROM v2_assignment WHERE task_id=? AND slot_id=? AND holds_slot=1",
                   (task["task_id"], slot)).fetchone():
        raise service.ServiceError("An earlier Assignment still owns this task's slot.")
    mode = payload.get("workspace_mode", "directory")
    if mode not in ("directory", "git_worktree"):
        raise service.ServiceError("workspace_mode must be directory or git_worktree.")
    kind = payload.get("task_kind", "artifact")
    if kind not in ("artifact", "code"):
        raise service.ServiceError("task_kind must be artifact or code.")
    base_commit = payload.get("workspace_base_commit")
    if mode == "git_worktree":
        if kind != "code":
            raise service.ServiceError("A Git worktree requires task_kind=code.")
        if not isinstance(base_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base_commit):
            raise service.ServiceError("git_worktree requires a full workspace_base_commit.")
        # Git reports object names in lowercase; freeze the same spelling the
        # send gate will compare against the materialized workspace base.
        base_commit = base_commit.lower()
    elif base_commit is not None:
        raise service.ServiceError("workspace_base_commit requires workspace_mode=git_worktree.")
    now = _instant()
    if task["priority"] == "P0":
        override = con.execute("SELECT expires_at FROM v2_priority_override WHERE task_id=?", (task["task_id"],)).fetchone()
        if not override or _date(override[0]) <= now:
            raise service.ServiceError("P0 needs a controller priority override with a reason and future expiry.")
    due = _date(payload["due_at"], "due_at").isoformat() if payload.get("due_at") is not None else None
    request = copy.deepcopy(payload["request"])
    generation = con.execute("SELECT COALESCE(MAX(generation),0)+1 FROM v2_assignment WHERE task_id=?", (task["task_id"],)).fetchone()[0]
    if policy:
        generation = max(generation, con.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM v2_collaboration_ticket WHERE task_id=?",
            (task["task_id"],)).fetchone()[0])
    if not isinstance(request, dict):
        raise service.ServiceError("request must be an object.")
    if "generation" in request and (type(request["generation"]) is not int or request["generation"] != generation):
        raise service.ServiceError("Request generation must equal the next Assignment generation.")
    # A frozen legacy DAG may omit generation (its protocol default is one).
    # Do not change that template hash by materializing an optional default.
    if policy or "generation" in request or not payload.get("dag_run") or generation != 1:
        request["generation"] = generation
    # A worktree Task freezes the exact directory the worker identity must report
    # as its launch cwd. The directory must be new; workspace.create provisions it.
    workspace_path = None
    if mode == "git_worktree":
        launch = request.get("worker", {}).get("expected_identity", {}).get("launch_cwd")
        if not isinstance(launch, str) or not Path(launch).is_absolute():
            raise service.ServiceError("git_worktree requires an absolute frozen worker launch_cwd.")
        planned = Path(launch).resolve()
        if planned.exists():
            raise service.ServiceError("Frozen worker launch directory must be new; refusing adoption.")
        workspace_path = str(planned)
    _request_check(con, request, payload["message"], allow_missing_launch_cwd=mode == "git_worktree")
    worker = _worker_binding(con, request)
    _agent_binding(payload["agent_id"], worker)
    if con.execute("SELECT 1 FROM tasks WHERE id=? OR nonce=?", (request["task_id"], request["nonce"])).fetchone():
        raise service.ServiceError("Attempt ID or nonce was already used; create a fresh attempt.")
    for row in con.execute("SELECT task_id,contract_json FROM v2_scheduler_queue"):
        old = json.loads(row["contract_json"])["request"]
        if old["task_id"] == request["task_id"] or old["nonce"] == request["nonce"]:
            raise service.ServiceError("Attempt ID or nonce is already frozen in another queue entry.")
    if not policy and task["task_state"] == "repair_required":
        latest = con.execute("SELECT legacy_task_id FROM v2_assignment WHERE task_id=? ORDER BY generation DESC LIMIT 1", (task["task_id"],)).fetchone()
        if not latest or request.get("parent_task") != latest[0] or request.get("parent_review", {}).get("verdict") != "repair":
            raise service.ServiceError("Repair must reference the last Assignment's sealed repair review.")
    required = payload.get("required_capabilities", list(DEFAULT_CAPABILITIES))
    if not isinstance(required, list) or any(not isinstance(x, str) or not x.strip() for x in required):
        raise service.ServiceError("required_capabilities must be a list of capability keys.")
    resources = payload.get("resources", {})
    if not isinstance(resources, dict) or any(not isinstance(k, str) or not k or type(v) is not int or v < 1 for k, v in resources.items()):
        raise service.ServiceError("resources must map names to positive integer units.")
    dimensions = budget._dimensions(payload["budget"], "budget")
    deps = payload.get("dependencies", [])
    if not isinstance(deps, list) or any(not isinstance(x, str) for x in deps) or len(set(deps)) != len(deps):
        raise service.ServiceError("dependencies must contain unique stable Task IDs.")
    for dep in deps:
        _task(con, project_id, dep)
    graph = {row["task_id"]: json.loads(row["contract_json"])["dependencies"]
             for row in con.execute("SELECT task_id,contract_json FROM v2_scheduler_queue WHERE project_id=?", (project_id,))}
    graph[task["task_id"]] = deps
    def visit(node, path):
        if node in path:
            raise service.ServiceError("Dependency cycle is not allowed.")
        for child in graph.get(node, []):
            visit(child, path | {node})
    visit(task["task_id"], set())
    if bool(payload.get("dag_run")) != bool(payload.get("dag_node")):
        raise service.ServiceError("dag_run and dag_node must be supplied together.")
    contract = {"task_id": task["task_id"], "request": request, "message": payload["message"],
                "agent_id": payload["agent_id"], "budget": dimensions,
                "required_capabilities": sorted(set(required) | set(DEFAULT_CAPABILITIES)),
                "resources": resources, "dependencies": deps, "worker_binding": worker,
                "workspace_mode": mode, "task_kind": kind,
                "workspace_base_commit": base_commit, "workspace_path": workspace_path,
                "dag_run": payload.get("dag_run"), "dag_node": payload.get("dag_node")}
    approval_state, approver = _guard(con, project_id, actor)
    contract.update(approved_controller_id=actor, approved_controller_version=approver["version"],
                    approved_epoch=approval_state["controller_epoch"])
    # Controller selects a registry identity and freezes its binding here.
    agent = con.execute("SELECT project_id,role,lifecycle FROM v2_agent WHERE agent_id=?", (contract["agent_id"],)).fetchone()
    if not agent or agent["project_id"] != project_id or agent["role"] != "worker" or agent["lifecycle"] != "active":
        raise service.ServiceError("Selected Agent is not an approved worker in this project.")
    prepared = None
    if policy:
        # Dependency/classification is Task-wide; competing slots cannot approve
        # different paths around the same governance gate.
        for row in con.execute("SELECT contract_json FROM v2_scheduler_queue WHERE task_id=?", (task["task_id"],)):
            old = json.loads(row[0])
            if (set(old["dependencies"]) != set(deps) or any(
                    old.get(key) != contract.get(key) for key in ("dag_run", "dag_node", "task_kind"))):
                raise service.ServiceError("Collaboration slots must share Task dependencies and artifact classification.")
        import collaboration_runtime as collaboration
        prepared = collaboration.prepare_enqueue(con, project_id, _principal(approver), task["task_id"],
                                                 slot, contract, payload["expected_collaboration_revision"])
        contract, request = prepared["contract"], prepared["contract"]["request"]
    revision = _task_progress(con, task, "queued" if task["task_state"] in ("draft", "ready", "repair_required", "blocked")
                              or not policy else task["task_state"], now.isoformat())
    con.execute("INSERT INTO v2_scheduler_queue(task_id,slot_id,project_id,attempt_id,enqueued_at,due_at,task_revision,contract_json,collaboration_ticket_id) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id,slot_id) DO UPDATE SET attempt_id=excluded.attempt_id,"
                "enqueued_at=excluded.enqueued_at,due_at=excluded.due_at,task_revision=excluded.task_revision,"
                "contract_json=excluded.contract_json,collaboration_ticket_id=excluded.collaboration_ticket_id,"
                "starved_notified=0,deadline_notified=0",
                (task["task_id"], slot, project_id, request["task_id"], now.isoformat(), due, revision,
                 _json(contract), prepared["ticket_id"] if prepared else None))
    event = _event(con, project_id, "task", task["task_id"], "task.enqueued", actor,
                   {"revision": revision, "slot_id": slot, "attempt_id": request["task_id"], "contract_sha256": service._sha(contract)})
    return {"status": "QUEUED", "task_id": task["task_id"], "slot_id": slot, "revision": revision, "event_id": event,
            **({"collaboration_ticket_id": prepared["ticket_id"], "collaboration_revision": prepared["revision"]}
               if prepared else {})}


def _priority(con, project_id, actor, payload):
    _fields(payload, ("task_id", "expected_revision", "priority", "reason", "expires_at"),
            ("task_id", "expected_revision", "priority", "reason"))
    task = _task(con, project_id, payload["task_id"])
    _revision(task, payload["expected_revision"])
    service._validate_priority(payload["priority"])
    pending_collaboration = _collaboration_policy(con, task["task_id"]) and any(
        r["task_id"] == task["task_id"] for r in _queue_rows(con, project_id))
    if task["task_state"] not in ("draft", "ready", "queued", "repair_required") and not pending_collaboration:
        raise service.ServiceError("Queue priority cannot interrupt or change a running Assignment.")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise service.ServiceError("A priority change needs a reason.")
    if payload["priority"] == "P0":
        expires = _date(payload.get("expires_at"), "expires_at")
        if expires <= _instant():
            raise service.ServiceError("P0 expiry must be in the future.")
        previous = con.execute("SELECT previous_priority FROM v2_priority_override WHERE task_id=?", (task["task_id"],)).fetchone()
        base = previous[0] if previous else (task["priority"] if task["priority"] != "P0" else "P3")
        con.execute("INSERT INTO v2_priority_override VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                    "expires_at=excluded.expires_at,reason=excluded.reason,actor=excluded.actor",
                    (task["task_id"], base, expires.isoformat(), payload["reason"], actor))
    else:
        if payload.get("expires_at") is not None:
            raise service.ServiceError("expires_at applies only to a P0 override.")
        con.execute("DELETE FROM v2_priority_override WHERE task_id=?", (task["task_id"],))
    revision = task["revision"] + 1
    con.execute("UPDATE v2_task SET priority=?,revision=?,updated_at=? WHERE task_id=?",
                (payload["priority"], revision, service._now(), task["task_id"]))
    con.execute("UPDATE v2_scheduler_queue SET task_revision=? WHERE task_id=? AND task_revision=?",
                (revision, task["task_id"], task["revision"]))
    event = _event(con, project_id, "task", task["task_id"], "priority_override", actor, payload)
    return {"status": "PRIORITY_UPDATED", "task_id": task["task_id"], "revision": revision, "event_id": event}


def _verified_delivery(con, attempt, event_id):
    task = con.execute("SELECT * FROM tasks WHERE id=?", (attempt,)).fetchone()
    event = con.execute("SELECT * FROM events WHERE id=? AND task=?", (event_id, attempt)).fetchone()
    rr = con.execute("SELECT data FROM reviews WHERE event=?", (event_id,)).fetchone()
    if not task or not event or not rr:
        raise service.ServiceError("Independent review is not recorded for this Assignment.")
    data, review = json.loads(task["data"]), json.loads(rr[0])
    request = data["request"]
    if task["request_hash"] != control.digest(request) or not request.get("dispatch_confirmed"):
        raise service.ServiceError("Frozen confirmed request hash differs.")
    if review.get("task_id") != attempt or review.get("nonce") != task["nonce"] or review.get("event_id") != event_id:
        raise service.ServiceError("Review identity differs from this attempt.")
    reviewer = review.get("actor")
    if not isinstance(reviewer, dict) or reviewer.get("mode") not in ("controller", "reviewer"):
        raise service.ServiceError("Review has no authoritative reviewer provenance.")
    if reviewer.get("role") != event["role"] or control.same_binding_agent(reviewer, data["worker_binding"]):
        raise service.ServiceError("Independent reviewer identity is required.")
    state, problems = service._review_check(task, request, event, review)
    evidence, issues = service._artifact_checks(task, request, event)
    record = json.loads(event["data"])
    receipt = record.get("receipt") or {}
    if record.get("request_sha256") != task["request_hash"]:
        issues.append("event belongs to a different frozen request")
    if receipt.get("completion_seq") is None or monitor.sequence(receipt["completion_seq"]) <= request["worker"]["baseline_completion_seq"]:
        issues.append("completion sequence is not fresh")
    expected = [request["completion"]["binding_artifact"]] + request["completion"].get("artifacts", [])
    paths = {str(Path(row[0]).resolve()) for row in evidence}
    for item in expected:
        name = item if isinstance(item, str) else item["path"]
        if str((Path(request["cwd"]) / name).resolve()) not in paths:
            issues.append("frozen deliverable missing from receipt")
    if state != "sealed" or problems or issues or not evidence:
        raise service.ServiceError("Reviewed evidence is missing or changed: " + "; ".join(problems + issues))
    return review, evidence


def _dependencies(con, project_id, contract):
    deps = list(contract["dependencies"])
    if contract.get("dag_run"):
        node = legacy.assert_ready(con, contract["dag_run"], contract["dag_node"], contract["request"])
        row = con.execute("SELECT data FROM dag_runs WHERE id=?", (contract["dag_run"],)).fetchone()
        graph = json.loads(row[0])["graph"]
        for key in node.get("needs", []):
            attempt = next(n for n in graph["nodes"] if n["id"] == key)["request"]["task_id"]
            mapped = con.execute("SELECT v2_task_id FROM v2_legacy_map WHERE legacy_task_id=?", (attempt,)).fetchone()
            if not mapped:
                raise service.ServiceError("DAG dependency needs reviewed artifact classification before V2 execution.")
            deps.append(mapped[0])
    code_dependencies = []
    for dependency in set(deps):
        parent = _task(con, project_id, dependency)
        if parent["task_state"] not in ("accepted", "finalized", "landed"):
            raise service.ServiceError("Dependency is waiting for independent acceptance: " + dependency)
        review = con.execute("SELECT * FROM v2_review WHERE task_id=? AND verdict='accept' AND state='sealed' "
                             "ORDER BY reviewed_at DESC LIMIT 1", (dependency,)).fetchone()
        if not review:
            raise service.ServiceError("Dependency has no sealed independent review: " + dependency)
        assignment = con.execute("SELECT * FROM v2_assignment WHERE assignment_id=?", (review["assignment_id"],)).fetchone()
        intent = con.execute("SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?", (review["assignment_id"],)).fetchone()
        if not assignment or assignment["holds_slot"] or not intent:
            raise service.ServiceError("Dependency closure or artifact classification is unverified: " + dependency)
        parent_kind = json.loads(intent[0]).get("task_kind")
        if parent_kind not in ("artifact", "code"):
            raise service.ServiceError("Dependency has an unsupported artifact classification: " + dependency)
        if parent_kind == "code":
            code_dependencies.append(dependency)
        _verified_delivery(con, assignment["legacy_task_id"], review["event_id"])
        if assignment["workspace_id"]:
            from workspace_runtime import verify_reviewed_workspace
            verify_reviewed_workspace(con, assignment["assignment_id"])
        evidence = con.execute("SELECT * FROM v2_evidence WHERE task_id=? AND assignment_id=?",
                               (dependency, assignment["assignment_id"])).fetchall()
        if not evidence or parent["accepted_evidence_id"] not in {e["evidence_id"] for e in evidence}:
            raise service.ServiceError("Dependency is missing its accepted evidence reference.")
        for item in evidence:
            if not item["sealed"] or item["state"] != "sealed" or service._file_sha(item["path"]) != item["sealed_sha256"]:
                raise service.ServiceError("Dependency evidence bytes changed: " + dependency)
    if code_dependencies:
        # A code dependency may only unlock a workspace built from the exact
        # recorded materialization of all required parents and their sealed
        # evidence. Missing, changed or partial proofs keep the slot blocked.
        base = contract.get("workspace_base_commit")
        if contract.get("workspace_mode") != "git_worktree" or not base:
            raise service.ServiceError(
                "Code dependencies require a git_worktree Task frozen at the materialized base commit.")
        import merge_queue as merge
        try:
            proof = merge.materialization_gate(con, project_id, contract["task_id"], base)
        except merge.MergeError as exc:
            raise service.ServiceError("Dependency materialization proof is invalid: " + str(exc)) from exc
        if not proof.get("ready"):
            raise service.ServiceError("Dependency materialization proof is missing, stale or incomplete.")
        missing = sorted(set(code_dependencies) - set(proof.get("materialized_from") or []))
        if missing:
            raise service.ServiceError("Materialization parents do not cover code dependencies: " + ", ".join(missing))


def _claims(con, exclude_assignment=None):
    """V2 holds_slot dominates lifecycle labels; include legacy claims once."""
    result, seen = [], set()
    for row in con.execute("SELECT a.assignment_id,a.legacy_task_id,i.contract_json FROM v2_assignment a "
                           "LEFT JOIN v2_scheduler_intent i ON i.assignment_id=a.assignment_id WHERE a.holds_slot=1"):
        seen.add(row["legacy_task_id"])
        if row["assignment_id"] == exclude_assignment:
            continue
        if not row["contract_json"]:
            raise service.ServiceError("An unresolved historical Assignment needs Controller inspection.")
        c = json.loads(row["contract_json"])
        result.append((row["legacy_task_id"], c))
    old = dict(legacy.claims(con))
    for row in con.execute("SELECT id,data FROM tasks WHERE status='running'"):
        if row["id"] not in old:
            data = json.loads(row["data"])
            old[row["id"]] = {"resources": {}, "request": data["request"]}
    for attempt, value in old.items():
        if attempt in seen:
            continue
        row = con.execute("SELECT data FROM tasks WHERE id=?", (attempt,)).fetchone()
        request = value.get("request") or json.loads(row[0])["request"]
        result.append((attempt, {"request": request, "resources": value.get("resources", {}),
                                 "host": "local", "harness": request["worker"].get("harness", "unknown")}))
    return result


def _capacity(con, contract, agent, exclude_assignment=None):
    claims, cfg = _claims(con, exclude_assignment), control.config_of(con).get("scheduler", {})
    limit = cfg.get("max_concurrency", 1)
    if type(limit) is not int or limit < 1:
        raise service.ServiceError("max_concurrency must be a positive integer.")
    if len(claims) >= limit:
        raise service.ServiceError("All workflow execution slots are occupied; wait for Controller closure.")
    request = contract["request"]
    for attempt, claim in claims:
        old = claim["request"]
        if old["worker"]["role"] == request["worker"]["role"]:
            raise service.ServiceError("This worker still owns an open Assignment: " + attempt)
        paths = [old["cwd"], old["worker"]["expected_identity"]["launch_cwd"]]
        wanted = [request["cwd"], request["worker"]["expected_identity"]["launch_cwd"]]
        if any(legacy.overlap(a, b) for a in paths for b in wanted):
            raise service.ServiceError("A directory is still claimed by " + attempt)
    for dimension, config_key in (("host", "per_host"), ("harness", "per_harness")):
        limits = cfg.get(config_key, {})
        if not isinstance(limits, dict) or any(type(v) is not int or v < 1 for v in limits.values()):
            raise service.ServiceError(config_key + " must map names to positive integer limits.")
        bound = limits.get(agent[dimension])
        used = sum(c.get(dimension, c["request"]["worker"].get(dimension, "local")) == agent[dimension] for _, c in claims)
        if bound is not None and used >= bound:
            raise service.ServiceError("The " + dimension + " execution limit is reached: " + agent[dimension])
    capacities = cfg.get("resources", {})
    for resource, amount in contract["resources"].items():
        capacity = capacities.get(resource)
        if type(capacity) is not int or capacity < 1:
            raise service.ServiceError("No positive capacity is configured for resource: " + resource)
        used = sum(c["resources"].get(resource, 0) for _, c in claims)
        if used + amount > capacity:
            raise service.ServiceError("Waiting for resource capacity: " + resource)
    from workflow_evidence import check_paths
    check_paths(con, request)


def _quiescent(request, observed):
    # A status alias cannot turn known uncertainty or a cancel request into
    # authoritative evidence that the old writer stopped.
    return (observed.get("status") not in ("unknown", "unavailable", "disconnected",
                                         "uncertain", "cancel_requested", "pause_requested")
            and legacy._worker_quiescent(request, observed))


def _idle(request, transport):
    observed = transport.status(request["worker"])
    if not monitor.identity_matches(observed.get("identity", {}), request["worker"]["expected_identity"]):
        raise service.ServiceError("Worker identity changed; inspect its binding.")
    if not _quiescent(request, observed):
        raise service.ServiceError("Worker is not idle; blocked or disconnected workers retain their claims.")
    if monitor.sequence(observed.get("completion_seq")) != request["worker"]["baseline_completion_seq"]:
        raise service.ServiceError("Worker completion baseline changed; prepare a freshly verified request.")
    if request["worker"]["adapter"] == "herdr":
        monitor.Transport.check_prompt_available(observed)
    return observed


def _candidate_checks(con, project_id, row, actor=None):
    """Shared read-only durable gates; no polling, writes, or reservations."""
    task = _task(con, project_id, row["task_id"])
    ticket_id = row["collaboration_ticket_id"]
    states = ("queued", "active", "review_ready", "repair_required", "blocked") if ticket_id else ("queued",)
    if task["task_state"] not in states or task["revision"] != row["task_revision"]:
        raise service.ServiceError("Queued Task revision changed; inspect its frozen inputs.")
    if not ticket_id and (task["execution_mode"] != "single" or task["fanout_slots"] != 1
                          or row["slot_id"] != 0 or _collaboration_policy(con, task["task_id"])):
        raise service.ServiceError("Only a single Assignment slot is enabled.")
    if con.execute("SELECT 1 FROM v2_assignment WHERE task_id=? AND slot_id=? AND holds_slot=1",
                   (task["task_id"], row["slot_id"])).fetchone():
        raise service.ServiceError("An Assignment already owns this task's slot.")
    c = json.loads(row["contract_json"])
    approval_state = service._state_row(con)
    actor = c["approved_controller_id"] if actor is None else actor
    approver = service._controller(con, actor, c["approved_controller_version"], "main")
    if (approval_state["project_id"] != project_id or approval_state["frozen"]
            or not approval_state["v2_enabled"]):
        raise service.ServiceError("Scheduling is frozen, disabled, or belongs to another project.")
    if (c["approved_controller_id"], c["approved_controller_version"], c["approved_epoch"]) != (
            actor, approver["version"], approval_state["controller_epoch"]):
        raise service.ServiceError("Queue approval belongs to an obsolete Controller binding or epoch.")
    if ticket_id:
        import collaboration_runtime as collaboration
        checked = collaboration.check_admission(con, project_id, _principal(approver), ticket_id)
        if (checked["contract"] != c or checked["task_id"] != task["task_id"]
                or checked["slot_id"] != row["slot_id"]):
            raise service.ServiceError("Queued collaboration contract differs from its immutable ticket.")
    request = c["request"]
    _request_check(con, request, c["message"], allow_missing_launch_cwd=c.get("workspace_mode") == "git_worktree")
    worker = _worker_binding(con, request, c["worker_binding"])
    agent = _capabilities(con, project_id, c)
    _dependencies(con, project_id, c)
    _capacity(con, c, agent)
    value = {"generation": request.get("generation", 1), "message": c["message"], "message_sha256": control.digest(c["message"]),
             "resources": c["resources"], "run": c.get("dag_run"), "node": c.get("dag_node")}
    legacy.check_claim(con, request, value)
    if con.execute("SELECT 1 FROM tasks WHERE id=? OR nonce=?", (request["task_id"], request["nonce"])).fetchone():
        raise service.ServiceError("Attempt ID or nonce is already registered.")
    generation = con.execute("SELECT COALESCE(MAX(generation),0)+1 FROM v2_assignment WHERE task_id=?", (task["task_id"],)).fetchone()[0]
    if not ticket_id and request.get("generation", 1) != generation:
        raise service.ServiceError("Queued generation is stale.")
    return task, c, worker, agent, value


def _admit(con, project_id, actor, row):
    task, c, worker, agent, value = _candidate_checks(con, project_id, row, actor)
    request, generation = c["request"], c["request"].get("generation", 1)
    _idle(request, monitor.Transport(request))
    state, who = _guard(con, project_id, actor)
    now, admitted = service._now(), None
    if row["collaboration_ticket_id"]:
        import collaboration_runtime as collaboration
        admitted = collaboration.admit_in_transaction(con, project_id, _principal(who), row["collaboration_ticket_id"])
        aid, rid, reservations = admitted["assignment_id"], admitted["run_id"], admitted["reservation_ids"]
        if admitted["contract"] != c or admitted["generation"] != generation or admitted["slot_id"] != row["slot_id"]:
            raise service.ServiceError("Collaboration admission differs from the validated contract.")
    else:
        aid, rid = "asg-" + uuid.uuid4().hex, "run-" + uuid.uuid4().hex
        con.execute("INSERT INTO v2_assignment(assignment_id,task_id,agent_id,generation,slot_id,holds_slot,"
                    "state,nonce,legacy_task_id,legacy_dispatch_status,created_at,updated_at) VALUES (?,?,?,?,0,1,'reserved',?,?,'reserved',?,?)",
                    (aid, task["task_id"], agent["agent_id"], generation, request["nonce"], request["task_id"], now, now))
        reservations = budget._reserve_in_transaction(con, project_id, rid, aid, generation, c["budget"], "scheduler_estimate")
    data = {"request": request, "request_sha256": control.digest(request),
            "authorization_sha256": control.digest(request["authorization"]), "worker_binding": worker,
            "controller": who, "registered_at": now}
    con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)", (request["task_id"], request["nonce"], control.digest(request),
                request["worker"]["role"], "running", _json(data)))
    legacy.reserve(con, request, who, value)
    con.execute("INSERT INTO v2_legacy_map(legacy_task_id,v2_task_id,v2_assignment_id,nonce,request_sha256,generation,source) "
                "VALUES (?,?,?,?,?,?,'v2-scheduler')", (request["task_id"], task["task_id"], aid, request["nonce"], control.digest(request), generation))
    c.update(host=agent["host"], harness=agent["harness"], active_revision=task["revision"] + 1)
    if admitted:
        c["collaboration_ticket_id"] = row["collaboration_ticket_id"]
    con.execute("INSERT INTO v2_scheduler_intent(assignment_id,run_id,attempt_id,contract_json,controller_id,"
                "controller_version,controller_epoch,send_state,created_at) VALUES (?,?,?,?,?,?,?,'ready',?)",
                (aid, rid, request["task_id"], _json(c), actor, who["version"], state["controller_epoch"], now))
    revision = _task_progress(con, task, "active", now)
    event = _event(con, project_id, "assignment", aid, "assignment.reserved", actor,
                   {"run_id": rid, "generation": generation, "slot_id": row["slot_id"]})
    return {"status": "RESERVED", "task_id": task["task_id"], "assignment_id": aid, "run_id": rid,
            "generation": generation, "slot_id": row["slot_id"], "revision": 1, "task_revision": revision,
            "reservation_ids": reservations, "event_id": event, "sent": False,
            **({"collaboration_ticket_id": row["collaboration_ticket_id"], "collaboration_revision": admitted["revision"]}
               if admitted else {})}


def _claim(con, project_id, actor, payload):
    _fields(payload, ("task_id", "expected_revision", "agent_id", "slot_id"))
    if ("task_id" in payload) != ("expected_revision" in payload):
        raise service.ServiceError("task_id and expected_revision must be supplied together.")
    if "slot_id" in payload:
        _slot(payload)
        if "task_id" not in payload:
            raise service.ServiceError("slot_id requires task_id and expected_revision.")
    if "agent_id" in payload and (not isinstance(payload["agent_id"], str) or not payload["agent_id"]):
        raise service.ServiceError("agent_id must be nonempty.")
    if "task_id" in payload:
        _revision(_task(con, project_id, payload["task_id"]), payload["expected_revision"])
    now, reasons = _instant(), []
    rows = sorted(_queue_rows(con, project_id), key=lambda r: admission_key(r, now))
    for row in rows:
        if "task_id" in payload and row["task_id"] != payload["task_id"]:
            continue
        if "slot_id" in payload and row["slot_id"] != payload["slot_id"]:
            continue
        if "agent_id" in payload and json.loads(row["contract_json"])["agent_id"] != payload["agent_id"]:
            continue
        detail = _priority_details(row, now)
        for flag, column, kind in (("starved", "starved_notified", "queue.starved"),
                                    ("deadline_critical", "deadline_notified", "queue.deadline_risk")):
            if detail[flag] and not row[column]:
                _event(con, project_id, "task", row["task_id"], kind, actor, {"enqueued_at": row["enqueued_at"]})
                con.execute("UPDATE v2_scheduler_queue SET " + column + "=1 WHERE task_id=? AND slot_id=?",
                            (row["task_id"], row["slot_id"]))
        # A budget rejection can occur after insertion. Roll back this whole
        # candidate even when we continue to the next candidate in this tick.
        con.execute("SAVEPOINT scheduler_candidate")
        try:
            result = _admit(con, project_id, actor, row)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            con.execute("ROLLBACK TO scheduler_candidate")
            con.execute("RELEASE scheduler_candidate")
            reasons.append({"task_id": row["task_id"], "slot_id": row["slot_id"], "reason": str(exc)})
        except BaseException:
            con.execute("ROLLBACK TO scheduler_candidate")
            con.execute("RELEASE scheduler_candidate")
            raise
        else:
            con.execute("RELEASE scheduler_candidate")
            return result
    return {"status": "WAITING", "sent": False, "reasons": reasons,
            "reason": "No queued task currently passes admission." if reasons else "No matching queued task."}


def _intent(con, project_id, assignment_id):
    assignment = assignment_get(con, project_id, assignment_id)
    intent = con.execute("SELECT * FROM v2_scheduler_intent WHERE assignment_id=?", (assignment_id,)).fetchone()
    if not intent:
        raise service.ServiceError("Assignment has no V2 scheduler send intent.")
    return assignment, dict(intent), json.loads(intent["contract_json"])


def _runtime_open(state, actor, version, context, expected_epoch):
    con = service._connect(service._db_path(state))
    try:
        con.execute("BEGIN IMMEDIATE")
        service._controller(con, actor, version, context)
        row = service._state_row(con)
        if type(expected_epoch) is not int or expected_epoch != row["controller_epoch"]:
            raise service.ServiceError("Stale or invalid controller epoch.")
        _guard(con, row["project_id"], actor)
        return con, row["project_id"]
    except BaseException:
        con.rollback()
        con.close()
        raise


def _reserved_budget(con, project_id, assignment, intent, contract):
    """A pause, lowered cap, or changed reservation fences an unsent run."""
    if con.execute("SELECT 1 FROM v2_usage_close WHERE run_id=?", (intent["run_id"],)).fetchone():
        raise service.ServiceError("Run usage was already closed; sending is forbidden.")
    scopes = (("run", intent["run_id"]), ("task", assignment["task_id"]), ("workflow", project_id))
    for kind, identity in scopes:
        account = budget._budget(con, project_id, kind, identity, create=False)
        if not account or account["state"] in budget.BLOCKED_STATES:
            raise service.ServiceError("Budget state now blocks this reserved run.")
        rows = {r["dimension"]: r for r in con.execute("SELECT * FROM v2_budget_reservation WHERE budget_id=? AND assignment_id=? AND generation=?",
                (account["budget_id"], assignment["assignment_id"], assignment["generation"]))}
        if set(rows) != set(contract["budget"]):
            raise service.ServiceError("Frozen budget reservation dimensions changed.")
        for key, value in contract["budget"].items():
            if rows[key]["state"] != "held" or rows[key]["amount"] != value:
                raise service.ServiceError("Frozen budget reservation changed.")
        limits, exposure = budget._limits(account), budget._exposure(con, account)
        if set(limits) - set(contract["budget"]):
            raise service.ServiceError("Newly bounded budget dimensions require fresh admission.")
        if any(exposure.get(key, 0) > cap for key, cap in limits.items() if key not in ("attempts", "repairs")):
            raise service.ServiceError("Budget cap now blocks this reserved run.")
        if "attempts" in limits and budget._derived_attempts(con, account) > limits["attempts"]:
            raise service.ServiceError("Attempt cap now blocks this reserved run.")
        if "repairs" in limits and budget._derived_repairs(con, account) >= limits["repairs"]:
            raise service.ServiceError("Repair cap now blocks this reserved run.")
        _, _, unknown, _ = budget._known_actual(con, account)
        for key, count in budget._coverage_unknown(con, account, limits, exclude_assignment=assignment["assignment_id"]).items():
            unknown[key] = unknown.get(key, 0) + count
        if budget._policy(account)["on_unknown"] == "block" and any(unknown.get(k) for k in limits):
            raise service.ServiceError("Unknown usage now blocks this reserved run.")


def _admitted_collaboration(con, project_id, assignment, contract, approver):
    """Revalidate an admitted immutable ticket without reserving it again."""
    import collaboration_runtime as collaboration
    view = collaboration.query(con, project_id, _principal(approver),
                               "collaboration_get", {"task_id": assignment["task_id"]})
    ticket_id = contract.get("collaboration_ticket_id")
    ticket = con.execute("SELECT * FROM v2_collaboration_ticket WHERE ticket_id=? AND task_id=?",
                         (ticket_id, assignment["task_id"])).fetchone()
    queue = con.execute("SELECT * FROM v2_scheduler_queue WHERE task_id=? AND slot_id=?",
                        (assignment["task_id"], assignment["slot_id"])).fetchone()
    runtime_keys = {"host", "harness", "active_revision", "collaboration_ticket_id"}
    frozen = {k: v for k, v in contract.items() if k not in runtime_keys}
    sealed = hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
    if (not ticket or ticket["state"] != "admitted" or ticket["assignment_id"] != assignment["assignment_id"]
            or ticket["generation"] != assignment["generation"] or ticket["slot_id"] != assignment["slot_id"]
            or ticket["contract_sha256"] != sealed or json.loads(ticket["contract_json"]) != frozen or not queue
            or queue["collaboration_ticket_id"] != ticket_id
            or json.loads(queue["contract_json"]) != frozen
            or queue["task_revision"] != view["task"]["revision"]):
        raise service.ServiceError("Admitted collaboration ticket or Task revision changed.")
    if con.execute("SELECT 1 FROM v2_collaboration_proposal WHERE task_id=? AND withdrawn_at IS NULL",
                   (assignment["task_id"],)).fetchone():
        raise service.ServiceError("A winner is selected; no new competitor may start.")


def _send_gates(con, project_id, assignment, intent, contract, actor, version, expected_epoch, transport):
    if not assignment["holds_slot"] or assignment["state"] != "reserved":
        raise service.ServiceError("Assignment no longer holds an unsent reserved slot.")
    if (intent["controller_id"], intent["controller_version"], intent["controller_epoch"]) != (actor, version, expected_epoch):
        raise service.ServiceError("Intent belongs to a different controller binding or epoch.")
    task = _task(con, project_id, assignment["task_id"])
    if _collaboration_policy(con, task["task_id"]):
        if task["task_state"] not in ("active", "review_ready") or task["accepted_evidence_id"]:
            raise service.ServiceError("Collaboration Task is no longer open for execution.")
        _admitted_collaboration(con, project_id, assignment, contract, service._controller(con, actor, version, "main"))
    elif (contract.get("collaboration_ticket_id") or assignment["slot_id"] != 0
          or task["task_state"] != "active" or task["revision"] != contract["active_revision"]):
        raise service.ServiceError("Task changed after admission.")
    _request_check(con, contract["request"], contract["message"],
                   allow_missing_launch_cwd=contract.get("workspace_mode") == "git_worktree")
    _worker_binding(con, contract["request"], contract["worker_binding"])
    agent = _capabilities(con, project_id, contract)
    _dependencies(con, project_id, contract)
    _capacity(con, contract, agent, assignment["assignment_id"])
    _reserved_budget(con, project_id, assignment, intent, contract)
    if assignment["workspace_id"]:
        import workspace_manager
        from workspace_manager import check_workspace_ready
        ready = check_workspace_ready(con, assignment["workspace_id"],
                                      assignment_id=assignment["assignment_id"], host=agent["host"],
                                      allow_active=intent["send_state"] == "attempted")
        if not ready["ready"]:
            raise service.ServiceError("Managed workspace is not ready: " + "; ".join(ready["reasons"]))
        if contract.get("workspace_mode") == "git_worktree":
            # The frozen worker binding must launch in this exact source
            # workspace at the materialized base; inventory alone is not proof.
            ws = workspace_manager.get_workspace(con, assignment["workspace_id"])
            if not ws or ws["kind"] != "git_worktree":
                raise service.ServiceError("Code execution requires the reserved Git source workspace.")
            launch = Path(contract["request"]["worker"]["expected_identity"]["launch_cwd"]).resolve()
            if launch != Path(ws["path"]).resolve():
                raise service.ServiceError("Worker launch directory differs from the frozen source workspace.")
            if contract.get("workspace_base_commit") != ws["base_commit"]:
                raise service.ServiceError("Workspace base commit differs from the materialized dependency base.")
            if contract["request"]["worker"].get("adapter") != "command":
                raise service.ServiceError("Git worktree execution requires the verified native command adapter.")
            try:
                execution = json.loads(contract["message"])
            except (TypeError, ValueError):
                execution = None
            if not isinstance(execution, dict):
                raise service.ServiceError("Git worktree execution requires a frozen JSON message contract.")
            declared = execution.get("execution_cwd")
            if not isinstance(declared, str) or not Path(declared).is_absolute():
                raise service.ServiceError("Frozen message lacks an absolute execution_cwd.")
            if Path(declared).resolve() != Path(ws["path"]).resolve():
                raise service.ServiceError("Frozen execution_cwd differs from the reserved source workspace.")
            if execution.get("sandbox") != "workspace-write":
                raise service.ServiceError("Git worktree execution requires sandbox=workspace-write.")
            delivery = Path(contract["request"]["cwd"]).resolve()
            if delivery == Path(ws["path"]).resolve() or delivery.is_relative_to(Path(ws["path"]).resolve()):
                raise service.ServiceError("Delivery cwd must remain outside the source workspace.")
    elif contract.get("workspace_mode") == "git_worktree":
        raise service.ServiceError("Git worktree Task has no reserved managed workspace.")
    _idle(contract["request"], transport)


def dispatch_assignment(state, assignment_id, actor, version, context="main", *, expected_epoch, transport=None):
    """Attempt send once. A persisted attempted intent is never retried."""
    con, project_id = _runtime_open(state, actor, version, context, expected_epoch)
    try:
        assignment, intent, contract = _intent(con, project_id, assignment_id)
        if intent["send_state"] != "ready":
            return {"status": "ALREADY_ATTEMPTED", "assignment_id": assignment_id,
                    "send_state": intent["send_state"], "sent": False}
        transport = transport or monitor.Transport(contract["request"])
        _send_gates(con, project_id, assignment, intent, contract, actor, version, expected_epoch, transport)
        if assignment["workspace_id"]:
            from workspace_manager import mark_workspace_active
            mark_workspace_active(con, assignment["workspace_id"])
        con.execute("UPDATE v2_scheduler_intent SET send_state='attempted',attempted_at=? WHERE assignment_id=?",
                    (service._now(), assignment_id))
        legacy.journal(con, intent["attempt_id"], assignment["generation"], actor, "dispatch_attempted",
                       {"assignment_id": assignment_id, "no_blind_retry": True})
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    # The committed intent fences process death both before and after the
    # external effect. Hold the writer lock across final guards and bounded send
    # so a controller handoff/freeze cannot race with submission.
    con = None
    try:
        con, project_id = _runtime_open(state, actor, version, context, expected_epoch)
        assignment, intent, contract = _intent(con, project_id, assignment_id)
        if intent["send_state"] != "attempted":
            raise service.ServiceError("Intent changed before send; inspect, do not retry.")
        _send_gates(con, project_id, assignment, intent, contract, actor, version, expected_epoch, transport)
        response = transport.send(contract["request"]["worker"], contract["message"])
        now = service._now()
        con.execute("UPDATE v2_scheduler_intent SET send_state='submitted',submitted_at=?,response_json=? WHERE assignment_id=?",
                    (now, _json({"response": str(response)[:2000]}), assignment_id))
        dispatch = json.loads(con.execute("SELECT data FROM dispatches WHERE task=?", (intent["attempt_id"],)).fetchone()[0])
        dispatch.update(response=str(response)[:2000], submitted_at=now)
        con.execute("UPDATE dispatches SET status='submitted',data=? WHERE task=?", (_json(dispatch), intent["attempt_id"]))
        con.execute("UPDATE v2_assignment SET legacy_dispatch_status='submitted',revision=revision+1,updated_at=? WHERE assignment_id=?", (now, assignment_id))
        legacy.journal(con, intent["attempt_id"], assignment["generation"], actor, "dispatch_submitted", {"assignment_id": assignment_id})
        _event(con, project_id, "assignment", assignment_id, "assignment.submitted", actor, {})
        con.commit()
        return {"status": "SUBMITTED", "assignment_id": assignment_id, "sent": True, "revision": assignment["revision"] + 1}
    except Exception as exc:
        if con is not None:
            con.rollback()
        # No additional send, and no claim release on error. If the authority
        # fence changed, leave the committed attempted intent for recovery.
        return {"status": "DISPATCH_UNCERTAIN", "assignment_id": assignment_id,
                "reason": str(exc), "claims_retained": True, "retry_allowed": False}
    finally:
        if con is not None:
            con.close()


def _confirmed(con, contract):
    attempt = contract["request"]["task_id"]
    row = con.execute("SELECT data,request_hash FROM tasks WHERE id=?", (attempt,)).fetchone()
    if not row:
        raise service.ServiceError("Legacy attempt record is missing.")
    data = json.loads(row["data"])
    if control.digest(data["request"]) != row["request_hash"]:
        raise service.ServiceError("Legacy attempt hash changed.")
    expected = copy.deepcopy(contract["request"])
    if data["request"].get("dispatch_confirmed"):
        expected["dispatch_confirmed"] = True
    if data["request"] != expected:
        raise service.ServiceError("Legacy attempt differs from the frozen intent.")
    if not data["request"]["dispatch_confirmed"]:
        data["request"]["dispatch_confirmed"] = True
        data["request_sha256"] = control.digest(data["request"])
        con.execute("UPDATE tasks SET request_hash=?,data=? WHERE id=?", (data["request_sha256"], _json(data), attempt))
        con.execute("UPDATE v2_legacy_map SET request_sha256=? WHERE legacy_task_id=?", (data["request_sha256"], attempt))
    return data["request"]


def _publish_completion(con, request, detail, receipt, generation, actor):
    # Same identity and schema as workflow_control.publish_event; no new inbox
    # authority and no nested transaction/notification side effect.
    event_id = control.digest({"request": control.digest(request), "status": "review_ready"})
    report = {"status": "review_ready", "detail": detail}
    data = {"event_id": event_id, "request_sha256": control.digest(request),
            "report": report, "receipt": receipt, "created_at": control.stamp()}
    old = con.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
    if old:
        if json.loads(old[0]).get("receipt") != receipt:
            raise service.ServiceError("Completion evidence changed after its first recorded receipt.")
    else:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                    (event_id, request["task_id"], request["nonce"], request["reviewer"]["role"], "pending", _json(data)))
        legacy.journal(con, request["task_id"], generation, actor, "delivery_review_ready", data, event_id)
    con.execute("UPDATE tasks SET status='finished' WHERE id=? AND status='running'", (request["task_id"],))
    return event_id


def reconcile_in_transaction(con, project_id, actor, assignment_id, *, transport=None):
    """Observe actual completion; never send, accept, or release a slot."""
    _guard(con, project_id, actor)
    assignment, intent, contract = _intent(con, project_id, assignment_id)
    if intent["send_state"] == "closed":
        return {"status": "CLOSED", "assignment": assignment}
    if intent["send_state"] == "ready":
        return {"status": "NOT_SENT", "assignment_id": assignment_id, "claims_retained": True}
    request = contract["request"]
    transport = transport or monitor.Transport(request)
    observed, receipt, detail, event_id = None, None, "", None
    try:
        observed = transport.status(request["worker"])
        # inspect_once consumes exactly the observation used for confirmation.
        class Observation:
            def status(self, spec):
                return observed

            def output(self, spec):
                return transport.output(spec)
        status, detail, _, receipt = monitor.inspect_once(request, Observation())
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        status, detail = "unavailable", str(exc)
    mapped = {"working": "running", "review_ready": "completed", "blocked": "blocked",
              "failed": "failed", "exited": "failed"}.get(status, "uncertain")
    confirmed = status in ("working", "review_ready")
    if confirmed:
        confirmed_request = _confirmed(con, contract)
        if status == "review_ready":
            event_id = _publish_completion(con, confirmed_request, detail, receipt, assignment["generation"], actor)
    if assignment["review_status"] == "awaiting_review" and status != "review_ready":
        # Do not erase an existing review receipt on a later transport outage.
        mapped = "uncertain"
    review_status = "awaiting_review" if status == "review_ready" else assignment["review_status"]
    send_state = "review_ready" if status == "review_ready" else ("running" if status == "working" else "uncertain")
    now = service._now()
    if (mapped, review_status, send_state) != (assignment["state"], assignment["review_status"], intent["send_state"]):
        con.execute("UPDATE v2_assignment SET state=?,review_status=?,legacy_dispatch_status=?,revision=revision+1,updated_at=? WHERE assignment_id=?",
                    (mapped, review_status, send_state, now, assignment_id))
        _event(con, project_id, "assignment", assignment_id, "assignment.observed", actor,
               {"status": status, "detail": detail, "event_id": event_id, "claims_retained": True})
    con.execute("UPDATE v2_scheduler_intent SET send_state=? WHERE assignment_id=?", (send_state, assignment_id))
    # Keep legacy dispatch status compatible with its existing protocol.
    con.execute("UPDATE dispatches SET status=? WHERE task=?", ("running" if confirmed else "uncertain", intent["attempt_id"]))
    if status == "review_ready":
        task = _task(con, project_id, assignment["task_id"])
        terminal = task["task_state"] in ("accepted", "finalized", "landed", "rejected", "cancelled")
        if task["task_state"] != "review_ready" and not (
                _collaboration_policy(con, task["task_id"]) and (terminal or task["accepted_evidence_id"])):
            _task_progress(con, task, "review_ready", now)
    return {"status": status.upper(), "assignment_id": assignment_id, "detail": detail, "event_id": event_id,
            "claims_retained": True, "assignment": assignment_get(con, project_id, assignment_id)}


def reconcile_assignment(state, assignment_id, actor, version, context="main", *, expected_epoch, transport=None):
    con, project_id = _runtime_open(state, actor, version, context, expected_epoch)
    try:
        result = reconcile_in_transaction(con, project_id, actor, assignment_id, transport=transport)
        con.commit()
        return result
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _close(con, project_id, actor, payload):
    _fields(payload, ("assignment_id", "expected_revision", "outcome", "event_id", "evidence_path"),
            ("assignment_id", "expected_revision", "outcome"))
    assignment, intent, contract = _intent(con, project_id, payload["assignment_id"])
    _revision(assignment, payload["expected_revision"])
    outcome = payload["outcome"]
    if outcome not in ("accepted", "repair_required", "rejected", "cancelled"):
        raise service.ServiceError("Unsupported controller closure outcome.")
    if not assignment["holds_slot"] or intent["send_state"] == "closed":
        raise service.ServiceError("Assignment is already closed.")
    request, review, evidence = contract["request"], None, []
    closure = {"outcome": outcome, "actor": actor, "closed_at": service._now()}
    if outcome == "cancelled":
        if payload.get("event_id") or not isinstance(payload.get("evidence_path"), str):
            raise service.ServiceError("Cancellation needs a task/nonce-bound termination evidence_path.")
        path = Path(payload["evidence_path"]).resolve()
        text = path.read_text(encoding="utf-8-sig")
        if request["task_id"] not in text or request["nonce"] not in text or "Outcome: terminated" not in text.splitlines():
            raise service.ServiceError("Termination note must bind attempt ID, nonce, and Outcome: terminated.")
        from workflow_evidence import check_review_path
        check_review_path(con, request["task_id"], path)
        closure.update(path=str(path), sha256=service._file_sha(path))
    else:
        if not isinstance(payload.get("event_id"), str) or payload.get("evidence_path"):
            raise service.ServiceError("Review closure needs the existing independent review event_id.")
        review, evidence = _verified_delivery(con, request["task_id"], payload["event_id"])
        wanted = {"accepted": "accept", "repair_required": "repair", "rejected": "reject"}[outcome]
        if review["verdict"] != wanted or assignment["review_status"] != "awaiting_review":
            raise service.ServiceError("Closure must match this Assignment's pending independent review.")
        closure.update(event_id=payload["event_id"], review_sha256=review["sha256"])
    _worker_binding(con, request, contract["worker_binding"])
    observed = monitor.Transport(request).status(request["worker"])
    if not monitor.identity_matches(observed.get("identity", {}), request["worker"]["expected_identity"]):
        raise service.ServiceError("Worker identity changed; claims remain held.")
    if not _quiescent(request, observed):
        raise service.ServiceError("Worker is not authoritatively idle; claims remain held.")
    if review:
        receipt = json.loads(con.execute("SELECT data FROM events WHERE id=?", (payload["event_id"],)).fetchone()[0])["receipt"]
        if monitor.sequence(observed.get("completion_seq")) != monitor.sequence(receipt["completion_seq"]):
            raise service.ServiceError("Worker sequence changed since reviewed delivery; inspect before releasing.")
    closure["observation"] = observed
    if outcome == "accepted" and assignment["workspace_id"]:
        from workspace_runtime import verify_reviewed_workspace
        verify_reviewed_workspace(con, assignment["assignment_id"])
    task = _task(con, project_id, assignment["task_id"])
    task_outcome = outcome
    if _collaboration_policy(con, task["task_id"]):
        import collaboration_runtime as collaboration
        _, approver = _guard(con, project_id, actor)
        if review:
            collaboration.review_guard(con, project_id, _principal(review["actor"]), task["task_id"])
        if outcome == "accepted":
            collaboration.acceptance_guard(con, project_id, _principal(approver),
                                           assignment["assignment_id"], payload["event_id"])
        projection = collaboration.closure_projection(con, project_id, _principal(approver),
                                                     assignment["assignment_id"], outcome)
        task_outcome = projection["suggested_task_state"]
        closure["collaboration_projection"] = projection
    now, accepted_id = service._now(), None
    if review:
        for path, sealed, current, size, state in evidence:
            old = con.execute("SELECT * FROM v2_evidence WHERE task_id=? AND path=? AND sealed_sha256=?",
                              (assignment["task_id"], str(path), sealed)).fetchone()
            if old:
                if (old["assignment_id"] != assignment["assignment_id"] or not old["sealed"]
                        or old["state"] != "sealed" or old["bytes"] != size
                        or old["current_sha256"] != current):
                    raise service.ServiceError("Existing immutable evidence differs in Assignment or seal.")
                eid = old["evidence_id"]
            else:
                eid = "evd-" + service._sha({"assignment": assignment["assignment_id"], "path": str(path), "sha": sealed})[:24]
                con.execute("INSERT INTO v2_evidence(evidence_id,task_id,assignment_id,kind,path,sealed_sha256,current_sha256,bytes,sealed,state,source,created_at) "
                            "VALUES (?,?,?,'artifact',?,?,?,?,1,'sealed','v2-scheduler',?)",
                            (eid, assignment["task_id"], assignment["assignment_id"], str(path), sealed, current, size, now))
            accepted_id = accepted_id or eid
        con.execute("INSERT INTO v2_review(review_id,task_id,assignment_id,event_id,verdict,note_path,note_sha256,state,actor_json,reviewed_at,source) "
                    "VALUES (?,?,?,?,?,?,?,'sealed',?,?,'v2-scheduler')",
                    ("rev-" + uuid.uuid4().hex, assignment["task_id"], assignment["assignment_id"], payload["event_id"],
                     review["verdict"], review["path"], review["sha256"], _json(review["actor"]), review["reviewed_at"]))
        con.execute("UPDATE v2_legacy_map SET review_event_id=?,review_sha256=? WHERE legacy_task_id=?",
                    (payload["event_id"], review["sha256"], request["task_id"]))
    else:
        control.audit(con, "task_termination_inspected", dict(closure, task_id=request["task_id"] ))
    review_status = {"accepted": "accepted", "repair_required": "repair", "rejected": "rejected", "cancelled": "none"}[outcome]
    state = "cancelled" if outcome == "cancelled" else ("rejected" if outcome == "rejected" else "completed")
    con.execute("UPDATE v2_assignment SET holds_slot=0,state=?,review_status=?,revision=revision+1,updated_at=? WHERE assignment_id=?",
                (state, review_status, now, assignment["assignment_id"]))
    _task_progress(con, task, task_outcome, now, accepted_id if outcome == "accepted" else None)
    con.execute("UPDATE v2_scheduler_intent SET send_state='closed',closure_json=? WHERE assignment_id=?", (_json(closure), assignment["assignment_id"]))
    con.execute("UPDATE tasks SET status='resolved' WHERE id=?", (request["task_id"],))
    legacy.journal(con, request["task_id"], assignment["generation"], actor, "assignment_closed", closure)
    event = _event(con, project_id, "assignment", assignment["assignment_id"], "assignment.closed", actor, closure)
    return {"status": "CLOSED", "outcome": outcome, "task_state": task_outcome, "assignment_id": assignment["assignment_id"],
            "revision": assignment["revision"] + 1, "event_id": event, "holds_slot": False}


def apply_command(con, project_id, actor, tool, payload):
    """Controller-approved writes; caller owns transaction, epoch and replay."""
    if tool not in TOOLS:
        raise service.ServiceError("Unknown scheduler tool: " + str(tool))
    _guard(con, project_id, actor)
    return {"scheduler.enqueue": _enqueue, "scheduler.claim": _claim,
            "scheduler.priority": _priority, "assignment.close": _close}[tool](con, project_id, actor, payload)


def claim_for_agent(con, project_id, agent_id, payload=None):
    """Service-authenticated Worker may only consume its Controller approval."""
    payload = {} if payload is None else payload
    _fields(payload, ("task_id", "expected_revision", "slot_id"))
    if ("task_id" in payload) != ("expected_revision" in payload):
        raise service.ServiceError("task_id and expected_revision must be supplied together.")
    if "slot_id" in payload:
        _slot(payload)
        if "task_id" not in payload:
            raise service.ServiceError("slot_id requires task_id and expected_revision.")
    if not isinstance(agent_id, str) or not agent_id:
        raise service.ServiceError("Authenticated agent_id is required.")
    current = service._state_row(con)
    if current["project_id"] != project_id or current["frozen"] or not current["v2_enabled"]:
        raise service.ServiceError("Scheduling is frozen, disabled, or belongs to another project.")
    now = _instant()
    rows = sorted(_queue_rows(con, project_id), key=lambda row: admission_key(row, now))
    for row in rows:
        c = json.loads(row["contract_json"])
        if c["agent_id"] != agent_id or ("task_id" in payload and payload["task_id"] != row["task_id"]):
            continue
        if "slot_id" in payload and payload["slot_id"] != row["slot_id"]:
            continue
        service._controller(con, c["approved_controller_id"], c["approved_controller_version"], "main")
        current, who = _guard(con, project_id, c["approved_controller_id"])
        if (who["version"], current["controller_epoch"]) != (c["approved_controller_version"], c["approved_epoch"]):
            raise service.ServiceError("Controller approval changed; Worker cannot renew it.")
        return apply_command(con, project_id, c["approved_controller_id"], "scheduler.claim", dict(payload, agent_id=agent_id))
    if "task_id" in payload:
        raise service.ServiceError("No Controller-approved queue entry for this Agent and Task.")
    return {"status": "WAITING", "reason": "No Controller-approved task is assigned to this Agent.", "sent": False}


def complete_for_agent(con, project_id, agent_id, assignment_id, *, transport=None):
    """Completion is a request to observe; a Worker cannot supply a receipt."""
    assignment, intent, contract = _intent(con, project_id, assignment_id)
    if assignment["agent_id"] != agent_id:
        raise service.ServiceError("Assignment belongs to a different Agent.")
    current, who = _guard(con, project_id, intent["controller_id"])
    if (who["version"], current["controller_epoch"]) != (intent["controller_version"], intent["controller_epoch"]):
        raise service.ServiceError("Controller binding changed; request Controller reconciliation.")
    return reconcile_in_transaction(con, project_id, intent["controller_id"], assignment_id, transport=transport)


def reservation_status(con, project_id, assignment_id):
    """Read the frozen claim; no independently mutable resource claim schema."""
    assignment, intent, contract = _intent(con, project_id, assignment_id)
    request = contract["request"]
    rows = [dict(r) for r in con.execute(
        "SELECT budget_id,dimension,amount,state FROM v2_budget_reservation WHERE assignment_id=? AND generation=? ORDER BY budget_id,dimension",
        (assignment_id, assignment["generation"]))]
    return {"assignment_id": assignment_id, "holds_slot": bool(assignment["holds_slot"]),
            "resources": contract["resources"], "host": contract["host"], "harness": contract["harness"],
            "directories": [str(Path(request["cwd"]).resolve()),
                            str(Path(request["worker"]["expected_identity"]["launch_cwd"]).resolve())],
            "budget_reservations": rows, "release_requires_controller_close": bool(assignment["holds_slot"])}


def review_in_transaction(con, project_id, actor, version, context, event_id, evidence_path, verdict):
    """Same legacy review authority, usable inside the service transaction."""
    if not con.in_transaction:
        raise service.ServiceError("Review requires caller-owned BEGIN IMMEDIATE.")
    con.execute("UPDATE v2_service_state SET id=id WHERE id=1")
    current = service._state_row(con)
    if current["project_id"] != project_id or current["frozen"] or not current["v2_enabled"]:
        raise service.ServiceError("Project is not enabled for review writes.")
    who = control.guard(con, actor, version, context, "review")
    event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event or event["role"] != who["role"] or event["status"] not in ("pending", "acknowledged"):
        raise service.ServiceError("Review event is not pending for this reviewer role.")
    mapping = con.execute("SELECT v2_task_id,v2_assignment_id FROM v2_legacy_map WHERE legacy_task_id=?", (event["task"],)).fetchone()
    if not mapping:
        raise service.ServiceError("Review has no mapped V2 Assignment.")
    _task(con, project_id, mapping["v2_task_id"])
    if _collaboration_policy(con, mapping["v2_task_id"]):
        import collaboration_runtime as collaboration
        collaboration.review_guard(con, project_id, _principal(who), mapping["v2_task_id"])
    control.check_self_review(con, who, event["task"])
    task = con.execute("SELECT * FROM tasks WHERE id=?", (event["task"],)).fetchone()
    request = json.loads(task["data"])["request"]
    delivery = json.loads(event["data"])["report"]["status"]
    if delivery != "review_ready" or task["status"] == "resolved":
        raise service.ServiceError("Only open review_ready deliveries may use this review API.")
    assignment = assignment_get(con, project_id, mapping["v2_assignment_id"])
    if not assignment["holds_slot"] or assignment["review_status"] != "awaiting_review":
        raise service.ServiceError("Assignment is not awaiting independent review.")
    cfg = control.config_of(con)
    if not isinstance(verdict, str) or verdict not in cfg.get("workflow", {}).get("transitions", {}):
        raise service.ServiceError("Review verdict is not configured.")
    path = Path(evidence_path).resolve()
    if request.get("review_path") and Path(request["review_path"]).resolve() != path:
        raise service.ServiceError("Review path differs from the frozen request.")
    from workflow_evidence import check_review_path
    check_review_path(con, event["task"], path)
    raw = path.read_bytes()
    result = {"event_id": event_id, "task_id": event["task"], "nonce": event["nonce"],
              "path": str(path), "sha256": service._file_sha(path), "verdict": verdict,
              "action": cfg["workflow"]["transitions"][verdict], "actor": who,
              "workflow_config_sha256": control.digest(cfg), "reviewed_at": control.stamp()}
    # Validate using the original review/receipt validators before recording.
    synthetic_event = dict(event, status="reviewed")
    state, problems = service._review_check(task, request, synthetic_event, result)
    _, issues = service._artifact_checks(task, request, event)
    if state != "sealed" or problems or issues or raw != path.read_bytes():
        raise service.ServiceError("Review or completion evidence is invalid: " + "; ".join(problems + issues))
    if assignment["workspace_id"]:
        from workspace_runtime import review_gate
        result["workspace_inventory"] = review_gate(con, assignment, verdict=verdict)
    con.execute("INSERT INTO reviews VALUES (?,?)", (event_id, _json(result)))
    con.execute("UPDATE events SET status='reviewed' WHERE id=?", (event_id,))
    control.audit(con, "event_acknowledged", {"event": event_id, "actor": actor, "version": version})
    control.audit(con, "review_recorded", result)
    legacy.journal(con, event["task"], assignment["generation"], actor, "review_" + verdict, result)
    return result
