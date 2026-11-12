"""Durable P1-1 messages in the caller's AWO SQLite transaction.

Authentication, command replay, epoch fencing and the outer command event belong
to awo_service. These domain functions validate registry identity and ownership,
record domain events, and never change Task or Assignment state. There is no
transport here: a queued delivery is available to an authenticated inbox reader.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import uuid


KINDS = ("question", "answer", "handoff", "notice", "consult", "state_request", "steer")
ROLES = ("controller", "worker", "reviewer", "observer")
UNREAD = ("held", "sending", "pushed", "notified")
DELIVERY_STATES = UNREAD + ("read", "acknowledged", "answered", "expired")
MAX_RECIPIENTS = 50
MAX_BODY_BYTES = 65536


def _fail(code, detail):
    # Lazy imports keep the parent service free to import this domain module.
    import awo_service as service
    raise service.ServiceError(code + ": " + detail)


def _event(con, project_id, principal, entity_type, entity_id, event_type, payload):
    import awo_service as service
    return service._event(con, project_id, entity_type, entity_id, event_type,
                          principal["principal_id"], "v2", None, payload)


def _now():
    return datetime.now(timezone.utc)


def _rows(con, sql, values=()):
    cursor = con.execute(sql, values)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _one(con, sql, values=()):
    rows = _rows(con, sql, values)
    return rows[0] if rows else None


def _text(value, label, maximum=256):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        _fail("invalid_request", label + " must be a nonempty bounded string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("invalid_request", label + " must be valid UTF-8 text")
    return value


def _integer(value, label, maximum=None):
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        _fail("invalid_request", label + " must be a positive integer")
    return value


def _timestamp(value, label):
    if not isinstance(value, str) or len(value) > 64:
        _fail("invalid_request", label + " must be an ISO timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail("invalid_request", label + " must be an ISO timestamp with timezone")


def _object(value, allowed, label):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail("invalid_request", label + " must be an object with string keys")
    extra = set(value) - set(allowed)
    if extra:
        _fail("invalid_request", label + " has unknown fields: " + ", ".join(sorted(extra)))


def _transaction(con):
    if not con.in_transaction:
        _fail("transaction_required", "caller must begin a write transaction")


def create_message_schema(con):
    """Add tables/indexes; no commit, migration, connection or transaction start."""
    _transaction(con)
    statements = (
        """CREATE TABLE IF NOT EXISTS v2_message (
          message_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          sender_agent_id TEXT NOT NULL REFERENCES v2_agent(agent_id),
          from_principal TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN
            ('question','answer','handoff','notice','consult','state_request','steer')),
          body TEXT NOT NULL CHECK (length(body)>0),
          thread_id TEXT NOT NULL,
          reply_to TEXT REFERENCES v2_message(message_id),
          task_id TEXT REFERENCES v2_task(task_id),
          assignment_id TEXT REFERENCES v2_assignment(assignment_id),
          generation INTEGER,
          priority TEXT NOT NULL CHECK (priority IN ('P0','P1','P2','P3','P4')),
          expires_at TEXT,
          created_at TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1 CHECK (revision>=1),
          steer_status TEXT CHECK (steer_status IN ('queued','unsupported')),
          capability_level TEXT CHECK (capability_level IN ('supported','unsupported','unknown')),
          capability_reason TEXT,
          UNIQUE (project_id,message_id),
          CHECK ((assignment_id IS NULL AND generation IS NULL) OR
                 (assignment_id IS NOT NULL AND generation IS NOT NULL AND generation>=1 AND task_id IS NOT NULL)),
          CHECK (kind<>'answer' OR reply_to IS NOT NULL),
          CHECK (kind<>'steer' OR assignment_id IS NOT NULL)
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS v2_message_one_answer
           ON v2_message(reply_to) WHERE kind='answer'""",
        """CREATE INDEX IF NOT EXISTS v2_message_thread
           ON v2_message(project_id,thread_id,created_at,message_id)""",
        """CREATE TABLE IF NOT EXISTS v2_message_delivery (
          delivery_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          message_id TEXT NOT NULL,
          recipient_agent_id TEXT NOT NULL REFERENCES v2_agent(agent_id),
          state TEXT NOT NULL DEFAULT 'held' CHECK (state IN
            ('held','sending','pushed','notified','read','acknowledged','answered','expired')),
          revision INTEGER NOT NULL DEFAULT 1 CHECK (revision>=1),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          read_at TEXT,
          acknowledged_at TEXT,
          answered_at TEXT,
          expired_at TEXT,
          UNIQUE (message_id,recipient_agent_id),
          FOREIGN KEY (project_id,message_id) REFERENCES v2_message(project_id,message_id)
        )""",
        """CREATE INDEX IF NOT EXISTS v2_message_inbox
           ON v2_message_delivery(project_id,recipient_agent_id,created_at,delivery_id)""",
    )
    for statement in statements:
        con.execute(statement)


def _principal(con, project_id, principal, write=False):
    _text(project_id, "project_id")
    if not isinstance(principal, dict):
        _fail("permission_denied", "service-derived principal is required")
    for key in ("agent_id", "role", "principal_id", "project_id"):
        _text(principal.get(key), "principal." + key)
    if principal["project_id"] != project_id or principal["role"] not in ROLES:
        _fail("permission_denied", "principal project or role is invalid")
    if not _one(con, "SELECT project_id FROM v2_project WHERE project_id=?", (project_id,)):
        _fail("permission_denied", "principal project is not registered")
    agent = _one(con, "SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?",
                 (principal["agent_id"], project_id))
    # The accepted access service may issue an observer-scoped credential to an
    # existing agent. This can remove write rights, never add another role.
    if (not agent or agent["lifecycle"] != "active"
            or (agent["role"] != principal["role"] and principal["role"] != "observer")):
        _fail("permission_denied", "principal does not match an active registry entry")
    if write and principal["role"] == "observer":
        _fail("permission_denied", "observer messaging access is read-only")
    return agent


def _agent(con, project_id, agent_id):
    _text(agent_id, "recipient agent_id")
    agent = _one(con, "SELECT * FROM v2_agent WHERE project_id=? AND agent_id=?",
                 (project_id, agent_id))
    if not agent or agent["lifecycle"] != "active":
        _fail("permission_denied", "recipient must be an active agent in this project")
    return agent


def _binding(con, project_id, principal, task_id, assignment_id, generation, reply=False):
    task = assignment = None
    if task_id is not None:
        _text(task_id, "task_id")
        task = _one(con, "SELECT * FROM v2_task WHERE task_id=? AND project_id=?",
                    (task_id, project_id))
        if not task:
            _fail("permission_denied", "task does not belong to this project")
    if assignment_id is not None:
        _text(assignment_id, "assignment_id")
        _integer(generation, "generation")
        assignment = _one(con, "SELECT a.* FROM v2_assignment a JOIN v2_task t "
                          "ON t.task_id=a.task_id WHERE a.assignment_id=? AND t.project_id=?",
                          (assignment_id, project_id))
        if not assignment:
            _fail("permission_denied", "assignment does not belong to this project")
        owner = _one(con, "SELECT project_id FROM v2_agent WHERE agent_id=?",
                     (assignment["agent_id"],))
        if not owner or owner["project_id"] != project_id:
            _fail("permission_denied", "assignment owner must be registered in this project")
        if task_id is not None and assignment["task_id"] != task_id:
            _fail("state_conflict", "assignment does not belong to the specified task")
        task_id = assignment["task_id"]
        if assignment["generation"] != generation:
            _fail("state_conflict", "assignment generation does not match")
        newer = _one(con, "SELECT assignment_id FROM v2_assignment WHERE task_id=? "
                     "AND slot_id=? AND generation>? LIMIT 1",
                     (task_id, assignment["slot_id"], generation))
        if newer:
            _fail("state_conflict", "assignment generation has been superseded")
        if principal["role"] == "worker" and not reply and assignment["agent_id"] != principal["agent_id"]:
            _fail("permission_denied", "worker may bind only its own assignment")
    elif generation is not None:
        _fail("invalid_request", "generation requires assignment_id")
    if task_id and principal["role"] == "worker" and not reply and not assignment:
        owned = _one(con, "SELECT a.assignment_id FROM v2_assignment a WHERE a.task_id=? "
                     "AND a.agent_id=? AND NOT EXISTS (SELECT 1 FROM v2_assignment n "
                     "WHERE n.task_id=a.task_id AND n.slot_id=a.slot_id AND n.generation>a.generation) "
                     "LIMIT 1", (task_id, principal["agent_id"]))
        if not owned:
            _fail("permission_denied", "worker may bind only its own task")
    return task_id, assignment


def _expired(message, now):
    return message["expires_at"] is not None and _timestamp(message["expires_at"], "expires_at") <= now


def _effective_state(delivery, message, now):
    if delivery["state"] in UNREAD and _expired(message, now):
        return "expired"
    return delivery["state"]


def _visible_message(con, project_id, principal, message_id):
    message = _one(con, "SELECT * FROM v2_message WHERE project_id=? AND message_id=?",
                   (project_id, message_id))
    recipient = _one(con, "SELECT * FROM v2_message_delivery WHERE project_id=? "
                     "AND message_id=? AND recipient_agent_id=?",
                     (project_id, message_id, principal["agent_id"]))
    if not message or (not recipient and message["sender_agent_id"] != principal["agent_id"]):
        _fail("permission_denied", "reply target is not visible to this principal")
    return message, recipient


def _steer_capability(con, agent):
    capability = _one(con, "SELECT * FROM v2_agent_capability WHERE agent_id=? AND capability_key='steer'",
                      (agent["agent_id"],))
    if not capability:
        return "unknown", "no_capability_record"
    if any(capability[key] != agent[key] for key in ("harness_version", "os", "host")):
        return "unknown", "capability_context_changed"
    if capability["level"] == "unsupported":
        return "unsupported", "registry_unsupported"
    if capability["level"] != "supported":
        return "unknown", "registry_unknown"
    if (capability["source"] not in ("live_probe", "official_doc", "regression_test", "operator_note")
            or not capability["checked_at"] or agent["harness_version"] == "unknown"
            or not capability["evidence_path"] or not capability["evidence_sha256"]):
        return "unknown", "unverified_capability"
    try:
        checked = _timestamp(capability["checked_at"], "capability.checked_at")
        if checked > _now():
            return "unknown", "capability_check_in_future"
        digest = hashlib.sha256(Path(capability["evidence_path"]).read_bytes()).hexdigest()
    except (OSError, ValueError):
        return "unknown", "capability_evidence_unreadable"
    if digest != capability["evidence_sha256"]:
        return "unknown", "capability_evidence_changed"
    return "supported", "verified_registry_evidence"


def _send(con, project_id, principal, payload):
    _object(payload, {"to", "kind", "body", "task_id", "assignment_id", "generation", "thread_id",
                      "reply_to", "priority", "expires_at"}, "message.send")
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        _fail("invalid_request", "kind must be one of " + ", ".join(KINDS))
    body = payload.get("body")
    try:
        valid_body = isinstance(body, str) and bool(body.strip()) and len(body.encode("utf-8")) <= MAX_BODY_BYTES
    except UnicodeEncodeError:
        valid_body = False
    if not valid_body or "\x00" in body:
        _fail("invalid_request", "body must be nonempty UTF-8 text of at most 65536 bytes")
    to = payload.get("to")
    if not isinstance(to, list) or not 1 <= len(to) <= MAX_RECIPIENTS:
        _fail("invalid_request", "to must contain 1..50 registered agent IDs")
    recipients = {}
    for agent_id in to:
        agent = _agent(con, project_id, agent_id)
        recipients[agent_id] = agent
    if kind == "question" and principal["agent_id"] in recipients:
        _fail("permission_denied", "self questions are forbidden")
    priority = payload.get("priority", "P3")
    if not isinstance(priority, str) or priority not in ("P0", "P1", "P2", "P3", "P4"):
        _fail("invalid_request", "priority must be P0..P4")
    now = _now()
    expires_at = payload.get("expires_at")
    if expires_at is not None:
        expires = _timestamp(expires_at, "expires_at")
        if expires <= now:
            _fail("state_conflict", "new message expiry must be in the future")
        expires_at = expires.isoformat()
    reply_to = payload.get("reply_to")
    parent = parent_delivery = None
    binding = {key: payload.get(key) for key in ("task_id", "assignment_id", "generation")}
    if reply_to is not None:
        _text(reply_to, "reply_to")
        parent, parent_delivery = _visible_message(con, project_id, principal, reply_to)
        for key in binding:
            if key in payload and payload[key] != parent[key]:
                _fail("state_conflict", "reply binding differs from its parent: " + key)
            binding[key] = parent[key]
    if kind == "answer":
        if not parent or parent["kind"] != "question":
            _fail("state_conflict", "answer requires reply_to a question")
        if (not parent_delivery or parent["sender_agent_id"] == principal["agent_id"]
                or set(recipients) != {parent["sender_agent_id"]}):
            _fail("permission_denied", "only a question recipient may answer its sender")
        if _expired(parent, now) or parent_delivery["state"] == "expired":
            _fail("state_conflict", "expired question cannot be answered")
        if _one(con, "SELECT message_id FROM v2_message WHERE reply_to=? AND kind='answer'", (reply_to,)):
            _fail("state_conflict", "question already has an answer")
    # Receipt of a bound message grants permission to reply in that exact context,
    # not permission to originate messages for another worker's assignment.
    task_id, assignment = _binding(con, project_id, principal, binding["task_id"],
                                  binding["assignment_id"], binding["generation"],
                                  reply=parent_delivery is not None)
    message_id = "msg-" + uuid.uuid4().hex
    thread_id = payload.get("thread_id")
    if thread_id is not None:
        _text(thread_id, "thread_id")
    if parent:
        if thread_id is not None and thread_id != parent["thread_id"]:
            _fail("state_conflict", "reply must remain in the parent thread")
        thread_id = parent["thread_id"]
    elif thread_id is not None:
        exists = _one(con, "SELECT message_id FROM v2_message WHERE project_id=? AND thread_id=? LIMIT 1",
                      (project_id, thread_id))
        if exists:
            participant = _one(con, "SELECT m.message_id FROM v2_message m WHERE m.project_id=? "
                               "AND m.thread_id=? AND (m.sender_agent_id=? OR EXISTS "
                               "(SELECT 1 FROM v2_message_delivery d WHERE d.message_id=m.message_id "
                               "AND d.recipient_agent_id=?)) LIMIT 1",
                               (project_id, thread_id, principal["agent_id"], principal["agent_id"]))
            if not participant:
                _fail("permission_denied", "thread is not visible to this principal")
    thread_id = thread_id or message_id
    steer_status = capability_level = capability_reason = None
    if kind == "steer":
        if principal["role"] not in ("controller", "reviewer"):
            _fail("permission_denied", "only controller or reviewer may steer")
        if (not assignment or assignment["state"] != "running" or not assignment["holds_slot"]
                or set(recipients) != {assignment["agent_id"]}):
            _fail("state_conflict", "steer must target the current running assignment's agent")
        capability_level, capability_reason = _steer_capability(con, recipients[assignment["agent_id"]])
        steer_status = "unsupported" if capability_level == "unsupported" else "queued"
    at = now.isoformat()
    con.execute("INSERT INTO v2_message(message_id,project_id,sender_agent_id,from_principal,kind,body,"
                "thread_id,reply_to,task_id,assignment_id,generation,priority,expires_at,created_at,"
                "steer_status,capability_level,capability_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, project_id, principal["agent_id"], principal["principal_id"], kind, body,
                 thread_id, reply_to, task_id, binding["assignment_id"], binding["generation"], priority,
                 expires_at, at, steer_status, capability_level, capability_reason))
    deliveries = []
    for agent_id in sorted(recipients):
        delivery_id = "dlv-" + hashlib.sha256((message_id + ":" + agent_id).encode()).hexdigest()[:32]
        con.execute("INSERT INTO v2_message_delivery(delivery_id,project_id,message_id,recipient_agent_id,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (delivery_id, project_id, message_id, agent_id, at, at))
        deliveries.append({"delivery_id": delivery_id, "recipient_agent_id": agent_id,
                           "state": "held", "revision": 1})
    if kind == "answer":
        con.execute("UPDATE v2_message_delivery SET state='answered',answered_at=?,updated_at=?,"
                    "revision=revision+1 WHERE delivery_id=?", (at, at, parent_delivery["delivery_id"]))
        _event(con, project_id, principal, "message_delivery", parent_delivery["delivery_id"],
               "message.answered", {"message_id": reply_to, "answer_id": message_id,
                                    "entity_revision": parent_delivery["revision"] + 1})
    event_id = _event(con, project_id, principal, "message", message_id, "message.sent",
                      {"kind": kind, "delivery_ids": [item["delivery_id"] for item in deliveries],
                       "task_id": task_id, "assignment_id": binding["assignment_id"],
                       "generation": binding["generation"], "steer_status": steer_status,
                       "capability_level": capability_level, "entity_revision": 1})
    result = {"status": "CREATED", "message_id": message_id, "thread_id": thread_id,
              "revision": 1, "deliveries": deliveries, "event_id": event_id}
    if kind == "steer":
        result.update(steer_status=steer_status, capability_level=capability_level,
                      capability_reason=capability_reason)
    return result


def _ack(con, project_id, principal, payload):
    _object(payload, {"delivery_id", "state", "expected_revision"}, "message.ack")
    delivery_id = _text(payload.get("delivery_id"), "delivery_id")
    expected = _integer(payload.get("expected_revision"), "expected_revision")
    state = payload.get("state")
    if not isinstance(state, str) or state not in ("read", "acknowledged"):
        _fail("invalid_request", "ACK state must be read or acknowledged")
    delivery = _one(con, "SELECT * FROM v2_message_delivery WHERE delivery_id=? "
                    "AND project_id=? AND recipient_agent_id=?",
                    (delivery_id, project_id, principal["agent_id"]))
    if not delivery:
        _fail("permission_denied", "only this delivery's recipient may ACK")
    if delivery["revision"] != expected:
        _fail("revision_conflict", "expected " + str(expected) + ", current " + str(delivery["revision"]))
    message = _one(con, "SELECT * FROM v2_message WHERE message_id=? AND project_id=?",
                   (delivery["message_id"], project_id))
    now = _now()
    current = _effective_state(delivery, message, now)
    if current in ("expired", "answered") or (current == "acknowledged" and state == "read"):
        _fail("state_conflict", "cannot ACK delivery from " + current + " to " + state)
    if current == state:
        return {"status": "UNCHANGED", "message_id": delivery["message_id"],
                "delivery_id": delivery_id, "state": state, "revision": delivery["revision"]}
    at = now.isoformat()
    con.execute("UPDATE v2_message_delivery SET state=?,revision=revision+1,updated_at=?,"
                "read_at=COALESCE(read_at,?),acknowledged_at=? WHERE delivery_id=? AND revision=?",
                (state, at, at, at if state == "acknowledged" else None, delivery_id, expected))
    event_id = _event(con, project_id, principal, "message_delivery", delivery_id, "message." + state,
                      {"message_id": delivery["message_id"], "state": state, "entity_revision": expected + 1})
    return {"status": "UPDATED", "message_id": delivery["message_id"], "delivery_id": delivery_id,
            "state": state, "revision": expected + 1, "event_id": event_id}


def apply_command(con, project_id, principal, tool, payload):
    """Apply one domain mutation; caller handles auth, replay and commit/rollback."""
    _transaction(con)
    _principal(con, project_id, principal, write=True)
    if tool == "message.send":
        return _send(con, project_id, principal, payload)
    if tool == "message.ack":
        return _ack(con, project_id, principal, payload)
    _fail("invalid_request", "unknown messaging command")


def read_messages(con, project_id, principal, filters=None):
    """Return only the principal's deliveries without any database mutation.

    TTL is projected as state=expired for unread deliveries. stored_state and
    revision describe durable ACK state; expire_deliveries materializes expiry.
    """
    _principal(con, project_id, principal)
    filters = {} if filters is None else filters
    _object(filters, {"message_id", "delivery_id", "thread_id", "task_id", "assignment_id", "kind",
                      "state", "limit", "offset"}, "message.read filters")
    limit = _integer(filters.get("limit", 100), "limit", maximum=500)
    offset = filters.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= 9223372036854775807:
        _fail("invalid_request", "offset must be a nonnegative signed 64-bit integer")
    clauses = ["d.project_id=?", "d.recipient_agent_id=?"]
    values = [project_id, principal["agent_id"]]
    for key in ("message_id", "delivery_id", "thread_id", "task_id", "assignment_id", "kind"):
        if key in filters:
            _text(filters[key], key)
            if key == "kind" and filters[key] not in KINDS:
                _fail("invalid_request", "unknown message kind")
            clauses.append(("d." if key == "delivery_id" else "m.") + key + "=?")
            values.append(filters[key])
    now = _now()
    # Canonical UTC timestamps are written by this module. SQLite evaluates the
    # same deadline as Python, while keeping state-filtered pagination bounded.
    effective = ("CASE WHEN d.state IN ('held','sending','pushed','notified') "
                 "AND m.expires_at IS NOT NULL AND m.expires_at<=? THEN 'expired' ELSE d.state END")
    state = filters.get("state")
    if state is not None:
        if not isinstance(state, str) or state not in DELIVERY_STATES:
            _fail("invalid_request", "unknown delivery state")
        clauses.append(effective + "=?")
        values.extend([now.isoformat(), state])
    rows = _rows(con, "SELECT m.*,d.delivery_id,d.recipient_agent_id,d.state AS stored_state,"
                 "d.revision AS delivery_revision,d.read_at,d.acknowledged_at,d.answered_at,d.expired_at,"
                 + effective + " AS state FROM v2_message_delivery d JOIN v2_message m "
                 "ON m.message_id=d.message_id AND m.project_id=d.project_id WHERE "
                 + " AND ".join(clauses) + " ORDER BY d.created_at,d.delivery_id LIMIT ? OFFSET ?",
                 [now.isoformat()] + values + [limit, offset])
    for row in rows:
        row["message_revision"] = row.pop("revision")
        row["revision"] = row.pop("delivery_revision")
        row["message_expired"] = _expired(row, now)
    return {"messages": rows, "limit": limit, "offset": offset}


def expire_deliveries(con, project_id, principal):
    """Controller maintenance: persist unread TTL expiry in the caller's tx."""
    _transaction(con)
    _principal(con, project_id, principal, write=True)
    if principal["role"] != "controller":
        _fail("permission_denied", "only controller may materialize delivery expiry")
    at = _now().isoformat()
    rows = _rows(con, "SELECT d.delivery_id,d.message_id,d.revision FROM v2_message_delivery d "
                 "JOIN v2_message m ON m.message_id=d.message_id AND m.project_id=d.project_id "
                 "WHERE d.project_id=? AND d.state IN ('held','sending','pushed','notified') "
                 "AND m.expires_at IS NOT NULL AND m.expires_at<=? ORDER BY d.delivery_id",
                 (project_id, at))
    for row in rows:
        con.execute("UPDATE v2_message_delivery SET state='expired',expired_at=?,updated_at=?,"
                    "revision=revision+1 WHERE delivery_id=?", (at, at, row["delivery_id"]))
        _event(con, project_id, principal, "message_delivery", row["delivery_id"], "message.expired",
               {"message_id": row["message_id"], "entity_revision": row["revision"] + 1})
    return {"status": "EXPIRED", "expired_count": len(rows)}
