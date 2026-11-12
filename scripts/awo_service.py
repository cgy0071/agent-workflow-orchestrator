"""Opt-in AWO V2 application-service boundary, schema, and conservative migration.

The existing workflow database is the single authority. Stable Tasks and
Assignments extend it through guarded, additive migrations. Runtime modules
share the command/idempotency, revision, controller and freeze boundaries.
Legacy projection import never rewrites a Task already owned by the V2 runtime.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import uuid

import workflow_control as control

V2_SCHEMA_VERSION = 6
TASK_STATES = (
    "draft", "ready", "queued", "active", "review_ready", "accepted", "landed",
    "finalized", "rejected", "repair_required", "cancelled", "blocked",
    "migration_uncertain",
)
ASSIGNMENT_STATES = (
    "reserved", "running", "completed", "failed", "blocked", "cancelled",
    "preempted", "rejected", "uncertain", "historical_registered",
)
REVIEW_STATUSES = ("none", "awaiting_review", "reviewing", "accepted", "repair", "rejected")
EVIDENCE_STATES = ("sealed", "unknown", "missing", "changed")
REVIEW_STATES = ("sealed", "unknown", "missing", "changed")
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
EXECUTION_MODES = ("single", "fanout", "ensemble")


class ServiceError(ValueError):
    """An actionable service-level rejection."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _project_id(con):
    row = con.execute("SELECT value FROM meta WHERE key='workflow_id'").fetchone()
    if not row or not row[0]:
        raise ServiceError("workflow_id is missing from the ledger")
    return row[0]


def _table_exists(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _connect(path, readonly=False):
    path = Path(path).resolve()
    if readonly:
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    else:
        con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _db_path(state):
    path = Path(state).resolve() / "workflow.sqlite3"
    if not path.is_file():
        raise ServiceError("workflow state is not initialized: " + str(path))
    return path


DDL = (
    """CREATE TABLE IF NOT EXISTS v2_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS v2_project (
      project_id TEXT PRIMARY KEY,
      root_path TEXT NOT NULL,
      created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS v2_service_state (
      id INTEGER PRIMARY KEY CHECK (id=1),
      project_id TEXT NOT NULL REFERENCES v2_project(project_id),
      v2_enabled INTEGER NOT NULL DEFAULT 0 CHECK (v2_enabled IN (0,1)),
      frozen INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
      controller_epoch INTEGER NOT NULL DEFAULT 0 CHECK (controller_epoch >= 0),
      updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS v2_task (
      task_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES v2_project(project_id),
      legacy_status TEXT,
      title TEXT NOT NULL DEFAULT '',
      body TEXT NOT NULL DEFAULT '',
      priority TEXT NOT NULL DEFAULT 'P3' CHECK (priority IN ('P0','P1','P2','P3','P4')),
      task_state TEXT NOT NULL CHECK (task_state IN
        ('draft','ready','queued','active','review_ready','accepted','landed',
         'finalized','rejected','repair_required','cancelled','blocked','migration_uncertain')),
      execution_mode TEXT NOT NULL DEFAULT 'single'
        CHECK (execution_mode IN ('single','fanout','ensemble')),
      fanout_slots INTEGER NOT NULL DEFAULT 1 CHECK (fanout_slots BETWEEN 1 AND 8),
      accepted_evidence_id TEXT,
      revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS v2_agent (
      agent_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES v2_project(project_id),
      handle TEXT NOT NULL,
      role TEXT NOT NULL CHECK (role IN ('worker','observer','reviewer','controller')),
      harness TEXT NOT NULL,
      harness_version TEXT NOT NULL,
      host TEXT NOT NULL,
      os TEXT NOT NULL,
      lifecycle TEXT NOT NULL CHECK (lifecycle IN ('pending','active','revoked')),
      token_hash TEXT UNIQUE,
      source TEXT NOT NULL,
      parent_agent_id TEXT REFERENCES v2_agent(agent_id),
      created_at TEXT NOT NULL,
      approved_at TEXT,
      last_seen_at TEXT,
      revoked_at TEXT,
      revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
      UNIQUE (project_id, handle)
    )""",
    """CREATE TABLE IF NOT EXISTS v2_enrollment_grant (
      grant_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES v2_project(project_id),
      role TEXT NOT NULL CHECK (role IN ('worker','observer')),
      max_capabilities INTEGER NOT NULL CHECK (max_capabilities BETWEEN 0 AND 64),
      allowed_capabilities_json TEXT NOT NULL,
      issued_by TEXT NOT NULL,
      issued_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      used_at TEXT,
      used_by_agent_id TEXT REFERENCES v2_agent(agent_id),
      revoked_at TEXT,
      secret_hash TEXT NOT NULL UNIQUE,
      state TEXT NOT NULL CHECK (state IN ('pending','used','revoked','expired')),
      revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1)
    )""",
    """CREATE TABLE IF NOT EXISTS v2_agent_capability (
      agent_id TEXT NOT NULL REFERENCES v2_agent(agent_id),
      capability_key TEXT NOT NULL,
      level TEXT NOT NULL CHECK (level IN ('supported','unsupported','unknown')),
      harness_version TEXT NOT NULL,
      os TEXT NOT NULL,
      host TEXT NOT NULL,
      checked_at TEXT,
      evidence_sha256 TEXT,
      evidence_path TEXT,
      source TEXT NOT NULL,
      declared_by TEXT NOT NULL,
      revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
      PRIMARY KEY (agent_id, capability_key)
    )""",
    """CREATE TABLE IF NOT EXISTS v2_assignment (
      assignment_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES v2_task(task_id),
      agent_id TEXT REFERENCES v2_agent(agent_id),
      generation INTEGER NOT NULL CHECK (generation >= 1),
      slot_id INTEGER NOT NULL DEFAULT 0 CHECK (slot_id BETWEEN 0 AND 32),
      holds_slot INTEGER NOT NULL DEFAULT 0 CHECK (holds_slot IN (0,1)),
      state TEXT NOT NULL CHECK (state IN
        ('reserved','running','completed','failed','blocked','cancelled',
         'preempted','rejected','uncertain','historical_registered')),
      review_status TEXT NOT NULL DEFAULT 'none' CHECK (review_status IN
        ('none','awaiting_review','reviewing','accepted','repair','rejected')),
      workspace_id TEXT,
      nonce TEXT,
      legacy_task_id TEXT,
      legacy_dispatch_status TEXT,
      revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (task_id, slot_id, generation)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS v2_assignment_slot_owner
       ON v2_assignment(task_id, slot_id) WHERE holds_slot=1""",
    """CREATE TABLE IF NOT EXISTS v2_evidence (
      evidence_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES v2_task(task_id),
      assignment_id TEXT REFERENCES v2_assignment(assignment_id),
      kind TEXT NOT NULL,
      path TEXT NOT NULL,
      sealed_sha256 TEXT,
      current_sha256 TEXT,
      bytes INTEGER,
      sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0,1)),
      state TEXT NOT NULL CHECK (state IN ('sealed','unknown','missing','changed')),
      source TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE (task_id, path, sealed_sha256)
    )""",
    """CREATE TABLE IF NOT EXISTS v2_review (
      review_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES v2_task(task_id),
      assignment_id TEXT REFERENCES v2_assignment(assignment_id),
      event_id TEXT NOT NULL,
      verdict TEXT NOT NULL,
      note_path TEXT NOT NULL,
      note_sha256 TEXT,
      state TEXT NOT NULL CHECK (state IN ('sealed','unknown','missing','changed')),
      actor_json TEXT,
      reviewed_at TEXT,
      source TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS v2_legacy_map (
      legacy_task_id TEXT PRIMARY KEY,
      v2_task_id TEXT NOT NULL REFERENCES v2_task(task_id),
      v2_assignment_id TEXT NOT NULL REFERENCES v2_assignment(assignment_id),
      nonce TEXT NOT NULL,
      request_sha256 TEXT NOT NULL,
      review_event_id TEXT,
      review_sha256 TEXT,
      generation INTEGER NOT NULL CHECK (generation >= 1),
      source TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS v2_command_log (
      project_id TEXT NOT NULL,
      principal TEXT NOT NULL,
      command_id TEXT NOT NULL,
      tool TEXT NOT NULL,
      request_sha256 TEXT NOT NULL,
      response_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (project_id, principal, command_id)
    )""",
    """CREATE TABLE IF NOT EXISTS v2_event (
      event_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      entity_type TEXT NOT NULL,
      entity_id TEXT NOT NULL,
      event_type TEXT NOT NULL,
      actor TEXT NOT NULL,
      global_seq INTEGER NOT NULL UNIQUE CHECK (global_seq >= 1),
      source TEXT NOT NULL,
      source_seq INTEGER,
      source_key TEXT,
      at TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      UNIQUE (source, source_seq)
    )""",
    """CREATE TABLE IF NOT EXISTS v2_migration_run (
      run_id TEXT PRIMARY KEY,
      mode TEXT NOT NULL CHECK (mode IN ('dry_run','apply')),
      actor TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      status TEXT NOT NULL,
      counts_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS v2_migration_uncertain (
      task_id TEXT NOT NULL,
      reason TEXT NOT NULL,
      detail TEXT NOT NULL,
      PRIMARY KEY (task_id, reason)
    )""",
    "CREATE INDEX IF NOT EXISTS v2_task_state ON v2_task(task_state)",
    "CREATE INDEX IF NOT EXISTS v2_assignment_task ON v2_assignment(task_id)",
    "CREATE INDEX IF NOT EXISTS v2_event_entity ON v2_event(entity_type, entity_id)",
)


def _ensure_column(con, table, column, declaration):
    names = {row[1] for row in con.execute("PRAGMA table_info(" + table + ")")}
    if column not in names:
        con.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + declaration)


def create_schema(con):
    """Create/upgrade the additive v2 schema; caller owns the transaction."""
    existing = 0
    if _table_exists(con, "v2_meta"):
        row = con.execute("SELECT value FROM v2_meta WHERE key='schema_version'").fetchone()
        if row:
            try:
                existing = int(row[0])
            except (TypeError, ValueError):
                raise ServiceError("v2 schema_version is not an integer")
    if existing > V2_SCHEMA_VERSION:
        raise ServiceError("v2 schema version " + str(existing) + " is newer than this build (" +
                           str(V2_SCHEMA_VERSION) + "); refusing to downgrade or reset it")
    for statement in DDL:
        con.execute(statement)
    _ensure_column(con, "v2_assignment", "agent_id", "TEXT REFERENCES v2_agent(agent_id)")
    _ensure_column(con, "v2_agent_capability", "evidence_path", "TEXT")
    _ensure_column(con, "v2_task", "runtime_owner",
                   "TEXT NOT NULL DEFAULT 'legacy' CHECK (runtime_owner IN ('legacy','v2'))")
    _backfill_runtime_owners(con)
    from budget import create_budget_schema
    create_budget_schema(con)
    from service_access import create_access_schema
    create_access_schema(con)
    from priority_scheduler import create_scheduler_schema
    create_scheduler_schema(con)
    from workspace_manager import create_workspace_schema
    create_workspace_schema(con)
    from workspace_runtime import create_runtime_schema
    create_runtime_schema(con)
    from agent_messages import create_message_schema
    create_message_schema(con)
    from project_knowledge import create_knowledge_schema
    create_knowledge_schema(con)
    from service_extensions import create_extension_schema
    create_extension_schema(con)
    from merge_queue import create_merge_schema
    create_merge_schema(con)
    from collaboration_runtime import create_collaboration_schema
    create_collaboration_schema(con)
    from supervision import create_supervision_schema
    create_supervision_schema(con)
    from owner_access import create_owner_schema
    create_owner_schema(con)
    con.execute("INSERT INTO v2_meta(key,value) VALUES ('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(V2_SCHEMA_VERSION),))


def schema_present(con):
    return _table_exists(con, "v2_service_state") and _table_exists(con, "v2_task")


def _project_row(con):
    project_id = _project_id(con)
    root = str(Path(con.execute("SELECT 1").fetchone() and ""))
    return project_id


def _ensure_project(con, state):
    project_id = _project_id(con)
    root = str(Path(state).resolve())
    con.execute("INSERT INTO v2_project(project_id,root_path,created_at) VALUES (?,?,?) "
                "ON CONFLICT(project_id) DO NOTHING", (project_id, root, _now()))
    con.execute("INSERT INTO v2_service_state(id,project_id,v2_enabled,frozen,controller_epoch,updated_at) "
                "VALUES (1,?,0,0,0,?) ON CONFLICT(id) DO NOTHING", (project_id, _now()))
    return project_id


def _state_row(con):
    row = con.execute("SELECT project_id,v2_enabled,frozen,controller_epoch FROM v2_service_state WHERE id=1").fetchone()
    if not row:
        raise ServiceError("v2 service state is missing; run migrate --apply")
    return row


def _event(con, project_id, entity_type, entity_id, event_type, actor, source, source_seq, payload, source_key=None):
    seq = con.execute("SELECT COALESCE(MAX(global_seq),0)+1 FROM v2_event").fetchone()[0]
    event_id = "evt-" + hashlib.sha256(
        (str(source) + ":" + str(source_seq) + ":" + str(seq) + ":" + entity_id + ":" + event_type).encode()
    ).hexdigest()[:24]
    con.execute("INSERT INTO v2_event(event_id,project_id,entity_type,entity_id,event_type,actor,"
                "global_seq,source,source_seq,source_key,at,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, project_id, entity_type, entity_id, event_type, actor, seq, source,
                 source_seq, source_key, _now(), json.dumps(payload, sort_keys=True)))
    return event_id


# ---------------------------------------------------------------------------
# Binding / freeze / epoch guards

def _binding(con, actor, version, context):
    if context != "main":
        raise ServiceError("side or unregistered context is advisory-only")
    row = con.execute("SELECT role,mode,version,data FROM bindings "
                      "WHERE id=? AND version=? AND status='active'", (actor, version)).fetchone()
    if not row:
        raise ServiceError("stale or unregistered actor binding")
    return row


def _controller(con, actor, version, context):
    # Reuse the legacy permission/controller gate: an explicit permissions=[]
    # must not gain service, migration, freeze, or enable authority.
    return control.guard(con, actor, version, context, "dispatch")


def legacy_dispatch_guard(con, actor, version, context):
    """Called inside the legacy dispatch/register transaction.

    No v2 schema means legacy behavior is unchanged. Once the opt-in service
    state exists, freeze or v2-enabled mode blocks legacy dispatch so there are
    never two lifecycle authorities.
    """
    if not _table_exists(con, "v2_service_state"):
        return
    row = con.execute("SELECT project_id,v2_enabled,frozen,controller_epoch FROM v2_service_state WHERE id=1").fetchone()
    if not row:
        return
    _binding(con, actor, version, context)
    if row["frozen"]:
        raise ServiceError("AWO V2 freeze is active; dispatch is blocked until a controller unfreezes")
    if row["v2_enabled"]:
        raise ServiceError("AWO V2 is active; legacy dispatch/register is disabled; use the service boundary")


def _unsafe_v2(con):
    """Only actual unresolved/quiescence hazards block rollback.

    Closed historical rows with holds_slot=0 are history, not hazards; counting
    them would make disable/unfreeze impossible forever after importing closed
    accepted work.
    """
    return con.execute("SELECT count(*) FROM v2_assignment "
                       "WHERE holds_slot=1 OR state='uncertain'").fetchone()[0]


def set_freeze(state, actor, version, context, frozen, reason):
    path = _db_path(state)
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        _controller(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        state_row = _state_row(con)
        if bool(state_row["frozen"]) == bool(frozen):
            con.rollback()
            return {"status": "ALREADY_FROZEN" if frozen else "ALREADY_UNFROZEN",
                    "frozen": bool(frozen), "epoch": state_row["controller_epoch"]}
        if not frozen:
            unsafe = _unsafe_v2(con)
            if unsafe:
                raise ServiceError("cannot unfreeze: " + str(unsafe) +
                                   " V2 assignment(s) hold a slot or are uncertain; inspect/close them first")
        epoch = state_row["controller_epoch"] + 1
        con.execute("UPDATE v2_service_state SET frozen=?,controller_epoch=?,updated_at=? WHERE id=1",
                    (1 if frozen else 0, epoch, _now()))
        _event(con, state_row["project_id"], "service", "1",
               "frozen" if frozen else "unfrozen", actor, "v2", epoch,
               {"reason": reason, "epoch": epoch})
        con.commit()
        return {"status": "FROZEN" if frozen else "UNFROZEN", "frozen": bool(frozen),
                "epoch": epoch, "reason": reason}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def set_enabled(state, actor, version, context, enabled):
    path = _db_path(state)
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        _controller(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        state_row = _state_row(con)
        if bool(state_row["v2_enabled"]) == bool(enabled):
            con.rollback()
            return {"status": "ALREADY_ENABLED" if enabled else "ALREADY_DISABLED",
                    "v2_enabled": bool(enabled), "epoch": state_row["controller_epoch"]}
        if enabled:
            applied = con.execute("SELECT 1 FROM v2_migration_run WHERE status='applied' LIMIT 1").fetchone()
            if not applied:
                raise ServiceError("migration has not been applied; run migrate --apply")
            plans, _counts, _uncertain = _plan(state, con)
            drift = _projection_drift(con, plans)
            if drift:
                raise ServiceError("migration is stale or incomplete for " + str(len(drift)) +
                                   " item(s); run migrate --apply before enabling: " + "; ".join(drift[:4]))
            # Existing claim closure is tasks.status='resolved'. workflow_scheduler.release
            # never writes dispatches.status='resolved', so a dispatch row that is still
            # 'running' under a resolved task is closed history, not unclosed work.
            running = con.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
            unresolved = con.execute(
                "SELECT count(*) FROM dispatches d JOIN tasks t ON t.id=d.task "
                "WHERE d.status<>'resolved' AND t.status<>'resolved'").fetchone()[0]
            if running or unresolved:
                raise ServiceError("cannot enable V2 while " + str(running) + " legacy task(s) and " +
                                   str(unresolved) + " genuinely unclosed dispatch row(s) remain; inspect them first")
        else:
            unsafe = _unsafe_v2(con)
            if unsafe:
                raise ServiceError("cannot disable V2: " + str(unsafe) +
                                   " V2 assignment(s) hold a slot or are uncertain")
        epoch = state_row["controller_epoch"] + 1
        con.execute("UPDATE v2_service_state SET v2_enabled=?,controller_epoch=?,updated_at=? WHERE id=1",
                    (1 if enabled else 0, epoch, _now()))
        _event(con, state_row["project_id"], "service", "1",
               "enabled" if enabled else "disabled", actor, "v2", epoch,
               {"enabled": bool(enabled), "epoch": epoch})
        con.commit()
        return {"status": "ENABLED" if enabled else "DISABLED", "v2_enabled": bool(enabled), "epoch": epoch}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Service commands

def _command_envelope(envelope, allowed=("task.create", "task.update", "scheduler.enqueue",
                                       "scheduler.claim", "scheduler.priority", "assignment.close",
                                       "project.bind_root")):
    if not isinstance(envelope, dict):
        raise ServiceError("command envelope must be an object")
    for key in ("command_id", "tool", "expected_epoch", "payload"):
        if key not in envelope:
            raise ServiceError("command envelope is missing " + key)
    if not isinstance(envelope["command_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", envelope["command_id"]):
        raise ServiceError("command_id must be 8..128 safe characters")
    if envelope["tool"] not in allowed:
        raise ServiceError("unknown tool: " + str(envelope["tool"]))
    if type(envelope["expected_epoch"]) is not int or envelope["expected_epoch"] < 0:
        raise ServiceError("expected_epoch must be a nonnegative integer")
    if not isinstance(envelope["payload"], dict):
        raise ServiceError("payload must be an object")
    return envelope


def execute_command(state, actor, version, context, envelope):
    env = _command_envelope(envelope)
    path = _db_path(state)
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        binding = _controller(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        state_row = _state_row(con)
        if state_row["frozen"]:
            raise ServiceError("AWO V2 is frozen; state-changing commands are blocked")
        if not state_row["v2_enabled"]:
            raise ServiceError("AWO V2 is disabled; migrate and enable it before using service writes")
        if env["expected_epoch"] != state_row["controller_epoch"]:
            raise ServiceError("stale controller epoch: expected " + str(env["expected_epoch"]) +
                               ", current " + str(state_row["controller_epoch"]))
        project_id = state_row["project_id"]
        principal = str(binding["role"]) + ":" + actor
        request_sha = _sha({"tool": env["tool"], "payload": env["payload"]})
        row = con.execute("SELECT tool,request_sha256,response_json FROM v2_command_log "
                          "WHERE project_id=? AND principal=? AND command_id=?",
                          (project_id, principal, env["command_id"])).fetchone()
        if row:
            if row["tool"] != env["tool"] or row["request_sha256"] != request_sha:
                raise ServiceError("command_id was reused with a different tool or payload")
            con.rollback()
            return json.loads(row["response_json"]) | {"replayed": True}
        response = _apply_tool(con, project_id, actor, env["tool"], env["payload"])
        response["replayed"] = False
        con.execute("INSERT INTO v2_command_log(project_id,principal,command_id,tool,request_sha256,"
                    "response_json,created_at) VALUES (?,?,?,?,?,?,?)",
                    (project_id, principal, env["command_id"], env["tool"], request_sha,
                     json.dumps(response, sort_keys=True), _now()))
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _apply_tool(con, project_id, actor, tool, payload):
    if tool == 'project.bind_root':
        from service_extensions import bind_root
        return bind_root(con, project_id, actor, payload)
    if tool == "task.create":
        return _task_create(con, project_id, actor, payload)
    if tool == "task.update":
        return _task_update(con, project_id, actor, payload)
    from priority_scheduler import TOOLS, apply_command
    if tool in TOOLS:
        return apply_command(con, project_id, actor, tool, payload)
    raise ServiceError("unknown tool: " + tool)


def _validate_priority(priority):
    if priority not in PRIORITIES:
        raise ServiceError("priority must be one of " + ", ".join(PRIORITIES))


def _task_create(con, project_id, actor, payload):
    extra = set(payload) - {"task_id", "title", "body", "priority", "execution_mode", "fanout_slots"}
    if extra:
        raise ServiceError("task.create has unknown fields: " + ", ".join(sorted(extra)))
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ServiceError("task.create needs a nonempty title")
    body = payload.get("body", "")
    if not isinstance(body, str):
        raise ServiceError("body must be a string")
    priority = payload.get("priority", "P3")
    _validate_priority(priority)
    mode = payload.get("execution_mode", "single")
    if mode not in EXECUTION_MODES:
        raise ServiceError("execution_mode must be single, fanout, or ensemble")
    slots = payload.get("fanout_slots", 1)
    if type(slots) is not int or not 1 <= slots <= 8:
        raise ServiceError("fanout_slots must be an integer from 1 to 8")
    if mode == "single" and slots != 1:
        raise ServiceError("single execution_mode requires fanout_slots=1")
    task_id = payload.get("task_id") or ("T-" + uuid.uuid4().hex[:12].upper())
    if not control.valid_task_id(task_id):
        raise ServiceError("invalid task_id")
    if con.execute("SELECT 1 FROM v2_task WHERE task_id=?", (task_id,)).fetchone():
        raise ServiceError("task already exists: " + task_id)
    now = _now()
    con.execute("INSERT INTO v2_task(task_id,project_id,legacy_status,title,body,priority,task_state,"
                "execution_mode,fanout_slots,revision,created_at,updated_at) "
                "VALUES (?,?,NULL,?,?,?,'draft',?,?,1,?,?)",
                (task_id, project_id, title, body, priority, mode, slots, now, now))
    con.execute('UPDATE v2_task SET runtime_owner=? WHERE task_id=?', ('v2', task_id))
    event_id = _event(con, project_id, "task", task_id, "task.created", actor, "v2", None,
                      {"title": title, "body": body, "priority": priority, "execution_mode": mode})
    return {"status": "CREATED", "task_id": task_id, "revision": 1, "event_id": event_id}


def _task_update(con, project_id, actor, payload):
    allowed = {"task_id", "expected_revision", "title", "body", "priority"}
    extra = set(payload) - allowed
    if extra:
        raise ServiceError("task.update has unknown fields: " + ", ".join(sorted(extra)))
    task_id = payload.get("task_id")
    if not isinstance(task_id, str):
        raise ServiceError("task.update needs task_id")
    expected = payload.get("expected_revision")
    if type(expected) is not int or expected < 1:
        raise ServiceError("task.update needs expected_revision")
    row = con.execute("SELECT revision,task_state FROM v2_task WHERE task_id=? AND project_id=?",
                      (task_id, project_id)).fetchone()
    if not row:
        raise ServiceError("task not found: " + task_id)
    if row["revision"] != expected:
        raise ServiceError("stale revision: expected " + str(expected) + ", current " + str(row["revision"]))
    if row["task_state"] != "draft":
        raise ServiceError("P0-1 only updates draft tasks; " + task_id + " is " + row["task_state"])
    sets, values = [], []
    if "title" in payload:
        if not isinstance(payload["title"], str) or not payload["title"].strip():
            raise ServiceError("title must be nonempty")
        sets.append("title=?")
        values.append(payload["title"])
    if "body" in payload:
        if not isinstance(payload["body"], str):
            raise ServiceError("body must be a string")
        sets.append("body=?")
        values.append(payload["body"])
    if "priority" in payload:
        _validate_priority(payload["priority"])
        sets.append("priority=?")
        values.append(payload["priority"])
    if not sets:
        raise ServiceError("task.update has nothing to change")
    revision = row["revision"] + 1
    values.extend([revision, _now(), task_id])
    con.execute("UPDATE v2_task SET " + ", ".join(sets) + ",revision=?,updated_at=? WHERE task_id=?", values)
    event_id = _event(con, project_id, "task", task_id, "task.updated", actor, "v2", None,
                      {"revision": revision, "fields": sorted(set(payload) - {"task_id", "expected_revision"})})
    return {"status": "UPDATED", "task_id": task_id, "revision": revision, "event_id": event_id}


def task_get(state, actor, version, context, task_id):
    con = _connect(_db_path(state), readonly=True)
    try:
        _binding(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        row = con.execute("SELECT * FROM v2_task WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise ServiceError("task not found: " + task_id)
        return dict(row)
    finally:
        con.close()


def task_list(state, actor, version, context):
    con = _connect(_db_path(state), readonly=True)
    try:
        _binding(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        return [dict(row) for row in con.execute("SELECT * FROM v2_task ORDER BY task_id")]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Minimal Agent Registry (P0-2)

CAPABILITY_KEY = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
HANDLE = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z")
CAPABILITY_LEVELS = ("supported", "unsupported", "unknown")
CAPABILITY_SOURCES = ("live_probe", "official_doc", "regression_test", "operator_note",
                      "self_declared", "imported_binding")


def _secret_hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _secret_file(path, secret):
    """Create an exclusive 0600 local file; never overwrite an existing secret."""
    target = Path(path)
    if not target.is_absolute():
        raise ServiceError("secret output path must be absolute")
    if not target.parent.is_dir():
        raise ServiceError("secret output directory does not exist: " + str(target.parent))
    if ".git" in target.parts:
        raise ServiceError("secret output must not be inside .git")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(target), flags, 0o600)
    try:
        os.write(fd, secret.encode() + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    return str(target)


def _remove_secret_file(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def _read_secret_file(path, label):
    target = Path(path)
    if not target.is_absolute() or not target.is_file():
        raise ServiceError(label + " file must be an existing absolute local file")
    value = target.read_text(encoding="utf-8-sig").strip()
    if not value or len(value) > 512:
        raise ServiceError(label + " file is empty or too large")
    return value


def _reject_identity_fields(payload):
    forbidden = {"project_id", "principal", "agent_id"} & set(payload)
    if forbidden:
        raise ServiceError("payload cannot set authenticated identity fields: " + ", ".join(sorted(forbidden)))


def _request_sha(env):
    return _sha({"tool": env["tool"], "payload": env["payload"]})


def _replay(con, project_id, principal, env, request_sha):
    row = con.execute("SELECT tool,request_sha256,response_json FROM v2_command_log "
                      "WHERE project_id=? AND principal=? AND command_id=?",
                      (project_id, principal, env["command_id"])).fetchone()
    if not row:
        return None
    if row["tool"] != env["tool"] or row["request_sha256"] != request_sha:
        raise ServiceError("command_id was reused with a different tool or payload")
    return json.loads(row["response_json"]) | {"replayed": True}


def _record(con, project_id, principal, env, request_sha, response):
    con.execute("INSERT INTO v2_command_log(project_id,principal,command_id,tool,request_sha256,"
                "response_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (project_id, principal, env["command_id"], env["tool"], request_sha,
                 json.dumps(response, sort_keys=True), _now()))


def _registry_open(state):
    con = _connect(_db_path(state))
    con.execute("BEGIN IMMEDIATE")
    return con


def _registry_controller(con, actor, version, context, env):
    _controller(con, actor, version, context)
    if not schema_present(con):
        raise ServiceError("v2 schema is absent; run migrate --apply first")
    state_row = _state_row(con)
    if env["expected_epoch"] != state_row["controller_epoch"]:
        raise ServiceError("stale controller epoch: expected " + str(env["expected_epoch"]) +
                           ", current " + str(state_row["controller_epoch"]))
    return state_row


def _strict_int(value):
    return type(value) is int


def _refuse_frozen(state_row):
    if state_row["frozen"]:
        raise ServiceError("AWO V2 is frozen; new admission/registration/capability changes are blocked")


def _enroll_agent(state, actor, version, context, envelope, secret_out):
    env = _command_envelope(envelope, ("agent.enroll",))
    payload = env["payload"]
    allowed_fields = {"role", "allowed_capabilities", "max_capabilities", "expires_minutes",
                      "project_root"}
    extra = set(payload) - allowed_fields
    if extra:
        raise ServiceError("agent.enroll has unknown fields: " + ", ".join(sorted(extra)))
    _reject_identity_fields(payload)
    role = payload.get("role")
    if role not in ("worker", "observer"):
        raise ServiceError("enrollment role must be worker or observer")
    keys = payload.get("allowed_capabilities", [])
    if not isinstance(keys, list) or not all(isinstance(k, str) and CAPABILITY_KEY.fullmatch(k) for k in keys):
        raise ServiceError("allowed_capabilities must be capability keys")
    if len(set(keys)) != len(keys):
        raise ServiceError("allowed_capabilities contains duplicates")
    if len(keys) > 64:
        raise ServiceError("allowed_capabilities is limited to 64 keys")
    max_capabilities = payload.get("max_capabilities", len(keys))
    if type(max_capabilities) is not int or max_capabilities != len(keys):
        raise ServiceError("max_capabilities must equal the number of allowed capability keys")
    minutes = payload.get("expires_minutes", 10)
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise ServiceError("expires_minutes must be an integer from 1 to 1440")
    secret = "awo_enroll_" + secrets.token_urlsafe(32)
    grant_id = "gr-" + uuid.uuid4().hex
    con = _registry_open(state)
    target = None
    try:
        state_row = _registry_controller(con, actor, version, context, env)
        _refuse_frozen(state_row)
        project_id = state_row["project_id"]
        principal = str(_binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = _request_sha(env)
        replay = _replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        target = _secret_file(secret_out, secret)
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(minutes=minutes)).isoformat()
        con.execute("INSERT INTO v2_enrollment_grant(grant_id,project_id,role,max_capabilities,"
                    "allowed_capabilities_json,issued_by,issued_at,expires_at,used_at,used_by_agent_id,"
                    "revoked_at,secret_hash,state,revision) "
                    "VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?, 'pending',1)",
                    (grant_id, project_id, role, len(keys), json.dumps(keys), actor,
                     now.isoformat(), expires, _secret_hash(secret)))
        if "project_root" in payload:
            from service_access import canonical_root, root_hash
            root = canonical_root(payload["project_root"])
            con.execute("UPDATE v2_enrollment_grant SET root_path=?,root_sha256=?,"
                        "issuer_version=? WHERE grant_id=?",
                        (root, root_hash(root), version, grant_id))
        _event(con, project_id, "enrollment_grant", grant_id, "grant.issued", actor, "v2", None,
               {"role": role, "allowed_capabilities": keys, "expires_at": expires})
        response = {"status": "GRANT_ISSUED", "grant_id": grant_id, "role": role,
                    "allowed_capabilities": keys, "max_capabilities": len(keys),
                    "expires_at": expires, "secret_file": target, "replayed": False}
        _record(con, project_id, principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        if target:
            _remove_secret_file(target)
        raise
    finally:
        con.close()


def _registration_payload(payload, grant):
    allowed = {"handle", "harness", "harness_version", "host", "os", "capabilities"}
    extra = set(payload) - allowed
    if extra:
        raise ServiceError("agent.register has unknown fields: " + ", ".join(sorted(extra)))
    _reject_identity_fields(payload)
    handle = payload.get("handle")
    if not isinstance(handle, str) or not HANDLE.fullmatch(handle):
        raise ServiceError("handle must match [a-z][a-z0-9_-]{1,31}")
    for key in ("harness", "harness_version", "host", "os"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ServiceError(key + " must be a nonempty string")
    capabilities = payload.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(
            isinstance(k, str) and CAPABILITY_KEY.fullmatch(k) for k in capabilities):
        raise ServiceError("capabilities must be capability keys")
    allowed_keys = set(json.loads(grant["allowed_capabilities_json"]))
    if set(capabilities) - allowed_keys:
        raise ServiceError("requested capabilities exceed the enrollment grant")
    if len(set(capabilities)) != len(capabilities):
        raise ServiceError("capabilities contains duplicates")
    return handle, capabilities


def _register_agent_tx(con, project_id, grant, payload, token_hash):
    """Consume one grant in the application transaction; never create secrets."""
    if grant["project_id"] != project_id:
        raise ServiceError("enrollment grant belongs to another project")
    if grant["state"] != "pending" or grant["used_at"] or grant["revoked_at"]:
        raise ServiceError("enrollment grant is already used, revoked, or expired")
    if datetime.fromisoformat(grant["expires_at"]) <= datetime.now(timezone.utc):
        raise ServiceError("enrollment grant has expired")
    if grant["role"] not in ("worker", "observer"):
        raise ServiceError("enrollment grant role cannot escalate")
    handle, capabilities = _registration_payload(payload, grant)
    agent_id, now = "agt-" + uuid.uuid4().hex[:20], _now()
    con.execute("INSERT INTO v2_agent(agent_id,project_id,handle,role,harness,harness_version,"
                "host,os,lifecycle,token_hash,source,parent_agent_id,created_at,approved_at,"
                "last_seen_at,revoked_at,revision) "
                "VALUES (?,?,?,?,?,?,?,?, 'active',?, 'enrollment_grant',NULL,?,?,NULL,NULL,1)",
                (agent_id, project_id, handle, grant["role"], payload["harness"],
                 payload["harness_version"], payload["host"], payload["os"], token_hash, now, now))
    for key in capabilities:
        con.execute("INSERT INTO v2_agent_capability(agent_id,capability_key,level,harness_version,"
                    "os,host,checked_at,evidence_sha256,source,declared_by,revision) "
                    "VALUES (?,?, 'unknown',?,?,?,?,NULL,'self_declared',?,1)",
                    (agent_id, key, payload["harness_version"], payload["os"],
                     payload["host"], now, agent_id))
    con.execute("UPDATE v2_enrollment_grant SET state='used',used_at=?,used_by_agent_id=?,revision=revision+1 "
                "WHERE grant_id=? AND state='pending'", (now, agent_id, grant["grant_id"]))
    _event(con, project_id, "agent", agent_id, "agent.registered", "grant:" + grant["grant_id"],
           "v2", None, {"handle": handle, "role": grant["role"], "grant_id": grant["grant_id"],
                         "capabilities": capabilities})
    return {"status": "REGISTERED", "agent_id": agent_id, "handle": handle,
            "role": grant["role"], "lifecycle": "active", "capabilities": capabilities,
            "revision": 1, "replayed": False}


def register_agent(state, grant_file, envelope, token_out):
    env = _command_envelope(envelope, ("agent.register",))
    grant_secret = _read_secret_file(grant_file, "grant")
    grant_hash = _secret_hash(grant_secret)
    con = _registry_open(state)
    target = None
    try:
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        state_row = _state_row(con)
        _refuse_frozen(state_row)
        project_id = state_row["project_id"]
        if env["expected_epoch"] != state_row["controller_epoch"]:
            raise ServiceError("stale controller epoch: expected " + str(env["expected_epoch"]) +
                               ", current " + str(state_row["controller_epoch"]))
        grant = con.execute("SELECT * FROM v2_enrollment_grant WHERE secret_hash=?",
                            (grant_hash,)).fetchone()
        if not grant:
            raise ServiceError("enrollment grant is unknown")
        if grant["project_id"] != project_id:
            raise ServiceError("enrollment grant belongs to another project")
        principal = "grant:" + grant["grant_id"]
        request_sha = _request_sha(env)
        replay = _replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        token = "awo_agent_" + secrets.token_urlsafe(32)
        target = _secret_file(token_out, token)
        response = _register_agent_tx(con, project_id, grant, env["payload"], _secret_hash(token))
        response["token_file"] = target
        _record(con, project_id, principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        if target:
            _remove_secret_file(target)
        raise
    finally:
        con.close()


def self_register_agent(state, envelope):
    env = _command_envelope(envelope, ("agent.self_register",))
    payload = env["payload"]
    allowed = {"handle", "harness", "harness_version", "host", "os", "capabilities", "role"}
    extra = set(payload) - allowed
    if extra:
        raise ServiceError("agent.self_register has unknown fields: " + ", ".join(sorted(extra)))
    _reject_identity_fields(payload)
    if payload.get("role", "worker") != "worker":
        raise ServiceError("self-registration can only request the worker role")
    handle = payload.get("handle")
    if not isinstance(handle, str) or not HANDLE.fullmatch(handle):
        raise ServiceError("handle must match [a-z][a-z0-9_-]{1,31}")
    for key in ("harness", "harness_version", "host", "os"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ServiceError(key + " must be a nonempty string")
    capabilities = payload.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(
            isinstance(k, str) and CAPABILITY_KEY.fullmatch(k) for k in capabilities):
        raise ServiceError("capabilities must be capability keys")
    con = _registry_open(state)
    try:
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        state_row = _state_row(con)
        _refuse_frozen(state_row)
        project_id = state_row["project_id"]
        if env["expected_epoch"] != state_row["controller_epoch"]:
            raise ServiceError("stale controller epoch")
        principal = "self:" + handle
        request_sha = _request_sha(env)
        replay = _replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        if con.execute("SELECT 1 FROM v2_agent WHERE project_id=? AND handle=?",
                       (project_id, handle)).fetchone():
            raise ServiceError("handle is already registered")
        agent_id = "agt-" + uuid.uuid4().hex[:20]
        now = _now()
        con.execute("INSERT INTO v2_agent(agent_id,project_id,handle,role,harness,harness_version,"
                    "host,os,lifecycle,token_hash,source,parent_agent_id,created_at,approved_at,"
                    "last_seen_at,revoked_at,revision) "
                    "VALUES (?,?,?, 'worker',?,?,?,?, 'pending',NULL,'self_registration',NULL,?,NULL,NULL,NULL,1)",
                    (agent_id, project_id, handle, payload["harness"], payload["harness_version"],
                     payload["host"], payload["os"], now))
        for key in capabilities:
            con.execute("INSERT INTO v2_agent_capability(agent_id,capability_key,level,harness_version,"
                        "os,host,checked_at,evidence_sha256,source,declared_by,revision) "
                        "VALUES (?,?, 'unknown',?,?,?,?,NULL,'self_declared',?,1)",
                        (agent_id, key, payload["harness_version"], payload["os"], payload["host"],
                         now, agent_id))
        _event(con, project_id, "agent", agent_id, "agent.pending", principal, "v2", None,
               {"handle": handle, "role": "worker", "capabilities": capabilities})
        response = {"status": "PENDING", "agent_id": agent_id, "handle": handle,
                    "role": "worker", "lifecycle": "pending", "replayed": False}
        _record(con, project_id, principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def approve_agent(state, actor, version, context, envelope, token_out):
    env = _command_envelope(envelope, ("agent.approve",))
    payload = env["payload"]
    if set(payload) - {"agent_id", "expected_revision"}:
        raise ServiceError("agent.approve has unknown fields")
    con = _registry_open(state)
    target = None
    try:
        state_row = _registry_controller(con, actor, version, context, env)
        _refuse_frozen(state_row)
        project_id = state_row["project_id"]
        principal = str(_binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = _request_sha(env)
        replay = _replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        agent = con.execute("SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?",
                            (payload.get("agent_id"), project_id)).fetchone()
        if not agent or agent["lifecycle"] != "pending":
            raise ServiceError("agent is not a pending pending registration")
        if agent["role"] != "worker":
            raise ServiceError("pending registration can only be approved as worker")
        if not _strict_int(payload.get("expected_revision")) or payload["expected_revision"] != agent["revision"]:
            raise ServiceError("stale revision")
        token = "awo_agent_" + secrets.token_urlsafe(32)
        target = _secret_file(token_out, token)
        now = _now()
        con.execute("UPDATE v2_agent SET lifecycle='active',token_hash=?,approved_at=?,revision=revision+1 "
                    "WHERE agent_id=? AND lifecycle='pending'", (_secret_hash(token), now, agent["agent_id"]))
        _event(con, project_id, "agent", agent["agent_id"], "agent.approved", actor, "v2", None,
               {"handle": agent["handle"], "role": "worker"})
        response = {"status": "APPROVED", "agent_id": agent["agent_id"], "handle": agent["handle"],
                    "role": "worker", "lifecycle": "active", "token_file": target, "replayed": False}
        _record(con, project_id, principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        if target:
            _remove_secret_file(target)
        raise
    finally:
        con.close()


def revoke_agent(state, actor, version, context, envelope):
    env = _command_envelope(envelope, ("agent.revoke",))
    payload = env["payload"]
    if set(payload) - {"agent_id", "expected_revision"}:
        raise ServiceError("agent.revoke has unknown fields")
    con = _registry_open(state)
    try:
        state_row = _registry_controller(con, actor, version, context, env)
        project_id = state_row["project_id"]
        principal = str(_binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = _request_sha(env)
        replay = _replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        agent = con.execute("SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?",
                            (payload.get("agent_id"), project_id)).fetchone()
        if not agent:
            raise ServiceError("agent not found")
        if not _strict_int(payload.get("expected_revision")) or payload["expected_revision"] != agent["revision"]:
            raise ServiceError("stale revision")
        if agent["lifecycle"] == "revoked":
            con.rollback()
            return {"status": "ALREADY_REVOKED", "agent_id": agent["agent_id"], "replayed": False}
        con.execute("UPDATE v2_agent SET lifecycle='revoked',revoked_at=?,revision=revision+1 "
                    "WHERE agent_id=?", (_now(), agent["agent_id"]))
        _event(con, project_id, "agent", agent["agent_id"], "agent.revoked", actor, "v2", None,
               {"handle": agent["handle"]})
        response = {"status": "REVOKED", "agent_id": agent["agent_id"], "handle": agent["handle"],
                    "lifecycle": "revoked", "replayed": False}
        _record(con, project_id, principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def revoke_grant(state, actor, version, context, envelope):
    env = _command_envelope(envelope, ("grant.revoke",))
    payload = env["payload"]
    if set(payload) - {"grant_id", "expected_revision"}:
        raise ServiceError("grant.revoke has unknown fields")
    con = _registry_open(state)
    try:
        state_row = _registry_controller(con, actor, version, context, env)
        project_id = state_row["project_id"]
        principal = str(_binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = _request_sha(env)
        replay = _replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        grant = con.execute("SELECT * FROM v2_enrollment_grant WHERE grant_id=? AND project_id=?",
                            (payload.get("grant_id"), project_id)).fetchone()
        if not grant:
            raise ServiceError("enrollment grant not found")
        if not _strict_int(payload.get("expected_revision")) or payload["expected_revision"] != grant["revision"]:
            raise ServiceError("stale revision")
        if grant["state"] == "revoked":
            con.rollback()
            return {"status": "ALREADY_REVOKED", "grant_id": grant["grant_id"], "replayed": False}
        con.execute("UPDATE v2_enrollment_grant SET state='revoked',revoked_at=?,revision=revision+1 "
                    "WHERE grant_id=?", (_now(), grant["grant_id"]))
        _event(con, project_id, "enrollment_grant", grant["grant_id"], "grant.revoked", actor, "v2", None,
               {"role": grant["role"]})
        response = {"status": "REVOKED", "grant_id": grant["grant_id"], "role": grant["role"],
                    "state": "revoked", "replayed": False}
        _record(con, project_id, principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def set_agent_capability(state, actor, version, context, envelope):
    env = _command_envelope(envelope, ("agent.capability",))
    payload = env["payload"]
    allowed = {"agent_id", "capability_key", "level", "harness_version", "os", "host",
               "checked_at", "evidence_sha256", "evidence_path", "source", "expected_revision"}
    if set(payload) - allowed:
        raise ServiceError("agent.capability has unknown fields")
    key = payload.get("capability_key")
    if not isinstance(key, str) or not CAPABILITY_KEY.fullmatch(key):
        raise ServiceError("invalid capability key")
    level = payload.get("level")
    if level not in CAPABILITY_LEVELS:
        raise ServiceError("level must be supported, unsupported, or unknown")
    source = payload.get("source")
    if source not in CAPABILITY_SOURCES:
        raise ServiceError("source must be one of " + ", ".join(CAPABILITY_SOURCES))
    evidence = payload.get("evidence_sha256")
    evidence_path = payload.get("evidence_path")
    checked = payload.get("checked_at")
    harness_version = payload.get("harness_version")
    if level == "supported":
        if not isinstance(evidence, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence):
            raise ServiceError("supported requires an evidence_sha256")
        if not isinstance(evidence_path, str) or not Path(evidence_path).is_file():
            raise ServiceError("supported requires an existing local evidence_path")
        try:
            actual_evidence = _file_sha(Path(evidence_path))
        except OSError as exc:
            raise ServiceError("capability evidence is unreadable: " + str(exc))
        if actual_evidence != evidence:
            raise ServiceError("capability evidence hash does not match evidence_sha256")
        if not isinstance(checked, str) or not checked:
            raise ServiceError("supported requires checked_at")
        try:
            datetime.fromisoformat(checked)
        except ValueError:
            raise ServiceError("checked_at must be an ISO timestamp")
        if not isinstance(harness_version, str) or not harness_version.strip() or harness_version == "unknown":
            raise ServiceError("supported requires an explicit measured harness_version")
        if source in ("self_declared", "imported_binding"):
            raise ServiceError("a declaration or imported binding cannot verify a capability")
    con = _registry_open(state)
    try:
        state_row = _registry_controller(con, actor, version, context, env)
        _refuse_frozen(state_row)
        project_id = state_row["project_id"]
        principal = str(_binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = _request_sha(env)
        replay = _replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        agent = con.execute("SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?",
                            (payload.get("agent_id"), project_id)).fetchone()
        if not agent:
            raise ServiceError("agent not found")
        if not _strict_int(payload.get("expected_revision")) or payload["expected_revision"] != agent["revision"]:
            raise ServiceError("stale revision")
        if level == "supported":
            if (harness_version != agent["harness_version"]
                    or payload.get("os", agent["os"]) != agent["os"]
                    or payload.get("host", agent["host"]) != agent["host"]):
                raise ServiceError("supported capability context must match the agent harness_version/os/host")
        con.execute("INSERT INTO v2_agent_capability(agent_id,capability_key,level,harness_version,os,host,"
                    "checked_at,evidence_sha256,evidence_path,source,declared_by,revision) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(agent_id,capability_key) DO UPDATE SET "
                    "level=excluded.level,harness_version=excluded.harness_version,os=excluded.os,"
                    "host=excluded.host,checked_at=excluded.checked_at,"
                    "evidence_sha256=excluded.evidence_sha256,evidence_path=excluded.evidence_path,"
                    "source=excluded.source,declared_by=excluded.declared_by,"
                    "revision=v2_agent_capability.revision+1",
                    (agent["agent_id"], key, level, payload.get("harness_version", agent["harness_version"]),
                     payload.get("os", agent["os"]), payload.get("host", agent["host"]),
                     checked, evidence, evidence_path, source, principal))
        con.execute("UPDATE v2_agent SET revision=revision+1 WHERE agent_id=?", (agent["agent_id"],))
        _event(con, project_id, "agent", agent["agent_id"], "agent.capability", actor, "v2", None,
               {"capability_key": key, "level": level, "source": source})
        response = {"status": "CAPABILITY_SET", "agent_id": agent["agent_id"],
                    "capability_key": key, "level": level, "revision": agent["revision"] + 1, "replayed": False}
        _record(con, project_id, principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _agent_by_token(con, token):
    agent = con.execute("SELECT * FROM v2_agent WHERE token_hash=?", (_secret_hash(token),)).fetchone()
    if not agent:
        raise ServiceError("agent token is unknown")
    if agent["lifecycle"] != "active":
        raise ServiceError("agent is not active")
    return agent


def heartbeat_agent(state, token_file, envelope):
    env = _command_envelope(envelope, ("agent.heartbeat",))
    payload = env["payload"]
    if set(payload) - {"expected_revision", "status", "detail"}:
        raise ServiceError("agent.heartbeat has unknown fields")
    token = _read_secret_file(token_file, "agent token")
    con = _registry_open(state)
    try:
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        state_row = _state_row(con)
        if env["expected_epoch"] != state_row["controller_epoch"]:
            raise ServiceError("stale controller epoch")
        agent = _agent_by_token(con, token)
        if agent["project_id"] != state_row["project_id"]:
            raise ServiceError("agent belongs to another project")
        principal = "agent:" + agent["agent_id"]
        request_sha = _request_sha(env)
        replay = _replay(con, agent["project_id"], principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        if not _strict_int(payload.get("expected_revision")) or payload["expected_revision"] != agent["revision"]:
            raise ServiceError("stale revision")
        now = _now()
        con.execute("UPDATE v2_agent SET last_seen_at=?,revision=revision+1 WHERE agent_id=?",
                    (now, agent["agent_id"]))
        _event(con, agent["project_id"], "agent", agent["agent_id"], "agent.heartbeat", principal, "v2", None,
               {"status": payload.get("status"), "last_seen_at": now})
        response = {"status": "HEARTBEAT", "agent_id": agent["agent_id"],
                    "last_seen_at": now, "revision": agent["revision"] + 1, "replayed": False}
        _record(con, agent["project_id"], principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def agent_status(state, token_file):
    token = _read_secret_file(token_file, "agent token")
    con = _connect(_db_path(state), readonly=True)
    try:
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        agent = _agent_by_token(con, token)
        capabilities = [dict(row) for row in con.execute(
            "SELECT capability_key,level,harness_version,os,host,checked_at,evidence_sha256,source "
            "FROM v2_agent_capability WHERE agent_id=? ORDER BY capability_key", (agent["agent_id"],))]
        return {"agent": {k: agent[k] for k in ("agent_id", "handle", "role", "harness",
                                                 "harness_version", "host", "os", "lifecycle",
                                                 "last_seen_at", "revision")},
                "capabilities": capabilities}
    finally:
        con.close()


def agent_list(state, actor, version, context):
    con = _connect(_db_path(state), readonly=True)
    try:
        _binding(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        return [dict(row) for row in con.execute(
            "SELECT agent_id,handle,role,harness,harness_version,host,os,lifecycle,last_seen_at,revision "
            "FROM v2_agent ORDER BY handle")]
    finally:
        con.close()


def agent_get(state, actor, version, context, agent_id):
    con = _connect(_db_path(state), readonly=True)
    try:
        _binding(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        agent = con.execute("SELECT * FROM v2_agent WHERE agent_id=?", (agent_id,)).fetchone()
        if not agent:
            raise ServiceError("agent not found")
        caps = [dict(row) for row in con.execute(
            "SELECT capability_key,level,harness_version,os,host,checked_at,evidence_sha256,source "
            "FROM v2_agent_capability WHERE agent_id=? ORDER BY capability_key", (agent_id,))]
        return {"agent": dict(agent), "capabilities": caps}
    finally:
        con.close()


def agent_match(state, actor, version, context, agent_id, required):
    con = _connect(_db_path(state), readonly=True)
    try:
        _binding(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError("v2 schema is absent; run migrate --apply first")
        agent = con.execute("SELECT * FROM v2_agent WHERE agent_id=?", (agent_id,)).fetchone()
        if not agent:
            raise ServiceError("agent not found")
        caps = {row["capability_key"]: row for row in con.execute(
            "SELECT * FROM v2_agent_capability WHERE agent_id=?", (agent_id,))}
        missing, unknown = [], []
        for key in required:
            row = caps.get(key)
            if row is None or row["level"] == "unknown":
                unknown.append(key)
                missing.append(key)
                continue
            if row["level"] != "supported":
                missing.append(key)
                continue
            if (row["harness_version"] != agent["harness_version"]
                    or row["os"] != agent["os"] or row["host"] != agent["host"]):
                missing.append(key)
                continue
            evidence = row["evidence_sha256"]
            path = row["evidence_path"]
            if not evidence or not path or not Path(path).is_file():
                missing.append(key)
                continue
            try:
                current = _file_sha(Path(path))
            except OSError:
                missing.append(key)
                continue
            if current != evidence:
                missing.append(key)
        return {"agent_id": agent_id,
                "matched": agent["lifecycle"] == "active" and agent["role"] == "worker" and not missing,
                "missing": missing, "unknown": unknown}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Migration planning / apply

def _review_check(task, request, event, review):
    problems = []
    try:
        event_data = json.loads(event["data"])
    except (TypeError, ValueError):
        event_data = {}
    report = event_data.get("report") if isinstance(event_data, dict) else None
    if not isinstance(report, dict) or report.get("status") != "review_ready":
        problems.append("review event is not review_ready")
    if event["status"] != "reviewed":
        problems.append("review event is not in the reviewed state")
    raw_path = review.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return "unknown", ["review path is missing or not a string"]
    path = Path(raw_path)
    if not path.is_file():
        return "missing", ["review file is missing"]
    try:
        sha = _file_sha(path)
    except OSError as exc:
        return "unknown", ["review file unreadable: " + str(exc)]
    if review.get("sha256") != sha:
        return "changed", ["review file hash differs from the sealed review"]
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return "unknown", ["review file unreadable as text: " + str(exc)]
    if task["id"] not in text or task["nonce"] not in text:
        problems.append("review file does not bind task/nonce")
    verdict = review.get("verdict")
    if not re.search(r"^Verdict:\s*" + re.escape(str(verdict)) + r"\s*$", text, re.M):
        problems.append("review file verdict does not match the sealed verdict")
    if event is not None and event["task"] != task["id"]:
        problems.append("review event belongs to another task")
    return ("sealed" if not problems else "changed"), problems


def _artifact_checks(task, request, event):
    """Return evidence rows and problems without rehashing new bytes as history.

    Each row is (path, sealed_sha256, current_sha256, bytes, state). A missing
    historical sha256 stays unknown even when the current file is readable: the
    current bytes are not evidence that the historical bytes matched.
    """
    try:
        data = json.loads(event["data"])
    except (TypeError, ValueError):
        return [], ["event data is not valid JSON"]
    if not isinstance(data, dict):
        return [], ["event data is not an object"]
    report = data.get("report")
    receipt = data.get("receipt")
    rows, problems = [], []
    if not isinstance(report, dict) or report.get("status") != "review_ready":
        return rows, ["event is not review_ready"]
    if not isinstance(receipt, dict):
        return rows, ["review_ready event has no completion receipt"]
    if receipt.get("task_id") != task["id"] or receipt.get("nonce") != task["nonce"]:
        problems.append("receipt does not bind task/nonce")
    cwd = Path(request.get("cwd", ".")).resolve()
    raw_items = receipt.get("artifacts", [])
    if not isinstance(raw_items, list):
        problems.append("receipt artifacts is not a list")
        raw_items = []
    items = list(raw_items)
    if receipt.get("binding_artifact") is not None:
        items.append(receipt["binding_artifact"])
    if not items:
        problems.append("receipt has no artifacts")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            problems.append("malformed artifact receipt entry")
            rows.append((Path("<malformed>"), None, None, None, "unknown"))
            continue
        path = Path(item["path"])
        resolved = path
        try:
            resolved = path.resolve()
            resolved.relative_to(cwd)
        except (OSError, ValueError):
            problems.append("artifact escapes task cwd: " + str(path))
            rows.append((resolved, None, None, None, "changed"))
            continue
        expected = item.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            problems.append("artifact has no sealed sha256: " + str(resolved))
            rows.append((resolved, None, None, None, "unknown"))
            continue
        if not resolved.is_file():
            problems.append("artifact is missing: " + str(resolved))
            rows.append((resolved, expected, None, None, "missing"))
            continue
        try:
            size = resolved.stat().st_size
            current = _file_sha(resolved)
        except OSError as exc:
            problems.append("artifact is unreadable: " + str(resolved) + ": " + str(exc))
            rows.append((resolved, expected, None, None, "unknown"))
            continue
        if current != expected:
            problems.append("artifact hash differs from the sealed receipt: " + str(resolved))
            rows.append((resolved, expected, current, size, "changed"))
            continue
        rows.append((resolved, expected, current, size, "sealed"))
    return rows, problems


def _termination_closure(con, task):
    """Return (valid_audit, problem). Acceptance is deliberately not required."""
    if not _table_exists(con, "audit"):
        return None, None
    for row in con.execute("SELECT data FROM audit WHERE action='task_termination_inspected'"):
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict) or value.get("task_id") != task["id"]:
            continue
        path = value.get("path")
        sealed = value.get("sha256")
        if not isinstance(path, str) or not isinstance(sealed, str):
            return None, "termination audit lacks a preserved note path/hash"
        note = Path(path)
        if not note.is_file():
            return None, "termination note is missing"
        try:
            current = _file_sha(note)
            text = note.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            return None, "termination note is unreadable: " + str(exc)
        if current != sealed:
            return None, "termination note hash differs from the audit"
        if task["id"] not in text or task["nonce"] not in text:
            return None, "termination note does not bind task/nonce"
        return value, None
    return None, None


def _frozen_binding(task):
    try:
        data = json.loads(task["data"])
    except (TypeError, ValueError):
        return None
    binding = data.get("worker_binding") if isinstance(data, dict) else None
    return binding if isinstance(binding, dict) else None


def _agent_id_for_frozen(task):
    binding = _frozen_binding(task)
    if not binding or not binding.get("instance_id"):
        return None
    return _agent_id_for_binding(binding["instance_id"])


def _import_task_bindings(con, project_id, tasks):
    """Import the frozen worker_binding identity from each historical attempt.

    Never use the role's current active binding: a replacement worker must not
    inherit an old attempt's attribution. A binding no longer active is
    historical provenance and is imported revoked, without a token.
    """
    now = _now()
    active_ids = {row[0] for row in con.execute(
        "SELECT id FROM bindings WHERE status='active'")}
    for task in tasks:
        binding = _frozen_binding(task)
        if not binding or not binding.get("instance_id"):
            continue
        agent_id = _agent_id_for_binding(binding["instance_id"])
        if con.execute("SELECT 1 FROM v2_agent WHERE agent_id=?", (agent_id,)).fetchone():
            continue
        role = binding.get("mode") if binding.get("mode") in ("worker", "reviewer", "controller") else "worker"
        handle = "frozen-" + re.sub(r"[^a-z0-9_-]", "-", str(binding.get("role") or role).lower())[:18] + \
                 "-" + agent_id[-8:]
        active = binding["instance_id"] in active_ids
        con.execute("INSERT OR IGNORE INTO v2_agent(agent_id,project_id,handle,role,harness,"
                    "harness_version,host,os,lifecycle,token_hash,source,parent_agent_id,created_at,"
                    "approved_at,last_seen_at,revoked_at,revision) "
                    "VALUES (?,?,?,?,?,?,?,?,?,NULL,'legacy_task_binding',NULL,?,?,NULL,?,1)",
                    (agent_id, project_id, handle, role,
                     str((binding.get("spec") or {}).get("harness") or "unknown"),
                     str((binding.get("spec") or {}).get("version") or "unknown"), "local", sys.platform,
                     "active" if active else "revoked", now, now if active else None,
                     None if active else now))
        for key in IMPORTED_CAPABILITIES:
            con.execute("INSERT OR IGNORE INTO v2_agent_capability(agent_id,capability_key,level,"
                        "harness_version,os,host,checked_at,evidence_sha256,source,declared_by,revision) "
                        "VALUES (?,?, 'unknown',?,?,?,NULL,NULL,'imported_binding','legacy',1)",
                        (agent_id, key, str((binding.get("spec") or {}).get("version") or "unknown"),
                         sys.platform, "local"))


def _agent_id_for_role(con, role):
    if not role:
        return None
    row = con.execute("SELECT id FROM bindings WHERE role=? AND status='active' LIMIT 1", (role,)).fetchone()
    return _agent_id_for_binding(row[0]) if row else None


def _agent_id_for_binding(binding_id):
    return "agt-" + hashlib.sha256(str(binding_id).encode()).hexdigest()[:16]


def _task_plan(con, state, task):
    """Compute one task's conservative v2 projection. Read-only."""
    task_id, nonce = task["id"], task["nonce"]
    data = json.loads(task["data"])
    if not isinstance(data, dict):
        raise ValueError("task data is not an object")
    request = data.get("request") or {}
    if not isinstance(request, dict):
        raise ValueError("task request is not an object")
    legacy_status = task["status"]
    uncertain = []

    provenance_ok = True
    try:
        request_digest = control.digest(request)
    except (TypeError, ValueError):
        request_digest = None
    if request_digest != task["request_hash"] or data.get("request_sha256") != task["request_hash"]:
        provenance_ok = False
        uncertain.append(("frozen_request_digest_mismatch",
                          "task data/request hash does not match the frozen request digest"))
    if request.get("task_id") != task_id or request.get("nonce") != nonce:
        provenance_ok = False
        uncertain.append(("frozen_identity_mismatch",
                          "frozen request task_id/nonce does not match the ledger row"))

    assignments = []
    if _table_exists(con, "dispatches"):
        assignments = [dict(row) for row in con.execute(
            "SELECT * FROM dispatches WHERE task=? ORDER BY rowid", (task_id,))]
    events = [dict(row) for row in con.execute(
        "SELECT rowid,* FROM events WHERE task=? ORDER BY rowid", (task_id,))]
    reviews = {}
    reviews_broken = False
    for row in con.execute("SELECT r.event,r.data FROM reviews r JOIN events e ON e.id=r.event "
                           "WHERE e.task=?", (task_id,)):
        try:
            value = json.loads(row[1])
        except (TypeError, ValueError):
            reviews_broken = True
            continue
        if isinstance(value, dict) and value.get("task_id") == task_id:
            reviews[row[0]] = value
    if reviews_broken:
        uncertain.append(("malformed_review_record", "a review row for this task is not valid JSON"))

    evidence_by_event = {}
    problems_by_event = {}
    for event in events:
        try:
            report = json.loads(event["data"]).get("report")
        except (TypeError, ValueError, AttributeError):
            report = None
        if isinstance(report, dict) and report.get("status") == "review_ready":
            evidence, problems = _artifact_checks(task, request, event)
            evidence_by_event[event["id"]] = evidence
            problems_by_event[event["id"]] = problems

    review_rows = []
    for event in events:
        review = reviews.get(event["id"])
        if not review:
            continue
        state_name, problems = _review_check(task, request, event, review)
        review_rows.append((event, review, state_name, problems))
        if review.get("task_id") != task_id or review.get("nonce") != nonce:
            problems.append("review does not bind task/nonce")

    # Reuse the existing acceptance gate, in this same connection. It rechecks
    # verdict/report/receipt/task+nonce and the review/artifact sealed hashes.
    try:
        from workflow_scheduler import acceptance as legacy_acceptance
        accepted_review = legacy_acceptance(con, task_id)
    except Exception:
        accepted_review = None
        uncertain.append(("acceptance_gate_error", "existing acceptance gate could not be evaluated"))
    accepted_valid = False
    if accepted_review is not None:
        accepted_event = next((e for e in events if e["id"] == accepted_review.get("event_id")), None)
        if (legacy_status not in ("finished", "resolved") or accepted_event is None
                or accepted_event["status"] != "reviewed"):
            uncertain.append(("accept_not_closed_or_bound",
                              "accept review is not tied to a reviewed event on a finished/resolved task"))
        elif accepted_review.get("task_id") != task_id or accepted_review.get("nonce") != nonce:
            uncertain.append(("accept_identity_mismatch", "accept review does not bind task/nonce"))
        else:
            try:
                event_data = json.loads(accepted_event["data"])
            except (TypeError, ValueError):
                event_data = {}
            if event_data.get("request_sha256") != task["request_hash"]:
                uncertain.append(("accept_request_hash_mismatch",
                                  "accepted event does not bind the frozen request hash"))
            else:
                evidence = evidence_by_event.get(accepted_event["id"], [])
                problems = problems_by_event.get(accepted_event["id"], ["missing evidence"])
                if evidence and all(row[4] == "sealed" for row in evidence) and not problems:
                    accepted_valid = provenance_ok
                else:
                    uncertain.append(("accept_evidence_not_sealed",
                                      "; ".join(sorted(set(problems))) or "accepted event evidence is not sealed"))

    evidence_sealed_any = provenance_ok and any(
        bool(evidence_by_event.get(event["id"])) and
        all(row[4] == "sealed" for row in evidence_by_event.get(event["id"], [])) and
        not problems_by_event.get(event["id"], [])
        for event in events
    )
    review_any_bad = any(item[2] != "sealed" or item[3] for item in review_rows)
    sealed_review_verdicts = {str(item[1].get("verdict")) for item in review_rows
                              if item[2] == "sealed" and not item[3]}
    accept_present = any(str(item[1].get("verdict")) == "accept" for item in review_rows)

    if not provenance_ok:
        task_state = "migration_uncertain"
    elif accepted_valid:
        task_state = "accepted"
    elif accepted_review is not None:
        task_state = "migration_uncertain"
    elif "reject" in sealed_review_verdicts and evidence_sealed_any:
        task_state = "rejected"
    elif "repair" in sealed_review_verdicts and evidence_sealed_any:
        task_state = "repair_required"
    elif legacy_status == "resolved":
        # A resolved (closed) attempt without a valid accepted/rejected/repair
        # review is not review_ready; it is deliberately uncertain.
        task_state = "migration_uncertain"
        uncertain.append(("resolved_without_accept_evidence", "resolved never implies accepted"))
    elif review_any_bad:
        task_state = "migration_uncertain"
        detail = "; ".join(sorted({p for item in review_rows for p in item[3]})) or \
                 "review is missing, changed, or unreadable"
        uncertain.append(("delivery_or_review_not_sealed", detail))
    elif accept_present:
        task_state = "migration_uncertain"
        uncertain.append(("accept_gate_not_valid",
                          "an accept review exists but the existing acceptance gate/identity binding is not valid"))
    elif reviews_broken:
        task_state = "migration_uncertain"
    elif legacy_status == "running":
        task_state = "active"
    elif evidence_sealed_any:
        task_state = "review_ready"
    elif review_rows or evidence_by_event:
        task_state = "migration_uncertain"
        detail = "; ".join(sorted({p for values in problems_by_event.values() for p in values})) or \
                 "review/evidence is missing, changed, or unreadable"
        uncertain.append(("delivery_or_review_not_sealed", detail))
    elif legacy_status == "finished":
        task_state = "migration_uncertain"
        uncertain.append(("finished_without_sealed_delivery", "finished task has no sealed review_ready receipt"))
    else:
        task_state = "migration_uncertain"
        uncertain.append(("unmapped_legacy_status", "legacy status " + str(legacy_status)))

    closure, closure_problem = _termination_closure(con, task)
    if closure_problem:
        uncertain.append(("invalid_termination_record", closure_problem))
    closed_review = legacy_status == "resolved" and bool(sealed_review_verdicts & {"reject", "repair"})
    released = (provenance_ok and legacy_status == "resolved"
                and ((task_state in ("accepted", "rejected", "repair_required")
                      and (accepted_valid or closed_review))
                     or closure is not None))

    # Assignment projection.
    assignment_rows = []
    assignment_agent_id = _agent_id_for_frozen(task)
    review_status = ("accepted" if task_state == "accepted" else
                     "awaiting_review" if task_state == "review_ready" else
                     "rejected" if task_state == "rejected" else
                     "repair" if task_state == "repair_required" else "none")
    if assignments:
        for dispatch in assignments:
            try:
                ddata = json.loads(dispatch["data"])
            except (TypeError, ValueError):
                ddata = {}
            status = dispatch["status"]
            state_map = {"reserved": "reserved", "submitted": "running", "uncertain": "uncertain",
                         "running": "running", "not_sent": "cancelled", "terminated": "cancelled"}
            ass_state = state_map.get(status, "uncertain")
            if legacy_status == "resolved":
                # Authoritative task closure wins over a legacy dispatch row that
                # release() left at running; dispatch_status is provenance only.
                ass_state = "completed" if task_state in ("review_ready", "accepted", "rejected",
                                                          "repair_required") else "cancelled"
            elif status == "resolved":
                ass_state = "completed" if task_state in ("review_ready", "accepted", "rejected",
                                                          "repair_required") else "cancelled"
            assignment_rows.append({
                "assignment_id": "asg-" + hashlib.sha256(
                    (task_id + ":" + str(ddata.get("generation", 1))).encode()).hexdigest()[:16],
                "generation": int(ddata.get("generation", 1)),
                "state": ass_state,
                "review_status": review_status,
                "holds_slot": 0 if released else 1,
                "dispatch_status": status,
                "legacy_dispatch_status": status,
                "agent_id": assignment_agent_id,
                "nonce": nonce,
            })
    else:
        request_generation = request.get("generation", 1)
        if type(request_generation) is not int or request_generation < 1:
            request_generation = 1
            uncertain.append(("invalid_request_generation", "request generation is not a positive integer"))
        assignment_rows.append({
            "assignment_id": "asg-hist-" + hashlib.sha256(
                (task_id + ":" + str(request_generation)).encode()).hexdigest()[:16],
            "generation": request_generation,
            "state": "historical_registered",
            "review_status": review_status,
            "holds_slot": 0 if released else 1,
            "dispatch_status": None,
            "legacy_dispatch_status": None,
            "agent_id": assignment_agent_id,
            "nonce": nonce,
        })
        if legacy_status == "running":
            uncertain.append(("registered_only_unresolved",
                              "registered-only task is still running/unresolved; slot retained"))

    return {
        "task": task,
        "legacy_status": legacy_status,
        "task_state": task_state,
        "assignments": assignment_rows,
        "evidence": [row for values in evidence_by_event.values() for row in values],
        "reviews": review_rows,
        "uncertain": uncertain,
        "request_sha256": task["request_hash"],
    }


def _projection_drift(con, plans):
    """Compare a fresh legacy projection with the stored v2 projection.

    Enable must not hand authority to a stale/incomplete snapshot. A missing
    map row, changed task state, slot ownership, or evidence/review integrity
    state is source drift and requires an explicit re-apply.
    """
    problems = []
    for plan in plans:
        if _runtime_owned(con, plan['task']['id']):
            continue
        task_id = plan["task"]["id"]
        task_row = con.execute("SELECT task_state FROM v2_task WHERE task_id=?", (task_id,)).fetchone()
        map_row = con.execute("SELECT 1 FROM v2_legacy_map WHERE legacy_task_id=?", (task_id,)).fetchone()
        if task_row is None or map_row is None:
            problems.append(task_id + ": not migrated")
            continue
        if task_row["task_state"] != plan["task_state"]:
            problems.append(task_id + ": task state drifted from " + task_row["task_state"] +
                            " to " + plan["task_state"])
        for ass in plan["assignments"]:
            row = con.execute("SELECT state,holds_slot,review_status FROM v2_assignment "
                              "WHERE assignment_id=?", (ass["assignment_id"],)).fetchone()
            if row is None:
                problems.append(task_id + ": assignment missing")
            elif (row["state"], row["holds_slot"], row["review_status"]) != (
                    ass["state"], ass["holds_slot"], ass["review_status"]):
                problems.append(task_id + ": assignment drifted")
        for path, sealed_sha, current_sha, size, evidence_state in plan["evidence"]:
            evidence_id = "ev-" + hashlib.sha256((task_id + ":" + str(path)).encode()).hexdigest()[:20]
            row = con.execute("SELECT state FROM v2_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            if row is None:
                problems.append(task_id + ": evidence missing from migration")
            elif row["state"] != evidence_state:
                problems.append(task_id + ": evidence integrity drifted")
        for event, review, review_state, problems_list in plan["reviews"]:
            review_id = "rev-" + hashlib.sha256((task_id + ":" + event["id"]).encode()).hexdigest()[:20]
            row = con.execute("SELECT state FROM v2_review WHERE review_id=?", (review_id,)).fetchone()
            if row is None:
                problems.append(task_id + ": review missing from migration")
            elif row["state"] != review_state:
                problems.append(task_id + ": review integrity drifted")
    return problems


def _fallback_plan(task, exc):
    return {
        "task": task,
        "legacy_status": task["status"],
        "task_state": "migration_uncertain",
        "assignments": [{
            "assignment_id": "asg-hist-" + hashlib.sha256(task["id"].encode()).hexdigest()[:16],
            "generation": 1,
            "state": "historical_registered",
            "review_status": "none",
            "holds_slot": 1,
            "dispatch_status": None,
            "legacy_dispatch_status": None,
            "agent_id": None,
            "nonce": task["nonce"],
        }],
        "evidence": [],
        "reviews": [],
        "uncertain": [("malformed_legacy_data",
                       "legacy task data could not be read safely: " + str(exc)[:400])],
        "request_sha256": task["request_hash"],
    }


def _plan(state, con):
    tasks = [dict(row) for row in con.execute("SELECT id,nonce,request_hash,worker,status,data FROM tasks ORDER BY id")]
    plans = []
    for task in tasks:
        try:
            plans.append(_task_plan(con, state, task))
        except Exception as exc:
            plans.append(_fallback_plan(task, exc))
    counts = {
        "legacy_tasks": len(tasks),
        "registered_only": sum(1 for p in plans if p["assignments"] and
                               p["assignments"][0]["state"] == "historical_registered"),
        "dispatches": con.execute("SELECT count(*) FROM dispatches").fetchone()[0]
        if _table_exists(con, "dispatches") else 0,
        "events": con.execute("SELECT count(*) FROM events").fetchone()[0],
        "reviews": con.execute("SELECT count(*) FROM reviews").fetchone()[0],
        "accepted": sum(1 for p in plans if p["task_state"] == "accepted"),
        "review_ready": sum(1 for p in plans if p["task_state"] == "review_ready"),
        "migration_uncertain": sum(1 for p in plans if p["uncertain"]),
    }
    uncertain = [{"task_id": p["task"]["id"], "reason": reason, "detail": detail}
                 for p in plans for reason, detail in p["uncertain"]]
    return plans, counts, uncertain


def migration_report(state, actor=None, version=None, context="main"):
    """Read-only dry run. Never creates schema, WAL, or rows."""
    path = _db_path(state)
    con = _connect(path, readonly=True)
    try:
        if actor is not None:
            _controller(con, actor, version, context)
        present = schema_present(con)
        plans, counts, uncertain = _plan(state, con)
        counts["v2_schema"] = "present" if present else "absent"
        return {"mode": "dry_run", "status": "DRY_RUN", "counts": counts,
                "uncertain": uncertain, "schema_present": present}
    finally:
        con.close()


IMPORTED_CAPABILITIES = (
    "spawn", "attach_resume", "send", "steer", "status", "cancel", "collect",
    "heartbeat", "session.resume", "execution.one_shot",
)


def _legacy_handle(con, project_id, role, agent_id):
    """A stable handle for an imported binding identity.

    The role name alone is not unique: a role rebind creates a new identity
    while the old one must remain as revoked provenance. Suffix a collision
    with the identity hash; never reuse the old identity's handle.
    """
    base = re.sub(r"[^a-z0-9_-]", "-", str(role).lower())[:20] or "legacy"
    if not base[0].isalpha():
        base = "agent-" + base
    base = base[:28]
    for attempt in range(8):
        candidate = base if attempt == 0 else (base + "-" + agent_id[-(6 + attempt * 2):])[:32]
        row = con.execute("SELECT agent_id FROM v2_agent WHERE project_id=? AND handle=?",
                          (project_id, candidate)).fetchone()
        if row is None or row[0] == agent_id:
            return candidate
    return (base + "-" + agent_id)[:32]


def _retire_legacy_agents(con, project_id, active_agent_ids):
    """Mark superseded imported identities revoked; never revive revoked rows."""
    now = _now()
    for row in con.execute("SELECT agent_id,lifecycle FROM v2_agent "
                           "WHERE project_id=? AND source LIKE 'legacy%'", (project_id,)):
        if row["agent_id"] not in active_agent_ids and row["lifecycle"] == "active":
            con.execute("UPDATE v2_agent SET lifecycle='revoked',revoked_at=?,revision=revision+1 "
                        "WHERE agent_id=? AND lifecycle='active'", (now, row["agent_id"]))


def _import_bindings(con, project_id):
    """Import active local bindings as provenance-bearing registry identities.

    No token is issued and no capability is verified: every imported operation
    stays unknown until independent evidence is recorded.
    """
    now = _now()
    active_agent_ids = set()
    for row in con.execute("SELECT id,role,version,mode,data FROM bindings WHERE status='active'"):
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        spec = data.get("spec") if isinstance(data.get("spec"), dict) else {}
        role = row["mode"] if row["mode"] in ("worker", "reviewer", "controller") else "observer"
        agent_id = _agent_id_for_binding(row["id"])
        active_agent_ids.add(agent_id)
        handle = _legacy_handle(con, project_id, row["role"], agent_id)
        con.execute("INSERT OR IGNORE INTO v2_agent(agent_id,project_id,handle,role,harness,"
                    "harness_version,host,os,lifecycle,token_hash,source,parent_agent_id,created_at,"
                    "approved_at,last_seen_at,revoked_at,revision) "
                    "VALUES (?,?,?,?,?,?,?,?, 'active',NULL,'legacy_binding',NULL,?,?,NULL,NULL,1)",
                    (agent_id, project_id, handle, role,
                     str(spec.get("harness") or "unknown"), str(spec.get("version") or "unknown"),
                     "local", sys.platform, now, now))
        for key in IMPORTED_CAPABILITIES:
            con.execute("INSERT OR IGNORE INTO v2_agent_capability(agent_id,capability_key,level,"
                        "harness_version,os,host,checked_at,evidence_sha256,source,declared_by,revision) "
                        "VALUES (?,?, 'unknown',?,?,?,NULL,NULL,'imported_binding','legacy',1)",
                        (agent_id, key, str(spec.get("version") or "unknown"), sys.platform, "local"))
    return active_agent_ids


def _insert_plan(con, state, plans):
    for plan in plans:
        attempt = plan['task']['id']
        linked = con.execute('SELECT task_id FROM v2_assignment WHERE legacy_task_id=?',
                             (attempt,)).fetchall()
        if _runtime_owned(con, attempt) or any(_runtime_owned(con, row[0]) for row in linked):
            raise ServiceError('legacy import cannot overwrite a V2-owned task: ' +
                               plan['task']['id'] + '; use schema-upgrade for additive schema changes')
    project_id = _ensure_project(con, state)
    active_agent_ids = _import_bindings(con, project_id)
    _import_task_bindings(con, project_id, [plan["task"] for plan in plans])
    _retire_legacy_agents(con, project_id, active_agent_ids)
    now = _now()
    for plan in plans:
        task = plan["task"]
        con.execute("INSERT OR IGNORE INTO v2_task(task_id,project_id,legacy_status,title,body,priority,"
                    "task_state,execution_mode,fanout_slots,accepted_evidence_id,revision,created_at,updated_at) "
                    "VALUES (?,?,?,?, '', 'P3',?,'single',1,NULL,1,?,?)",
                    (task["id"], project_id, plan["legacy_status"], task["id"], plan["task_state"], now, now))
        for ass in plan["assignments"]:
            con.execute("INSERT OR IGNORE INTO v2_assignment(assignment_id,task_id,agent_id,generation,slot_id,"
                        "holds_slot,state,review_status,workspace_id,nonce,legacy_task_id,"
                        "legacy_dispatch_status,revision,created_at,updated_at) "
                        "VALUES (?,?,?,?,0,?,?,?,NULL,?,?,?,1,?,?)",
                        (ass["assignment_id"], task["id"], ass.get("agent_id"), ass["generation"],
                         ass["holds_slot"], ass["state"], ass["review_status"], ass["nonce"], task["id"],
                         ass.get("legacy_dispatch_status"), now, now))
            con.execute("UPDATE v2_assignment SET state=?,holds_slot=?,review_status=?,"
                        "legacy_dispatch_status=?,agent_id=?,updated_at=? WHERE assignment_id=?",
                        (ass["state"], ass["holds_slot"], ass["review_status"],
                         ass.get("legacy_dispatch_status"), ass.get("agent_id"), now, ass["assignment_id"]))
        first_ass = plan["assignments"][0]
        accept_review = next((r for r in plan["reviews"] if r[1].get("verdict") == "accept"), None)
        con.execute("INSERT OR IGNORE INTO v2_legacy_map(legacy_task_id,v2_task_id,v2_assignment_id,"
                    "nonce,request_sha256,review_event_id,review_sha256,generation,source) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (task["id"], task["id"], first_ass["assignment_id"], task["nonce"],
                     plan["request_sha256"], accept_review[0]["id"] if accept_review else None,
                     accept_review[1].get("sha256") if accept_review else None,
                     first_ass["generation"], "legacy"))
        first_sealed = None
        for path, sealed_sha, current_sha, size, evidence_state in plan["evidence"]:
            evidence_id = "ev-" + hashlib.sha256((task["id"] + ":" + str(path)).encode()).hexdigest()[:20]
            con.execute("INSERT OR IGNORE INTO v2_evidence(evidence_id,task_id,assignment_id,kind,path,"
                        "sealed_sha256,current_sha256,bytes,sealed,state,source,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (evidence_id, task["id"], first_ass["assignment_id"], "artifact", str(path),
                         sealed_sha, current_sha, size, 1 if evidence_state == "sealed" else 0,
                         evidence_state, "legacy", now))
            con.execute("UPDATE v2_evidence SET current_sha256=?,bytes=?,sealed=?,state=? "
                        "WHERE evidence_id=?", (current_sha, size,
                                                1 if evidence_state == "sealed" else 0,
                                                evidence_state, evidence_id))
            if evidence_state == "sealed" and first_sealed is None:
                first_sealed = evidence_id
        if plan["task_state"] == "accepted" and first_sealed:
            con.execute("UPDATE v2_task SET accepted_evidence_id=? WHERE task_id=?",
                        (first_sealed, task["id"]))
        con.execute("UPDATE v2_task SET task_state=?,accepted_evidence_id=?,updated_at=? "
                    "WHERE task_id=? AND legacy_status IS NOT NULL",
                    (plan["task_state"], first_sealed if plan["task_state"] == "accepted" else None,
                     now, task["id"]))
        for event, review, review_state, problems in plan["reviews"]:
            review_id = "rev-" + hashlib.sha256((task["id"] + ":" + event["id"]).encode()).hexdigest()[:20]
            con.execute("INSERT OR IGNORE INTO v2_review(review_id,task_id,assignment_id,event_id,verdict,"
                        "note_path,note_sha256,state,actor_json,reviewed_at,source) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (review_id, task["id"], first_ass["assignment_id"], event["id"],
                         str(review.get("verdict")), str(review.get("path")), review.get("sha256"),
                         review_state, json.dumps(review.get("actor")) if review.get("actor") else None,
                         review.get("reviewed_at"), "legacy"))
            con.execute("UPDATE v2_review SET state=? WHERE review_id=?", (review_state, review_id))
        for row in con.execute("SELECT rowid,id,task,nonce,role,status,data FROM events WHERE task=? ORDER BY rowid",
                               (task["id"],)):
            con.execute("INSERT OR IGNORE INTO v2_event(event_id,project_id,entity_type,entity_id,event_type,"
                        "actor,global_seq,source,source_seq,source_key,at,payload_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("legacy-event-" + str(row["rowid"]), project_id, "task", task["id"],
                         "legacy.event", "legacy", _next_seq(con), "legacy_events", row["rowid"],
                         row["id"], now, row["data"]))
        if _table_exists(con, "task_journal"):
            for row in con.execute("SELECT seq,id,task,kind,data FROM task_journal WHERE task=? ORDER BY seq",
                                   (task["id"],)):
                con.execute("INSERT OR IGNORE INTO v2_event(event_id,project_id,entity_type,entity_id,event_type,"
                            "actor,global_seq,source,source_seq,source_key,at,payload_json) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("legacy-journal-" + str(row["seq"]), project_id, "task", task["id"],
                             "legacy." + str(row["kind"]), "legacy", _next_seq(con), "legacy_journal",
                             row["seq"], row["id"], now, row["data"]))
        for reason, detail in plan["uncertain"]:
            con.execute("INSERT OR REPLACE INTO v2_migration_uncertain(task_id,reason,detail) VALUES (?,?,?)",
                        (task["id"], reason, detail))
    for row in con.execute("SELECT seq,action,data,at FROM audit ORDER BY seq"):
        con.execute("INSERT OR IGNORE INTO v2_event(event_id,project_id,entity_type,entity_id,event_type,"
                    "actor,global_seq,source,source_seq,source_key,at,payload_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("legacy-audit-" + str(row["seq"]), project_id, "audit", str(row["seq"]),
                     "legacy." + str(row["action"]), "legacy", _next_seq(con), "legacy_audit",
                     row["seq"], None, row["at"], row["data"]))
    return project_id


def _next_seq(con):
    return con.execute("SELECT COALESCE(MAX(global_seq),0)+1 FROM v2_event").fetchone()[0]


def _runtime_owned(con, task_id):
    if not _table_exists(con, 'v2_task'):
        return False
    row = con.execute('SELECT * FROM v2_task WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return False
    if 'runtime_owner' in row.keys() and row['runtime_owner'] == 'v2':
        return True
    # Schema 1/2 already supported native task.create. Ownership did not begin
    # when the explicit column was introduced. Ambiguous old rows fail closed.
    return row['legacy_status'] is None or bool(con.execute(
        "SELECT 1 FROM v2_event WHERE entity_type='task' AND entity_id=? "
        "AND event_type='task.created' AND source='v2'", (task_id,)).fetchone())


def _backfill_runtime_owners(con):
    for row in con.execute("SELECT task_id,legacy_status FROM v2_task WHERE runtime_owner='legacy'").fetchall():
        native = con.execute("SELECT 1 FROM v2_event WHERE entity_type='task' AND entity_id=? "
                             "AND event_type='task.created' AND source='v2'", (row['task_id'],)).fetchone()
        if not native and row['legacy_status'] is not None:
            continue
        mapped = con.execute('SELECT 1 FROM v2_legacy_map WHERE v2_task_id=?', (row['task_id'],)).fetchone()
        if not native or mapped:
            raise ServiceError('ambiguous pre-upgrade Task ownership requires inspection: ' + row['task_id'])
        con.execute("UPDATE v2_task SET runtime_owner='v2' WHERE task_id=?", (row['task_id'],))


def upgrade_schema(state, actor, version, context):
    # Additive upgrade never replays historical lifecycle projections.
    con = _connect(_db_path(state))
    try:
        con.execute('BEGIN IMMEDIATE')
        _controller(con, actor, version, context)
        if not schema_present(con):
            raise ServiceError('initial migration is required before schema-upgrade')
        current = _state_row(con)
        if current['v2_enabled'] and not current['frozen']:
            raise ServiceError('freeze the active runtime before schema-upgrade')
        create_schema(con)
        _event(con, current['project_id'], 'service', '1', 'schema.upgraded',
               actor, 'v2', None, {'schema_version': V2_SCHEMA_VERSION})
        con.commit()
        return {'status': 'SCHEMA_UPGRADED', 'schema_version': V2_SCHEMA_VERSION,
                'legacy_projection_replayed': False}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def apply_migration(state, actor, version, context):
    """Apply the conservative projection in one transaction. Repeat-safe."""
    path = _db_path(state)
    con = _connect(path)
    run_id = uuid.uuid4().hex
    try:
        con.execute("BEGIN IMMEDIATE")
        _controller(con, actor, version, context)
        if schema_present(con):
            held = con.execute('SELECT DISTINCT task_id FROM v2_assignment WHERE holds_slot=1').fetchall()
            if any(_runtime_owned(con, row[0]) for row in held):
                raise ServiceError('legacy import cannot overwrite a V2-owned runtime reservation; '
                                   'use schema-upgrade for additive schema changes')
        create_schema(con)
        plans, counts, uncertain = _plan(state, con)
        _insert_plan(con, state, plans)
        con.execute("INSERT INTO v2_migration_run(run_id,mode,actor,started_at,finished_at,status,counts_json) "
                    "VALUES (?,?,?,?,?,'applied',?)",
                    (run_id, "apply", actor, _now(), _now(), json.dumps(counts, sort_keys=True)))
        con.commit()
        return {"mode": "apply", "status": "APPLIED", "run_id": run_id,
                "counts": counts, "uncertain": uncertain}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def service_status(state):
    path = _db_path(state)
    con = _connect(path, readonly=True)
    try:
        legacy = {
            "tasks": con.execute("SELECT count(*) FROM tasks").fetchone()[0],
            "events": con.execute("SELECT count(*) FROM events").fetchone()[0],
            "reviews": con.execute("SELECT count(*) FROM reviews").fetchone()[0],
        }
        if not schema_present(con):
            return {"v2": "absent", "legacy": legacy}
        row = _state_row(con)
        return {
            "v2": "present",
            "schema_version": con.execute("SELECT value FROM v2_meta WHERE key='schema_version'").fetchone()[0],
            "project_id": row["project_id"],
            "v2_enabled": bool(row["v2_enabled"]),
            "frozen": bool(row["frozen"]),
            "controller_epoch": row["controller_epoch"],
            "counts": {
                "tasks": con.execute("SELECT count(*) FROM v2_task").fetchone()[0],
                "assignments": con.execute("SELECT count(*) FROM v2_assignment").fetchone()[0],
                "slots_held": con.execute("SELECT count(*) FROM v2_assignment WHERE holds_slot=1").fetchone()[0],
                "uncertain": con.execute("SELECT count(*) FROM v2_assignment WHERE state='uncertain'").fetchone()[0],
                "agents": con.execute("SELECT count(*) FROM v2_agent").fetchone()[0],
                "grants": con.execute("SELECT count(*) FROM v2_enrollment_grant").fetchone()[0],
                "events": con.execute("SELECT count(*) FROM v2_event").fetchone()[0],
            },
        }
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI

def _load_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return value


def _print(value):
    print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True))


def gateway_principal(state, project_root, token):
    from service_access import principal
    from service_api import APIError, _error_code
    try:
        return principal(state, project_root, token)
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise APIError(_error_code(exc), str(exc)) from exc


def gateway_tool_specs(messages_enabled=False):
    from service_api import tool_specs
    return tool_specs(messages_enabled)


def gateway_call(state, project_root, token, tool, arguments):
    from service_api import call
    return call(state, project_root, token, tool, arguments)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="awo_service.py",
        description="Opt-in AWO V2 service boundary: dry-run/apply migration, status, "
                    "freeze/epoch controls, and envelope commands.")
    parser.add_argument("--state", required=True, help="AWO state directory containing workflow.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="read-only service/legacy counts")
    status.add_argument("--format", choices=("json", "text"), default="json")

    migrate = sub.add_parser("migrate", help="conservative legacy-to-v2 projection; dry-run by default")
    migrate.add_argument("--apply", action="store_true", help="write the projection (requires controller)")
    migrate.add_argument("--actor")
    migrate.add_argument("--version", type=int)
    migrate.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    for name, help_text in (("freeze", "block dispatch/register; increments epoch"),
                            ("unfreeze", "clear freeze only when no V2 slot/uncertain remains"),
                            ("enable", "make V2 the authority; blocks legacy dispatch"),
                            ("disable", "return to legacy mode; never deletes V2 tables"),
                            ("schema-upgrade", "additive schema upgrade without legacy state replay")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--actor", required=True)
        cmd.add_argument("--version", type=int, required=True)
        cmd.add_argument("--context", choices=("main", "side", "unknown"), default="main")
        if name == "freeze":
            cmd.add_argument("--reason", default="")

    command = sub.add_parser("command", help="execute a Controller Task or scheduler envelope")
    command.add_argument("--envelope", required=True, help="JSON envelope file")
    command.add_argument("--actor", required=True)
    command.add_argument("--version", type=int, required=True)
    command.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    get = sub.add_parser("task-get", help="read one v2 task")
    get.add_argument("--task", required=True)
    get.add_argument("--actor", required=True)
    get.add_argument("--version", type=int, required=True)
    get.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    listing = sub.add_parser("task-list", help="read v2 tasks")
    listing.add_argument("--actor", required=True)
    listing.add_argument("--version", type=int, required=True)
    listing.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    enroll = sub.add_parser("agent-enroll", help="controller issues a single-use enrollment grant")
    enroll.add_argument("--envelope", required=True)
    enroll.add_argument("--secret-out", required=True, help="exclusive local file for the grant secret")
    enroll.add_argument("--actor", required=True)
    enroll.add_argument("--version", type=int, required=True)
    enroll.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    reg = sub.add_parser("agent-register", help="redeem an enrollment grant and receive an agent token")
    reg.add_argument("--envelope", required=True)
    reg.add_argument("--grant-file", required=True)
    reg.add_argument("--token-out", required=True)

    self_reg = sub.add_parser("agent-self-register", help="request pending worker registration")
    self_reg.add_argument("--envelope", required=True)

    approve = sub.add_parser("agent-approve", help="controller approves a pending worker and issues its token")
    approve.add_argument("--envelope", required=True)
    approve.add_argument("--token-out", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--version", type=int, required=True)
    approve.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    revoke = sub.add_parser("agent-revoke", help="controller revokes an agent")
    revoke.add_argument("--envelope", required=True)
    revoke.add_argument("--actor", required=True)
    revoke.add_argument("--version", type=int, required=True)
    revoke.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    grevoke = sub.add_parser("grant-revoke", help="controller revokes an unused enrollment grant")
    grevoke.add_argument("--envelope", required=True)
    grevoke.add_argument("--actor", required=True)
    grevoke.add_argument("--version", type=int, required=True)
    grevoke.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    cap = sub.add_parser("agent-capability", help="controller records capability evidence")
    cap.add_argument("--envelope", required=True)
    cap.add_argument("--actor", required=True)
    cap.add_argument("--version", type=int, required=True)
    cap.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    beat = sub.add_parser("agent-heartbeat", help="agent updates liveness metadata only")
    beat.add_argument("--envelope", required=True)
    beat.add_argument("--token-file", required=True)

    astatus = sub.add_parser("agent-status", help="agent reads its own record and capabilities")
    astatus.add_argument("--token-file", required=True)

    alist = sub.add_parser("agent-list", help="list registry agents")
    alist.add_argument("--actor", required=True)
    alist.add_argument("--version", type=int, required=True)
    alist.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    aget = sub.add_parser("agent-get", help="read one registry agent")
    aget.add_argument("--agent", required=True)
    aget.add_argument("--actor", required=True)
    aget.add_argument("--version", type=int, required=True)
    aget.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    amatch = sub.add_parser("agent-match", help="check required capabilities against an agent")
    amatch.add_argument("--agent", required=True)
    amatch.add_argument("--required", required=True, help="comma-separated capability keys")
    amatch.add_argument("--actor", required=True)
    amatch.add_argument("--version", type=int, required=True)
    amatch.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    bstatus = sub.add_parser("budget-status", help="read a budget scope projection")
    bstatus.add_argument("--scope-type", choices=("run", "task", "workflow"), required=True)
    bstatus.add_argument("--scope-id", required=True)

    for name, help_text in (
            ("budget-reserve", "reserve every bounded scope/dimension atomically"),
            ("budget-sample", "record a delta or cumulative usage sample"),
            ("budget-close", "close a run usage; authoritative snapshot replaces deltas"),
            ("budget-limit", "controller replaces limits/policy with revision CAS"),
            ("budget-action", "controller records warning/pause/approval/cancel-request state"),
            ("budget-approve", "controller clears approval state")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--envelope", required=True)
        cmd.add_argument("--actor", required=True)
        cmd.add_argument("--version", type=int, required=True)
        cmd.add_argument("--context", choices=("main", "side", "unknown"), default="main")

    for name in ('access-issue', 'access-revoke'):
        access = sub.add_parser(name, help='issue or revoke scoped application access')
        access.add_argument('--actor', required=True)
        access.add_argument('--version', type=int, required=True)
        access.add_argument('--context', choices=('main', 'side', 'unknown'), default='main')
        access.add_argument('--envelope', required=True)
        if name == 'access-issue':
            access.add_argument('--token-out', required=True)

    api = sub.add_parser('api', help='same authenticated tool call used by MCP and HTTP')
    api.add_argument('--project-root', required=True)
    api.add_argument('--token-file', required=True)
    api.add_argument('--tool', required=True)
    api.add_argument('--arguments', required=True, help='tool arguments JSON file')
    api.add_argument('--credential-out', help='exclusive file for a new enrollment credential')
    ext = sub.add_parser('extension', help='authenticated project knowledge and document operations')
    ext.add_argument('--project-root', required=True)
    ext.add_argument('--token-file', required=True)
    ext.add_argument('--tool', required=True)
    ext.add_argument('--arguments', required=True, help='payload and command envelope JSON file')

    for name in ('merge-execute', 'merge-recover', 'collaboration-run'):
        cmd = sub.add_parser(name, help='authenticated runner for a committed external-effect intent')
        cmd.add_argument('--project-root', required=True)
        cmd.add_argument('--token-file', required=True)
        cmd.add_argument('--expected-epoch', type=int, required=True)
        cmd.add_argument('--verification' if name == 'collaboration-run' else '--intent', required=True)

    for name in ('scheduler-queue', 'assignment-dispatch', 'assignment-reconcile'):
        cmd = sub.add_parser(name, help='guarded V2 scheduler runtime')
        cmd.add_argument('--actor', required=True)
        cmd.add_argument('--version', type=int, required=True)
        cmd.add_argument('--context', choices=('main', 'side', 'unknown'), default='main')
        if name != 'scheduler-queue':
            cmd.add_argument('--assignment', required=True)
            cmd.add_argument('--expected-epoch', type=int, required=True)

    for name in ('workspace-create', 'workspace-recover', 'workspace-release', 'workspace-inspect',
                 'workspace-freeze'):
        cmd = sub.add_parser(name, help='Controller managed workspace operation')
        cmd.add_argument('--actor', required=True)
        cmd.add_argument('--version', type=int, required=True)
        cmd.add_argument('--context', choices=('main', 'side', 'unknown'), default='main')
        if name == 'workspace-inspect':
            cmd.add_argument('--workspace', required=True)
        else:
            cmd.add_argument('--envelope', required=True)

    for name in ('views', 'views-html'):
        cmd = sub.add_parser(name, help='authenticated read-only runtime projection')
        cmd.add_argument('--project-root', required=True)
        cmd.add_argument('--token-file', required=True)
        cmd.add_argument('--limit', type=int, default=200)
        if name == 'views-html':
            cmd.add_argument('--view', choices=('project', 'milestones', 'task_board', 'registry',
                                                'assignments', 'queue', 'workspaces', 'transcript',
                                                'evidence', 'review', 'budget', 'resources', 'recovery'))

    export_cmd = sub.add_parser('receipt-export', help='opt-in export to a new isolated local Git repo')
    export_cmd.add_argument('--project-root', required=True)
    export_cmd.add_argument('--token-file', required=True)
    export_cmd.add_argument('--destination', required=True)
    export_cmd.add_argument('--enable-export', action='store_true')
    export_cmd.add_argument('--protected-root', action='append', required=True)

    for name in ('receipt-verify', 'receipt-rebuild'):
        cmd = sub.add_parser(name, help='authenticated local audit receipt operation')
        cmd.add_argument('--project-root', required=True)
        cmd.add_argument('--token-file', required=True)
        cmd.add_argument('--repository', required=True)
        cmd.add_argument('--manifest-sha256')
        cmd.add_argument('--commit')
        if name == 'receipt-rebuild':
            cmd.add_argument('--audit-db', required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = service_status(args.state)
        elif args.command == "migrate":
            if args.apply:
                if args.actor is None or args.version is None:
                    parser.error("migrate --apply requires --actor and --version for the active controller")
                result = apply_migration(args.state, args.actor, args.version, args.context)
            else:
                result = migration_report(args.state, args.actor, args.version, args.context)
        elif args.command == "freeze":
            result = set_freeze(args.state, args.actor, args.version, args.context, True, args.reason)
        elif args.command == "unfreeze":
            result = set_freeze(args.state, args.actor, args.version, args.context, False, "")
        elif args.command == "enable":
            result = set_enabled(args.state, args.actor, args.version, args.context, True)
        elif args.command == "disable":
            result = set_enabled(args.state, args.actor, args.version, args.context, False)
        elif args.command == "schema-upgrade":
            result = upgrade_schema(args.state, args.actor, args.version, args.context)
        elif args.command == "command":
            result = execute_command(args.state, args.actor, args.version, args.context,
                                     _load_json(args.envelope))
        elif args.command == "task-get":
            result = task_get(args.state, args.actor, args.version, args.context, args.task)
        elif args.command == "task-list":
            result = task_list(args.state, args.actor, args.version, args.context)
        elif args.command == "agent-enroll":
            result = _enroll_agent(args.state, args.actor, args.version, args.context,
                                   _load_json(args.envelope), args.secret_out)
        elif args.command == "agent-register":
            result = register_agent(args.state, args.grant_file, _load_json(args.envelope), args.token_out)
        elif args.command == "agent-self-register":
            result = self_register_agent(args.state, _load_json(args.envelope))
        elif args.command == "agent-approve":
            result = approve_agent(args.state, args.actor, args.version, args.context,
                                   _load_json(args.envelope), args.token_out)
        elif args.command == "agent-revoke":
            result = revoke_agent(args.state, args.actor, args.version, args.context,
                                  _load_json(args.envelope))
        elif args.command == "grant-revoke":
            result = revoke_grant(args.state, args.actor, args.version, args.context,
                                  _load_json(args.envelope))
        elif args.command == "agent-capability":
            result = set_agent_capability(args.state, args.actor, args.version, args.context,
                                          _load_json(args.envelope))
        elif args.command == "agent-heartbeat":
            result = heartbeat_agent(args.state, args.token_file, _load_json(args.envelope))
        elif args.command == "agent-status":
            result = agent_status(args.state, args.token_file)
        elif args.command == "agent-list":
            result = agent_list(args.state, args.actor, args.version, args.context)
        elif args.command == "agent-get":
            result = agent_get(args.state, args.actor, args.version, args.context, args.agent)
        elif args.command == "agent-match":
            required = [item.strip() for item in args.required.split(",") if item.strip()]
            result = agent_match(args.state, args.actor, args.version, args.context, args.agent, required)
        elif args.command == 'access-issue':
            from service_access import issue_token
            result = issue_token(args.state, args.actor, args.version, args.context,
                                 _load_json(args.envelope), args.token_out)
        elif args.command == 'access-revoke':
            from service_access import revoke_token
            result = revoke_token(args.state, args.actor, args.version, args.context,
                                  _load_json(args.envelope))
        elif args.command == 'extension':
            from service_extensions import call
            result = call(args.state, args.project_root, _read_secret_file(args.token_file, 'access'),
                          args.tool, _load_json(args.arguments))
        elif args.command == 'api':
            if args.tool == 'awo_agent_register' and not args.credential_out:
                raise ServiceError('agent registration requires --credential-out; credentials are not printed')
            result = gateway_call(args.state, args.project_root,
                                  _read_secret_file(args.token_file, 'access'), args.tool,
                                  _load_json(args.arguments))
            if 'access_token' in result:
                secret = result.pop('access_token')
                # A repeated command may recover the same credential, but never
                # overwrites any existing file with a different secret.
                target = Path(args.credential_out)
                if target.exists():
                    if _read_secret_file(target, 'issued access') != secret:
                        raise ServiceError('credential output already exists with different content')
                    result['token_file'] = str(target)
                else:
                    result['token_file'] = _secret_file(args.credential_out, secret)
        elif args.command in ('merge-execute', 'merge-recover', 'collaboration-run'):
            import application_runtime
            token = _read_secret_file(args.token_file, 'Controller access')
            if args.command == 'collaboration-run':
                result = application_runtime.collaboration_run(args.state, args.project_root, token,
                                                               args.verification, args.expected_epoch)
            else:
                result = application_runtime.merge_run(args.state, args.project_root, token,
                        args.intent, args.expected_epoch, recover=args.command == 'merge-recover')
        elif args.command == 'scheduler-queue':
            from priority_scheduler import queue_status
            con = _connect(_db_path(args.state), readonly=True)
            try:
                _binding(con, args.actor, args.version, args.context)
                result = queue_status(con, _project_id(con))
            finally:
                con.close()
        elif args.command in ('assignment-dispatch', 'assignment-reconcile'):
            from priority_scheduler import dispatch_assignment, reconcile_assignment
            handler = dispatch_assignment if args.command == 'assignment-dispatch' else reconcile_assignment
            result = handler(args.state, args.assignment, args.actor, args.version,
                             args.context, expected_epoch=args.expected_epoch)
        elif args.command.startswith('workspace-'):
            import workspace_runtime
            handler = getattr(workspace_runtime, args.command.removeprefix('workspace-'))
            target = args.workspace if args.command == 'workspace-inspect' else _load_json(args.envelope)
            result = handler(args.state, args.actor, args.version, args.context, target)
        elif args.command in ('views', 'views-html', 'receipt-export', 'receipt-verify', 'receipt-rebuild'):
            import service_observability
            token = _read_secret_file(args.token_file, 'access')
            if args.command == 'views':
                result = service_observability.views(args.state, args.project_root, token, limit=args.limit)
            elif args.command == 'views-html':
                result = service_observability.views_html(args.state, args.project_root, token,
                                                          limit=args.limit, view=args.view)
            elif args.command == 'receipt-export':
                if not args.enable_export:
                    raise ServiceError('Receipt export requires the explicit --enable-export opt-in.')
                result = service_observability.export(
                    args.state, args.project_root, token, args.destination,
                    enabled=True, protected_roots=args.protected_root)
            elif args.command == 'receipt-verify':
                result = service_observability.verify(
                    args.state, args.project_root, token, args.repository,
                    expected_manifest_sha256=args.manifest_sha256, expected_commit=args.commit)
            else:
                result = service_observability.rebuild(
                    args.state, args.project_root, token, args.repository, args.audit_db,
                    expected_manifest_sha256=args.manifest_sha256, expected_commit=args.commit)
        elif args.command == "budget-status":
            from budget import budget_status
            result = budget_status(args.state, args.scope_type, args.scope_id)
        elif args.command.startswith("budget-"):
            from budget import (budget_reserve, budget_sample, budget_close,
                                budget_limit, budget_action, budget_approve)
            handlers = {"budget-reserve": budget_reserve, "budget-sample": budget_sample,
                        "budget-close": budget_close, "budget-limit": budget_limit,
                        "budget-action": budget_action, "budget-approve": budget_approve}
            result = handlers[args.command](args.state, args.actor, args.version, args.context,
                                            _load_json(args.envelope))
        else:
            raise ServiceError("unknown command")
    except (ServiceError, FileNotFoundError, sqlite3.Error, OSError, ValueError) as exc:
        print("awo_service: " + str(exc), file=sys.stderr)
        return 2
    if args.command == "status" and args.format == "text":
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
